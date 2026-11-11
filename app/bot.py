"""Telegram admin bot. It only adds and edits lectures (no keys, no streaming).

Add a lecture with direct links:
    /addlecture <unit_id> | <title> | <video_link> | <pdf_link> | free
Optionally the files can also live in a private Telegram channel; post a file there
with the caption  <unit_id> | <title> | free  and the bot links it to the lecture.
"""
import logging
import re
from functools import wraps
from html import escape

from sqlalchemy import func, or_, select
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config
from .db import Batch, Lecture, SessionLocal, Subject, Unit
from .utils import parse_caption, parse_lecture_line

HELP = """<b>Itachi Universe admin bot</b>

<b>Easiest: /menu</b>
Tap through batch, subject, unit, lecture. Add, rename, delete and set links with buttons. No typing of ids.

<b>Overview</b>
/stats  totals and what is missing
/tree [batch_id]  all batches, units, lectures with ids
/search &lt;text&gt;  find lectures by title
/missing  lectures with no video link

<b>Structure</b>
/addbatch &lt;name&gt;
/addsubject &lt;batch_id&gt; &lt;name&gt;
/addunit &lt;subject_id&gt; &lt;name&gt;
/rename &lt;batch|subject|unit|lecture&gt; &lt;id&gt; &lt;new name&gt;

<b>Add a lecture</b>
/addlecture &lt;unit_id&gt; | &lt;title&gt; | &lt;video_link&gt; | &lt;pdf_link&gt; | free
- Links must start with https://
- pdf_link and "free" are optional, in any order
- Use - when there is no video (a PDF-only item)
- Same unit + same title updates the existing lecture
- You can paste many lecture lines in one message

<b>Edit</b>
/setvideo &lt;lecture_id&gt; &lt;link&gt;  (- removes)
/setpdf &lt;lecture_id&gt; &lt;link&gt;  (- removes)
/free &lt;lecture_id&gt; on|off
/del &lt;batch|subject|unit|lecture&gt; &lt;id&gt;  (asks to confirm)
"""

MODELS = {"batch": Batch, "subject": Subject, "unit": Unit, "lecture": Lecture}
HAS_VIDEO = or_(Lecture.video_url.is_not(None), Lecture.video_msg_id.is_not(None))
HAS_PDF = or_(Lecture.pdf_url.is_not(None), Lecture.pdf_msg_id.is_not(None))
MENU = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("Open menu", callback_data="n:r:0")],
        [InlineKeyboardButton("Stats", callback_data="m:stats"), InlineKeyboardButton("Tree", callback_data="m:tree")],
        [InlineKeyboardButton("Missing videos", callback_data="m:missing"), InlineKeyboardButton("Help", callback_data="m:help")],
    ]
)


def admin_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in config.ADMIN_IDS:
            return  # silently ignore everyone else
        return await fn(update, context)

    return wrapper


async def _send(message, text: str, markup=None):
    """Send HTML text, splitting on line breaks to stay under Telegram's limit."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for i, c in enumerate(chunks):
        await message.reply_text(
            c.rstrip() or "-",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=markup if i == len(chunks) - 1 else None,
        )


async def _reply(update: Update, text: str, markup=None):
    await _send(update.message, escape(text), markup)


def _https(*urls) -> bool:
    return all(u is None or u.lower().startswith("https://") for u in urls)


def _find_or_create(db, unit_id: int, title: str) -> Lecture:
    lec = db.scalars(
        select(Lecture).where(
            Lecture.unit_id == unit_id, func.lower(Lecture.title) == title.lower()
        )
    ).first()
    if not lec:
        lec = Lecture(unit_id=unit_id, title=title)
        db.add(lec)
    return lec


@admin_only
async def help_cmd(update, context):
    await _send(update.message, HELP, MENU)


@admin_only
async def addbatch(update, context):
    name = " ".join(context.args).strip()
    if not name:
        return await _reply(update, "Usage: /addbatch <name>")
    with SessionLocal() as db:
        b = Batch(name=name)
        db.add(b)
        db.commit()
        await _reply(update, f"Batch added. id = {b.id}")


@admin_only
async def addsubject(update, context):
    if len(context.args) < 2 or not context.args[0].isdigit():
        return await _reply(update, "Usage: /addsubject <batch_id> <name>")
    with SessionLocal() as db:
        if not db.get(Batch, int(context.args[0])):
            return await _reply(update, "No batch with that id.")
        s = Subject(batch_id=int(context.args[0]), name=" ".join(context.args[1:]))
        db.add(s)
        db.commit()
        await _reply(update, f"Subject added. id = {s.id}")


@admin_only
async def addunit(update, context):
    if len(context.args) < 2 or not context.args[0].isdigit():
        return await _reply(update, "Usage: /addunit <subject_id> <name>")
    with SessionLocal() as db:
        if not db.get(Subject, int(context.args[0])):
            return await _reply(update, "No subject with that id.")
        u = Unit(subject_id=int(context.args[0]), name=" ".join(context.args[1:]))
        db.add(u)
        db.commit()
        await _reply(
            update,
            f"Unit added. id = {u.id}\nNow add lectures:\n/addlecture {u.id} | Lecture 1 | https://...",
        )


def _save_lecture(db, parsed: dict) -> str:
    """Save one parsed lecture line. Returns a one-line result message."""
    video, pdf = parsed["video_url"], parsed["pdf_url"]
    if not video and not pdf:
        return f"Skipped '{parsed['title']}': give a video or a PDF link."
    if not _https(video, pdf):
        return f"Skipped '{parsed['title']}': links must start with https://"
    unit = db.get(Unit, parsed["unit_id"])
    if not unit:
        return f"Skipped '{parsed['title']}': no unit with id {parsed['unit_id']}."
    lec = _find_or_create(db, parsed["unit_id"], parsed["title"])
    if video:
        lec.video_url = video
    if pdf:
        lec.pdf_url = pdf
    if parsed["free"]:
        lec.free = True
    db.commit()
    return f"Saved: {lec.title} (L{lec.id})" + (" FREE" if lec.free else "")


@admin_only
async def addlecture(update, context):
    parts = update.message.text.split(None, 1)
    lines = [ln.strip() for ln in (parts[1] if len(parts) > 1 else "").splitlines() if ln.strip()]
    if not lines:
        return await _reply(
            update,
            "Usage:\n/addlecture <unit_id> | <title> | <video_link> | <pdf_link> | free\n"
            "You can paste many lines in one message. Or use /menu and tap buttons.",
        )
    out = []
    with SessionLocal() as db:
        for ln in lines:
            parsed = parse_lecture_line(ln)
            out.append(_save_lecture(db, parsed) if parsed else f"Could not read: {ln[:60]}")
    await _reply(update, "\n".join(out))


async def _set_link(update, context, field: str):
    if len(context.args) != 2 or not context.args[0].isdigit():
        return await _reply(update, f"Usage: /set{field} <lecture_id> <https link>  (send - to remove)")
    link = None if context.args[1] == "-" else context.args[1]
    if link and not _https(link):
        return await _reply(update, "The link must start with https://")
    with SessionLocal() as db:
        lec = db.get(Lecture, int(context.args[0]))
        if not lec:
            return await _reply(update, "No lecture with that id.")
        setattr(lec, f"{field}_url", link)
        db.commit()
        await _reply(update, f"{field.capitalize()} {'saved' if link else 'removed'} for '{lec.title}'.")


@admin_only
async def setvideo(update, context):
    await _set_link(update, context, "video")


@admin_only
async def setpdf(update, context):
    await _set_link(update, context, "pdf")


def stats_text() -> str:
    with SessionLocal() as db:
        n = lambda q: db.scalar(q) or 0
        lectures = n(select(func.count(Lecture.id)))
        lines = [
            "<b>Library</b>",
            f"Batches: <b>{n(select(func.count(Batch.id)))}</b>",
            f"Subjects: <b>{n(select(func.count(Subject.id)))}</b>",
            f"Units: <b>{n(select(func.count(Unit.id)))}</b>",
            f"Lectures: <b>{lectures}</b>",
            "",
            f"With video: <b>{n(select(func.count(Lecture.id)).where(HAS_VIDEO))}</b>",
            f"With PDF: <b>{n(select(func.count(Lecture.id)).where(HAS_PDF))}</b>",
            f"Free (demo): <b>{n(select(func.count(Lecture.id)).where(Lecture.free.is_(True)))}</b>",
            f"PDF only: <b>{n(select(func.count(Lecture.id)).where(~HAS_VIDEO, HAS_PDF))}</b>",
            f"Empty (no video, no PDF): <b>{n(select(func.count(Lecture.id)).where(~HAS_VIDEO, ~HAS_PDF))}</b>",
        ]
        rows = db.execute(
            select(Batch.name, func.count(Lecture.id))
            .select_from(Batch)
            .outerjoin(Subject, Subject.batch_id == Batch.id)
            .outerjoin(Unit, Unit.subject_id == Subject.id)
            .outerjoin(Lecture, Lecture.unit_id == Unit.id)
            .group_by(Batch.id, Batch.name)
            .order_by(Batch.id)
        ).all()
        if rows:
            lines += ["", "<b>Lectures per batch</b>"]
            lines += [f"{escape(name)}: {count}" for name, count in rows]
    return "\n".join(lines)


def tree_text(batch_id: int | None = None) -> str:
    lines = []
    with SessionLocal() as db:
        q = select(Batch).order_by(Batch.id)
        if batch_id:
            q = q.where(Batch.id == batch_id)
        for b in db.scalars(q):
            lines.append(f"\n<b>[B{b.id}] {escape(b.name)}</b>")
            for s in b.subjects:
                lines.append(f"  <b>[S{s.id}] {escape(s.name)}</b>")
                for u in s.units:
                    lines.append(f"    [U{u.id}] {escape(u.name)}  ({len(u.lectures)})")
                    for l in u.lectures:
                        v = "V" if (l.video_url or l.video_msg_id) else "-"
                        p = "P" if (l.pdf_url or l.pdf_msg_id) else "-"
                        free = "  FREE" if l.free else ""
                        lines.append(f"        L{l.id}: {escape(l.title)}  [{v}{p}]{free}")
    return "\n".join(lines).strip() or "Nothing yet. Start with /addbatch"


def missing_text() -> str:
    with SessionLocal() as db:
        rows = db.scalars(select(Lecture).where(~HAS_VIDEO).order_by(Lecture.unit_id, Lecture.id)).all()
        if not rows:
            return "Every lecture has a video."
        lines = [f"<b>{len(rows)} lecture(s) without a video</b>"]
        for l in rows:
            kind = "PDF only" if (l.pdf_url or l.pdf_msg_id) else "EMPTY"
            lines.append(f"L{l.id}: {escape(l.title)}  [{kind}]")
        lines.append("\nFix with /setvideo &lt;id&gt; &lt;link&gt;")
        return "\n".join(lines)


@admin_only
async def stats(update, context):
    await _send(update.message, stats_text(), MENU)


@admin_only
async def tree(update, context):
    bid = int(context.args[0]) if context.args and context.args[0].isdigit() else None
    await _send(update.message, tree_text(bid))


@admin_only
async def missing(update, context):
    await _send(update.message, missing_text())


@admin_only
async def search(update, context):
    term = " ".join(context.args).strip()
    if not term:
        return await _reply(update, "Usage: /search <text>")
    with SessionLocal() as db:
        rows = db.scalars(
            select(Lecture).where(Lecture.title.ilike(f"%{term}%")).order_by(Lecture.id).limit(30)
        ).all()
        if not rows:
            return await _reply(update, "No lecture found.")
        lines = [f"<b>{len(rows)} result(s)</b>"]
        for l in rows:
            v = "V" if (l.video_url or l.video_msg_id) else "-"
            p = "P" if (l.pdf_url or l.pdf_msg_id) else "-"
            lines.append(f"L{l.id}: {escape(l.title)}  [{v}{p}]  unit U{l.unit_id}" + ("  FREE" if l.free else ""))
    await _send(update.message, "\n".join(lines))


@admin_only
async def rename(update, context):
    if len(context.args) < 3 or context.args[0] not in MODELS or not context.args[1].isdigit():
        return await _reply(update, "Usage: /rename <batch|subject|unit|lecture> <id> <new name>")
    new = " ".join(context.args[2:]).strip()
    attr = "title" if context.args[0] == "lecture" else "name"
    with SessionLocal() as db:
        row = db.get(MODELS[context.args[0]], int(context.args[1]))
        if not row:
            return await _reply(update, "Not found.")
        setattr(row, attr, new)
        db.commit()
    await _reply(update, f"Renamed to: {new}")


@admin_only
async def free_cmd(update, context):
    if len(context.args) != 2 or not context.args[0].isdigit() or context.args[1] not in ("on", "off"):
        return await _reply(update, "Usage: /free <lecture_id> on|off")
    with SessionLocal() as db:
        lec = db.get(Lecture, int(context.args[0]))
        if not lec:
            return await _reply(update, "No lecture with that id.")
        lec.free = context.args[1] == "on"
        db.commit()
        await _reply(update, f"'{lec.title}' is now {'FREE' if lec.free else 'locked'}.")


KINDS = {"b": ("batch", Batch), "s": ("subject", Subject), "u": ("unit", Unit), "l": ("lecture", Lecture)}
SHORT = {"batch": "b", "subject": "s", "unit": "u", "lecture": "l"}
CHILD = {"b": "s", "s": "u", "u": "l"}  # what lives inside what
CHILD_WORD = {"b": "subject", "s": "unit", "u": "lectures"}
PROMPTS = {
    "b": "Send batch names, one per line.",
    "s": "Send subject names, one per line.",
    "u": "Send unit names, one per line.",
    "l": "Send lectures, one per line:\n<code>title | video_link | pdf_link | free</code>\n"
         "pdf_link and free are optional. Use - when there is no video.",
}
MAX_BUTTONS = 40


def _btn(text: str, data: str):
    return InlineKeyboardButton(text, callback_data=data)


def _short(t: str, n: int = 36) -> str:
    return t if len(t) <= n else t[: n - 1] + "…"


def _flags(l) -> str:
    v = "V" if (l.video_url or l.video_msg_id) else "-"
    p = "P" if (l.pdf_url or l.pdf_msg_id) else "-"
    return f"{v}{p}"


def _parent(k: str, row):
    if k == "l":
        return "u", row.unit_id
    if k == "u":
        return "s", row.subject_id
    if k == "s":
        return "b", row.batch_id
    return "r", 0


def build_view(k: str, rid: int):
    """Returns (html text, keyboard) for a screen: r=root, b/s/u/l = that item."""
    with SessionLocal() as db:
        if k == "r":
            rows = db.scalars(select(Batch).order_by(Batch.id)).all()
            text = "<b>Itachi Universe</b>\n" + ("Choose a batch." if rows else "No batches yet. Tap + Add batch.")
            kb = [[_btn(_short(b.name), f"n:b:{b.id}")] for b in rows[:MAX_BUTTONS]]
            kb.append([_btn("+ Add batch", "a:b:0")])
            kb.append([_btn("Stats", "m:stats"), _btn("Missing", "m:missing"), _btn("Help", "m:help")])
            return text, InlineKeyboardMarkup(kb)

        row = db.get(KINDS[k][1], rid)
        if not row:
            return build_view("r", 0)
        pk, pid = _parent(k, row)
        back = _btn("« Back", f"n:{pk}:{pid}")

        if k == "l":
            text = (
                f"<b>{escape(row.title)}</b>  (L{row.id})\n"
                f"Video: {escape(row.video_url or ('Telegram file' if row.video_msg_id else 'none'))}\n"
                f"PDF: {escape(row.pdf_url or ('Telegram file' if row.pdf_msg_id else 'none'))}\n"
                f"Access: {'FREE (demo)' if row.free else 'Premium only'}"
            )
            kb = [
                [_btn("Make locked" if row.free else "Make FREE", f"f:{row.id}")],
                [_btn("Set video link", f"v:{row.id}"), _btn("Set PDF link", f"p:{row.id}")],
                [_btn("Rename", f"r:l:{row.id}"), _btn("Delete", f"x:l:{row.id}")],
                [back],
            ]
            return text, InlineKeyboardMarkup(kb)

        title = {"b": "Batch", "s": "Subject", "u": "Unit"}[k]
        children = row.subjects if k == "b" else row.units if k == "s" else row.lectures
        ck = CHILD[k]
        kb = []
        for c in children[:MAX_BUTTONS]:
            if ck == "l":
                label = f"{c.title}  [{_flags(c)}]" + ("  FREE" if c.free else "")
            elif ck == "u":
                label = f"{c.name}  ({len(c.lectures)})"
            else:
                label = c.name
            kb.append([_btn(_short(label, 44), f"n:{ck}:{c.id}")])
        text = f"<b>{title}: {escape(row.name)}</b>  (id {row.id})\n{len(children)} {CHILD_WORD[k]}"
        if len(children) > MAX_BUTTONS:
            text += f"\nShowing the first {MAX_BUTTONS}. Use /search for the rest."
        kb.append([_btn(f"+ Add {CHILD_WORD[k]}", f"a:{ck}:{row.id}")])
        kb.append([_btn("Rename", f"r:{k}:{row.id}"), _btn("Delete", f"x:{k}:{row.id}")])
        kb.append([back])
        return text, InlineKeyboardMarkup(kb)


async def _show(q, k: str, rid: int):
    text, markup = build_view(k, rid)
    try:
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
        )
    except BadRequest:
        pass  # message is already showing this screen


@admin_only
async def menu_cmd(update, context):
    context.user_data.pop("await", None)
    text, markup = build_view("r", 0)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


@admin_only
async def del_cmd(update, context):
    if len(context.args) != 2 or context.args[0] not in SHORT or not context.args[1].isdigit():
        return await _reply(update, "Usage: /del <batch|subject|unit|lecture> <id>  (or use /menu)")
    k, rid = SHORT[context.args[0]], int(context.args[1])
    await _ask_delete(update.message, k, rid)


async def _ask_delete(message, k: str, rid: int):
    with SessionLocal() as db:
        row = db.get(KINDS[k][1], rid)
        if not row:
            return await _send(message, "Not found.")
        name = getattr(row, "title", None) or row.name
    extra = "" if k == "l" else "\nEverything inside it is deleted too."
    markup = InlineKeyboardMarkup(
        [[_btn("Yes, delete", f"d:{k}:{rid}"), _btn("Cancel", f"n:{k}:{rid}")]]
    )
    await _send(message, f"Delete {KINDS[k][0]} <b>{escape(name)}</b>?{extra}", markup)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or q.from_user.id not in config.ADMIN_IDS:
        return
    await q.answer()
    data = q.data or ""
    parts = data.split(":")
    act = parts[0]

    if act == "m":
        key = parts[1]
        if key == "stats":
            await _send(q.message, stats_text())
        elif key == "tree":
            await _send(q.message, tree_text())
        elif key == "missing":
            await _send(q.message, missing_text())
        elif key == "help":
            await _send(q.message, HELP)
        return

    if act == "n":  # navigate
        context.user_data.pop("await", None)
        return await _show(q, parts[1], int(parts[2]))

    if act == "a":  # add children: wait for the admin's next message(s)
        k, parent = parts[1], int(parts[2])
        context.user_data["await"] = ("add", k, parent)
        pk = {"s": "b", "u": "s", "l": "u"}.get(k, "r")
        done = _btn("Done", f"n:{pk}:{parent}")
        return await _send(q.message, PROMPTS[k] + "\nI keep listening until you press Done.", InlineKeyboardMarkup([[done]]))

    if act == "r":  # rename
        k, rid = parts[1], int(parts[2])
        context.user_data["await"] = ("ren", k, rid)
        return await _send(q.message, "Send the new name.", InlineKeyboardMarkup([[_btn("Cancel", f"n:{k}:{rid}")]]))

    if act in ("v", "p"):  # set link
        rid = int(parts[1])
        context.user_data["await"] = ("lnk", "video" if act == "v" else "pdf", rid)
        return await _send(
            q.message,
            f"Send the new {'video' if act == 'v' else 'PDF'} link (https://...), or - to remove it.",
            InlineKeyboardMarkup([[_btn("Cancel", f"n:l:{rid}")]]),
        )

    if act == "f":  # toggle free
        with SessionLocal() as db:
            lec = db.get(Lecture, int(parts[1]))
            if lec:
                lec.free = not lec.free
                db.commit()
        return await _show(q, "l", int(parts[1]))

    if act == "x":  # ask before deleting
        return await _ask_delete(q.message, parts[1], int(parts[2]))

    if act == "d":  # confirmed delete
        k, rid = parts[1], int(parts[2])
        with SessionLocal() as db:
            row = db.get(KINDS[k][1], rid)
            if not row:
                return await q.edit_message_text("Already deleted.")
            pk, pid = _parent(k, row)
            db.delete(row)  # children are deleted too
            db.commit()
        await q.edit_message_text("Deleted.")
        text, markup = build_view(pk, pid)
        return await q.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Plain messages: answers to the question the last button asked."""
    user = update.effective_user
    if not user or user.id not in config.ADMIN_IDS or not update.message or not update.message.text:
        return
    state = context.user_data.get("await")
    if not state:
        return await menu_cmd(update, context)
    text = update.message.text
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    kind = state[0]

    if kind == "ren":
        _, k, rid = state
        with SessionLocal() as db:
            row = db.get(KINDS[k][1], rid)
            if not row:
                return await _reply(update, "Not found.")
            setattr(row, "title" if k == "l" else "name", text.strip())
            db.commit()
        context.user_data.pop("await", None)
        t, m = build_view(k, rid)
        return await update.message.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=m)

    if kind == "lnk":
        _, field, rid = state
        link = None if text.strip() == "-" else text.strip()
        if link and not _https(link):
            return await _reply(update, "The link must start with https://  Try again or press Cancel.")
        with SessionLocal() as db:
            lec = db.get(Lecture, rid)
            if not lec:
                return await _reply(update, "Not found.")
            setattr(lec, f"{field}_url", link)
            db.commit()
        context.user_data.pop("await", None)
        t, m = build_view("l", rid)
        return await update.message.reply_text(t, parse_mode=ParseMode.HTML, reply_markup=m)

    if kind == "add":
        _, k, parent = state
        out = []
        with SessionLocal() as db:
            if k == "l":
                for ln in lines:
                    parsed = parse_lecture_line(f"{parent} | {ln}")
                    out.append(_save_lecture(db, parsed) if parsed else f"Could not read: {ln[:60]}")
            else:
                model, field = {"b": (Batch, None), "s": (Subject, "batch_id"), "u": (Unit, "subject_id")}[k]
                for ln in lines:
                    db.add(model(name=ln, **({field: parent} if field else {})))
                    out.append(f"Added: {ln}")
                db.commit()
        pk = {"s": "b", "u": "s", "l": "u"}.get(k, "r")
        done = _btn("Done", f"n:{pk}:{parent}")
        return await _send(
            update.message,
            escape("\n".join(out)) + "\n\nSend more, or press Done.",
            InlineKeyboardMarkup([[done]]),
        )


# ---- optional: files stored in a private Telegram channel ----

async def _notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str):
    for admin_id in config.ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, text)
        except Exception:
            pass  # the admin has not started the bot yet


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = update.channel_post
    if not post:
        return
    kind = None
    if post.video:
        kind = "video"
    elif post.photo:
        kind = "thumb"
    elif post.document:
        mime = post.document.mime_type or ""
        if mime == "application/pdf":
            kind = "pdf"
        elif mime.startswith("video/"):
            kind = "video"
    if kind is None:
        return

    parsed = parse_caption(post.caption)
    if not parsed:
        return await _notify_admins(
            context,
            "A file in the channel was ignored: its caption must look like\n<unit_id> | <lecture title> | free",
        )
    unit_id, title, free = parsed
    with SessionLocal() as db:
        unit = db.get(Unit, unit_id)
        if not unit:
            return await _notify_admins(context, f"Unit {unit_id} does not exist. Check /tree.")
        lec = _find_or_create(db, unit_id, title)
        if kind == "video":
            lec.video_msg_id = post.message_id
        elif kind == "pdf":
            lec.pdf_msg_id = post.message_id
        else:
            lec.thumb_msg_id = post.message_id
        if free:
            lec.free = True
        db.commit()
        label = {"video": "Video", "pdf": "PDF", "thumb": "Thumbnail"}[kind]
        await _notify_admins(
            context, f"{label} saved: '{lec.title}' (lecture id {lec.id}) in unit '{unit.name}'."
        )


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logging.getLogger(__name__).exception("Bot error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Something went wrong. Check the Termux window for details.")
        except Exception:
            pass


async def start():
    if not config.BOT_TOKEN:
        return None
    app = Application.builder().token(config.BOT_TOKEN).build()
    private = filters.ChatType.PRIVATE
    for name, fn in [
        (["start", "menu"], menu_cmd),
        (["help"], help_cmd),
        (["addbatch"], addbatch),
        (["addsubject"], addsubject),
        (["addunit"], addunit),
        (["addlecture"], addlecture),
        (["setvideo"], setvideo),
        (["setpdf"], setpdf),
        (["tree"], tree),
        (["stats"], stats),
        (["missing"], missing),
        (["search"], search),
        (["rename"], rename),
        (["free"], free_cmd),
        (["del"], del_cmd),
    ]:
        app.add_handler(CommandHandler(name, fn, filters=private))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    if config.CHANNEL_ID:  # channel upload is optional
        app.add_handler(
            MessageHandler(filters.UpdateType.CHANNEL_POST & filters.Chat(config.CHANNEL_ID), on_channel_post)
        )
    await app.initialize()
    try:
        await app.bot.set_my_commands(
            [
                BotCommand("menu", "Open the button menu"),
                BotCommand("stats", "Totals and what is missing"),
                BotCommand("tree", "All batches, units and lectures"),
                BotCommand("search", "Find a lecture by title"),
                BotCommand("missing", "Lectures without a video"),
                BotCommand("addbatch", "Add a batch"),
                BotCommand("addsubject", "Add a subject"),
                BotCommand("addunit", "Add a unit"),
                BotCommand("addlecture", "Add a lecture with links"),
                BotCommand("setvideo", "Change a video link"),
                BotCommand("setpdf", "Change a PDF link"),
                BotCommand("free", "Make a lecture free or locked"),
                BotCommand("rename", "Rename anything"),
                BotCommand("del", "Delete (asks to confirm)"),
                BotCommand("help", "Show all commands"),
            ]
        )
    except Exception:
        pass  # the menu is a nicety, not required
    await app.start()
    await app.updater.start_polling(
        allowed_updates=["message", "callback_query", "channel_post"], drop_pending_updates=False
    )
    return app


async def stop(app):
    if app is None:
        return
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
