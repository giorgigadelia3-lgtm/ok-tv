# telegram_hotel_booking_bot.py
"""
Telegram HotelClaimBot
Flow:
 - Primary buttons: "მოძებნე. 🔍"  and "დაწყება / start. 🚀"
 - "მოძებნე. 🔍" asks for hotel name, checks Google Sheet:
     - if exists -> "კორპორაციისთვის შეთავაზება მიწოდებულია. ❌️" and end
     - if not exists -> "კორპორაცია თავისუფალია, გისურვებთ წარმატებებს. ✅️" and show Start button
 - "დაწყება / start. 🚀" starts form:
     - corporate name (if not already),
     - address,
     - comment,
     - agent name
 - At the end, saves a row to Google Sheet with: hotel, address, comment, agent, user, timestamp
Important env vars (on Render):
 - BOT_TOKEN
 - SPREADSHEET_ID
 - GOOGLE_APPLICATION_CREDENTIALS_JSON  (full JSON content of service account)
 - (optional) WEBHOOK_URL (https://<your-render-url>/<BOT_TOKEN>)
"""

import os
import json
import logging
from datetime import datetime

from flask import Flask, request, jsonify
import telebot
from telebot import types

import gspread
from google.oauth2.service_account import Credentials

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hotelclaimbot")

# ---------- Environment ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # optional: e.g. https://ok-tv-1.onrender.com/<BOT_TOKEN>

if not BOT_TOKEN:
    logger.error("Missing BOT_TOKEN environment variable.")
    raise SystemExit("BOT_TOKEN environment variable is required.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ---------- Google Sheets connection ----------
sheet = None
try:
    if not GOOGLE_CREDS_JSON:
        raise Exception("GOOGLE_APPLICATION_CREDENTIALS_JSON is missing in environment.")

    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)

    if not SPREADSHEET_ID:
        raise Exception("SPREADSHEET_ID environment variable is missing.")

    sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
    logger.info("Connected to Google Sheets successfully.")
except Exception as e:
    sheet = None
    logger.warning(f"Google Sheets not available: {e}")

# ---------- In-memory state for chat flows ----------
# Structure: user_states[chat_id] = {"hotel_name":..., "address":..., "comment":..., "agent":...}
user_states = {}

# ---------- Helper functions ----------
def check_hotel_exists(hotel_name: str) -> bool:
    """Return True if hotel_name exists in column A (case-insensitive)."""
    if not sheet:
        logger.warning("check_hotel_exists: sheet is not available.")
        return False
    try:
        colA = sheet.col_values(1)  # read column A (hotel names)
        target = hotel_name.strip().lower()
        for v in colA:
            if v and v.strip().lower() == target:
                return True
        return False
    except Exception as e:
        logger.error(f"Error checking hotel existence: {e}")
        return False

def save_to_sheet(hotel, address, comment, agent, user_fullname):
    """Append a row to the Google Sheet. Returns True on success."""
    if not sheet:
        logger.error("save_to_sheet: sheet is not available.")
        return False
    try:
        row = [
            hotel,
            address,
            comment,
            agent,
            user_fullname,
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        ]
        sheet.append_row(row)
        logger.info(f"Appended row to sheet: {row}")
        return True
    except Exception as e:
        logger.exception(f"Failed to append row to sheet: {e}")
        return False

# ---------- Keyboards ----------
def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False)
    kb.row(types.KeyboardButton("მოძებნე. 🔍"), types.KeyboardButton("დაწყება / start. 🚀"))
    return kb

def start_only_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row(types.KeyboardButton("დაწყება / start. 🚀"))
    return kb

# ---------- Handlers ----------
@bot.message_handler(commands=["start"])
def cmd_start(m):
    chat_id = m.chat.id
    user_states.pop(chat_id, None)
    bot.send_message(chat_id, "👋 სალამი! დააჭირე 'მოძებნე. 🔍' ძიებისთვის ან 'დაწყება / start. 🚀' ჩასაწერად.", reply_markup=main_keyboard())

@bot.message_handler(func=lambda msg: msg.text and msg.text.strip().lower() == "მოძებნე. 🔍")
def handle_search(msg):
    chat_id = msg.chat.id
    user_states.pop(chat_id, None)
    sent = bot.send_message(chat_id, "გთხოვთ ჩაწეროთ სასტუმროს ან კორპორაციის სახელი, რომლის სტატუსს გსურთ შემოწმება:")
    bot.register_next_step_handler(sent, process_search_input)

def process_search_input(msg):
    chat_id = msg.chat.id
    hotel_name = (msg.text or "").strip()
    if not hotel_name:
        s = bot.send_message(chat_id, "სახელი ცარიელია. გთხოვთ ჩაწეროთ სასტუმროს/კორპორაციის სახელი:")
        bot.register_next_step_handler(s, process_search_input)
        return

    exists = check_hotel_exists(hotel_name)
    if exists:
        bot.send_message(chat_id, "კორპორაციისთვის შეთავაზება მიწოდებულია. ❌️", reply_markup=types.ReplyKeyboardRemove())
        user_states.pop(chat_id, None)
        return
    else:
        user_states[chat_id] = {"hotel_name": hotel_name}
        bot.send_message(chat_id, "კორპორაცია თავისუფალია, გისურვებთ წარმატებებს. ✅️")
        # show start button to continue filling
        bot.send_message(chat_id, "თუ გსურთ ახლა შეავსოთ დამატებითი ინფორმაცია, დააჭირეთ \"დაწყება / start. 🚀\"", reply_markup=start_only_keyboard())

@bot.message_handler(func=lambda msg: msg.text and msg.text.strip().lower() == "დაწყება / start. 🚀")
def handle_start_fill(msg):
    chat_id = msg.chat.id
    state = user_states.get(chat_id, {})
    # If we already have hotel_name from search -> move to address prompt
    if "hotel_name" in state and state["hotel_name"]:
        s = bot.send_message(chat_id, "მისამართი. 📍")
        bot.register_next_step_handler(s, ask_comment)
        return
    # otherwise ask for hotel name first
    s = bot.send_message(chat_id, "კორპორაციის დასახელება. 🏢")
    bot.register_next_step_handler(s, ask_address)

def ask_address(msg):
    chat_id = msg.chat.id
    hotel = (msg.text or "").strip()
    if not hotel:
        s = bot.send_message(chat_id, "სახელი ცარიელია. გთხოვთ ჩაწეროთ კორპორაციის დასახელება:")
        bot.register_next_step_handler(s, ask_address)
        return
    user_states.setdefault(chat_id, {})["hotel_name"] = hotel
    s = bot.send_message(chat_id, "მისამართი. 📍")
    bot.register_next_step_handler(s, ask_comment)

def ask_comment(msg):
    chat_id = msg.chat.id
    address = (msg.text or "").strip()
    user_states.setdefault(chat_id, {})["address"] = address
    s = bot.send_message(chat_id, "კომენტარი. 📩")
    bot.register_next_step_handler(s, ask_agent)

def ask_agent(msg):
    chat_id = msg.chat.id
    comment = (msg.text or "").strip()
    user_states.setdefault(chat_id, {})["comment"] = comment
    s = bot.send_message(chat_id, "აგენტის სახელი და გვარი. 👩‍💻")
    bot.register_next_step_handler(s, finish_and_store)

def finish_and_store(msg):
    chat_id = msg.chat.id
    agent = (msg.text or "").strip()
    state = user_states.get(chat_id, {})
    state["agent"] = agent

    hotel = state.get("hotel_name", "").strip()
    address = state.get("address", "").strip()
    comment = state.get("comment", "").strip()
    agent_name = state.get("agent", "").strip()
    user_full = f"{msg.from_user.first_name or ''} {msg.from_user.last_name or ''}".strip()

    if not hotel:
        s = bot.send_message(chat_id, "კორპორაციის სახელი არ არის მითითებული. გთხოვთ ჩაწეროთ კორპორაციის დასახელება:")
        bot.register_next_step_handler(s, ask_address)
        return

    ok = save_to_sheet(hotel, address, comment, agent_name, user_full)
    if ok:
        bot.send_message(chat_id, "OK TV გისურვებთ წარმატებულ დღეს. 🥰", reply_markup=types.ReplyKeyboardRemove())
    else:
        bot.send_message(chat_id, "შეცდომა მონაცემების შენახვისას. გთხოვთ მიმართოთ ადმინისტრატორს.", reply_markup=types.ReplyKeyboardRemove())

    user_states.pop(chat_id, None)

# ---------- Webhook and Flask endpoints ----------
@app.route("/", methods=["GET"])
def index():
    return "HotelClaimBot running."

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = request.get_json(force=True)
        if update:
            bot.process_new_updates([telebot.types.Update.de_json(update)])
    except Exception as e:
        logger.exception(f"Webhook processing failed: {e}")
    return jsonify({"ok": True})

def set_webhook():
    if not WEBHOOK_URL:
        logger.info("WEBHOOK_URL is not set; skipping webhook registration.")
        return
    try:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL)
        logger.info(f"Webhook set to {WEBHOOK_URL}")
    except Exception as e:
        logger.exception(f"Failed to set webhook: {e}")

if __name__ == "__main__":
    set_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
