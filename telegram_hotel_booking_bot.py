import os
import json
import logging
import threading
import asyncio
from typing import Dict, Any, List, Tuple

from flask import Flask, request, jsonify, Response

from fuzzywuzzy import fuzz
import gspread
from oauth2client.service_account import ServiceAccountCredentials

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
)

# =========================
# LOGGING
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("hotel_bot")

# =========================
# ENV VARIABLES
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
GOOGLE_SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN env")
if not APP_BASE_URL:
    raise RuntimeError("Missing APP_BASE_URL env")
if not SPREADSHEET_ID:
    raise RuntimeError("Missing SPREADSHEET_ID env")
if not GOOGLE_SA_JSON:
    raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON env")

# =========================
# FLASK APP
# =========================
app = Flask(__name__)

# =========================
# GOOGLE SHEETS
# =========================
SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]


def _sa_client():
    data = json.loads(GOOGLE_SA_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(data, scopes=SCOPE)
    gc = gspread.authorize(creds)
    return gc


def open_sheet():
    gc = _sa_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.sheet1


def read_all_hotels() -> List[Dict[str, Any]]:
    ws = open_sheet()
    values = ws.get_all_records()
    normalized = []
    for row in values:
        normalized.append({
            "name": str(row.get("Hotel Name", "")).strip(),
            "address": str(row.get("Address", "")).strip(),
            "status": str(row.get("Status", "")).strip(),
            "comment": str(row.get("Comment", "")).strip(),
        })
    return normalized


def append_new_row(payload: Dict[str, Any]) -> None:
    ws = open_sheet()
    ws.append_row(
        [
            payload.get("name", ""),
            payload.get("address", ""),
            payload.get("status", "NEW"),
            payload.get("comment", ""),
            payload.get("contact_name", ""),
            payload.get("contact_phone", ""),
            payload.get("notes", ""),
        ],
        value_input_option="USER_ENTERED",
    )


# =========================
# HELPERS
# =========================
def normalize(s: str) -> str:
    return " ".join(s.lower().strip().split())


def best_matches(hotels: List[Dict[str, Any]], name: str, address: str, limit: int = 5):
    res = []
    for h in hotels:
        nscore = fuzz.token_set_ratio(normalize(name), normalize(h["name"]))
        ascore = fuzz.token_set_ratio(normalize(address), normalize(h["address"]))
        score = (nscore + ascore) // 2
        res.append((h, score))
    res.sort(key=lambda x: x[1], reverse=True)
    return res[:limit]


def is_strong_match(score: int) -> bool:
    return score >= 90


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔎 მოძებნა"), KeyboardButton("▶️ Start")],
            [KeyboardButton("ℹ️ დახმარება")],
        ],
        resize_keyboard=True,
    )


# =========================
# CONVERSATION STATES
# =========================
S_NAME, S_ADDR, S_CONFIRM = range(3)
N_NAME, N_ADDR, N_CONTACT, N_PHONE, N_NOTES, N_CONFIRM = range(6)

# =========================
# BOT LOGIC
# =========================
application: Application
loop: asyncio.AbstractEventLoop
_app_ready = threading.Event()


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "მოგესალმებით! 👋 ეს არის OK TV Hotel Bot — აირჩიეთ მოქმედება 👇",
        reply_markup=main_menu(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔎 *მოძებნა* — მოძებნე არსებული სასტუმრო Sheet-ში.\n"
        "▶️ *Start* — ახალი ობიექტის დამატება.\n"
        "ℹ️ *დახმარება* — მოკლე ინსტრუქცია.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu(),
    )


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ბრძანება ვერ გავიგე. აირჩიე მენიუდან ⬇️", reply_markup=main_menu())


async def fallback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("აირჩიე მოქმედება ⬇️", reply_markup=main_menu())


async def _build_and_start_application():
    global application
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.Regex("^ℹ️ დახმარება$"), help_cmd))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))
    application.add_handler(MessageHandler(filters.ALL, fallback_router))

    await application.initialize()
    await application.start()
    log.info("✅ Telegram bot started successfully")
    _app_ready.set()


def start_background_loop():
    global loop
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(_build_and_start_application()), daemon=True).start()
    _app_ready.wait()


# =========================
# FLASK ROUTES
# =========================
@app.get("/")
def health() -> Response:
    return Response("OK", status=200)


@app.post(f"/webhook/{TELEGRAM_TOKEN}")
def telegram_webhook():
    update_json = request.get_json(force=True, silent=True)
    if not update_json:
        return jsonify(ok=False)
    update = Update.de_json(update_json, application.bot)

    async def _process():
        await application.process_update(update)

    asyncio.run_coroutine_threadsafe(_process(), loop)
    return jsonify(ok=True)


# =========================
# START
# =========================
if __name__ == "__main__":
    start_background_loop()
    with app.app_context():
        try:
            url = f"{APP_BASE_URL}/webhook/{TELEGRAM_TOKEN}"
            async def _do():
                await application.bot.set_webhook(url=url, drop_pending_updates=True)
            fut = asyncio.run_coroutine_threadsafe(_do(), loop)
            fut.result(timeout=15)
            log.info("Webhook set (masked): %s/*** -> True", APP_BASE_URL)
        except Exception as e:
            log.warning("Webhook set failed initially: %s", e)

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
