"""Tiny Redis JSON cache with graceful degradation when Redis is unavailable."""
import json
import logging
import time
from typing import Any, Optional

import redis

from config import CFG

log = logging.getLogger("monitoring.cache")

_client: Optional[redis.Redis] = None
_failed_at = 0.0
_RETRY_AFTER = 30.0  # seconds before trying Redis again after a failure


def _get() -> Optional[redis.Redis]:
    global _client, _failed_at
    if _client is None:
        if time.time() - _failed_at < _RETRY_AFTER:
            return None
        try:
            _client = redis.Redis.from_url(
                CFG.redis_url, socket_timeout=2, socket_connect_timeout=2, decode_responses=True
            )
            _client.ping()
        except Exception as exc:
            _failed_at = time.time()
            log.warning("redis unavailable: %s", exc)
            return None
    return _client


def get_json(key: str) -> Optional[Any]:
    c = _get()
    if c is None:
        return None
    try:
        raw = c.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def set_json(key: str, value: Any, ttl: int) -> None:
    c = _get()
    if c is None:
        return
    try:
        c.set(key, json.dumps(value, default=str), ex=ttl)
    except Exception:
        pass
