import os
import json
import gspread
from google.oauth2.service_account import Credentials

# --- Google Sheets ავტორიზაცია ---
google_creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if google_creds_json:
    creds_dict = json.loads(google_creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    client = gspread.authorize(creds)

    # ჩაანაცვლე შენი ცხრილის ID-ით (ნახავ URL-ში: https://docs.google.com/spreadsheets/d/🟩_აქაა_ID_🟩/edit)
    SPREADSHEET_ID = "აქ ჩასვი შენი Google Sheet ID"
    sheet = client.open_by_key(SPREADSHEET_ID).sheet1
else:
    sheet = None
    print("⚠️ Google Sheets ავტორიზაცია ვერ შესრულდა.")
# telegram_hotel_claim_bot.py
# -- coding: utf-8 --
"""
HotelClaimBot — Telegram webhook-based bot for searching and registering hotel/corporation offers.

Flow:
- User presses "მოძებნე. 🔍"
- Bot asks for name to search
  - if exists -> "კორპორაციისთვის შეთავაზება მიწოდებულია. ❌️" (end)
  - if not exists -> "კორპორაცია თავისუფალია, გისურვებთ წარმატებებს. ✅️" + show Start button
- If user presses Start -> registration flow:
  1) "კორპორაციის დასახელება. 🏢"
  2) "მისამართი. 📍"
  3) "კომენტარი. 📩"
  4) "აგენტის სახელი და გვარი. 👩‍💻"
  -> Save to SQLite and reply "OK TV გისურვებთ წარმატებულ დღეს. 🥰"

Command:
/myhotels - list saved records
"""

import os
import sqlite3
import time
import requests
from datetime import datetime
from flask import Flask, request, jsonify

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Please set BOT_TOKEN environment variable")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_PATH = "data.db"

app = Flask(_name_)

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
    return " ".join(s.strip().lower().split()) if s else ""

# ---------------- Business logic ----------------
def hotel_exists_by_name(name: str):
    n = normalize(name)
    rows = db_execute("SELECT id, name, address FROM hotels WHERE LOWER(name)=?", (n,), fetch=True)
    return rows[0] if rows else None

def add_hotel(name, address, comment, agent):
    ts = int(time.time())
    db_execute(
        "INSERT INTO hotels (name, address, comment, agent, created_at) VALUES (?, ?, ?, ?, ?)",
        (name.strip(), address.strip() if address else None, comment.strip() if comment else None, agent.strip() if agent else None, ts)
    )

def get_all_hotels():
    return db_execute("SELECT id, name, address, comment, agent, created_at FROM hotels ORDER BY created_at DESC", fetch=True)

# ---------------- Pending flow helpers ----------------
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
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=15)
        return r.json()
    except Exception as e:
        print("Failed to send message:", e)
        return None

def keyboard_search_only():
    return {"keyboard": [[{"text": "მოძებნე. 🔍"}]], "resize_keyboard": True, "one_time_keyboard": False}

def keyboard_main():
    return {"keyboard": [[{"text": "მოძებნე. 🔍"}, {"text": "დაწყება / start. 🚀"}], [{"text": "/myhotels"}]], "resize_keyboard": True, "one_time_keyboard": False}

def keyboard_start_only():
    return {"keyboard": [[{"text": "დაწყება / start. 🚀"}]], "resize_keyboard": True, "one_time_keyboard": False}

# ---------------- Webhook handler ----------------
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json(force=True)
    if 'message' not in update:
        return jsonify({"ok": True})

    msg = update['message']
    chat_id = msg['chat']['id']
    text = msg.get('text', '').strip()
    if not text:
        return jsonify({"ok": True})

    # Command to view DB
    if text.strip().lower() in ('/myhotels', 'myhotels'):
        rows = get_all_hotels()
        if not rows:
            send_message(chat_id, "ჩანაწერები არ მოიძებნა.", reply_markup=keyboard_main())
        else:
            out = "<b>ჩაწერილი კორპორაციები / სასტუმროები:</b>\n"
            for r in rows:
                hid, name, address, comment, agent, ts = r
                dt = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
                out += f"\n🏷️ <b>{name}</b>\n📍 {address or '-'}\n📝 {comment or '-'}\n👤 {agent or '-'}\n⏱ {dt}\n"
            send_message(chat_id, out, reply_markup=keyboard_main())
        return jsonify({"ok": True})

    # Start flows
    # If user pressed search:
    if text in ("მოძებნე. 🔍", "მოძებნე", "მოძებნე 🔍"):
        set_pending(chat_id, "awaiting_search_name")
        send_message(chat_id, "გთხოვ, ჩაწერეთ სასტუმროს/კორპორაციის სახელი საძიებლად.", reply_markup=keyboard_search_only())
        return jsonify({"ok": True})

    # If user presses start button to begin registration
    if text in ("დაწყება / start. 🚀", "start", "/start"):
        # If the user had previously searched and we have temp_name, begin from that; otherwise ask for name.
        state, temp_name, temp_address, temp_comment = get_pending(chat_id)
        if temp_name:
            set_pending(chat_id, "awaiting_name", temp_name=temp_name)
            send_message(chat_id, "დავიწყოთ რეგისტრაცია. პირველი, გთხოვთ დაადასტურეთ ან ჩაწერეთ — <b>კორპორაციის დასახელება. 🏢</b>", reply_markup=keyboard_start_only())
        else:
            set_pending(chat_id, "awaiting_name")
            send_message(chat_id, "დავიწყოთ რეგისტრაცია. გთხოვთ ჩაწერეთ — <b>კორპორაციის დასახელება. 🏢</b>", reply_markup=keyboard_start_only())
        return jsonify({"ok": True})

    # handle pending states
    state, temp_name, temp_address, temp_comment = get_pending(chat_id)

    # Search state: user types name to check
    if state == "awaiting_search_name":
        search_name = text
        existing = hotel_exists_by_name(search_name)
        if existing:
            send_message(chat_id, "კორპორაციისთვის შეთავაზება მიწოდებულია. ❌️", reply_markup=keyboard_main())
            clear_pending(chat_id)
            return jsonify({"ok": True})
        else:
            # not exists
            set_pending(chat_id, "ready_to_register", temp_name=search_name)
            send_message(chat_id, "კორპორაცია თავისუფალია, გისურვებთ წარმატებებს. ✅️\n\nთუ გსურთ, შეყვანა (რეგისტრაცია) ჩააბათ მაშინ დააჭირეთ ღილაკს \"დაწყება / start. 🚀\".", reply_markup=keyboard_main())
            return jsonify({"ok": True})

    # awaiting_name - from start flow
    if state == "awaiting_name":
        # Accept name (either typed or confirm temp_name)
        name_val = text
        # store and move to address
        set_pending(chat_id, "awaiting_address", temp_name=name_val)
        send_message(chat_id, "კორპორაციის დასახელება მიღებულია. გთხოვთ ჩაწერეთ — <b>მისამართი. 📍</b>", reply_markup=keyboard_start_only())
        return jsonify({"ok": True})

    # awaiting_address
    if state == "awaiting_address":
        address = text
        set_pending(chat_id, "awaiting_comment", temp_name=temp_name, temp_address=address)
        send_message(chat_id, "მისამართი მიღებულია. გთხოვთ ჩაწერეთ — <b>კომენტარი. 📩</b>", reply_markup=keyboard_start_only())
        return jsonify({"ok": True})

    # awaiting_comment
    if state == "awaiting_comment":
        comment = text
        set_pending(chat_id, "awaiting_agent", temp_name=temp_name, temp_address=temp_address, temp_comment=comment)
        send_message(chat_id, "კომენტარი მიღებულია. გთხოვთ ჩაწერეთ — <b>აგენტის სახელი და გვარი. 👩‍💻</b>", reply_markup=keyboard_start_only())
        return jsonify({"ok": True})

    # awaiting_agent
    if state == "awaiting_agent":
        agent = text
        if not temp_name:
            send_message(chat_id, "ხარვეზი: სახელი არ მოიძებნა. გთხოვთ დაიწყოთ თავიდან ღილაკით \"მოძებნე. 🔍\" ან დააჭირეთ \"დაწყება / start. 🚀\".", reply_markup=keyboard_main())
            clear_pending(chat_id)
            return jsonify({"ok": True})
        # Save to DB
        add_hotel(temp_name, temp_address or "", temp_comment or "", agent or "")
        clear_pending(chat_id)
        send_message(chat_id, "OK TV გისურვებთ წარმატებულ დღეს. 🥰", reply_markup=keyboard_main())
        return jsonify({"ok": True})

    # No known flow: nudge user to search
    send_message(chat_id, "გთხოვთ დაიწყოთ ღილაკით \"მოძებნე. 🔍\" საწყისისთვის ან გამოიყენეთ /myhotels რათა ნახოთ ჩანაწერები.", reply_markup=keyboard_main())
    return jsonify({"ok": True})

# index
@app.route('/')
def index():
    return "HotelClaimBot is running."

# set webhook on start (optional; will try)
if _name_ == '_main_':
    webhook_url = f"https://ok-tv-1.onrender.com/{BOT_TOKEN}"
    try:
        r = requests.get(f"{API_URL}/setWebhook?url={webhook_url}", timeout=10)
        print("Webhook set response:", r.text)
    except Exception as e:
        print("Failed to set webhook automatically:", e)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
