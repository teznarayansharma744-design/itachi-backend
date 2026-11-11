"""Small pure helpers (no Telegram or database code), easy to test."""
import base64
import hashlib
import hmac
import json
import secrets
import time

_KEY_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def new_key() -> str:
    """Random key like 7K2M-9XQP-4TDA-H8WE."""
    groups = ["".join(secrets.choice(_KEY_CHARS) for _ in range(4)) for _ in range(4)]
    return "-".join(groups)


def norm(text: str) -> str:
    return (text or "").strip().upper()


def make_token(secret: str, key: str, device_id: str, days: int) -> str:
    payload = {"k": key, "d": device_id, "e": int(time.time() + days * 86400)}
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def make_admin_token(secret: str, days: int) -> str:
    payload = {"r": "admin", "e": int(time.time() + days * 86400)}
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_token(secret: str, token: str):
    """Returns the payload dict, or None if the token is bad or expired."""
    try:
        body, sig = token.split(".", 1)
        good = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        padded = body + "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        if data["e"] < time.time():
            return None
        return data
    except Exception:
        return None


def parse_range(header, size: int):
    """Parse an HTTP Range header. Returns (start, end, is_partial).
    Raises ValueError if the range cannot be satisfied."""
    if not header or not header.startswith("bytes="):
        return 0, size - 1, False
    first = header[6:].split(",")[0].strip()
    a, _, b = first.partition("-")
    if a == "":
        n = int(b)
        if n <= 0:
            raise ValueError("bad range")
        start, end = max(size - n, 0), size - 1
    else:
        start = int(a)
        end = int(b) if b else size - 1
    end = min(end, size - 1)
    if start < 0 or start > end or start >= size:
        raise ValueError("bad range")
    return start, end, True


def parse_caption(text):
    """Channel post caption: 'unit_id | lecture title | free' (free is optional).
    Returns (unit_id, title, free) or None."""
    parts = [p.strip() for p in (text or "").split("|")]
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1]:
        return None
    free = len(parts) > 2 and parts[2].lower() == "free"
    return int(parts[0]), parts[1], free


def parse_lecture_line(text):
    """'unit_id | title | video_link | pdf_link | free' -> dict, or None if malformed.
    Use '-' for "no video". The pdf link and 'free' are optional and can come in any order."""
    parts = [x.strip() for x in (text or "").split("|")]
    if len(parts) < 3 or not parts[0].isdigit() or not parts[1]:
        return None
    is_link = lambda v: v.lower().startswith(("http://", "https://")) and " " not in v
    video = None
    if parts[2] not in ("", "-"):
        if not is_link(parts[2]):
            return None
        video = parts[2]
    pdf, free = None, False
    for extra in parts[3:]:
        if not extra:
            continue
        if extra.lower() == "free":
            free = True
        elif is_link(extra):
            pdf = extra
        else:
            return None
    return {"unit_id": int(parts[0]), "title": parts[1], "video_url": video, "pdf_url": pdf, "free": free}
