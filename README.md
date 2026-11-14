# Itachi Universe backend

Two separate programs that share one database:
- **Server** (FastAPI): key check, catalog, tells the app where each video/PDF is, admin API.
- **Bot** (`run_bot.py`): only adds lectures. It has no link to the server.

Videos and PDFs are **direct https links** that you give to the bot. The app opens
them itself, so the server carries almost no video traffic (that keeps free hosting
bandwidth small). The server only checks the student's key before it hands out a link.

Keys and students are managed from the **admin panel inside the app**.

## What the server does
- `POST /activate` checks a key against the device id and returns a login token.
- `GET /catalog` lists batches, subjects, units, lectures. Locked items are still listed.
- `GET /play/{lecture_id}/video|pdf` returns the link, but only for free lectures or
  with a valid premium token.
- `/admin/...` is used by the admin panel in the app.
- Optional: files kept in a private Telegram channel can still be streamed by the
  server (`/media/...`). Skip this unless you need it.

## Where things run
| Part | Where |
| --- | --- |
| Server | Render (free, no card) or any server you have |
| Database | Neon or Supabase (free Postgres), shared by server and bot |
| Bot (`run_bot.py`) | Termux on your phone, only while adding lectures |
| Admin panel | Inside the app (tap the logo 7 times on the first screen) |

## Setup
1. **Database:** make a free Postgres on Neon or Supabase. Copy its connection string.
2. **Bot:** make a bot in @BotFather. Copy the token. Send /start to @userinfobot to get
   your user id for `ADMIN_IDS`.
3. **Server (Render):** new Web Service from this repo.
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - Variables: `SECRET_KEY` (long random text), `ADMIN_PASSWORD` (long),
     `DATABASE_URL`, `PYTHON_VERSION` = `3.12.7`.
   - The server needs no Telegram settings.
4. **Keep it awake:** free UptimeRobot monitor on `https://YOUR-APP.onrender.com/health`
   every 5 minutes.
5. **App:** put your Render address in `lib/api.dart` (`defaultBaseUrl`), build the APK.
   To use the admin panel: tap the logo 7 times, enter the address and `ADMIN_PASSWORD`.

## Run the bot (Termux)
```
pip install -r requirements-bot.txt
```
`.env` next to `run_bot.py`:
```
BOT_TOKEN=...
ADMIN_IDS=your_telegram_user_id
DATABASE_URL=...same as the server...
```
Then `python run_bot.py`. Run it only while adding lectures, stop with Ctrl+C.

## Adding content
```
/addbatch Class 12 Science        -> id 1
/addsubject 1 Physics             -> id 1
/addunit 1 Laws of Motion         -> id 1

/addlecture 1 | Lecture 1: Newton | https://example.com/v1.mp4
/addlecture 1 | Lecture 2 | https://example.com/v2.m3u8 | https://example.com/n2.pdf
/addlecture 1 | Formula Sheet | - | https://example.com/f.pdf
/addlecture 1 | Free Intro | https://example.com/i.mp4 | free
```
- Links must start with `https://` (plain `http://` is blocked on phones).
- Same unit + same title updates that lecture, so you can add the PDF later.
- `/setvideo <id> <link>`, `/setpdf <id> <link>` change a link (`-` removes it).
- `/free <id> on|off`, `/tree` (see all ids), `/del lecture 5`.

### About the links
- `.mp4` and `.m3u8` (HLS) links play in the app.
- A link has to stay valid. Links that expire after a few hours will stop working.
- A student who is allowed to watch receives the link, so a determined student could
  copy it. Use links you are happy to hand to paying students.

## Notes
- Render free sleeps after 15 min idle, so the first request can take a minute.
- Do not share `BOT_TOKEN`, `SECRET_KEY` or `ADMIN_PASSWORD`.
- Admin login allows 5 tries per minute per address. Admin sessions last 7 days.
- Tests: `pip install pytest` then `pytest tests`.

## Optional: Telegram channel storage
Set `API_ID`, `API_HASH`, `SESSION_STRING` (from `gen_session.py`) and `CHANNEL_ID` on the
server, and `CHANNEL_ID` for the bot. Then post a video or PDF in the channel with the
caption `unit_id | title | free` and the bot links it. This path streams through your
server, so it uses hosting bandwidth.

## Sell keys through a Telegram shop bot (auto payment)

The buyer picks a plan in Telegram, pays on a Razorpay page, and the server creates the key
and sends it to them. It runs on Render through webhooks, so your phone can stay off.

1. **New bot:** make a second bot in @BotFather (not the lecture bot). Copy its token.
2. **Razorpay:** create an account, then Settings > API Keys > generate keys (use Test mode first).
3. **Webhook:** Razorpay dashboard > Settings > Webhooks > Add. URL
   `https://YOUR-APP.onrender.com/shop/razorpay`, pick a secret, tick only **payment_link.paid**.
4. **Render environment variables:**
   `SHOP_BOT_TOKEN`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`,
   `PUBLIC_URL=https://YOUR-APP.onrender.com`, and optionally
   `PLANS=Lifetime:36500:100` (name:days:rupees, default is one lifetime plan for 100 rupees),
   `SUPPORT_USERNAME=Use_r_tg`.
5. Redeploy. On start the server registers the Telegram webhook itself.
6. Test: send /start to the shop bot, pay with a Razorpay test UPI/card, the key arrives.

Notes: the bot asks for the buyer's Device ID first and the key is locked to it (the admin panel's
"Change device" still works). Keep UptimeRobot pinging
`/health` so Render is awake when a payment comes in. Razorpay retries a missed webhook, and
the "I have paid, check now" button asks Razorpay directly as a backup.

## YouTube links in external players

The default in-app player continues to receive the original YouTube URL and uses the
existing YouTube player view. For MX Player, PLAYit and VLC, `/play/{lecture_id}/external`
continues to issue the same short-lived signed app link. When that link is opened,
`/s/{token}` checks the lecture and premium-key status, then resolves a YouTube URL to
a single progressive audio+video stream using `yt-dlp` and redirects the external player
to that stream. Direct MP4/M3U8 links still use the existing redirect path.

`yt-dlp` is added to `requirements.txt`; redeploy the backend so the dependency is
installed. Some videos (private, age/region restricted, sign-in required, live, or without
a compatible progressive format) may not work in external players. For those videos,
use the Default Player. Only use external extraction for videos you own or are authorized
to distribute, and follow the platform's terms and applicable copyright rules.
