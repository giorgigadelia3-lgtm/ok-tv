# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-
import os
import json
import sqlite3
import time
import difflib
import requests
from datetime import datetime
from flask import Flask, request, jsonify

# Google Sheets libraries
import gspread
from google.oauth2.service_account import Credentials

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Please set BOT_TOKEN environment variable")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_PATH = os.path.join(os.getcwd(), "data.db")

# Google Sheets envs
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")

# ---------------- Google Sheets connection ----------------
sheet = None
if GOOGLE_CREDS_JSON and SPREADSHEET_ID:
    try:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
        print("✅ Google Sheets connected.")
    except Exception as e:
        sheet = None
        print("⚠️ Google Sheets auth failed:", e)
else:
    print("⚠️ Google Sheets environment not fully configured.")

app = Flask(__name__)

# ---------------- DATABASE (with retry protection) ----------------
def get_connection():
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)

def db_execute(query, params=(), fetch=False, retries=5):
    """Safe SQLite executor with retry in case of lock."""
    for attempt in range(retries):
        try:
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(query, params)
            data = cur.fetchall() if fetch else None
            conn.commit()
            conn.close()
            return data
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower():
                print(f"⚠️ DB locked, retrying ({attempt + 1}/{retries})...")
                time.sleep(1)
                continue
            else:
                raise
    print("❌ DB permanently locked, failed to execute query.")
    return None

def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS pending (
        chat_id INTEGER PRIMARY KEY,
        state TEXT,
        temp_name TEXT,
        temp_address TEXT,
        temp_comment TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

# ---------------- HELPERS ----------------
def normalize(s: str) -> str:
    return " ".join(s.strip().lower().split()) if s else ""

def find_hotel_in_sheet(name, address):
    """Search hotel in Google Sheets by name or address (approximate match)."""
    if not sheet:
        return None

    try:
        data = sheet.get_all_records()
        norm_name, norm_addr = normalize(name), normalize(address)
        for row in data:
            sheet_name = normalize(row.get("hotel name", ""))
            sheet_addr = normalize(row.get("address", ""))
            if sheet_name == norm_name or sheet_addr == norm_addr:
                return row

        # If not exact, find similar
        names = [normalize(row.get("hotel name", "")) for row in data]
        similar = difflib.get_close_matches(norm_name, names, n=1, cutoff=0.7)
        if similar:
            for row in data:
                if normalize(row.get("hotel name", "")) == similar[0]:
                    return row
    except Exception as e:
        print("⚠️ Error reading sheet:", e)

    return None

# ---------------- TELEGRAM HELPERS ----------------
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{API_URL}/sendMessage", json=payload, timeout=15)
    except Exception as e:
        print("⚠️ Send message failed:", e)

def keyboard_search_only():
    return {"keyboard": [[{"text": "მოძებნე 🔍"}]], "resize_keyboard": True}

def keyboard_main():
    return {
        "keyboard": [
            [{"text": "მოძებნე 🔍"}, {"text": "დაწყება 🚀"}],
            [{"text": "/myhotels"}],
        ],
        "resize_keyboard": True,
    }

# ---------------- PENDING HELPERS ----------------
def set_pending(chat_id, state, temp_name=None, temp_address=None, temp_comment=None):
    db_execute(
        "REPLACE INTO pending (chat_id, state, temp_name, temp_address, temp_comment) VALUES (?, ?, ?, ?, ?)",
        (chat_id, state, temp_name, temp_address, temp_comment)
    )

def get_pending(chat_id):
    res = db_execute("SELECT state, temp_name, temp_address, temp_comment FROM pending WHERE chat_id=?",
                     (chat_id,), fetch=True)
    return res[0] if res else (None, None, None, None)

def clear_pending(chat_id):
    db_execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))

# ---------------- FLASK WEBHOOK ----------------
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json(force=True)
    msg = update.get('message', {})
    chat_id = msg.get('chat', {}).get('id')
    text = msg.get('text', '').strip()
    if not text:
        return jsonify({"ok": True})

    state, temp_name, temp_address, temp_comment = get_pending(chat_id)

    # Start search flow
    if text in ("მოძებნე", "მოძებნე 🔍"):
        set_pending(chat_id, "awaiting_search_name")
        send_message(chat_id, "გთხოვ, ჩაწერე სასტუმროს სახელი.", reply_markup=keyboard_search_only())
        return jsonify({"ok": True})

    # Step 1: hotel name
    if state == "awaiting_search_name":
        set_pending(chat_id, "awaiting_search_address", temp_name=text)
        send_message(chat_id, "შეიყვანე სასტუმროს ზუსტი მისამართი (ქუჩა, ნომერი, ქალაქი).")
        return jsonify({"ok": True})

    # Step 2: address
    if state == "awaiting_search_address":
        hotel_row = find_hotel_in_sheet(temp_name, text)
        if hotel_row:
            comment = hotel_row.get("comment", "არ არის კომენტარი")
            send_message(chat_id,
                         f"❌ <b>ეს სასტუმრო უკვე გამოკითხულია.</b>\n"
                         f"📌 კომენტარი: <i>{comment}</i>",
                         reply_markup=keyboard_main())
            clear_pending(chat_id)
        else:
            send_message(chat_id,
                         "✅ ეს სასტუმრო ბაზაში არ მოიძებნა, შეგიძლიათ გააგრძელოთ რეგისტრაცია ღილაკით <b>დაწყება 🚀</b>.",
                         reply_markup=keyboard_main())
            clear_pending(chat_id)
        return jsonify({"ok": True})

    send_message(chat_id, "გთხოვ დაიწყე ძიება ღილაკით „მოძებნე 🔍“", reply_markup=keyboard_main())
    return jsonify({"ok": True})

# ---------------- INDEX ----------------
@app.route('/')
def index():
    return "HotelClaimBot is running."

# ---------------- MAIN ----------------
if __name__ == '__main__':
    webhook_host = os.environ.get("WEBHOOK_HOST", "https://ok-tv-1.onrender.com")
    webhook_url = f"{webhook_host.rstrip('/')}/{BOT_TOKEN}"
    print(f"Setting webhook to: {webhook_url}")
    try:
        r = requests.get(f"{API_URL}/setWebhook?url={webhook_url}", timeout=10)
        print("Webhook set response:", r.text)
    except Exception as e:
        print("⚠️ Webhook set failed:", e)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
