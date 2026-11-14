import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from . import config, tg_client
from .admin import router as admin_router
from .shop import router as shop_router, set_telegram_webhook
from .db import Batch, Key, Lecture, LectureView, SessionLocal, Setting, Subject, Unit, init_db, utcnow
from .utils import make_token, norm, parse_range, read_token


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not config.SECRET_KEY:
        raise RuntimeError("Set SECRET_KEY in the environment.")
    init_db()
    set_telegram_webhook()  # best effort, only if the shop bot is configured
    if config.SESSION_STRING:  # Telegram storage is optional now (links need no Telegram)
        await tg_client.start()
    yield
    await tg_client.stop()


app = FastAPI(title="Itachi Universe API", lifespan=lifespan)
app.include_router(admin_router)
app.include_router(shop_router)


@app.get("/health")
def health():
    return {"ok": True}


# ---------- About developer (public, edited from the admin panel) ----------

DEFAULT_ABOUT = {"name": "Itachi Universe", "tagline": "", "sections": []}


@app.get("/about")
def about():
    import json

    with SessionLocal() as db:
        row = db.get(Setting, "about")
        try:
            return json.loads(row.value) if row and row.value else DEFAULT_ABOUT
        except ValueError:
            return DEFAULT_ABOUT


@app.get("/announce")
def announce():
    """The banner shown on the app's Home screen. Empty text means no banner."""
    import json

    with SessionLocal() as db:
        row = db.get(Setting, "announce")
        try:
            return json.loads(row.value) if row and row.value else {"text": "", "link": ""}
        except ValueError:
            return {"text": "", "link": ""}


# ---------- Access ----------

def premium_key(db, authorization: str | None):
    """Returns the Key row if the Bearer token is valid for premium, else None."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    data = read_token(config.SECRET_KEY, authorization[7:].strip())
    if not data or "k" not in data:
        return None  # bad token, or an admin token (those are not premium tokens)
    key = db.get(Key, data["k"])
    if not key or key.revoked or key.expires_at <= utcnow() or key.device_id != data["d"]:
        return None  # revoked, expired, or token from another device
    return key


_attempts: dict[str, list[float]] = defaultdict(list)


def _too_many_attempts(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _attempts[ip] if now - t < 60]
    recent.append(now)
    _attempts[ip] = recent
    return len(recent) > 10


class ActivateBody(BaseModel):
    key: str
    device_id: str


@app.post("/activate")
def activate(body: ActivateBody, request: Request):
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()
    if _too_many_attempts(ip):
        raise HTTPException(429, "Too many attempts. Try again in a minute.")
    key_text, device = norm(body.key), norm(body.device_id)
    with SessionLocal() as db:
        key = db.get(Key, key_text)
        if not key:
            raise HTTPException(404, "Invalid key.")
        if not key.device_id:  # sold through the shop bot: first phone to use it owns it
            if key.revoked or key.expires_at <= utcnow():
                raise HTTPException(403, "This key is not valid any more.")
            key.device_id = device
            db.commit()
        if key.device_id != device:
            raise HTTPException(403, "This key belongs to a different device.")
        if key.revoked:
            raise HTTPException(403, "This key has been revoked.")
        if key.expires_at <= utcnow():
            raise HTTPException(403, "This key has expired.")
        token = make_token(config.SECRET_KEY, key.key, device, config.TOKEN_DAYS)
        return {"token": token, "expires_at": key.expires_at.isoformat() + "Z"}


@app.get("/me")
def me(authorization: str | None = Header(None)):
    with SessionLocal() as db:
        key = premium_key(db, authorization)
        if not key:
            raise HTTPException(401, "Not premium.")
        return {"premium": True, "expires_at": key.expires_at.isoformat() + "Z"}


# ---------- Catalog ----------

@app.get("/catalog")
def catalog(authorization: str | None = Header(None)):
    """Everything is listed for everyone. Locked items are blocked at /media."""
    with SessionLocal() as db:
        premium = premium_key(db, authorization) is not None
        batches = db.scalars(
            select(Batch)
            .options(
                selectinload(Batch.subjects)
                .selectinload(Subject.units)
                .selectinload(Unit.lectures)
            )
            .order_by(Batch.id)
        ).all()
        return {
            "premium": premium,
            "batches": [
                {
                    "id": b.id,
                    "name": b.name,
                    "subjects": [
                        {
                            "id": s.id,
                            "name": s.name,
                            "units": [
                                {
                                    "id": u.id,
                                    "name": u.name,
                                    "lectures": [
                                        {
                                            "id": l.id,
                                            "title": l.title,
                                            "has_video": bool(l.video_url or l.video_msg_id),
                                            "has_pdf": bool(l.pdf_url or l.pdf_msg_id),
                                            "free": l.free,
                                        }
                                        for l in u.lectures
                                    ],
                                }
                                for u in s.units
                            ],
                        }
                        for s in b.subjects
                    ],
                }
                for b in batches
            ],
        }


# ---------- Play info ----------

@app.get("/play/{lecture_id}/{kind}")
def play(lecture_id: int, kind: str, authorization: str | None = Header(None)):
    """Tells the app where to get a video/PDF, after checking access.
    {"kind": "link", "url": ...}  -> the app opens the link directly (no load on this server)
    {"kind": "stream"}            -> the app streams it through /media (Telegram storage)"""
    if kind not in ("video", "pdf"):
        raise HTTPException(404)
    with SessionLocal() as db:
        lec = db.get(Lecture, lecture_id)
        if not lec:
            raise HTTPException(404, "Lecture not found.")
        url = lec.video_url if kind == "video" else lec.pdf_url
        msg_id = lec.video_msg_id if kind == "video" else lec.pdf_msg_id
        if not url and not msg_id:
            raise HTTPException(404, "Nothing here.")
        if not lec.free and premium_key(db, authorization) is None:
            raise HTTPException(403, "Premium required.")
        if kind == "video":  # counted for the admin dashboard
            row = db.get(LectureView, lecture_id)
            if row:
                row.count += 1
            else:
                db.add(LectureView(lecture_id=lecture_id, count=1))
            db.commit()
        return {"kind": "link", "url": url} if url else {"kind": "stream"}


# ---------- Media streaming (files stored in the Telegram channel) ----------

@app.get("/media/{lecture_id}/{kind}")
async def media(
    lecture_id: int,
    kind: str,
    request: Request,
    authorization: str | None = Header(None),
):
    if kind not in ("video", "pdf", "thumb"):
        raise HTTPException(404)

    with SessionLocal() as db:
        lec = db.get(Lecture, lecture_id)
        if not lec:
            raise HTTPException(404, "Lecture not found.")
        msg_id = {"video": lec.video_msg_id, "pdf": lec.pdf_msg_id}.get(kind)
        if kind == "thumb":
            msg_id = lec.thumb_msg_id or lec.video_msg_id
        if not msg_id:
            raise HTTPException(404, "Nothing here.")
        if kind != "thumb" and not lec.free and premium_key(db, authorization) is None:
            raise HTTPException(403, "Premium required.")

    if tg_client.client is None:
        raise HTTPException(503, "Telegram storage is not set up on this server.")
    msg = await tg_client.get_media_message(msg_id)
    if msg is None:
        raise HTTPException(404, "File missing in the channel.")

    if kind == "thumb":
        data = await tg_client.thumbnail_bytes(msg)
        if not data:
            raise HTTPException(404, "No thumbnail.")
        return Response(data, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})

    size = msg.file.size
    try:
        start, end, partial = parse_range(request.headers.get("range"), size)
    except ValueError:
        raise HTTPException(416, headers={"Content-Range": f"bytes */{size}"})

    default_type = "video/mp4" if kind == "video" else "application/pdf"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Cache-Control": "private, no-store",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        tg_client.iter_range(msg, start, end),
        status_code=206 if partial else 200,
        media_type=msg.file.mime_type or default_type,
        headers=headers,
    )
