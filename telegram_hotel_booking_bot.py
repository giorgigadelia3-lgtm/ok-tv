import os
import json
import logging
import threading
import asyncio
from typing import Dict, Any, List

from flask import Flask, request, jsonify, Response

from fuzzywuzzy import fuzz
import gspread
from oauth2client.service_account import ServiceAccountCredentials

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
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
# ENVIRONMENT
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")

if not TELEGRAM_TOKEN or not APP_BASE_URL:
    raise RuntimeError("Missing TELEGRAM_TOKEN or APP_BASE_URL environment variables")

# =========================
# FLASK APP
# =========================
app = Flask(__name__)

# =========================
# BOT APPLICATION (global)
# =========================
application: Application = None
loop = None
_app_ready = threading.Event()


# =========================
# BOT HELPERS
# =========================
def main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔎 მოძებნა"), KeyboardButton("▶️ Start")],
            [KeyboardButton("ℹ️ დახმარება")],
        ],
        resize_keyboard=True,
    )


# =========================
# BOT HANDLERS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "მოგესალმებით! 👋 ეს არის OK TV Hotel Bot — შეგიძლიათ დაჯავშნოთ სასტუმრო, გადაამოწმოთ ინფორმაცია ან დაუკავშირდეთ ოპერატორს.",
        reply_markup=main_menu(),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *დახმარება*\n\n"
        "🔎 მოძებნა — მოძებნე არსებული ობიექტი.\n"
        "▶️ Start — ახალი ობიექტის დამატება.\n"
        "OK TV Hotel Bot მზად არის დაგეხმაროს 💬",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu(),
    )


async def fallback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "აირჩიეთ მოქმედება მენიუდან 👇", reply_markup=main_menu()
    )


# =========================
# ASYNC BOT SETUP
# =========================
async def build_and_start_bot():
    global application
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.Regex("^ℹ️ დახმარება$"), help_cmd))
    application.add_handler(MessageHandler(filters.ALL, fallback_router))

    await application.initialize()
    await application.start()
    log.info("✅ Telegram bot started successfully")
    _app_ready.set()


def start_background_loop():
    global loop
    loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(build_and_start_bot())

    threading.Thread(target=_run, daemon=True).start()
    _app_ready.wait()


# =========================
# FLASK ROUTES
# =========================
@app.get("/")
def health():
    return Response("OK", status=200)


@app.post(f"/webhook/{TELEGRAM_TOKEN}")
def telegram_webhook():
    global application
    if application is None:
        return jsonify(error="Bot not initialized"), 500

    update_json = request.get_json(force=True, silent=True)
    if not update_json:
        return jsonify(ok=False)

    update = Update.de_json(update_json, application.bot)

    async def process_update():
        await application.process_update(update)

    asyncio.run_coroutine_threadsafe(process_update(), loop)
    return jsonify(ok=True)


# =========================
# STARTUP
# =========================
if __name__ == "__main__":
    start_background_loop()
    with app.app_context():
        try:
            async def _do():
                await application.bot.set_webhook(
                    url=f"{APP_BASE_URL}/webhook/{TELEGRAM_TOKEN}",
                    drop_pending_updates=True,
                )

            fut = asyncio.run_coroutine_threadsafe(_do(), loop)
            fut.result(timeout=15)
            log.info("✅ Webhook set successfully.")
        except Exception as e:
            log.warning("⚠️ Webhook set failed: %s", e)

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
