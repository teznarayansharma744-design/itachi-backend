"""Telegram admin bot. It only adds and edits lectures (no keys, no streaming).

Add a lecture with direct links:
    /addlecture <unit_id> | <title> | <video_link> | <pdf_link> | free
Optionally the files can also live in a private Telegram channel; post a file there
with the caption  <unit_id> | <title> | free  and the bot links it to the lecture.
"""
from functools import wraps

from sqlalchemy import func, select
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config
from .db import Batch, Lecture, SessionLocal, Subject, Unit
from .utils import parse_caption, parse_lecture_line

HELP = """Itachi Universe admin bot

Structure
/addbatch <name>
/addsubject <batch_id> <name>
/addunit <subject_id> <name>
/tree  (shows all ids)

Add a lecture with direct links
/addlecture <unit_id> | <title> | <video_link> | <pdf_link> | free

Rules
- Links must start with https://
- pdf_link and "free" are optional, in any order
- Use - when there is no video (a PDF-only item)
- Same unit + same title updates the existing lecture

Examples
/addlecture 1 | Lecture 1: Newton | https://example.com/v1.mp4
/addlecture 1 | Lecture 2 | https://example.com/v2.m3u8 | https://example.com/n2.pdf
/addlecture 1 | Formula Sheet | - | https://example.com/f.pdf
/addlecture 1 | Free Intro | https://example.com/i.mp4 | free

Edit
/setvideo <lecture_id> <https link>   (send - to remove)
/setpdf <lecture_id> <https link>     (send - to remove)
/free <lecture_id> on|off
/del <batch|subject|unit|lecture> <id>
"""


def admin_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in config.ADMIN_IDS:
            return  # silently ignore everyone else
        return await fn(update, context)

    return wrapper


async def _reply(update: Update, text: str):
    for i in range(0, len(text), 3800):  # Telegram limit is 4096 chars
        await update.message.reply_text(text[i:i + 3800])


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
    await _reply(update, HELP)


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


@admin_only
async def addlecture(update, context):
    parsed = parse_lecture_line(update.message.text.partition(" ")[2])
    if not parsed:
        return await _reply(
            update,
            "Usage:\n/addlecture <unit_id> | <title> | <video_link> | <pdf_link> | free\n"
            "Use - for no video. Send /help for examples.",
        )
    video, pdf = parsed["video_url"], parsed["pdf_url"]
    if not video and not pdf:
        return await _reply(update, "Give at least a video link or a PDF link.")
    if not _https(video, pdf):
        return await _reply(update, "Links must start with https:// (plain http does not work on phones).")
    with SessionLocal() as db:
        unit = db.get(Unit, parsed["unit_id"])
        if not unit:
            return await _reply(update, "No unit with that id. Check /tree.")
        lec = _find_or_create(db, parsed["unit_id"], parsed["title"])
        if video:
            lec.video_url = video
        if pdf:
            lec.pdf_url = pdf
        if parsed["free"]:
            lec.free = True
        db.commit()
        await _reply(
            update,
            f"Saved: '{lec.title}' (lecture id {lec.id}) in unit '{unit.name}'."
            + (" It is FREE." if lec.free else ""),
        )


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


@admin_only
async def tree(update, context):
    lines = []
    with SessionLocal() as db:
        for b in db.scalars(select(Batch).order_by(Batch.id)):
            lines.append(f"[B{b.id}] {b.name}")
            for s in b.subjects:
                lines.append(f"  [S{s.id}] {s.name}")
                for u in s.units:
                    lines.append(f"    [U{u.id}] {u.name}")
                    for l in u.lectures:
                        v = "V" if (l.video_url or l.video_msg_id) else "-"
                        p = "P" if (l.pdf_url or l.pdf_msg_id) else "-"
                        free = " FREE" if l.free else ""
                        lines.append(f"      [L{l.id}] {l.title} ({v}{p}){free}")
    await _reply(update, "\n".join(lines) or "Nothing yet. Start with /addbatch")


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


@admin_only
async def del_cmd(update, context):
    models = {"batch": Batch, "subject": Subject, "unit": Unit, "lecture": Lecture}
    if len(context.args) != 2 or context.args[0] not in models or not context.args[1].isdigit():
        return await _reply(update, "Usage: /del <batch|subject|unit|lecture> <id>")
    with SessionLocal() as db:
        row = db.get(models[context.args[0]], int(context.args[1]))
        if not row:
            return await _reply(update, "Not found.")
        db.delete(row)  # children are deleted too
        db.commit()
    await _reply(update, "Deleted.")


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


async def start():
    if not config.BOT_TOKEN:
        return None
    app = Application.builder().token(config.BOT_TOKEN).build()
    private = filters.ChatType.PRIVATE
    for name, fn in [
        (["start", "help"], help_cmd),
        (["addbatch"], addbatch),
        (["addsubject"], addsubject),
        (["addunit"], addunit),
        (["addlecture"], addlecture),
        (["setvideo"], setvideo),
        (["setpdf"], setpdf),
        (["tree"], tree),
        (["free"], free_cmd),
        (["del"], del_cmd),
    ]:
        app.add_handler(CommandHandler(name, fn, filters=private))
    if config.CHANNEL_ID:  # channel upload is optional
        app.add_handler(
            MessageHandler(filters.UpdateType.CHANNEL_POST & filters.Chat(config.CHANNEL_ID), on_channel_post)
        )
    await app.initialize()
    await app.start()
    await app.updater.start_polling(
        allowed_updates=["message", "channel_post"], drop_pending_updates=False
    )
    return app


async def stop(app):
    if app is None:
        return
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
