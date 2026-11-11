"""Run the admin bot on your own phone or PC.

    pip install -r requirements-bot.txt
    python run_bot.py

This bot only adds content (batches, units, lectures). It never talks to the
server. The only thing they share is the database, so use the SAME DATABASE_URL
as the server. Needs BOT_TOKEN, ADMIN_IDS, CHANNEL_ID and DATABASE_URL in a
.env file or in the environment.
"""
import asyncio

from app import bot, config
from app.db import init_db


async def main():
    if not config.BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN first.")
    init_db()
    app = await bot.start()
    print("Bot is running. Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()
    finally:
        await bot.stop(app)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
