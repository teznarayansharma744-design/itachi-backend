"""Reads files from the private Telegram channel with a Telethon user session.
A user session has no 20 MB limit (unlike the Bot API), so big lectures work."""
import time

from telethon import TelegramClient
from telethon.sessions import StringSession

from . import config

CHUNK = 1024 * 1024  # 1 MB, the size Telegram serves per request

client: TelegramClient | None = None
_cache: dict[int, tuple[float, object]] = {}


async def start() -> None:
    global client
    client = TelegramClient(StringSession(config.SESSION_STRING), config.API_ID, config.API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("SESSION_STRING is not logged in. Run gen_session.py again.")
    try:
        await client.get_entity(config.CHANNEL_ID)
    except Exception:
        await client.get_dialogs()  # fills Telethon's channel cache


async def stop() -> None:
    if client:
        await client.disconnect()


async def get_media_message(msg_id: int):
    hit = _cache.get(msg_id)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    msg = await client.get_messages(config.CHANNEL_ID, ids=msg_id)
    if not msg or not msg.media:
        return None
    if len(_cache) > 200:
        _cache.clear()
    _cache[msg_id] = (time.time(), msg)
    return msg


async def iter_range(msg, start: int, end: int):
    """Yield bytes start..end (inclusive) of the file in the message."""
    aligned = start - (start % CHUNK)
    parts = (end - aligned) // CHUNK + 1
    skip = start - aligned
    remaining = end - start + 1
    async for chunk in client.iter_download(
        msg.media,
        offset=aligned,
        limit=parts,
        request_size=CHUNK,
        file_size=msg.file.size,
    ):
        if skip:
            chunk = chunk[skip:]
            skip = 0
        if len(chunk) > remaining:
            chunk = chunk[:remaining]
        remaining -= len(chunk)
        yield chunk
        if remaining <= 0:
            break


async def thumbnail_bytes(msg):
    return await client.download_media(msg, file=bytes, thumb=-1)
