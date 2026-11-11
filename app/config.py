import os

try:  # optional: lets you keep settings in a .env file when running locally
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

API_ID = int(os.environ.get("API_ID") or 0)
API_HASH = os.environ.get("API_HASH", "")
SESSION_STRING = os.environ.get("SESSION_STRING", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = int(os.environ.get("CHANNEL_ID") or 0)
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}
SECRET_KEY = os.environ.get("SECRET_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL") or "sqlite:///./local.db"

# Password for the admin panel inside the app. Use something long.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# How long an app login token stays valid. The key's own expiry is checked too.
TOKEN_DAYS = 30
ADMIN_TOKEN_DAYS = 7
