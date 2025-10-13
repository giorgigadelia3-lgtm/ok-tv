# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-
import os
import json
import sqlite3
import time
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from difflib import get_close_matches  # მსგავსი სახელების მოსაძებნად

# Google Sheets libs
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

        # Check header
        try:
            values = sheet.row_values(1)
            if not values or len(values) < 5:
                header = ["hotel name", "address", "comment", "agent", "date"]
                sheet.insert_row(header, index=1)
        except Exception as e:
            print("⚠️ Could not verify header row:", e)

        print("✅ Google Sheets connected.")
    except Exception as e:
        print("⚠️ Google Sheets auth failed:", e)
else:
    print("⚠️ Missing Google Sheets credentials or ID.")

app = Flask(__name__)

# ---------------- Database helpers ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS hotels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT,
            comment TEXT,
            agent TEXT,
            created_at INTEGER
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS pending (
            chat_id INTEGER PRIMARY KEY,
            state TEXT,
            temp_name TEXT,
            temp_address TEXT,
            temp_comment TEXT
        )
    ''')
    conn.commit()
    conn.close()

def db_execute(query, params=(), fetch=False):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(query, params)
    data = None
    if fetch:
        data = cur.fetchall()
    conn.commit()
    conn.close()
    return data

init_db()

# ---------------- Utilities ----------------
def normalize(s: str) -> str:
    return " ".join(s.lower().strip().split()) if s else ""

def similar_name(search, names, cutoff=0.75):
    """პოულობს ყველაზე მსგავს სახელს"""
    matches = get_close_matches(normalize(search), [normalize(n) for n in names], n=1, cutoff=cutoff)
    if matches:
        for n in names:
            if normalize(n) == matches[0]:
                return n
    return None

# ---------------- Business logic ----------------
def get_all_sheet_hotels():
    """კითხულობს ყველა სასტუმროს სახელებს Google Sheet-იდან"""
    try:
        if sheet:
            data = sheet.col_values(1)
            return [d for d in data[1:] if d.strip()]  # skip header
    except Exception as e:
        print("⚠️ Could not read from Google Sheet:", e)
    return []

def hotel_exists_by_name(name: str):
    """ამოწმებს SQLite-ში"""
    n = normalize(name)
    rows = db_execute("SELECT id, name, address FROM hotels WHERE LOWER(TRIM(name)) = ?", (n,), fetch=True)
    return rows[0] if rows else None

def add_hotel(name, address, comment, agent):
    ts = int(time.time())
    db_execute(
        "INSERT INTO hotels (name, address, comment, agent, created_at) VALUES (?, ?, ?, ?, ?)",
        (name.strip(), address.strip() if address else None, comment.strip() if comment else None, agent.strip() if agent else None, ts)
    )
    if sheet:
        try:
            sheet.append_row([
                name.strip(),
                address.strip() if address else "",
                comment.strip() if comment else "",
                agent.strip() if agent else "",
                datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
            ], value_input_option='USER_ENTERED')
        except Exception as e:
            print("⚠️ Sheet sync failed:", e)

# ---------------- Pending helpers ----------------
def set_pending(chat_id, state, temp_name=None, temp_address=None, temp_comment=None):
    db_execute(
        "REPLACE INTO pending (chat_id, state, temp_name, temp_address, temp_comment) VALUES (?, ?, ?, ?, ?)",
        (chat_id, state, temp_name, temp_address, temp_comment)
    )

def get_pending(chat_id):
    rows = db_execute("SELECT state, temp_name, temp_address, temp_comment FROM pending WHERE chat_id=?", (chat_id,), fetch=True)
    if rows:
        return rows[0]
    return (None, None, None, None)

def clear_pending(chat_id):
    db_execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))

# ---------------- Telegram helpers ----------------
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{API_URL}/sendMessage", json=payload, timeout=15)
    except Exception as e:
        print("⚠️ Failed to send message:", e)

def keyboard_main():
    return {"keyboard": [[{"text": "მოძებნე. 🔍"}, {"text": "დაწყება / start. 🚀"}], [{"text": "/myhotels"}]], "resize_keyboard": True}

def keyboard_search_only():
    return {"keyboard": [[{"text": "მოძებნე. 🔍"}]], "resize_keyboard": True}

def keyboard_start_only():
    return {"keyboard": [[{"text": "დაწყება / start. 🚀"}]], "resize_keyboard": True}

# ---------------- Webhook ----------------
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json(force=True)
    msg = update.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "").strip()

    if not chat_id or not text:
        return jsonify({"ok": True})

    # Commands
    if text.lower() in ("/myhotels", "myhotels"):
        rows = db_execute("SELECT name, address, comment, agent, created_at FROM hotels ORDER BY created_at DESC", fetch=True)
        if not rows:
            send_message(chat_id, "ჩანაწერები არ მოიძებნა.", keyboard_main())
        else:
            out = "<b>ჩაწერილი კორპორაციები:</b>\n"
            for name, address, comment, agent, ts in rows:
                out += f"\n🏷️ <b>{name}</b>\n📍 {address or '-'}\n📝 {comment or '-'}\n👤 {agent or '-'}\n⏱ {datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')}\n"
            send_message(chat_id, out, keyboard_main())
        return jsonify({"ok": True})

    # Search flow
    state, temp_name, temp_address, temp_comment = get_pending(chat_id)
    if text in ("მოძებნე", "მოძებნე. 🔍", "მოძებნე 🔍"):
        set_pending(chat_id, "awaiting_search")
        send_message(chat_id, "გთხოვთ ჩაწერეთ სასტუმროს/კორპორაციის სახელი საძიებლად.", keyboard_search_only())
        return jsonify({"ok": True})

    if state == "awaiting_search":
        search_name = text
        sheet_names = get_all_sheet_hotels()
        match = None

        # 1. Check exact match in Sheet
        if any(normalize(search_name) == normalize(n) for n in sheet_names):
            send_message(chat_id, "✅ ეს სასტუმრო უკვე ჩაწერილია (Google Sheets-ში).", keyboard_main())
            clear_pending(chat_id)
            return jsonify({"ok": True})

        # 2. Check similar match
        match = similar_name(search_name, sheet_names)
        if match:
            send_message(chat_id, f"⚠️ მსგავსი სახელით სასტუმრო მოიძებნა: <b>{match}</b>\nშეამოწმე შეიძლება იგივე იყოს.", keyboard_main())
            clear_pending(chat_id)
            return jsonify({"ok": True})

        # 3. Check in local DB
        if hotel_exists_by_name(search_name):
            send_message(chat_id, "❌ კორპორაციისთვის შეთავაზება უკვე მიწოდებულია.", keyboard_main())
            clear_pending(chat_id)
            return jsonify({"ok": True})

        # 4. None found
        set_pending(chat_id, "ready_to_register", temp_name=search_name)
        send_message(chat_id, "✅ ეს კორპორაცია თავისუფალია. გისურვებთ წარმატებებს!\nდასაწყებად დააჭირეთ \"დაწყება / start. 🚀\"", keyboard_main())
        return jsonify({"ok": True})

    # Registration flow
    if text in ("დაწყება / start. 🚀", "start", "/start"):
        set_pending(chat_id, "awaiting_name")
        send_message(chat_id, "შეიყვანეთ — <b>კორპორაციის დასახელება. 🏢</b>", keyboard_start_only())
        return jsonify({"ok": True})

    if state == "awaiting_name":
        set_pending(chat_id, "awaiting_address", temp_name=text)
        send_message(chat_id, "დასახელება მიღებულია. ახლა ჩაწერეთ — <b>მისამართი. 📍</b>", keyboard_start_only())
        return jsonify({"ok": True})

    if state == "awaiting_address":
        set_pending(chat_id, "awaiting_comment", temp_name=temp_name, temp_address=text)
        send_message(chat_id, "მისამართი მიღებულია. ახლა ჩაწერეთ — <b>კომენტარი. 📩</b>", keyboard_start_only())
        return jsonify({"ok": True})

    if state == "awaiting_comment":
        set_pending(chat_id, "awaiting_agent", temp_name=temp_name, temp_address=temp_address, temp_comment=text)
        send_message(chat_id, "კომენტარი მიღებულია. გთხოვთ ჩაწერეთ — <b>აგენტის სახელი და გვარი. 👩‍💻</b>", keyboard_start_only())
        return jsonify({"ok": True})

    if state == "awaiting_agent":
        add_hotel(temp_name, temp_address, temp_comment, text)
        clear_pending(chat_id)
        send_message(chat_id, "✅ მონაცემები შენახულია. OK TV გისურვებთ წარმატებულ დღეს! 🥰", keyboard_main())
        return jsonify({"ok": True})

    send_message(chat_id, "გთხოვთ დაიწყოთ ღილაკით „მოძებნე. 🔍“ ან /myhotels.", keyboard_main())
    return jsonify({"ok": True})

# ---------------- INDEX ----------------
@app.route('/')
def index():
    return "HotelClaimBot is running."

# ---------------- MAIN ----------------
if __name__ == '__main__':
    webhook_url = f"https://ok-tv-1.onrender.com/{BOT_TOKEN}"
    try:
        r = requests.get(f"{API_URL}/setWebhook?url={webhook_url}", timeout=10)
        print("Webhook set response:", r.text)
    except Exception as e:
        print("Webhook error:", e)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
