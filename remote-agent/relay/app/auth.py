import hashlib
import hmac
import secrets
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from .db import Device


def hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def new_device_token() -> str:
    return "dev_" + secrets.token_urlsafe(32)


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    return authorization[7:].strip()


def _eq(a: str, b: str) -> bool:
    return bool(a) and hmac.compare_digest(a.encode(), b.encode())


def get_db(request: Request):
    with request.app.state.Session() as s:
        yield s


def require_brain(request: Request, authorization: str | None = Header(None)) -> str:
    if not _eq(_bearer(authorization), request.app.state.settings.brain_token):
        raise HTTPException(403, "invalid brain token")
    return "brain"


def require_admin(request: Request, authorization: str | None = Header(None)) -> str:
    if not _eq(_bearer(authorization), request.app.state.settings.admin_token):
        raise HTTPException(403, "invalid admin token")
    return "admin"


def require_enroll(request: Request, authorization: str | None = Header(None)) -> str:
    if not _eq(_bearer(authorization), request.app.state.settings.enroll_token):
        raise HTTPException(403, "invalid enrollment token")
    return "enroll"


def require_device(authorization: str | None = Header(None), db: Session = Depends(get_db)) -> Device:
    tok = _bearer(authorization)
    dev = db.query(Device).filter(Device.token_hash == hash_token(tok)).one_or_none()
    if dev is None:
        raise HTTPException(403, "invalid device token")
    if dev.revoked:
        raise HTTPException(403, "device token revoked")
    return dev
