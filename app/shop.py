"""Shop bot: a user picks a plan in Telegram, pays on a Razorpay page, and the server
creates the key and sends it back by itself. Runs inside the web server through
webhooks, so it needs no always-on phone."""
import html
import json
import secrets
import time
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from . import config
from .db import Key, Order, SessionLocal, utcnow
from .payments import (
    paid_payment_id,
    parse_device_id,
    parse_plans,
    rz_create_link,
    rz_get_link,
    shop_secret,
    tg_call,
    verify_razorpay_signature,
)
from .utils import new_key

router = APIRouter(prefix="/shop")


def _enabled() -> bool:
    return bool(config.SHOP_BOT_TOKEN and config.RAZORPAY_KEY_ID and config.RAZORPAY_KEY_SECRET)


def _send(chat_id: int, text: str, buttons: list | None = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        tg_call(config.SHOP_BOT_TOKEN, "sendMessage", payload)
    except Exception as e:  # never let a failed message break payment handling
        print("shop: sendMessage failed:", e)


def set_telegram_webhook() -> None:
    if not (config.SHOP_BOT_TOKEN and config.PUBLIC_URL and config.SECRET_KEY):
        return
    secret = shop_secret(config.SECRET_KEY)
    try:
        tg_call(
            config.SHOP_BOT_TOKEN,
            "setWebhook",
            {
                "url": f"{config.PUBLIC_URL}/shop/tg/{secret}",
                "secret_token": secret,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        print("shop: Telegram webhook set")
    except Exception as e:
        print("shop: could not set webhook:", e)


_bot_username: str | None = None


@router.get("/info")
def shop_info():
    """The app asks this to find the shop bot and the prices."""
    global _bot_username
    if not _enabled():
        raise HTTPException(503, "Shop is not set up.")
    if not _bot_username:
        try:
            _bot_username = tg_call(config.SHOP_BOT_TOKEN, "getMe", {})["result"]["username"]
        except Exception:
            raise HTTPException(503, "Shop bot is not reachable.")
    return {"bot": _bot_username, "plans": [
        {"name": p["name"], "days": p["days"], "price": p["price"]} for p in parse_plans(config.PLANS)
    ]}


# ---------- Fulfilment (the only place a key is created) ----------

def fulfil(order_id: str, payment_id: str) -> str | None:
    """Marks the order paid and creates the key for the device in the order. Safe to call
    twice: the second call does nothing. Returns the new key, or None if already done."""
    with SessionLocal() as db:
        order = db.scalars(select(Order).where(Order.id == order_id).with_for_update()).first()
        if not order or order.status == "paid":
            return None
        key = Key(
            key=new_key(),
            device_id=order.device_id,  # locked to the phone the buyer gave us
            expires_at=utcnow() + timedelta(days=order.days),
            note=f"tg:{order.tg_id} {order.plan_name}",
        )
        db.add(key)
        order.status = "paid"
        order.payment_id = payment_id
        order.key = key.key
        db.commit()
        tg_id, plan, value, device = order.tg_id, order.plan_name, key.key, order.device_id
        until = _until(key.expires_at, order.days)
    _send(tg_id, _key_text(plan, value, until, device))
    return value


def _until(expires_at, days: int) -> str:
    return "Lifetime" if days >= 36500 else expires_at.strftime("%d %b %Y")


def _key_text(plan: str, key: str, until: str, device: str) -> str:
    return (
        "<b>Payment received. Here is your key.</b>\n\n"
        f"Plan: {html.escape(plan)}\nValid till: {until}\nDevice: <code>{html.escape(device)}</code>\n\n"
        f"<code>{html.escape(key)}</code>\n\n"
        "Open the app, tap Get Premium, and paste this key. It works only on the phone whose "
        "Device ID you gave. Keep it private."
    )


# ---------- Razorpay webhook ----------

@router.post("/razorpay")
async def razorpay_webhook(request: Request):
    raw = await request.body()
    if not verify_razorpay_signature(
        config.RAZORPAY_WEBHOOK_SECRET, raw, request.headers.get("x-razorpay-signature")
    ):
        raise HTTPException(400, "Bad signature.")
    event = json.loads(raw or b"{}")
    if event.get("event") != "payment_link.paid":
        return {"ok": True}  # other events are ignored on purpose
    link = (event.get("payload") or {}).get("payment_link", {}).get("entity", {})
    pay = (event.get("payload") or {}).get("payment", {}).get("entity", {})
    await run_in_threadpool(_handle_paid, link, pay.get("id"))
    return {"ok": True}


def _handle_paid(link: dict, payment_id: str | None) -> None:
    order_id = link.get("reference_id")
    with SessionLocal() as db:
        order = db.get(Order, order_id) if order_id else None
        if not order or order.link_id != link.get("id"):
            print("shop: webhook for unknown order", order_id)
            return
        if link.get("status") != "paid" or int(link.get("amount_paid") or 0) < order.amount_paise:
            print("shop: amount/status mismatch for", order_id)
            return
    fulfil(order_id, payment_id or link.get("id"))


# ---------- Telegram bot ----------

@router.post("/tg/{secret}")
async def telegram_webhook(secret: str, request: Request):
    good = shop_secret(config.SECRET_KEY)
    if secret != good or request.headers.get("x-telegram-bot-api-secret-token") != good:
        raise HTTPException(403, "Forbidden.")
    update = await request.json()
    try:
        await run_in_threadpool(_handle_update, update)
    except Exception as e:
        print("shop: update failed:", e)
    return {"ok": True}  # always 200, or Telegram keeps retrying


def _price_lines(plans) -> str:
    return "\n".join(f"• {html.escape(p['name'])}: ₹{p['price']}" for p in plans)


def _ask_device(chat: int, plans) -> None:
    _send(
        chat,
        "<b>Itachi Universe Premium</b>\n\n"
        f"{_price_lines(plans)}\n\n"
        "Step 1: open the app, go to Get Premium and copy your <b>Device ID</b> "
        "(looks like <code>7K2M-9XQP-4TDA</code>).\n"
        "Step 2: send it to me here. Then you can pay and I will send your key.",
    )


def _handle_update(update: dict) -> None:
    if not _enabled():
        return
    plans = parse_plans(config.PLANS)
    if "callback_query" in update:
        cq = update["callback_query"]
        try:
            tg_call(config.SHOP_BOT_TOKEN, "answerCallbackQuery", {"callback_query_id": cq["id"]})
        except Exception:
            pass
        chat = cq["from"]["id"]
        data = cq.get("data", "")
        if data.startswith("buy:"):
            _, plan_id, device = (data.split(":", 2) + ["", ""])[:3]
            _start_purchase(chat, plan_id, parse_device_id(device), plans)
        elif data.startswith("paid:"):
            _check_paid(chat, data[5:])
        return
    msg = update.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    if not chat or (msg.get("chat") or {}).get("type") != "private":
        return
    text = (msg.get("text") or "").strip()
    low = text.lower()
    if low.startswith("/mykey"):
        _my_key(chat)
    elif low.startswith("/help"):
        _send(
            chat,
            "Send /start to buy. Send /mykey to see your keys again. "
            f"Problem with a payment? Message @{html.escape(config.SUPPORT_USERNAME)}.",
        )
    elif not parse_device_id(text):  # also covers a bare /start
        _ask_device(chat, plans)
    else:
        device = parse_device_id(text)
        if len(plans) == 1:
            _start_purchase(chat, plans[0]["id"], device, plans)
        else:
            _send(
                chat,
                f"Device ID: <code>{device}</code>\nPick a plan:",
                [[{"text": f"{p['name']} · ₹{p['price']}", "callback_data": f"buy:{p['id']}:{device}"}] for p in plans],
            )


def _start_purchase(chat: int, plan_id: str, device: str | None, plans) -> None:
    plan = next((p for p in plans if p["id"] == plan_id), None)
    if not plan:
        return _send(chat, "That plan is not available. Send /start again.")
    if not device:
        return _ask_device(chat, plans)
    since = utcnow() - timedelta(hours=1)
    with SessionLocal() as db:
        owned = db.scalars(
            select(Key).where(Key.device_id == device, Key.revoked == False, Key.expires_at > utcnow())  # noqa: E712
        ).first()
        if owned:
            return _send(
                chat,
                "This Device ID already has an active key, so no payment is needed. "
                f"If you lost it, send /mykey from the Telegram account you paid with, or message @{html.escape(config.SUPPORT_USERNAME)}.",
            )
        recent = db.scalars(
            select(Order).where(Order.tg_id == chat, Order.created_at > since, Order.status == "pending")
        ).all()
        if len(recent) >= 5:
            return _send(chat, "Too many unpaid links. Please pay one of them or try again in an hour.")
        order = Order(
            id=secrets.token_hex(6),
            tg_id=chat,
            plan_name=plan["name"],
            days=plan["days"],
            device_id=device,
            amount_paise=plan["price"] * 100,
        )
        db.add(order)
        db.commit()
        try:
            link = rz_create_link(
                config.RAZORPAY_KEY_ID,
                config.RAZORPAY_KEY_SECRET,
                amount_paise=order.amount_paise,
                description=f"Itachi Universe - {plan['name']}",
                reference_id=order.id,
                notes={"tg_id": str(chat), "order": order.id, "device": device},
                expire_by=int(time.time()) + 2 * 3600,
            )
        except Exception as e:
            print("shop: razorpay link failed:", e)
            db.delete(order)
            db.commit()
            return _send(chat, "Could not create the payment link. Please try again in a minute.")
        order.link_id = link["id"]
        db.commit()
        oid = order.id
    _send(
        chat,
        f"<b>{html.escape(plan['name'])}</b> · ₹{plan['price']}\nDevice: <code>{device}</code>\n\n"
        "Pay with UPI, card or netbanking. Your key arrives here by itself within a minute of "
        "paying. The link works for 2 hours.",
        [
            [{"text": f"Pay ₹{plan['price']}", "url": link["short_url"]}],
            [{"text": "I have paid, check now", "callback_data": f"paid:{oid}"}],
        ],
    )


def _check_paid(chat: int, order_id: str) -> None:
    """Backup for a late webhook: asks Razorpay directly, then fulfils."""
    with SessionLocal() as db:
        order = db.get(Order, order_id)
        if not order or order.tg_id != chat:
            return _send(chat, "Order not found.")
        status, link_id, key = order.status, order.link_id, order.key
    if status == "paid":
        return _my_key(chat)
    try:
        link = rz_get_link(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET, link_id)
    except Exception as e:
        print("shop: razorpay check failed:", e)
        return _send(chat, "Could not check right now. Try again in a minute.")
    pid = paid_payment_id(link)
    if pid and int(link.get("amount_paid") or 0) >= order.amount_paise:
        fulfil(order_id, pid)
    else:
        _send(chat, "No payment found yet. If you just paid, wait a minute and tap the button again.")


def _my_key(chat: int) -> None:
    with SessionLocal() as db:
        orders = db.scalars(
            select(Order).where(Order.tg_id == chat, Order.status == "paid", Order.key.is_not(None))
            .order_by(Order.created_at.desc()).limit(5)
        ).all()
        lines = []
        for o in orders:
            key = db.get(Key, o.key)
            if not key:
                continue
            state = "revoked" if key.revoked else ("expired" if key.expires_at <= utcnow() else "active")
            lines.append(
                f"<code>{html.escape(key.key)}</code>\nDevice: <code>{html.escape(key.device_id)}</code>"
                f" · {state} · {_until(key.expires_at, o.days)}"
            )
    if not lines:
        return _send(chat, "You have no key yet. Send /start to buy one.")
    _send(chat, "\n\n".join(lines))
