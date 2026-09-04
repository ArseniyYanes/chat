"""API-key helpers: generation, hashing and Redis-backed rate limiting.

All Redis operations fail *open* (allow the request) when Redis is
unavailable, so a cache outage never blocks vLLM traffic. The per-key
accounting that must survive (totals, last_used, usage logs) is written to
PostgreSQL separately in main.py.
"""
import hashlib
import secrets
import time
from datetime import datetime, timezone

import cache

_KEY_PREFIX = "sk-"
_KEY_LEN = 48  # hex chars after the prefix
_DISPLAY_PREFIX = 6  # chars shown to the user


def hash_key(key: str) -> str:
    """SHA256 hex digest of a raw API key."""
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


def generate_key() -> str:
    """Create a new random API key (sk-<48 hex>). Never stored in plain form."""
    return _KEY_PREFIX + secrets.token_hex(_KEY_LEN // 2)


def display_prefix(key: str) -> str:
    return (key or "")[:_DISPLAY_PREFIX]


def check_rate_limit(key_id: str, limit: int) -> bool:
    """Fixed per-minute window counter. Returns True when the request may pass."""
    if not limit or limit <= 0:
        return True
    c = cache.client()
    if c is None:
        return True
    key = f"apikey:rl:{key_id}:{int(time.time()) // 60}"
    try:
        n = c.incr(key)
        if n == 1:
            c.expire(key, 70)
        return n <= limit
    except Exception:
        return True


def check_daily_tokens(key_id: str, limit: int) -> bool:
    """True when the key is under its per-day token budget."""
    if not limit or limit <= 0:
        return True
    c = cache.client()
    if c is None:
        return True
    key = f"apikey:tok:{key_id}:{datetime.now(timezone.utc):%Y%m%d}"
    try:
        used = int(c.get(key) or 0)
        return used < limit
    except Exception:
        return True


def record_tokens(key_id: str, tokens: int) -> None:
    """Bump the per-day token counter used by check_daily_tokens()."""
    if not tokens or tokens <= 0:
        return
    c = cache.client()
    if c is None:
        return
    key = f"apikey:tok:{key_id}:{datetime.now(timezone.utc):%Y%m%d}"
    try:
        c.incrby(key, tokens)
        c.expire(key, 2 * 86400)
    except Exception:
        pass