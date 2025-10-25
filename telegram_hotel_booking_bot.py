# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-
import os
import json
import time
import requests
from flask import Flask, request, jsonify
import gspread
from google.oauth2.service_account import Credentials

# ================ 1. ENVIRONMENT VARIABLES =====================
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")  # BotFather token
APP_BASE_URL = os.environ.get("APP_BASE_URL")  # https://ok-tv-1.onrender.com
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

if not BOT_TOKEN or not APP_BASE_URL:
    raise RuntimeError("❌ Please set TELEGRAM_TOKEN and APP_BASE_URL in Render > Environment")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ================ 2. GOOGLE SHEETS CONNECTION =====================
sheet = None
try:
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SPREADSHEET_ID).worksheet("Hotels")  # <-- შენი Sheet-ის ტაბი
    print("✅ Connected to Google Sheets.")
except Exception as e:
    print("⚠️ Google Sheets connection failed:", e)

# ================= 3. FLASK APP ====================
app = Flask(__name__)

# ================= 4. HELPERS ======================
def send_message(chat_id, text, keyboard=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)
    try:
        requests.post(f"{API_URL}/sendMessage", json=data)
    except:
        pass

def keyboard_main():
    return {
        "keyboard": [
            [{"text": "🔍 მოძებნა"}]
        ],
        "resize_keyboard": True
    }

def normalize(text):
    return text.strip().lower().replace(" ", "") if text else ""

# ================= 5. SIMPLE STATE STORAGE IN MEMORY ====================
user_state = {}  # chat_id: {"step": ..., "name": ..., "address": ...}

# ================= 6. TELEGRAM WEBHOOK HANDLER ====================
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json()
    msg = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "")

    if not chat_id or not text:
        return jsonify({"ok": True})

    state = user_state.get(chat_id, {"step": None})

    # ================= START COMMAND =================
    if text == "/start":
        send_message(chat_id, "გამარჯობა! აირჩიე მოქმედება 👇", keyboard=keyboard_main())
        user_state[chat_id] = {"step": None}
        return jsonify({"ok": True})

    # ================= SEARCH FLOW =================
    if text == "🔍 მოძებნა":
        send_message(chat_id, "📌 ჩაწერე სასტუმროს <b>სახელი ინგლისურად</b>:")
        user_state[chat_id] = {"step": "ask_name"}
        return jsonify({"ok": True})

    if state["step"] == "ask_name":
        user_state[chat_id]["name"] = text
        user_state[chat_id]["step"] = "ask_address"
        send_message(chat_id, "📍 ახლა ჩაწერე <b>მისამართი ქართულად</b>:")
        return jsonify({"ok": True})

    if state["step"] == "ask_address":
        user_state[chat_id]["address"] = text
        name = normalize(user_state[chat_id]["name"])
        address = normalize(text)

        hotel_found = False
        if sheet:
            data = sheet.get_all_records()
            for row in data:
                sheet_name = normalize(row.get("name_en", ""))
                sheet_addr = normalize(row.get("address_ka", ""))
                if sheet_name == name and sheet_addr == address:
                    hotel_found = True
                    status = row.get("status", "სტატუსი უცნობია")
                    comm = row.get("comment", "—")
                    send_message(chat_id,
                        f"❗ ეს სასტუმრო უკვე არსებობს ბაზაში.\n"
                        f"სტატუსი: <b>{status}</b>\n"
                        f"კომენტარი: <i>{comm}</i>")
                    break

        if not hotel_found:
            send_message(chat_id,
                "✅ ბაზაში ეს სასტუმრო <b>არ არსებობს</b>.\n"
                "თუ გინდა კითხვარის გაგრძელება — დაწერე: <b>სტარტი</b>")
        user_state[chat_id]["step"] = None
        return jsonify({"ok": True})

    send_message(chat_id, "აირჩიე მენიუდან 👇", keyboard=keyboard_main())
    return jsonify({"ok": True})

# ================= 7. WEBHOOK SETUP ====================
def set_webhook():
    url = f"{APP_BASE_URL}/{BOT_TOKEN}"
    requests.get(f"{API_URL}/setWebhook?url={url}")
    print("✅ Webhook set to:", url)

set_webhook()

@app.route("/")
def index():
    return "HotelClaimBot is running."

# ================= 8. APP RUN ====================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
