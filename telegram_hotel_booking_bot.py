# telegram_hotel_claim_bot_full.py
import os
import requests
import sqlite3
import time
from datetime import datetime
from flask import Flask, request, jsonify

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Please set BOT_TOKEN environment variable")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_PATH = "data.db"

app = Flask(__name__)

# ---------- DB helpers ----------
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
        claimed_at INTEGER
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

# ---------- Telegram helpers ----------
def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    r = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=15)
    return r.json()

def normalize(s: str) -> str:
    return " ".join(s.strip().lower().split())

# ---------- Business / DB logic ----------
def hotel_exists_by_name(name):
    n = normalize(name)
    rows = db_execute("SELECT id, name, address FROM hotels WHERE LOWER(name)=?", (n,), fetch=True)
    return rows[0] if rows else None

def add_hotel(name, address, comment, agent):
    ts = int(time.time())
    db_execute('INSERT INTO hotels (name, address, comment, agent, claimed_at) VALUES (?, ?, ?, ?, ?)',
               (name.strip(), address.strip() if address else None, comment.strip() if comment else None, agent.strip() if agent else None, ts))
    return True

def list_hotels():
    rows = db_execute("SELECT id, name, address, comment, agent, claimed_at FROM hotels ORDER BY claimed_at DESC", fetch=True)
    return rows

# ---------- Pending conversation helpers ----------
def set_pending(chat_id, state, temp_name=None, temp_address=None, temp_comment=None):
    db_execute('REPLACE INTO pending (chat_id, state, temp_name, temp_address, temp_comment) VALUES (?, ?, ?, ?, ?)',
               (chat_id, state, temp_name, temp_address, temp_comment))

def get_pending(chat_id):
    rows = db_execute('SELECT state, temp_name, temp_address, temp_comment FROM pending WHERE chat_id=?', (chat_id,), fetch=True)
    if rows:
        return rows[0]  # tuple(state, temp_name, temp_address, temp_comment)
    return (None, None, None, None)

def clear_pending(chat_id):
    db_execute('DELETE FROM pending WHERE chat_id=?', (chat_id,))

# ---------- Reply keyboards ----------
def main_keyboard():
    # two row keyboard: [მოძებნე. 🔍] [დაწყება / start. 🚀]
    keyboard = {
        "keyboard": [
            [{"text": "მოძებნე. 🔍"}],
            [{"text": "დაწყება / start. 🚀"}]
        ],
        "one_time_keyboard": False,
        "resize_keyboard": True
    }
    return keyboard

def start_process_keyboard():
    # when you want choose only start (optional)
    keyboard = {
        "keyboard": [
            [{"text": "დაწყება / start. 🚀"}]
        ],
        "one_time_keyboard": False,
        "resize_keyboard": True
    }
    return keyboard

# ---------- Webhook endpoint ----------
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json(force=True)
    # Only handle simple messages here
    if 'message' in update:
        message = update['message']
        chat_id = message['chat']['id']
        text = message.get('text', '').strip()

        # If no text, ignore
        if not text:
            return jsonify({"ok": True})

        # Normalize text for command checks
        text_norm = text.strip()

        # --- If user asks list of claimed hotels (admin feature) ---
        if text_norm.lower() in ("/myhotels", "myhotels"):
            rows = list_hotels()
            if not rows:
                send_message(chat_id, "შენთან ჯერჯერობით არ არის ჩაწერილი სასტუმროები.")
            else:
                msg = "<b>ჩაწერილი სასტუმროები:</b>\n"
                for r in rows:
                    hid, name, addr, comment, agent, ts = r
                    dt = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
                    msg += f"\nID: {hid}\n🏨 <b>{name}</b>\n📍 {addr or '-'}\n📝 {comment or '-'}\n👤 {agent or '-'}\n⏱ {dt}\n"
                send_message(chat_id, msg)
            return jsonify({"ok": True})

        # --- Start or main keyboard (default) ---
        if text_norm in ("მოძებნე. 🔍", "მოძებნე", "მოძებნე 🔍"):
            # Prompt for searching a corp/hotel name immediately
            set_pending(chat_id, "awaiting_search_name")
            send_message(chat_id, "გთხოვ, ჩაწერეთ სასტუმროს / კორპორაციის სახელი, რათა დავამოწმოთ არსებული ჩანაწერი.", reply_markup=main_keyboard())
            return jsonify({"ok": True})

        if text_norm in ("დაწყება / start. 🚀", "/start", "start", "დაწყება"):
            # Begin full booking flow
            set_pending(chat_id, "awaiting_name")
            send_message(chat_id, "დავიწყოთ — შეიყვანეთ კორპორაციის სახელი. 🏢", reply_markup=start_process_keyboard())
            return jsonify({"ok": True})

        # --- handle pending states ---
        state, temp_name, temp_address, temp_comment = get_pending(chat_id)

        # If user is searching for a name (before starting full flow)
        if state == "awaiting_search_name":
            name = text
            existing = hotel_exists_by_name(name)
            if existing:
                send_message(chat_id, "კორპორაციისთვის შეთავაზება მიწოდებულია. ❌️", reply_markup=main_keyboard())
                clear_pending(chat_id)
            else:
                send_message(chat_id, "კორპორაცია თავისუფალია, გისურვებთ წარმატებებს. ✅️", reply_markup=main_keyboard())
                clear_pending(chat_id)
            return jsonify({"ok": True})

        # If user currently in full flow awaiting name:
        if state == "awaiting_name":
            name = text
            # if exists already, inform and end
            existing = hotel_exists_by_name(name)
            if existing:
                send_message(chat_id, "კორპორაციისთვის შეთავაზება მიწოდებულია. ❌️", reply_markup=main_keyboard())
                clear_pending(chat_id)
                return jsonify({"ok": True})
            # else proceed and save temp_name
            set_pending(chat_id, "awaiting_address", temp_name=name)
            send_message(chat_id, "კორპორაციის სახელი მიღებული. შეიყვანეთ მისამართი. 📍", reply_markup=start_process_keyboard())
            return jsonify({"ok": True})

        if state == "awaiting_address":
            address = text
            # save address in pending
            set_pending(chat_id, "awaiting_comment", temp_name=temp_name, temp_address=address)
            send_message(chat_id, "მისამართი მიღებულია. დაწერეთ კომენტარი. 📩", reply_markup=start_process_keyboard())
            return jsonify({"ok": True})

        if state == "awaiting_comment":
            comment = text
            # save comment in pending
            set_pending(chat_id, "awaiting_agent", temp_name=temp_name, temp_address=temp_address, temp_comment=comment)
            send_message(chat_id, "კომენტარი მიღებულია. ჩაწერეთ აგენტის სახელი და გვარი. 👩‍💻", reply_markup=start_process_keyboard())
            return jsonify({"ok": True})

        if state == "awaiting_agent":
            agent = text
            # read temp fields and add to DB
            # temp_name, temp_address, temp_comment come from get_pending call
            # but ensure we read fresh:
            s, t_name, t_addr, t_comment = get_pending(chat_id)
            # final check
            if not t_name:
                send_message(chat_id, "დაფიქსირდა შეცდომა: კორპორაციის სახელი ვერ მოიძებნა. გთხოვთ დაიწყოთ თავიდან.", reply_markup=main_keyboard())
                clear_pending(chat_id)
                return jsonify({"ok": True})
            # insert
            add_hotel(t_name, t_addr or "", t_comment or "", agent or "")
            clear_pending(chat_id)
            send_message(chat_id, "OK TV გისურვებთ წარმატებულ დღეს. 🥰", reply_markup=main_keyboard())
            return jsonify({"ok": True})

        # If no pending and user typed something else: show main keyboard
        send_message(chat_id, "გთხოვთ აირჩიოთ ღილაკი ან გამოაგზავნოთ /start.", reply_markup=main_keyboard())
        return jsonify({"ok": True})

    # Non-message update
    return jsonify({"ok": True})

# ---------- Index ----------
@app.route('/')
def index():
    return "Telegram Hotel Claim Bot is running."

# ---------- Run ----------
if __name__ == '__main__':
    # when starts try set webhook (idempotent)
    webhook_url = f"https://ok-tv-1.onrender.com/{BOT_TOKEN}"
    try:
        set_resp = requests.get(f"{API_URL}/setWebhook?url={webhook_url}", timeout=10)
        print("Set webhook response:", set_resp.text)
    except Exception as e:
        print("Failed to set webhook automatically:", str(e))
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
