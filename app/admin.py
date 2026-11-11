"""Admin API used by the admin panel inside the app.
Log in with ADMIN_PASSWORD, then manage keys (students)."""
import hmac
import time
from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from . import config
from .db import Key, SessionLocal, utcnow
from .utils import make_admin_token, new_key, norm, read_token

router = APIRouter(prefix="/admin")

_attempts: dict[str, list[float]] = defaultdict(list)


def _too_many_logins(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _attempts[ip] if now - t < 60]
    recent.append(now)
    _attempts[ip] = recent
    return len(recent) > 5


def require_admin(authorization: str | None) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Admin login required.")
    data = read_token(config.SECRET_KEY, authorization[7:].strip())
    if not data or data.get("r") != "admin":
        raise HTTPException(401, "Admin login required.")


def _status(k: Key) -> str:
    if k.revoked:
        return "revoked"
    if k.expires_at <= utcnow():
        return "expired"
    return "active"


def _key_dict(k: Key) -> dict:
    return {
        "key": k.key,
        "device_id": k.device_id,
        "note": k.note,
        "status": _status(k),
        "expires_at": k.expires_at.isoformat() + "Z",
        "created_at": k.created_at.isoformat() + "Z",
        "days_left": max(0, (k.expires_at - utcnow()).days),
    }


class LoginBody(BaseModel):
    password: str


@router.post("/login")
def login(body: LoginBody, request: Request):
    ip = (
        request.headers.get("x-forwarded-for")
        or (request.client.host if request.client else "")
    ).split(",")[0].strip()
    if _too_many_logins(ip):
        raise HTTPException(429, "Too many attempts. Try again in a minute.")
    if not config.ADMIN_PASSWORD:
        raise HTTPException(503, "ADMIN_PASSWORD is not set on the server.")
    if not hmac.compare_digest(body.password.encode(), config.ADMIN_PASSWORD.encode()):
        raise HTTPException(401, "Wrong password.")
    return {"token": make_admin_token(config.SECRET_KEY, config.ADMIN_TOKEN_DAYS)}


@router.get("/stats")
def stats(authorization: str | None = Header(None)):
    require_admin(authorization)
    now = utcnow()
    with SessionLocal() as db:
        total = db.scalar(select(func.count()).select_from(Key)) or 0
        revoked = db.scalar(select(func.count()).select_from(Key).where(Key.revoked)) or 0
        expired = db.scalar(
            select(func.count()).select_from(Key).where(~Key.revoked, Key.expires_at <= now)
        ) or 0
    return {
        "total": total,
        "active": total - revoked - expired,
        "expired": expired,
        "revoked": revoked,
    }


@router.get("/keys")
def list_keys(q: str = "", authorization: str | None = Header(None)):
    require_admin(authorization)
    with SessionLocal() as db:
        stmt = select(Key).order_by(Key.created_at.desc()).limit(500)
        q = q.strip()
        if q:
            like = f"%{q}%"
            stmt = stmt.where(
                or_(Key.key.ilike(like), Key.device_id.ilike(like), Key.note.ilike(like))
            )
        return [_key_dict(k) for k in db.scalars(stmt)]


class CreateBody(BaseModel):
    device_id: str = Field(min_length=4, max_length=32)
    days: int = Field(ge=1, le=3650)
    note: str = Field("", max_length=200)


@router.post("/keys")
def create_key(body: CreateBody, authorization: str | None = Header(None)):
    require_admin(authorization)
    with SessionLocal() as db:
        k = Key(
            key=new_key(),
            device_id=norm(body.device_id),
            expires_at=utcnow() + timedelta(days=body.days),
            note=body.note.strip(),
        )
        db.add(k)
        db.commit()
        return _key_dict(k)


def _get_key(db, key: str) -> Key:
    k = db.get(Key, norm(key))
    if not k:
        raise HTTPException(404, "No such key.")
    return k


@router.post("/keys/{key}/revoke")
def revoke(key: str, authorization: str | None = Header(None)):
    require_admin(authorization)
    with SessionLocal() as db:
        k = _get_key(db, key)
        k.revoked = True
        db.commit()
        return _key_dict(k)


@router.post("/keys/{key}/restore")
def restore(key: str, authorization: str | None = Header(None)):
    require_admin(authorization)
    with SessionLocal() as db:
        k = _get_key(db, key)
        k.revoked = False
        db.commit()
        return _key_dict(k)


class ExtendBody(BaseModel):
    days: int = Field(ge=1, le=3650)


@router.post("/keys/{key}/extend")
def extend(key: str, body: ExtendBody, authorization: str | None = Header(None)):
    require_admin(authorization)
    with SessionLocal() as db:
        k = _get_key(db, key)
        base = max(k.expires_at, utcnow())  # an expired key restarts from today
        k.expires_at = base + timedelta(days=body.days)
        db.commit()
        return _key_dict(k)


@router.delete("/keys/{key}")
def delete_key(key: str, authorization: str | None = Header(None)):
    require_admin(authorization)
    with SessionLocal() as db:
        k = _get_key(db, key)
        db.delete(k)
        db.commit()
    return {"ok": True}
