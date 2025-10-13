# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-
import os
import json
import sqlite3
import time
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from difflib import get_close_matches

# Google Sheets libraries
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

# ---------------- GOOGLE SHEETS CONNECTION ----------------
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
        print("✅ Google Sheets connected successfully.")
    except Exception as e:
        print("⚠️ Google Sheets connection failed:", e)
else:
    print("⚠️ Google Sheets environment not fully configured.")

app = Flask(__name__)

# ---------------- DATABASE INIT ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS hotels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
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

init_db()

# ---------------- HELPERS ----------------
def normalize(s: str) -> str:
    return " ".join(s.lower().strip().split()) if s else ""

def get_all_hotel_names_from_sheet():
    """Return all hotel names (normalized) from Google Sheet."""
    names = []
    if sheet:
        try:
            records = sheet.get_all_records()
            for row in records:
                name = row.get("hotel name")
                if name:
                    names.append(normalize(name))
        except Exception as e:
            print("⚠️ Failed to read from Google Sheets:", e)
    return names

def add_hotel_to_db_and_sheet(name, address, comment, agent):
    ts = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO hotels (name, address, comment, agent, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, address, comment, agent, ts)
    )
    conn.commit()
    conn.close()

    if sheet:
        try:
            sheet.append_row([
                name,
                address or "",
                comment or "",
                agent or "",
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            ], value_input_option="USER_ENTERED")
        except Exception as e:
            print("⚠️ Could not sync with Google Sheets:", e)

def set_pending(chat_id, state, temp_name=None, temp_address=None, temp_comment=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("REPLACE INTO pending (chat_id, state, temp_name, temp_address, temp_comment) VALUES (?, ?, ?, ?, ?)",
                (chat_id, state, temp_name, temp_address, temp_comment))
    conn.commit()
    conn.close()

def get_pending(chat_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT state, temp_name, temp_address, temp_comment FROM pending WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    return row if row else (None, None, None, None)

def clear_pending(chat_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

# ---------------- TELEGRAM HELPERS ----------------
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{API_URL}/sendMessage", json=payload, timeout=15)
    except Exception as e:
        print("⚠️ Failed to send message:", e)

def keyboard_search_only():
    return {"keyboard": [[{"text": "მოძებნე. 🔍"}]], "resize_keyboard": True}

def keyboard_main():
    return {"keyboard": [
        [{"text": "მოძებნე. 🔍"}, {"text": "დაწყება / start. 🚀"}],
        [{"text": "/myhotels"}]
    ], "resize_keyboard": True}

def keyboard_start_only():
    return {"keyboard": [[{"text": "დაწყება / start. 🚀"}]], "resize_keyboard": True}

# ---------------- MAIN WEBHOOK ----------------
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json(force=True)
    if not update or 'message' not in update:
        return jsonify({"ok": True})

    msg = update['message']
    chat_id = msg['chat']['id']
    text = msg.get('text', '').strip()
    if not text:
        return jsonify({"ok": True})

    # ----- /myhotels -----
    if text.lower() in ("/myhotels", "myhotels"):
        names = get_all_hotel_names_from_sheet()
        if not names:
            send_message(chat_id, "📭 ჩანაწერები არ მოიძებნა.", reply_markup=keyboard_main())
        else:
            out = "<b>📋 ბაზაში არსებული სასტუმროები:</b>\n\n" + "\n".join([f"🏨 {n}" for n in names])
            send_message(chat_id, out, reply_markup=keyboard_main())
        return jsonify({"ok": True})

    # ----- START SEARCH -----
    if text in ("მოძებნე. 🔍", "მოძებნე", "მოძებნე 🔍"):
        set_pending(chat_id, "awaiting_search_name")
        send_message(chat_id, "გთხოვ, ჩაწერეთ სასტუმროს ან კორპორაციის სახელი საძიებლად.", reply_markup=keyboard_search_only())
        return jsonify({"ok": True})

    # ----- START REGISTRATION -----
    if text in ("დაწყება / start. 🚀", "/start", "start"):
        state, temp_name, temp_address, temp_comment = get_pending(chat_id)
        if temp_name:
            send_message(chat_id, f"დავიწყოთ რეგისტრაცია სასტუმროსთვის: <b>{temp_name}</b>\nგთხოვთ მიუთითოთ მისამართი. 📍", reply_markup=keyboard_start_only())
            set_pending(chat_id, "awaiting_address", temp_name=temp_name)
        else:
            send_message(chat_id, "დავიწყოთ რეგისტრაცია. გთხოვთ ჩაწერეთ — <b>სასტუმროს დასახელება. 🏢</b>", reply_markup=keyboard_start_only())
            set_pending(chat_id, "awaiting_name")
        return jsonify({"ok": True})

    # ----- HANDLE STATES -----
    state, temp_name, temp_address, temp_comment = get_pending(chat_id)

    # --- SEARCH ---
    if state == "awaiting_search_name":
        search_query = normalize(text)
        all_names = get_all_hotel_names_from_sheet()

        if not all_names:
            send_message(chat_id, "⚠️ ბაზა ცარიელია ან ვერ ჩაიტვირთა Google Sheets-დან.", reply_markup=keyboard_main())
            return jsonify({"ok": True})

        if search_query in all_names:
            send_message(chat_id, "❌ ამ სასტუმროზე შეთავაზება უკვე გაკეთებულია.", reply_markup=keyboard_main())
            clear_pending(chat_id)
        else:
            similar = get_close_matches(search_query, all_names, n=1, cutoff=0.6)
            if similar:
                send_message(chat_id, f"🔎 ზუსტად ასეთი სასტუმრო ვერ მოიძებნა, მაგრამ ბაზაში არის მსგავსი: <b>{similar[0]}</b> 🏨", reply_markup=keyboard_main())
                clear_pending(chat_id)
            else:
                send_message(chat_id, "✅ ეს სასტუმრო თავისუფალია! შეგიძლიათ გააგრძელოთ რეგისტრაცია ღილაკით 'დაწყება / start. 🚀'.", reply_markup=keyboard_main())
                set_pending(chat_id, "ready_to_register", temp_name=text)
        return jsonify({"ok": True})

    # --- REGISTRATION FLOW ---
    if state == "awaiting_name":
        set_pending(chat_id, "awaiting_address", temp_name=text)
        send_message(chat_id, "📍 გთხოვთ ჩაწერეთ სასტუმროს მისამართი.", reply_markup=keyboard_start_only())
        return jsonify({"ok": True})

    if state == "awaiting_address":
        set_pending(chat_id, "awaiting_comment", temp_name=temp_name, temp_address=text)
        send_message(chat_id, "📝 გთხოვთ ჩაწერეთ კომენტარი.", reply_markup=keyboard_start_only())
        return jsonify({"ok": True})

    if state == "awaiting_comment":
        set_pending(chat_id, "awaiting_agent", temp_name=temp_name, temp_address=temp_address, temp_comment=text)
        send_message(chat_id, "👩‍💻 გთხოვთ ჩაწერეთ აგენტის სახელი და გვარი.", reply_markup=keyboard_start_only())
        return jsonify({"ok": True})

    if state == "awaiting_agent":
        add_hotel_to_db_and_sheet(temp_name, temp_address, temp_comment, text)
        clear_pending(chat_id)
        send_message(chat_id, "✅ ჩანაწერი წარმატებით დაემატა!\nOK TV გისურვებთ წარმატებულ დღეს! 🥰", reply_markup=keyboard_main())
        return jsonify({"ok": True})

    # DEFAULT
    send_message(chat_id, "გთხოვთ დაიწყოთ ღილაკით 'მოძებნე. 🔍' ან გამოიყენეთ /myhotels.", reply_markup=keyboard_main())
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
        print("Failed to set webhook:", e)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
