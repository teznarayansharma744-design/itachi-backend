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

# ---- Paid keys sold through a Telegram shop bot (all optional) ----
# A SEPARATE bot made in @BotFather. It runs on the server through a webhook, so it
# works while your phone is off. Do not reuse the lecture bot token.
SHOP_BOT_TOKEN = os.environ.get("SHOP_BOT_TOKEN", "")
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
# Your server address, e.g. https://itachi-backend.onrender.com (no slash at the end)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
# Plans as name:days:price_in_rupees, separated by commas.
PLANS = os.environ.get("PLANS", "1 Month:30:299,6 Months:180:1499,1 Year:365:2499")
SUPPORT_USERNAME = os.environ.get("SUPPORT_USERNAME", "Use_r_tg")
