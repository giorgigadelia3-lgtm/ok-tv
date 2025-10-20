# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-
import os
import json
import sqlite3
import time
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from difflib import SequenceMatcher, get_close_matches
import gspread
from google.oauth2.service_account import Credentials

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("❌ Please set BOT_TOKEN environment variable")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_PATH = os.path.join(os.getcwd(), "data.db")

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")

# ---------------- GOOGLE SHEETS ----------------
sheet = None
if GOOGLE_CREDS_JSON and SPREADSHEET_ID:
    try:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
        print("✅ Google Sheets connected.")
    except Exception as e:
        print("⚠️ Google Sheets connection failed:", e)
else:
    print("⚠️ Google Sheets configuration missing.")

app = Flask(__name__)

# ---------------- HELPERS ----------------
def normalize(text: str) -> str:
    if not text:
        return ""
    text = text.strip().lower()
    text = text.replace("ქ.", "").replace("ქალაქი", "").replace("სასტუმრო", "").strip()
    return " ".join(text.split())

def similarity(a, b):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()

def get_sheet_records():
    if not sheet:
        return []
    try:
        records = sheet.get_all_records()
        hotels = []
        for r in records:
            hotels.append({
                "name": str(r.get("hotel name", "")).strip(),
                "address": str(r.get("address", "")).strip(),
                "comment": str(r.get("comment", "")).strip(),
                "contact": str(r.get("Contact", "")).strip(),
                "agent": str(r.get("agent", "")).strip(),
                "date": str(r.get("date", "")).strip()
            })
        return hotels
    except Exception as e:
        print("⚠️ get_sheet_records error:", e)
        return []

def find_best_match(name_input, address_input, hotels):
    """Finds the most similar hotel by name/address combination"""
    if not hotels:
        return None, None

    name_input_norm = normalize(name_input)
    address_input_norm = normalize(address_input)

    best_match = None
    highest_score = 0.0

    for h in hotels:
        n_score = similarity(name_input_norm, h["name"])
        a_score = similarity(address_input_norm, h["address"])
        avg_score = (n_score + a_score) / 2

        if avg_score > highest_score:
            highest_score = avg_score
            best_match = h

    return best_match, highest_score

# ---------------- TELEGRAM HELPERS ----------------
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{API_URL}/sendMessage", json=payload, timeout=15)
    except Exception as e:
        print("⚠️ Telegram send error:", e)

def keyboard_main():
    return {"keyboard": [[{"text": "მოძებნე. 🔍"}], [{"text": "დაწყება / start. 🚀"}]], "resize_keyboard": True}

# ---------------- DB ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS pending (
        chat_id INTEGER PRIMARY KEY,
        state TEXT,
        temp_name TEXT,
        temp_address TEXT
    )''')
    conn.commit()
    conn.close()

def db_execute(query, params=(), fetch=False):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(query, params)
    data = cur.fetchall() if fetch else None
    conn.commit()
    conn.close()
    return data

def set_pending(chat_id, state, temp_name=None, temp_address=None):
    db_execute("REPLACE INTO pending (chat_id, state, temp_name, temp_address) VALUES (?, ?, ?, ?)",
               (chat_id, state, temp_name, temp_address))

def get_pending(chat_id):
    rows = db_execute("SELECT state, temp_name, temp_address FROM pending WHERE chat_id=?", (chat_id,), fetch=True)
    return rows[0] if rows else (None, None, None)

def clear_pending(chat_id):
    db_execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))

init_db()

# ---------------- WEBHOOK ----------------
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json(force=True)
    if "message" not in update:
        return jsonify(ok=True)

    msg = update["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()

    if not text:
        return jsonify(ok=True)

    # Start search flow
    if text in ("მოძებნე. 🔍", "მოძებნე", "🔍 მოძებნე"):
        set_pending(chat_id, "awaiting_name")
        send_message(chat_id, "გთხოვთ შეიყვანოთ სასტუმროს <b>დასახელება</b>.", reply_markup=keyboard_main())
        return jsonify(ok=True)

    state, temp_name, temp_address = get_pending(chat_id)

    if state == "awaiting_name":
        set_pending(chat_id, "awaiting_address", temp_name=text)
        send_message(chat_id, "შეიყვანეთ სასტუმროს <b>ოფიციალური მისამართი</b> ზუსტი იდენტიფიკაციისთვის.", reply_markup=keyboard_main())
        return jsonify(ok=True)

    if state == "awaiting_address":
        hotel_name = temp_name
        hotel_address = text
        records = get_sheet_records()

        if not records:
            send_message(chat_id, "⚠️ ვერ მოხერხდა ბაზასთან დაკავშირება. სცადეთ მოგვიანებით.")
            clear_pending(chat_id)
            return jsonify(ok=True)

        # იძებნება საუკეთესო დამთხვევა
        best_match, score = find_best_match(hotel_name, hotel_address, records)

        if score >= 0.8:
            comment = best_match["comment"] or "კომენტარი არ არის მითითებული."
            send_message(chat_id,
                         f"❌ <b>ეს სასტუმრო უკვე გამოკითხულია!</b>\n\n🏨 <b>{best_match['name']}</b>\n📍 {best_match['address']}\n💬 კომენტარი: <i>{comment}</i>",
                         reply_markup=keyboard_main())
        elif score >= 0.5:
            send_message(chat_id,
                         f"🔎 <b>მსგავსი ჩანაწერი მოიძებნა:</b>\n🏨 <b>{best_match['name']}</b>\n📍 {best_match['address']}\n💬 კომენტარი: <i>{best_match['comment'] or 'არ არის'}</i>",
                         reply_markup=keyboard_main())
        else:
            send_message(chat_id,
                         "✅ ეს სასტუმრო ჩვენს ბაზაში არ მოიძებნა. შეგიძლიათ დაიწყოთ რეგისტრაცია ღილაკით 'დაწყება / start. 🚀'",
                         reply_markup=keyboard_main())

        clear_pending(chat_id)
        return jsonify(ok=True)

    return jsonify(ok=True)

@app.route('/')
def index():
    return "Hotel Bot is running ✅"

if __name__ == "__main__":
    webhook_host = os.environ.get("WEBHOOK_HOST", "https://ok-tv-1.onrender.com")
    webhook_url = f"{webhook_host.rstrip('/')}/{BOT_TOKEN}"
    print(f"Setting webhook to: {webhook_url}")
    try:
        requests.get(f"{API_URL}/setWebhook?url={webhook_url}", timeout=10)
    except Exception as e:
        print("Webhook error:", e)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
