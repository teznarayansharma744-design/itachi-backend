"""Run this ONCE on your own phone or PC (not on Render).
It logs in to Telegram and prints a SESSION_STRING for the backend.

    pip install telethon
    python gen_session.py

Treat the printed string like a password: anyone with it can use that account.
Best practice: use a separate Telegram account that is only a member of your
storage channel, not your main account.
"""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\nYour SESSION_STRING (copy everything on the next line):\n")
    print(client.session.save())
