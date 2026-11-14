"""Admin API used by the admin panel inside the app.
Log in with ADMIN_PASSWORD, then manage keys (students) and the lectures."""
import hmac
import json
import time
from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from . import config
from .db import Batch, Key, Lecture, LectureView, Order, SessionLocal, Setting, Subject, Unit, utcnow
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
        paid = db.scalars(select(Order).where(Order.status == "paid")).all()
        day_ago = now - timedelta(days=1)
        week_ago = now - timedelta(days=7)
        top = db.execute(
            select(LectureView.lecture_id, LectureView.count, Lecture.title)
            .join(Lecture, Lecture.id == LectureView.lecture_id)
            .order_by(LectureView.count.desc())
            .limit(5)
        ).all()
    return {
        "total": total,
        "active": total - revoked - expired,
        "expired": expired,
        "revoked": revoked,
        "orders": len(paid),
        "revenue": sum(o.amount_paise for o in paid) // 100,
        "orders_today": sum(1 for o in paid if o.created_at >= day_ago),
        "revenue_week": sum(o.amount_paise for o in paid if o.created_at >= week_ago) // 100,
        "top_lectures": [{"title": t, "views": c} for _, c, t in top],
    }


class AnnounceBody(BaseModel):
    text: str = Field("", max_length=500)
    link: str = Field("", max_length=500)


@router.put("/announce")
def save_announce(body: AnnounceBody, authorization: str | None = Header(None)):
    """The banner on the app's Home screen. Empty text hides it."""
    require_admin(authorization)
    link = body.link.strip()
    if link and not link.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "The link must start with http:// or https://")
    value = json.dumps({"text": body.text.strip(), "link": link}, ensure_ascii=False)
    with SessionLocal() as db:
        row = db.get(Setting, "announce")
        if row:
            row.value = value
        else:
            db.add(Setting(key="announce", value=value))
        db.commit()
    return json.loads(value)


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


class DeviceBody(BaseModel):
    device_id: str = Field(min_length=4, max_length=32)


@router.post("/keys/{key}/device")
def change_device(key: str, body: DeviceBody, authorization: str | None = Header(None)):
    """Moves a key to another device. The old phone is logged out right away,
    because its saved login no longer matches the key's device."""
    require_admin(authorization)
    with SessionLocal() as db:
        k = _get_key(db, key)
        k.device_id = norm(body.device_id)
        db.commit()
        return _key_dict(k)


# ---------------- content: batches, subjects, units, lectures ----------------

def _clean_link(value: str | None) -> str | None:
    """Empty means no link. Otherwise it must be a plain https link."""
    v = (value or "").strip()
    if not v:
        return None
    if not v.lower().startswith("https://") or " " in v or len(v) > 2000:
        raise HTTPException(400, "Links must start with https:// and have no spaces.")
    return v


def _lecture_dict(l: Lecture) -> dict:
    return {
        "id": l.id,
        "title": l.title,
        "video_url": l.video_url or "",
        "pdf_url": l.pdf_url or "",
        "has_video": bool(l.video_url or l.video_msg_id),
        "has_pdf": bool(l.pdf_url or l.pdf_msg_id),
        "free": bool(l.free),
    }


@router.get("/content")
def content(authorization: str | None = Header(None)):
    """Everything in one call, so the panel can show what is added."""
    require_admin(authorization)
    with SessionLocal() as db:
        return [
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
                                "lectures": [_lecture_dict(l) for l in u.lectures],
                            }
                            for u in s.units
                        ],
                    }
                    for s in b.subjects
                ],
            }
            for b in db.scalars(select(Batch).order_by(Batch.id))
        ]


class LectureBody(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    video_url: str = ""
    pdf_url: str = ""
    free: bool = False
    unit_id: int | None = None  # needed when creating


@router.post("/lectures")
def add_lecture(body: LectureBody, authorization: str | None = Header(None)):
    require_admin(authorization)
    video, pdf = _clean_link(body.video_url), _clean_link(body.pdf_url)
    if not video and not pdf:
        raise HTTPException(400, "Add a video link, a PDF link, or both.")
    with SessionLocal() as db:
        if body.unit_id is None or not db.get(Unit, body.unit_id):
            raise HTTPException(400, "Unit not found.")
        lec = Lecture(
            unit_id=body.unit_id, title=body.title.strip(), video_url=video, pdf_url=pdf, free=body.free
        )
        db.add(lec)
        db.commit()
        return _lecture_dict(lec)


@router.put("/lectures/{lecture_id}")
def edit_lecture(lecture_id: int, body: LectureBody, authorization: str | None = Header(None)):
    require_admin(authorization)
    video, pdf = _clean_link(body.video_url), _clean_link(body.pdf_url)
    with SessionLocal() as db:
        lec = db.get(Lecture, lecture_id)
        if not lec:
            raise HTTPException(404, "Not found.")
        if not (video or pdf or lec.video_msg_id or lec.pdf_msg_id):
            raise HTTPException(400, "A lecture needs a video link or a PDF link.")
        lec.title = body.title.strip()
        lec.video_url = video
        lec.pdf_url = pdf
        lec.free = body.free
        db.commit()
        return _lecture_dict(lec)


class NameBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    parent_id: int | None = None  # batch id for a subject, subject id for a unit


# kind -> (model, parent model, parent column name)
_TREE = {
    "batches": (Batch, None, None),
    "subjects": (Subject, Batch, "batch_id"),
    "units": (Unit, Subject, "subject_id"),
}


class LinkBody(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    url: str = Field(max_length=500)


class SectionBody(BaseModel):
    heading: str = Field("", max_length=100)
    body: str = Field("", max_length=3000)
    links: list[LinkBody] = Field(default_factory=list, max_length=6)


class AboutBody(BaseModel):
    name: str = Field("", max_length=100)
    tagline: str = Field("", max_length=200)
    sections: list[SectionBody] = Field(default_factory=list, max_length=30)


@router.put("/about")
def save_about(body: AboutBody, authorization: str | None = Header(None)):
    """Replaces the whole About Developer page."""
    require_admin(authorization)
    for sec in body.sections:
        for link in sec.links:
            if not link.url.lower().startswith(("http://", "https://")):
                raise HTTPException(400, f'Link "{link.label}" must start with http:// or https://')
    value = json.dumps(body.model_dump(), ensure_ascii=False)
    with SessionLocal() as db:
        row = db.get(Setting, "about")
        if row:
            row.value = value
        else:
            db.add(Setting(key="about", value=value))
        db.commit()
    return body.model_dump()


@router.post("/{kind}")
def add_item(kind: str, body: NameBody, authorization: str | None = Header(None)):
    require_admin(authorization)
    if kind not in _TREE:
        raise HTTPException(404, "Not found.")
    model, parent_model, column = _TREE[kind]
    with SessionLocal() as db:
        fields = {"name": body.name.strip()}
        if parent_model:
            if body.parent_id is None or not db.get(parent_model, body.parent_id):
                raise HTTPException(400, "Parent not found.")
            fields[column] = body.parent_id
        row = model(**fields)
        db.add(row)
        db.commit()
        return {"id": row.id, "name": row.name}


@router.patch("/{kind}/{item_id}")
def rename_item(kind: str, item_id: int, body: NameBody, authorization: str | None = Header(None)):
    require_admin(authorization)
    if kind not in _TREE:
        raise HTTPException(404, "Not found.")
    with SessionLocal() as db:
        row = db.get(_TREE[kind][0], item_id)
        if not row:
            raise HTTPException(404, "Not found.")
        row.name = body.name.strip()
        db.commit()
    return {"ok": True}


@router.delete("/{kind}/{item_id}")
def delete_item(kind: str, item_id: int, authorization: str | None = Header(None)):
    require_admin(authorization)
    models = {**{k: v[0] for k, v in _TREE.items()}, "lectures": Lecture}
    if kind not in models:
        raise HTTPException(404, "Not found.")
    with SessionLocal() as db:
        row = db.get(models[kind], item_id)
        if not row:
            raise HTTPException(404, "Not found.")
        db.delete(row)  # everything inside it goes too
        db.commit()
    return {"ok": True}
