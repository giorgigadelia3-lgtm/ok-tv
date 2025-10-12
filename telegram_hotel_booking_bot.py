# telegram_hotel_claim_bot.py
# -*- coding: utf-8 -*-
import os
import requests
import sqlite3
import time
from datetime import datetime
from flask import Flask, request, jsonify

# ---------------- CONFIG ----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Please set BOT_TOKEN environment variable in your service environment")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_PATH = "data.db"

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
    return " ".join(s.strip().lower().split())

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

# keyboards
def keyboard_search_only():
    return {"keyboard": [[{"text": "მოძებნე. 🔍"}]], "resize_keyboard": True, "one_time_keyboard": False}

def keyboard_main():
    return {"keyboard": [[{"text": "მოძებნე. 🔍"}], [{"text": "/myhotels"}]], "resize_keyboard": True, "one_time_keyboard": False}

# ---------------- Webhook handler ----------------
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json(force=True)

    # only handle message updates
    if 'message' not in update:
        return jsonify({"ok": True})

    msg = update['message']
    chat_id = msg['chat']['id']
    text = msg.get('text', '').strip()
    if not text:
        return jsonify({"ok": True})

    # Admin command: view DB (you can remove or protect later)
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

    # If user pressed search button:
    if text in ("მოძებნე. 🔍", "მოძებნე", "მოძებნე 🔍"):
        set_pending(chat_id, "awaiting_search_name")
        send_message(chat_id, "გთხოვ, ჩაწერეთ სასტუმროს/კორპორაციის სახელი საძიებლად.", reply_markup=keyboard_search_only())
        return jsonify({"ok": True})

    # handle pending states
    state, temp_name, temp_address, temp_comment = get_pending(chat_id)

    # If user is searching a name (first step)
    if state == "awaiting_search_name":
        search_name = text
        existing = hotel_exists_by_name(search_name)
        if existing:
            # Exists -> inform and end
            send_message(chat_id, "კორპორაციისთვის შეთავაზება მიწოდებულია. ❌️", reply_markup=keyboard_main())
            clear_pending(chat_id)
            return jsonify({"ok": True})
        else:
            # Not exists -> inform user and proceed with flow using this name as temp_name
            set_pending(chat_id, "awaiting_address", temp_name=search_name)
            send_message(chat_id, "კორპორაცია თავისუფალია, გისურვებთ წარმატებებს. ✅️\n\nგთხოვთ დააჭიროთ ან დაწეროთ — <b>კორპორაციის დასახელება. 🏢</b>\n(თუ გსურთ გამოასწოროთ სახელი — დაწერეთ ახალი.)", reply_markup=keyboard_search_only())
            # The next wanted input is address, but we ask for confirmation of name first; if user types address, we'll accept as address.
            return jsonify({"ok": True})

    # If previously set temp_name and awaiting_address
    if state == "awaiting_address":
        # We expect this message either to be the (confirmed) name or address.
        # Heuristics: if message contains typical address markers (numbers, street keywords) — treat as address.
        # But simpler: treat current text as address.
        address = text
        set_pending(chat_id, "awaiting_comment", temp_name=temp_name, temp_address=address)
        send_message(chat_id, "მისამართი მიღებულია. გთხოვთ ჩაწერეთ კომენტარი. 📩", reply_markup=keyboard_search_only())
        return jsonify({"ok": True})

    # awaiting_comment
    if state == "awaiting_comment":
        comment = text
        set_pending(chat_id, "awaiting_agent", temp_name=temp_name, temp_address=temp_address, temp_comment=comment)
        send_message(chat_id, "კომენტარი მიღებულია. გთხოვთ ჩაწერეთ აგენტის სახელი და გვარი. 👩‍💻", reply_markup=keyboard_search_only())
        return jsonify({"ok": True})

    # awaiting_agent
    if state == "awaiting_agent":
        agent = text
        # Final validation: ensure temp_name exists
        if not temp_name:
            send_message(chat_id, "დაფიქსირდა შეცდომა: კორპორაციის სახელი დაკარგულია. გთხოვთ დაიწყოთ თავიდან ღილაკით \"მოძებნე. 🔍\".", reply_markup=keyboard_main())
            clear_pending(chat_id)
            return jsonify({"ok": True})
        # Save to DB
        add_hotel(temp_name, temp_address or "", temp_comment or "", agent or "")
        clear_pending(chat_id)
        send_message(chat_id, "OK TV გისურვებთ წარმატებულ დღეს. 🥰", reply_markup=keyboard_main())
        return jsonify({"ok": True})

    # If no pending state and user typed something else -> show keyboard
    send_message(chat_id, "გთხოვთ დააჭიროთ ღილაკს \"მოძებნე. 🔍\" საწყისისთვის ან გამოიყენოთ კითხვა /myhotels რათა ნახოთ ჩანაწერები.", reply_markup=keyboard_main())
    return jsonify({"ok": True})

# index
@app.route('/')
def index():
    return "HotelClaimBot is running."

# run (and set webhook)
if __name__ == '__main__':
    webhook_url = f"https://ok-tv-1.onrender.com/{BOT_TOKEN}"
    try:
        r = requests.get(f"{API_URL}/setWebhook?url={webhook_url}", timeout=10)
        print("Webhook set response:", r.text)
    except Exception as e:
        print("Failed to set webhook automatically:", e)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
