"""FastAPI application: monitoring API + static frontend serving."""
import asyncio
import collections
import hmac
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from sqlalchemy import Date, case, func, select, text
from sqlalchemy.orm import Session

import apiproxy
import cache
from auth import require_auth
from collector import _dig, run_forever
from config import CFG
import database
from database import SessionLocal, init_db, get_db
from models import (
    AdminAction,
    ApiKey,
    ApiUsageLog,
    HourlyAgg,
    MetricSnapshot,
    RequestLog,
    ServiceStatus,
    Setting,
)
from apiproxy import hash_key

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
    load_task = asyncio.create_task(_load_history_loop())
    if CFG.run_collector:
        collector_task = asyncio.create_task(run_forever())
        log.info("in-process collector started")
    yield
    load_task.cancel()
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
    key = f"history:{metric}:{range_key}:{tz_min}:v2"
    cached = cache.get_json(key)
    if cached:
        return cached
    hours, points = RANGE_MAP.get(range_key, (24, 120))
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    # Client-side timezone offset in minutes (UTC -> local), e.g. +180 for UTC+3.
    tz = timezone(timedelta(minutes=tz_min))
    # Uniform bucket grid covering the FULL selected window: periods without
    # data (collector/vLLM was down) stay visible as gaps instead of the
    # chart silently compressing to the existing data span.
    n = points
    bucket_s = hours * 3600 / n
    labels = [
        (since + timedelta(seconds=round(i * bucket_s))).astimezone(tz).strftime(
            "%H:%M" if hours <= 24 else "%m-%d %H:%M"
        )
        for i in range(n)
    ]
    sums = [0.0] * n
    cnts = [0] * n

    def _put(idx: int, value):
        if 0 <= idx < n:
            sums[idx] += value
            cnts[idx] += 1

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
            v = getattr(r, METRIC_FIELD[metric], None)
            if v is None:
                continue
            _put(int((r.hour_bucket - since).total_seconds() // bucket_s), v)
    else:
        rows = (
            db.execute(
                select(MetricSnapshot)
                .where(MetricSnapshot.ts >= since)
                .order_by(MetricSnapshot.ts)
            )
            .scalars()
            .all()
        )
        for r in rows:
            v = _extract_metric(r, metric)
            if v is None:
                continue
            _put(int((r.ts - since).total_seconds() // bucket_s), v)
    values = [round(sums[i] / cnts[i], 2) if cnts[i] else None for i in range(n)]
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

@app.get("/v1/keys/check")
async def check_key(
    authorization: str = Header(None),
    db: Session = Depends(get_db)
):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing API key")

    # Извлекаем ключ из "Bearer sk-..."
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    api_key = parts[1]

    # Проверяем ключ в базе
    key_record = db.query(ApiKey).filter(
        ApiKey.key_hash == hash_key(api_key),
        ApiKey.is_active == True
    ).first()

    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Проверяем лимиты (если есть)
    if key_record.daily_limit and key_record.used_today >= key_record.daily_limit:
        raise HTTPException(status_code=429, detail="Daily token limit exceeded")

    # Проверяем срок действия
    if key_record.expires_at and key_record.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail="API key expired")

    return {
        "valid": True,
        "key_id": key_record.id,
        "rate_limit": key_record.daily_limit,
        "used_today": key_record.used_today,
        "expires_at": key_record.expires_at
    }

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


# ---------------------------------------------------------------------------
# API keys management
# ---------------------------------------------------------------------------
def _key_dict(k: ApiKey) -> dict:
    return {
        "id": k.id,
        "name": k.name,
        "prefix": k.prefix,
        "created_at": k.created_at.isoformat() if k.created_at else None,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "is_active": bool(k.is_active),
        "rate_limit": k.rate_limit,
        "daily_token_limit": k.daily_token_limit,
        "total_requests": k.total_requests or 0,
        "total_tokens": k.total_tokens or 0,
        **_key_speed(k),
    }


def _key_speed(k: ApiKey) -> dict:
    """Average response latency (ms/req) and generation speed (tokens/s).

    Both are derived from the denormalized lifetime counters on the key,
    so no extra aggregation query is needed.
    """
    reqs = k.total_requests or 0
    total_ms = k.total_latency_ms or 0
    if not reqs or not total_ms:
        return {"avg_latency_ms": None, "avg_tokens_per_s": None}
    avg_latency_ms = round(total_ms / reqs)
    avg_tokens_per_s = round((k.total_tokens or 0) / (total_ms / 1000.0), 2)
    return {
        "avg_latency_ms": avg_latency_ms,
        "avg_tokens_per_s": avg_tokens_per_s or None,
    }


def _record_usage(key_id, status_code, input_tokens, output_tokens, endpoint, ip,
                  latency_ms=None):
    """Persist usage to Postgres and bump the Redis daily token counter.

    Opened in its own session; safe to call after the response has started.
    """
    total = (input_tokens or 0) + (output_tokens or 0)
    try:
        db = SessionLocal()
        try:
            row = db.get(ApiKey, key_id)
            if row:
                row.total_requests = (row.total_requests or 0) + 1
                row.total_tokens = (row.total_tokens or 0) + total
                row.total_latency_ms = (row.total_latency_ms or 0) + (latency_ms or 0)
                row.last_used_at = datetime.now(timezone.utc)
            db.add(
                ApiUsageLog(
                    api_key_id=key_id,
                    request_time=datetime.now(timezone.utc),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total,
                    endpoint=endpoint,
                    status_code=status_code,
                    ip_address=ip,
                    latency_ms=latency_ms,
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # never let bookkeeping break the request
        log.warning("api-key usage bookkeeping failed: %s", exc)
    log.info(
        "vllm-proxy usage: key=%s %s status=%s in=%s out=%s total=%s ip=%s",
        key_id,
        endpoint,
        status_code,
        input_tokens or 0,
        output_tokens or 0,
        total,
        ip,
    )
    apiproxy.record_tokens(key_id, total)


@app.get("/api/keys")
def list_keys(user: str = Depends(require_auth), db=Depends(get_session)):
    rows = db.execute(select(ApiKey).order_by(ApiKey.created_at.desc())).scalars().all()
    return {"items": [_key_dict(k) for k in rows]}


@app.post("/api/keys")
def create_key(payload: dict, user: str = Depends(require_auth), db=Depends(get_session)):
    master = str(payload.get("master_password") or "")
    if not CFG.master_password or not hmac.compare_digest(master, CFG.master_password):
        raise HTTPException(status_code=401, detail="Неверный мастер-пароль")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Название ключа обязательно")
    try:
        rate_limit = int(payload.get("rate_limit") or 60)
        daily_token_limit = int(payload.get("daily_token_limit") or 1000000)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Некорректные лимиты")
    raw = apiproxy.generate_key()
    row = ApiKey(
        name=name,
        key_hash=apiproxy.hash_key(raw),
        prefix=apiproxy.display_prefix(raw),
        rate_limit=rate_limit,
        daily_token_limit=daily_token_limit,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    log_action(db, user, "api-key.create", {"name": name, "prefix": row.prefix})
    # The full key is returned exactly once; only its hash is kept.
    return {"key": raw, "prefix": row.prefix, "item": _key_dict(row)}


@app.post("/api/keys/{key_id}/block")
def block_key(key_id: str, user: str = Depends(require_auth), db=Depends(get_session)):
    row = db.get(ApiKey, key_id)
    if not row:
        raise HTTPException(status_code=404, detail="Ключ не найден")
    row.is_active = False
    db.commit()
    log_action(db, user, "api-key.block", {"name": row.name})
    return _key_dict(row)


@app.post("/api/keys/{key_id}/unblock")
def unblock_key(key_id: str, user: str = Depends(require_auth), db=Depends(get_session)):
    row = db.get(ApiKey, key_id)
    if not row:
        raise HTTPException(status_code=404, detail="Ключ не найден")
    row.is_active = True
    db.commit()
    log_action(db, user, "api-key.unblock", {"name": row.name})
    return _key_dict(row)


@app.delete("/api/keys/{key_id}")
def delete_key(key_id: str, user: str = Depends(require_auth), db=Depends(get_session)):
    row = db.get(ApiKey, key_id)
    if not row:
        raise HTTPException(status_code=404, detail="Ключ не найден")
    db.execute(ApiUsageLog.__table__.delete().where(ApiUsageLog.api_key_id == key_id))
    db.delete(row)
    db.commit()
    log_action(db, user, "api-key.delete", {"name": row.name})
    return {"detail": "ok"}


@app.get("/api/keys/{key_id}/stats")
def key_stats(key_id: str, user: str = Depends(require_auth), db=Depends(get_session)):
    """Daily token/request usage for the last 7 days (for the mini chart)."""
    if not db.get(ApiKey, key_id):
        raise HTTPException(status_code=404, detail="Ключ не найден")
    now = datetime.now(timezone.utc)
    day = func.date(ApiUsageLog.request_time)
    rows = (
        db.execute(
            select(day.label("d"), func.sum(ApiUsageLog.total_tokens), func.count(ApiUsageLog.id))
            .where(
                ApiUsageLog.api_key_id == key_id,
                ApiUsageLog.request_time >= now - timedelta(days=6),
            )
            .group_by(day)
        )
    ).all()
    by_day = {str(r[0]): (r[1] or 0, r[2] or 0) for r in rows}
    labels, tokens, requests = [], [], []
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i)).date()
        labels.append(d.isoformat())
        used = by_day.get(str(d), (0, 0))
        tokens.append(used[0])
        requests.append(used[1])
    return {"days": labels, "tokens": tokens, "requests": requests}


@app.get("/api/keys/{key_id}/usage")
def key_usage(
    key_id: str,
    limit: int = Query(default=50, le=200, ge=1),
    user: str = Depends(require_auth),
    db=Depends(get_session),
):
    """Most recent per-request usage logs for a key (newest first)."""
    if not db.get(ApiKey, key_id):
        raise HTTPException(status_code=404, detail="Ключ не найден")
    rows = (
        db.execute(
            select(ApiUsageLog)
            .where(ApiUsageLog.api_key_id == key_id)
            .order_by(ApiUsageLog.request_time.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {
        "items": [
            {
                "request_time": r.request_time.isoformat() if r.request_time else None,
                "input_tokens": r.input_tokens or 0,
                "output_tokens": r.output_tokens or 0,
                "total_tokens": r.total_tokens or 0,
                "status_code": r.status_code,
                "ip_address": r.ip_address,
                "latency_ms": r.latency_ms,
            }
            for r in rows
        ]
    }


@app.get("/api/keys/summary")
def keys_summary(
    request: Request,
    user: str = Depends(require_auth),
    db=Depends(get_session),
):
    """Aggregate usage across all keys + the proxy endpoint a client should call."""
    now = datetime.now(timezone.utc)
    day = func.date(ApiUsageLog.request_time)
    seven_ago = now - timedelta(days=6)

    # Totals (all time) straight from the denormalized counters on api_keys.
    tot = db.execute(
        select(
            func.count(ApiKey.id),
            func.sum(ApiKey.total_requests),
            func.sum(ApiKey.total_tokens),
            func.coalesce(func.sum(case((ApiKey.is_active.is_(True), 1), else_=0)), 0),
        )
    ).one()
    total_keys, total_requests, total_tokens, active_keys = int(tot[0]), int(tot[1] or 0), int(tot[2] or 0), int(tot[3])

    # Today's usage from the raw logs.
    start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    today = db.execute(
        select(func.count(ApiUsageLog.id), func.coalesce(func.sum(ApiUsageLog.total_tokens), 0))
        .where(ApiUsageLog.request_time >= start_of_day)
    ).one()

    # 7-day series across all keys (for the overview line).
    srows = (
        db.execute(
            select(day.label("d"), func.sum(ApiUsageLog.total_tokens), func.count(ApiUsageLog.id))
            .where(ApiUsageLog.request_time >= seven_ago)
            .group_by(day)
        )
    ).all()
    s_by_day = {str(r[0]): (int(r[1] or 0), int(r[2] or 0)) for r in srows}
    series_days, series_tokens, series_requests = [], [], []
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i)).date()
        series_days.append(d.isoformat())
        used = s_by_day.get(str(d), (0, 0))
        series_tokens.append(used[0])
        series_requests.append(used[1])

    # Per-key 7-day tokens (for the bar) merged with lifetime totals.
    prows = (
        db.execute(
            select(ApiUsageLog.api_key_id, func.sum(ApiUsageLog.total_tokens))
            .where(ApiUsageLog.request_time >= seven_ago)
            .group_by(ApiUsageLog.api_key_id)
        )
    ).all()
    p7 = {r[0]: int(r[1] or 0) for r in prows}
    keys = db.execute(select(ApiKey)).scalars().all()
    per_key = [
        {
            "id": k.id,
            "name": k.name,
            "is_active": bool(k.is_active),
            "total_requests": k.total_requests or 0,
            "total_tokens": k.total_tokens or 0,
            "tokens_7d": p7.get(k.id, 0),
        }
        for k in keys
    ]

    # Build the public proxy base URL the client should point at.
    base = str(request.base_url).rstrip("/")
    return {
        "totals": {
            "total_keys": total_keys,
            "active_keys": active_keys,
            "blocked_keys": total_keys - active_keys,
            "total_requests": total_requests,
            "total_tokens": total_tokens,
            "today_requests": int(today[0] or 0),
            "today_tokens": int(today[1] or 0),
        },
        "series": {"days": series_days, "tokens": series_tokens, "requests": series_requests},
        "per_key": per_key,
        "proxy": {
            "chat_completions": f"{base}/v1/chat/completions",
            "base": base,
        },
    }


# ---------------------------------------------------------------------------
# vLLM proxy with API-key authentication
# ---------------------------------------------------------------------------
class GatewayLoad:
    """Live (in-process) view of gateway load.

    Tracks per API key: how many requests are currently running against vLLM
    ("active"), how many wait in the concurrency queue ("queued") and the
    current output speed in tokens/sec, measured on live SSE streams
    (one non-empty content delta ~ one token).  The gateway is a single
    uvicorn process, so a plain dict guarded by a lock is enough — no Redis
    round-trip per token.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stats = {}    # key_id -> {"active", "queued", "streams": [{"t0", "deltas"}]}
        self._key_sems = {}  # key_id -> asyncio.Semaphore
        self.global_sem = asyncio.Semaphore(CFG.gw_global_concurrency)

    def per_key_sem(self, key_id: str) -> "asyncio.Semaphore":
        with self._lock:
            sem = self._key_sems.get(key_id)
            if sem is None:
                sem = asyncio.Semaphore(CFG.gw_per_key_concurrency)
                self._key_sems[key_id] = sem
            return sem

    def _stat(self, key_id: str) -> dict:
        # Must be called with self._lock held.
        return self._stats.setdefault(
            key_id,
            {
                "active": 0,
                "queued": 0,
                "streams": [],
                # wall durations (seconds) of completed requests (kept for
                # internal tooling)
                "durations": collections.deque(maxlen=100),
                # time-to-first-token (seconds) of completed requests — the
                # «Время ожидания» column: from request arrival at the gateway
                # (incl. queue wait) until the model's FIRST token is delivered
                "ttfts": collections.deque(maxlen=100),
            },
        )

    def record_duration(self, key_id: str, secs: float):
        """Non-streaming call: total duration doubles as TTFT."""
        with self._lock:
            st = self._stats.get(key_id)
            if st:
                st["durations"].append(max(0.0, float(secs)))
                # Non-streaming calls have no earlier signal than the final
                # response, so TTFT == total duration and the dashboard
                # column uses it directly.
                st["ttfts"].append(max(0.0, float(secs)))

    def record_total(self, key_id: str, secs: float):
        """Streaming call: full duration only; TTFT was recorded by
        stream_delta() at the first token."""
        with self._lock:
            st = self._stats.get(key_id)
            if st:
                st["durations"].append(max(0.0, float(secs)))

    def queued_inc(self, key_id):
        with self._lock:
            self._stat(key_id)["queued"] += 1

    def queued_dec(self, key_id):
        with self._lock:
            st = self._stats.get(key_id)
            if st and st["queued"] > 0:
                st["queued"] -= 1

    def active_inc(self, key_id):
        with self._lock:
            self._stat(key_id)["active"] += 1

    def active_dec(self, key_id):
        with self._lock:
            st = self._stats.get(key_id)
            if st and st["active"] > 0:
                st["active"] -= 1

    def stream_start(self, key_id, req_start: float = None):
        with self._lock:
            self._stat(key_id)["streams"].append(
                {
                    "t0": None,
                    "deltas": 0,
                    # gateway entry time (request arrival, before the queue) —
                    # the TTFT reference point
                    "start": req_start if req_start is not None else time.monotonic(),
                }
            )

    def stream_delta(self, key_id):
        with self._lock:
            st = self._stats.get(key_id)
            streams = st["streams"] if st else []
            if streams:
                s = streams[-1]
                s["deltas"] += 1
                if s["t0"] is None:
                    s["t0"] = time.monotonic()
                    # First content token of this stream → record TTFT
                    # (arrival at the gateway incl. queue wait → first token).
                    st["ttfts"].append(max(0.0, s["t0"] - s["start"]))

    def stream_stop(self, key_id):
        with self._lock:
            streams = self._stats.get(key_id, {}).get("streams", [])
            if streams:
                streams.pop()

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            rows = []
            for key_id, st in self._stats.items():
                tps = 0.0
                for s in st["streams"]:
                    if s["t0"] is not None:
                        elapsed = now - s["t0"]
                        if elapsed >= 0.5:
                            tps += s["deltas"] / elapsed
                ttfts = list(st["ttfts"])
                rows.append({
                    "key_id": key_id,
                    "active": st["active"],
                    "queued": st["queued"],
                    "streams": len(st["streams"]),
                    "tps": round(tps, 1),
                    "wait_ms": int(sum(ttfts) / len(ttfts) * 1000)
                    if ttfts else None,
                })
        all_ttfts = [d for st in self._stats.values() for d in st["ttfts"]]
        rows.sort(key=lambda r: (-(r["active"] + r["queued"]), -r["tps"]))
        return {
            "limits": {
                "per_key": CFG.gw_per_key_concurrency,
                "global": CFG.gw_global_concurrency,
                "queue_timeout_s": CFG.gw_queue_timeout,
            },
            "totals": {
                "active": sum(r["active"] for r in rows),
                "queued": sum(r["queued"] for r in rows),
                "keys_online": sum(1 for r in rows if r["active"] or r["queued"]),
                "tps": round(sum(r["tps"] for r in rows), 1),
                "wait_ms": int(sum(all_ttfts) / len(all_ttfts) * 1000)
                if all_ttfts else None,
            },
            "keys": rows,
        }


GATEWAY = GatewayLoad()


class LoadHistory:
    """In-memory ring buffer of gateway load samples.

    A background task records one sample per ``interval`` seconds (active /
    queued / tps totals); the buffer keeps the last hour.  Used by the
    «Нагрузка» tab chart.  Lost on restart — that is fine for a live view.
    """

    def __init__(self, interval: int = 10, span_s: int = 3600):
        self.interval = interval
        self._samples = collections.deque(maxlen=max(1, span_s // interval))

    def record(self, active: int, queued: int, tps: float):
        self._samples.append((time.time(), active, queued, tps))

    def series(self):
        return [
            {"t": int(ts * 1000), "active": a, "queued": q, "tps": s}
            for ts, a, q, s in self._samples
        ]


LOAD_HISTORY = LoadHistory()


async def _load_history_loop():
    """Sample the live gateway load every 10s into LOAD_HISTORY."""
    while True:
        try:
            t = GATEWAY.snapshot()["totals"]
            LOAD_HISTORY.record(t["active"], t["queued"], t["tps"])
        except Exception as e:  # pragma: no cover - never kill the loop
            log.warning("load history sample failed: %s", e)
        await asyncio.sleep(10)


class GatewayQueueTimeout(Exception):
    """A request could not get a concurrency slot within gw_queue_timeout."""

    def __init__(self, scope: str, waited: float):
        self.scope = scope  # "key" | "global"
        self.waited = waited


async def _acquire_slot(key_id: str) -> "asyncio.Semaphore":
    """Queue the request until a concurrency slot is free.

    Raises GatewayQueueTimeout if the wait exceeds CFG.gw_queue_timeout.
    The caller MUST call _release_slot() exactly once (in a finally).
    """
    t0 = time.monotonic()
    GATEWAY.queued_inc(key_id)
    key_sem = GATEWAY.per_key_sem(key_id)
    try:
        try:
            await asyncio.wait_for(key_sem.acquire(), timeout=CFG.gw_queue_timeout)
        except asyncio.TimeoutError:
            raise GatewayQueueTimeout("key", time.monotonic() - t0)
        try:
            await asyncio.wait_for(GATEWAY.global_sem.acquire(), timeout=CFG.gw_queue_timeout)
        except asyncio.TimeoutError:
            key_sem.release()
            raise GatewayQueueTimeout("global", time.monotonic() - t0)
    finally:
        GATEWAY.queued_dec(key_id)
    GATEWAY.active_inc(key_id)
    return key_sem


def _release_slot(key_id: str, key_sem: "asyncio.Semaphore"):
    GATEWAY.active_dec(key_id)
    GATEWAY.global_sem.release()
    key_sem.release()


@app.get("/api/keys/live")
def keys_live(user: str = Depends(require_auth), db=Depends(get_db)):
    """Live load snapshot: active/queued requests per key + current output speed."""
    snap = GATEWAY.snapshot()
    live = {r["key_id"]: r for r in snap["keys"]}
    rows = []
    for k in db.execute(select(ApiKey).order_by(ApiKey.created_at.desc())).scalars().all():
        l = live.get(k.id, {})
        rows.append({
            "key_id": k.id,
            "name": k.name,
            "blocked": not k.is_active,
            "active": l.get("active", 0),
            "queued": l.get("queued", 0),
            "streams": l.get("streams", 0),
            "tps": l.get("tps", 0.0),
            "wait_ms": l.get("wait_ms"),
        })
    rows.sort(key=lambda r: (-(r["active"] + r["queued"]), -r["tps"]))
    snap["keys"] = rows
    return snap


@app.get("/api/keys/live/history")
def keys_live_history(user: str = Depends(require_auth)):
    """Recent gateway load samples (10s cadence) for the load chart."""
    return {"samples": LOAD_HISTORY.series(), "interval_s": LOAD_HISTORY.interval}


def _bearer_key(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""


def _lookup_key(raw: str):
    db = SessionLocal()
    try:
        return db.execute(
            select(ApiKey).where(ApiKey.key_hash == apiproxy.hash_key(raw))
        ).scalars().first()
    finally:
        db.close()


def _estimate_tokens(raw_body, completion_text=""):
    """Rough fallback token estimate (~4 chars/token).

    Used only when vLLM does not return a `usage` block (e.g. some streaming
    setups), so the per-key accounting still reflects something meaningful.
    """
    try:
        body = json.loads(raw_body) if raw_body else {}
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    msgs = body.get("messages") or []
    prompt_chars = sum(len(m.get("content") or "") for m in msgs if isinstance(m, dict))
    in_tok = max(1, prompt_chars // 4) if prompt_chars else 0
    out_tok = max(1, len(completion_text) // 4) if completion_text else 0
    return in_tok, out_tok


def _extract_usage(usage: dict):
    """Derive per-request (input_tokens, output_tokens) from a vLLM ``usage``.

    Chat clients resend the WHOLE conversation on every request, so
    ``prompt_tokens`` is the size of the full prompt/context, not the new
    user message.  When vLLM's automatic prefix cache is enabled (default in
    recent vLLM versions) it reports the prompt portion served from the cache
    in ``prompt_tokens_details.cached_tokens``; counting only the uncached
    part makes per-request "in" reflect the *new* input tokens, so per-key
    totals stay in line with real consumption instead of the cumulative
    context size.
    """
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    try:
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    except Exception:
        cached = 0
    return max(0, prompt - cached), completion


async def _vllm_nonstream(url, fwd_body, fwd_headers, raw_body, key_id, client_ip):
    """Run a non-streaming vLLM call, record usage, and return a Response."""
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(url, content=fwd_body, headers=fwd_headers)
    except Exception as exc:
        latency_ms = round((time.monotonic() - t0) * 1000)
        _record_usage(key_id, 502, 0, 0, "/v1/chat/completions", client_ip, latency_ms)
        return JSONResponse(
            status_code=502, content={"error": {"message": f"vLLM unreachable: {exc}"}}
        )
    latency_ms = round((time.monotonic() - t0) * 1000)
    in_t = out_t = 0
    try:
        usage = r.json().get("usage") or {}
        in_t, out_t = _extract_usage(usage)
    except Exception:
        pass
    if in_t == 0 and out_t == 0:
        in_t, out_t = _estimate_tokens(raw_body)
    _record_usage(
        key_id, r.status_code, in_t, out_t, "/v1/chat/completions", client_ip, latency_ms
    )
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
    )


@app.post("/v1/chat/completions")
async def vllm_chat_completions(request: Request):
    """Authenticating reverse-proxy for vLLM /v1/chat/completions."""
    raw_body = await request.body()
    client_ip = request.client.host if request.client else ""
    key = _lookup_key(_bearer_key(request))
    if not key or not key.is_active:
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid API key"}})
    # NOTE: per-key rate limit / daily token checks were disabled (keys
    # created outside the UI may lack those columns; limits are enforced
    # by the gateway concurrency slots below and by vLLM itself).
    key_id = key.id

    # Concurrency limits + real queue (see GatewayLoad).  The slot is held for
    # the whole vLLM call, including stream generation (released in the
    # generator's finally for streaming requests).
    # Request arrival at the gateway — start point for the load tab's
    # «Время ожидания» (time to first token, queue wait included).
    req_t0 = time.monotonic()
    try:
        key_sem = await _acquire_slot(key_id)
    except GatewayQueueTimeout as qt:
        limit = (
            CFG.gw_per_key_concurrency if qt.scope == "key" else CFG.gw_global_concurrency
        )
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": (
                        f"Gateway is busy: waited {qt.waited:.0f}s in queue "
                        f"(limit {limit} parallel requests). Retry in a few seconds."
                    )
                }
            },
        )

    try:
        body = json.loads(raw_body) if raw_body else {}
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    stream = bool(body.get("stream"))
    url = CFG.vllm_url + "/v1/chat/completions"

    # For streaming we must ask vLLM to include a final `usage` chunk,
    # otherwise it is omitted by default and per-key token accounting
    # would always be 0. We inject stream_options into the forwarded body.
    fwd_body = raw_body
    if stream and isinstance(body, dict):
        body.setdefault("stream_options", {})["include_usage"] = True
        try:
            fwd_body = json.dumps(body).encode("utf-8")
        except Exception:
            fwd_body = raw_body

    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "authorization", "content-length", "content-encoding")
    }
    fwd_headers.setdefault("content-type", "application/json")
    # Defense-in-depth: if vLLM is protected by its own --api-key, send it.
    if CFG.vllm_api_key:
        fwd_headers["authorization"] = f"Bearer {CFG.vllm_api_key}"

    if not stream:
        response = await _vllm_nonstream(
            url, fwd_body, fwd_headers, raw_body, key_id, client_ip
        )
        _release_slot(key_id, key_sem)
        GATEWAY.record_duration(key_id, time.monotonic() - req_t0)
        return response

    async def gen():
        in_t = out_t = 0
        status = 200
        completion = []
        t0 = time.monotonic()
        GATEWAY.stream_start(key_id, req_t0)
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream(
                    "POST", url, content=fwd_body, headers=fwd_headers
                ) as resp:
                    status = resp.status_code
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                        text = chunk.decode("utf-8", "ignore")
                        for line in text.split("\n"):
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            payload = line[5:].strip()
                            if payload in ("", "[DONE]"):
                                continue
                            try:
                                obj = json.loads(payload)
                            except Exception:
                                continue
                            usage = obj.get("usage")
                            if usage:
                                in_t, out_t = _extract_usage(usage)
                            for choice in obj.get("choices") or []:
                                delta = choice.get("delta") or {}
                                piece = delta.get("content")
                                if piece:
                                    completion.append(piece)
                                    GATEWAY.stream_delta(key_id)
        finally:
            GATEWAY.stream_stop(key_id)
            if in_t == 0 and out_t == 0:
                in_t, out_t = _estimate_tokens(raw_body, "".join(completion))
            latency_ms = round((time.monotonic() - t0) * 1000)
            _record_usage(
                key_id, status, in_t, out_t, "/v1/chat/completions", client_ip, latency_ms
            )
            _release_slot(key_id, key_sem)
            # TTFT already recorded on the first token (stream_delta); keep
            # the full request duration for internal tooling.
            GATEWAY.record_total(key_id, time.monotonic() - req_t0)

    return StreamingResponse(gen(), media_type="text/event-stream")


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
