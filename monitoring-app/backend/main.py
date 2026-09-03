"""FastAPI application: monitoring API + static frontend serving."""
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import select, text

import cache
from auth import require_auth
from collector import _dig, run_forever
from config import CFG
import database
from database import SessionLocal, init_db
from models import (
    AdminAction,
    HourlyAgg,
    MetricSnapshot,
    RequestLog,
    ServiceStatus,
    Setting,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("monitoring.api")

FRONTEND_DIST = os.getenv("FRONTEND_DIST", "/app/frontend")
collector_task = None

DEFAULT_SETTINGS = {
    "gpu_threshold": 90.0,
    "error_rate": 5.0,
    "notifications_enabled": True,
    "telegram_chat_id": CFG.telegram_chat_id,
}

METRIC_FIELD = {
    "gpu_util": "gpu_util_avg",
    "gpu_temp": "gpu_temp_avg",
    "cpu_pct": "cpu_pct_avg",
    "ram_pct": "ram_pct_avg",
    "net_rx": "net_rx_avg",
    "net_tx": "net_tx_avg",
    "disk_read": "disk_read_avg",
    "disk_write": "disk_write_avg",
    "vllm_active": "vllm_active_avg",
    "vllm_tokens_in": "vllm_tokens_in_avg",
    "vllm_tokens_out": "vllm_tokens_out_avg",
    "vllm_ttft": "vllm_ttft_avg",
    "vllm_tpot": "vllm_tpot_avg",
}

RANGE_MAP = {
    "1h": (1, 30),
    "6h": (6, 60),
    "24h": (24, 120),
    "3d": (72, 180),
    "7d": (168, 336),
    "30d": (720, 720),
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global collector_task
    init_db()
    if CFG.run_collector:
        collector_task = asyncio.create_task(run_forever())
        log.info("in-process collector started")
    yield
    if collector_task:
        collector_task.cancel()
        try:
            await collector_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(title="AI Monitoring", version=CFG.version, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def log_action(db, user, action, details=None):
    db.add(AdminAction(ts=datetime.now(timezone.utc), user=user, action=action, details=details))
    db.commit()


def _service_dict(s):
    return {
        "name": s.name,
        "up": s.up,
        "latency_ms": s.latency_ms,
        "version": s.version,
        "last_ok": s.last_ok_ts.isoformat() if s.last_ok_ts else None,
        "last_check": s.last_check_ts.isoformat() if s.last_check_ts else None,
    }


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "version": CFG.version,
        "timescale": database.TIMESCALE,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/latest")
def latest(user: str = Depends(require_auth)):
    data = cache.get_json("latest")
    if data:
        return data
    db = SessionLocal()
    try:
        row = (
            db.execute(select(MetricSnapshot).order_by(MetricSnapshot.ts.desc()).limit(1))
            .scalars()
            .first()
        )
        svcs = db.execute(select(ServiceStatus)).scalars().all()
        if row is None:
            return {
                "ts": None,
                "gpu": [],
                "cpu": None,
                "ram": None,
                "disk": None,
                "net": None,
                "vllm": None,
                "services": [
                    {"name": s.name, "up": s.up, "latency_ms": s.latency_ms, "version": s.version}
                    for s in svcs
                ],
            }
        data = {
            "ts": row.ts.isoformat() if row.ts else None,
            "gpu": row.gpu or [],
            "cpu": row.cpu,
            "ram": row.ram,
            "disk": row.disk,
            "net": row.net,
            "vllm": row.vllm,
            "services": [
                {"name": s.name, "up": s.up, "latency_ms": s.latency_ms, "version": s.version}
                for s in svcs
            ],
        }
    finally:
        db.close()
    cache.set_json("latest", data, 10)
    return data


def _extract_metric(row, metric):
    if metric == "gpu_util":
        return _dig(row.gpu, 0, "util")
    if metric == "gpu_temp":
        return _dig(row.gpu, 0, "temp")
    if metric == "cpu_pct":
        return _dig(row.cpu, "pct")
    if metric == "ram_pct":
        return _dig(row.ram, "pct")
    if metric == "net_rx":
        return _dig(row.net, "rx_bps")
    if metric == "net_tx":
        return _dig(row.net, "tx_bps")
    if metric == "disk_read":
        return _dig(row.disk, "read_bps")
    if metric == "disk_write":
        return _dig(row.disk, "write_bps")
    if metric == "vllm_active":
        return _dig(row.vllm, "active")
    if metric == "vllm_tokens_in":
        return _dig(row.vllm, "tokens_in_s")
    if metric == "vllm_tokens_out":
        return _dig(row.vllm, "tokens_out_s")
    if metric == "vllm_ttft":
        return _dig(row.vllm, "ttft_ms")
    if metric == "vllm_tpot":
        return _dig(row.vllm, "tpot_ms")
    return None


@app.get("/api/history")
def history(
    metric: str = Query("cpu_pct"),
    range_key: str = Query("24h", alias="range"),
    tz_min: int = Query(0, ge=-840, le=840, alias="tz"),
    db=Depends(get_session),
):
    key = f"history:{metric}:{range_key}:{tz_min}"
    cached = cache.get_json(key)
    if cached:
        return cached
    hours, _points = RANGE_MAP.get(range_key, (24, 120))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    # Client-side timezone offset in minutes (UTC -> local), e.g. +180 for UTC+3.
    tz = timezone(timedelta(minutes=tz_min))
    labels = []
    values = []
    if range_key in ("7d", "30d") and metric in METRIC_FIELD:
        rows = (
            db.execute(
                select(HourlyAgg)
                .where(HourlyAgg.hour_bucket >= since)
                .order_by(HourlyAgg.hour_bucket)
            )
            .scalars()
            .all()
        )
        for r in rows:
            labels.append(r.hour_bucket.astimezone(tz).strftime("%m-%d %H:%M"))
            values.append(getattr(r, METRIC_FIELD[metric], None))
    if not labels:
        rows = (
            db.execute(
                select(MetricSnapshot)
                .where(MetricSnapshot.ts >= since)
                .order_by(MetricSnapshot.ts)
            )
            .scalars()
            .all()
        )
        if len(rows) > 360:
            step = len(rows) / 360
            rows = [rows[int(i * step)] for i in range(360)]
        fmt = "%H:%M" if hours <= 24 else "%m-%d %H:%M"
        for r in rows:
            labels.append(r.ts.astimezone(tz).strftime(fmt))
            values.append(_extract_metric(r, metric))
    data = {"metric": metric, "range": range_key, "labels": labels, "values": values}
    cache.set_json(key, data, 120)
    return data


@app.get("/api/requests")
def requests(
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
    q: str = Query(""),
    user: str = Query(""),
    model: str = Query(""),
    status: str = Query(""),
    db=Depends(get_session),
):
    filters = []
    params = {}
    if q:
        filters.append("prompt_preview ILIKE :q")
        params["q"] = f"%{q}%"
    if user:
        filters.append("user_id = :user")
        params["user"] = user
    if model:
        filters.append("model ILIKE :model")
        params["model"] = f"%{model}%"
    if status:
        filters.append("status = :status")
        params["status"] = status
    where = " AND ".join(filters) if filters else "TRUE"
    total = db.execute(
        text(f"SELECT COUNT(*) FROM request_logs WHERE {where}"), params
    ).scalar()
    rows = db.execute(
        text(
            "SELECT ts, source, chat_id, user_id, ip, model, prompt_preview, "
            "prompt_tokens, completion_tokens, latency_ms, status, temperature "
            "FROM request_logs WHERE "
            + where
            + " ORDER BY ts DESC LIMIT :limit OFFSET :offset"
        ),
        {**params, "limit": limit, "offset": offset},
    ).mappings().all()
    return {
        "total": total or 0,
        "items": [
            {
                "ts": r["ts"].isoformat() if r["ts"] else None,
                "source": r["source"],
                "chat_id": r["chat_id"],
                "user": r["user_id"],
                "ip": r["ip"],
                "model": r["model"],
                "prompt": r["prompt_preview"],
                "prompt_tokens": r["prompt_tokens"],
                "completion_tokens": r["completion_tokens"],
                "latency_ms": r["latency_ms"],
                "status": r["status"],
                "temperature": r["temperature"],
            }
            for r in rows
        ],
    }


@app.get("/api/status")
def status(db=Depends(get_session)):
    rows = db.execute(select(ServiceStatus)).scalars().all()
    return {"services": [_service_dict(r) for r in rows]}


RESTART_SCRIPTS = {
    "vllm": "docker compose restart vllm",
    "openwebui": "docker compose restart openwebui",
    "db": "docker compose restart db",
    "redis": "docker compose restart redis",
}


@app.post("/api/status/{name}/restart")
def restart_service(name: str, user: str = Depends(require_auth), db=Depends(get_session)):
    if name not in RESTART_SCRIPTS:
        raise HTTPException(status_code=400, detail="unknown service")
    log_action(db, user, f"restart:{name}")
    return {"detail": "queued", "script": RESTART_SCRIPTS[name]}


@app.post("/api/test-request")
async def test_request(
    payload: dict,
    user: str = Depends(require_auth),
    db=Depends(get_session),
):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    model = (payload.get("model") or "").strip()
    max_tokens = int(payload.get("max_tokens") or 256)
    temperature = float(payload.get("temperature") or 0.7)
    t0 = time.time()
    error = None
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                CFG.vllm_url + "/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
            if r.status_code >= 400:
                error = f"HTTP {r.status_code}: {r.text[:300]}"
    except Exception as exc:
        error = str(exc)
    latency = int((time.time() - t0) * 1000)
    status_ = "ok" if not error else "error"
    db.add(
        RequestLog(
            ts=datetime.now(timezone.utc),
            source="dashboard-test",
            user_id=user,
            model=model or None,
            prompt_preview=prompt[:500],
            latency_ms=latency,
            status=status_,
            temperature=temperature,
            raw={"error": error} if error else None,
        )
    )
    log_action(db, user, "test-request", {"model": model, "latency_ms": latency, "status": status_})
    if error:
        raise HTTPException(status_code=502, detail=error)
    return {"status": "ok", "latency_ms": latency}


@app.post("/api/admin/notify-test")
async def notify_test(user: str = Depends(require_auth), db=Depends(get_session)):
    import notifier

    log_action(db, user, "notify-test")
    ok = await notifier.send("Тестовое уведомление из AI Monitoring", cooldown_s=0)
    return {"sent": ok}


@app.get("/api/settings")
def get_settings(db=Depends(get_session)):
    rows = db.execute(select(Setting)).scalars().all()
    out = dict(DEFAULT_SETTINGS)
    for r in rows:
        if r.key in out:
            out[r.key] = r.value
    return out


@app.put("/api/settings")
def update_settings(payload: dict, user: str = Depends(require_auth), db=Depends(get_session)):
    updated = {}
    for key, value in payload.items():
        if key not in DEFAULT_SETTINGS:
            continue
        row = db.get(Setting, key)
        if row is None:
            row = Setting(key=key)
            db.add(row)
        row.value = value
        updated[key] = value
    log_action(db, user, "settings.update", updated)
    return get_settings(db)


@app.get("/api/actions")
def actions(limit: int = Query(50, le=500), db=Depends(get_session)):
    rows = (
        db.execute(select(AdminAction).order_by(AdminAction.ts.desc()).limit(limit))
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "ts": r.ts.isoformat() if r.ts else None,
                "user": r.user,
                "action": r.action,
                "details": r.details,
            }
            for r in rows
        ]
    }


@app.get("/{path:path}")
def frontend(path: str):
    if path.startswith("api/"):
        raise HTTPException(status_code=404)
    file_path = os.path.join(FRONTEND_DIST, path)
    if path and os.path.isfile(file_path):
        return FileResponse(file_path)
    index = os.path.join(FRONTEND_DIST, "index.html")
    if os.path.isfile(index):
        return FileResponse(index)
    return {"detail": "frontend build not found", "hint": "npm run build in frontend/"}
