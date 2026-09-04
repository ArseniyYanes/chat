"""FastAPI application: monitoring API + static frontend serving."""
import asyncio
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from sqlalchemy import Date, func, select, text

import apiproxy
import cache
from auth import require_auth
from collector import _dig, run_forever
from config import CFG
import database
from database import SessionLocal, init_db
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
    }


def _record_usage(key_id, status_code, input_tokens, output_tokens, endpoint, ip):
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


@app.post("/v1/chat/completions")
async def vllm_chat_completions(request: Request):
    """Authenticating reverse-proxy for vLLM /v1/chat/completions."""
    raw_body = await request.body()
    client_ip = request.client.host if request.client else ""
    key = _lookup_key(_bearer_key(request))
    if not key or not key.is_active:
        return JSONResponse(status_code=401, content={"error": {"message": "Invalid API key"}})
    if not apiproxy.check_rate_limit(key.id, key.rate_limit):
        return JSONResponse(status_code=429, content={"error": {"message": "Rate limit exceeded"}})
    if not apiproxy.check_daily_tokens(key.id, key.daily_token_limit):
        return JSONResponse(status_code=429, content={"error": {"message": "Daily token limit exceeded"}})
    key_id = key.id

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
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                r = await client.post(url, content=fwd_body, headers=fwd_headers)
        except Exception as exc:
            _record_usage(key_id, 502, 0, 0, "/v1/chat/completions", client_ip)
            return JSONResponse(
                status_code=502, content={"error": {"message": f"vLLM unreachable: {exc}"}}
            )
        in_t = out_t = 0
        try:
            usage = r.json().get("usage") or {}
            in_t = int(usage.get("prompt_tokens") or 0)
            out_t = int(usage.get("completion_tokens") or 0)
        except Exception:
            pass
        if in_t == 0 and out_t == 0:
            in_t, out_t = _estimate_tokens(raw_body)
        _record_usage(key_id, r.status_code, in_t, out_t, "/v1/chat/completions", client_ip)
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    async def gen():
        in_t = out_t = 0
        status = 200
        completion = []
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
                                in_t = int(usage.get("prompt_tokens") or 0)
                                out_t = int(usage.get("completion_tokens") or 0)
                            for choice in obj.get("choices") or []:
                                delta = choice.get("delta") or {}
                                piece = delta.get("content")
                                if piece:
                                    completion.append(piece)
        finally:
            if in_t == 0 and out_t == 0:
                in_t, out_t = _estimate_tokens(raw_body, "".join(completion))
            _record_usage(key_id, status, in_t, out_t, "/v1/chat/completions", client_ip)

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
