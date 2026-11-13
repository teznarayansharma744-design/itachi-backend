"""Payment and Telegram helpers with no database code (easy to test).
Uses only the standard library, so nothing extra to install."""
import base64
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.request


def parse_plans(text: str) -> list[dict]:
    """'1 Month:30:299,1 Year:365:2499' -> [{'id':'p0','name':..,'days':30,'price':299}, ...]"""
    plans = []
    for i, chunk in enumerate((text or "").split(",")):
        parts = [x.strip() for x in chunk.split(":")]
        if len(parts) != 3 or not parts[0] or not parts[1].isdigit() or not parts[2].isdigit():
            continue
        days, price = int(parts[1]), int(parts[2])
        if days < 1 or price < 1:
            continue
        plans.append({"id": f"p{i}", "name": parts[0], "days": days, "price": price})
    return plans


def verify_razorpay_signature(secret: str, raw_body: bytes, signature: str | None) -> bool:
    """Razorpay signs the raw request body with HMAC-SHA256 using the webhook secret."""
    if not secret or not signature:
        return False
    good = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, signature.strip())


def shop_secret(secret_key: str) -> str:
    """Secret used in the Telegram webhook path and header, derived from SECRET_KEY."""
    return hashlib.sha256((secret_key + "|shop").encode()).hexdigest()[:32]


def _request(req: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None


def tg_call(token: str, method: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    return _request(req)


def _basic(key_id: str, key_secret: str) -> str:
    return "Basic " + base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()


def rz_create_link(key_id, key_secret, *, amount_paise, description, reference_id, notes, expire_by):
    body = {
        "amount": amount_paise,
        "currency": "INR",
        "accept_partial": False,
        "description": description,
        "reference_id": reference_id,
        "notes": notes,
        "expire_by": expire_by,
        "reminder_enable": False,
    }
    req = urllib.request.Request(
        "https://api.razorpay.com/v1/payment_links",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": _basic(key_id, key_secret)},
    )
    return _request(req)


def rz_get_link(key_id, key_secret, link_id) -> dict:
    req = urllib.request.Request(
        f"https://api.razorpay.com/v1/payment_links/{link_id}",
        headers={"Authorization": _basic(key_id, key_secret)},
    )
    return _request(req)


def paid_payment_id(link: dict) -> str | None:
    """From a Razorpay payment link object, returns the captured payment id, or None."""
    if link.get("status") != "paid":
        return None
    for p in link.get("payments") or []:
        if p.get("status") in ("captured", "paid"):
            return p.get("payment_id")
    return None


_DEVICE_RE = re.compile(r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}")


def parse_device_id(text: str) -> str | None:
    """Finds a Device ID like 7K2M-9XQP-4TDA in a message. Returns it in capitals, or None."""
    m = _DEVICE_RE.search((text or "").upper())
    return m.group(0) if m else None
