"""HTTP Basic auth (ADMIN_USER / MONITORING_PASSWORD)."""
import hmac

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from config import CFG

_security = HTTPBasic(realm="AI Monitoring")


def require_auth(creds: HTTPBasicCredentials = Depends(_security)) -> str:
    """FastAPI dependency; returns the username on success, 401 otherwise."""
    user_ok = hmac.compare_digest(creds.username, CFG.admin_user)
    pass_ok = hmac.compare_digest(creds.password, CFG.admin_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="AI Monitoring"'},
        )
    return creds.username
