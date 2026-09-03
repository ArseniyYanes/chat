"""Alert delivery (Telegram), with per-message cooldown."""
import hashlib
import logging
import time

import httpx

from config import CFG

log = logging.getLogger("monitoring.notifier")

_cooldown: dict = {}


async def send(message: str, cooldown_s: int = 900) -> bool:
    """Send an alert. Returns True if it was actually delivered."""
    key = hashlib.sha1(message.encode()).hexdigest()
    now = time.time()
    if now - _cooldown.get(key, 0) < cooldown_s:
        return False
    _cooldown[key] = now

    if not CFG.telegram_bot_token or not CFG.telegram_chat_id:
        log.warning("ALERT (notifications not configured): %s", message)
        return False

    url = f"https://api.telegram.org/bot{CFG.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                url, json={"chat_id": CFG.telegram_chat_id, "text": f"🚨 AI Monitoring: {message}"}
            )
            delivered = r.status_code == 200
            log.info("telegram alert sent, status=%s", r.status_code)
            return delivered
    except Exception as exc:
        log.warning("telegram alert failed: %s", exc)
        return False
