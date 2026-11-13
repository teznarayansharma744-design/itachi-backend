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


# ---------- Fulfilment (the only place a key is created) ----------

def fulfil(order_id: str, payment_id: str) -> str | None:
    """Marks the order paid and gives the buyer a key. Safe to call twice: the second call
    does nothing. Returns the key if THIS call created or extended it, else None."""
    with SessionLocal() as db:
        order = db.scalars(select(Order).where(Order.id == order_id).with_for_update()).first()
        if not order or order.status == "paid":
            return None
        # A buyer who already owns a working key gets that key extended, not a second one.
        old = None
        prev = db.scalars(
            select(Order)
            .where(Order.tg_id == order.tg_id, Order.status == "paid", Order.key.is_not(None))
            .order_by(Order.created_at.desc())
        ).first()
        if prev:
            old = db.get(Key, prev.key)
            if old and old.revoked:
                old = None
        if old:
            start = max(old.expires_at, utcnow())
            old.expires_at = start + timedelta(days=order.days)
            key = old
        else:
            key = Key(
                key=new_key(),
                device_id="",  # binds to the first phone that activates it
                expires_at=utcnow() + timedelta(days=order.days),
                note=f"tg:{order.tg_id} {order.plan_name}",
            )
            db.add(key)
        order.status = "paid"
        order.payment_id = payment_id
        order.key = key.key
        db.commit()
        tg_id, plan, value, extended = order.tg_id, order.plan_name, key.key, old is not None
        until = key.expires_at.strftime("%d %b %Y")
    _send(tg_id, _key_text(plan, value, until, extended))
    return value


def _key_text(plan: str, key: str, until: str, extended: bool) -> str:
    head = "Payment received. Your key was extended." if extended else "Payment received. Here is your key."
    return (
        f"<b>{head}</b>\n\n"
        f"Plan: {html.escape(plan)}\nValid till: {until}\n\n"
        f"<code>{html.escape(key)}</code>\n\n"
        "Open the app, tap Get Premium, and paste this key. The key locks to the first phone "
        "that uses it, so keep it private."
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


def _plans_text(plans) -> str:
    lines = [f"• {html.escape(p['name'])}: ₹{p['price']}" for p in plans]
    return "<b>Itachi Universe Premium</b>\n\n" + "\n".join(lines) + "\n\nPick a plan to pay."


def _plan_buttons(plans):
    return [[{"text": f"{p['name']} · ₹{p['price']}", "callback_data": f"buy:{p['id']}"}] for p in plans]


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
            _start_purchase(chat, data[4:], plans)
        elif data.startswith("paid:"):
            _check_paid(chat, data[5:])
        return
    msg = update.get("message") or {}
    chat = (msg.get("chat") or {}).get("id")
    if not chat or (msg.get("chat") or {}).get("type") != "private":
        return
    text = (msg.get("text") or "").strip().lower()
    if text.startswith("/mykey"):
        _my_key(chat)
    elif text.startswith("/help"):
        _send(
            chat,
            "Send /start to see plans and pay. Send /mykey to see your key again. "
            f"Problem with a payment? Message @{html.escape(config.SUPPORT_USERNAME)}.",
        )
    else:
        _send(chat, _plans_text(plans), _plan_buttons(plans))


def _start_purchase(chat: int, plan_id: str, plans) -> None:
    plan = next((p for p in plans if p["id"] == plan_id), None)
    if not plan:
        return _send(chat, "That plan is not available. Send /start again.")
    since = utcnow() - timedelta(hours=1)
    with SessionLocal() as db:
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
                notes={"tg_id": str(chat), "order": order.id},
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
        f"<b>{html.escape(plan['name'])}</b> · ₹{plan['price']}\n\n"
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
        order = db.scalars(
            select(Order).where(Order.tg_id == chat, Order.status == "paid", Order.key.is_not(None))
            .order_by(Order.created_at.desc())
        ).first()
        key = db.get(Key, order.key) if order else None
        if not key:
            return _send(chat, "You have no key yet. Send /start to buy one.")
        state = "revoked" if key.revoked else ("expired" if key.expires_at <= utcnow() else "active")
        text = (
            f"<code>{html.escape(key.key)}</code>\n\nStatus: {state}\n"
            f"Valid till: {key.expires_at.strftime('%d %b %Y')}"
        )
    _send(chat, text)
