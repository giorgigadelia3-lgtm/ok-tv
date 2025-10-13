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
        print("✅ Google Sheets connected.")
    except Exception as e:
        sheet = None
        print("⚠️ Google Sheets connection failed:", e)
else:
    print("⚠️ Google Sheets env not configured (SPREADSHEET_ID or creds missing).")

app = Flask(__name__)

# ---------------- DB INIT + MIGRATE ----------------
def init_db_and_migrate():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # hotels table: ensure decision_contact column exists
    cur.execute('''
        CREATE TABLE IF NOT EXISTS hotels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT,
            decision_contact TEXT,
            comment TEXT,
            agent TEXT,
            created_at INTEGER
        )
    ''')

    # pending table includes temp_decision_contact
    cur.execute('''
        CREATE TABLE IF NOT EXISTS pending (
            chat_id INTEGER PRIMARY KEY,
            state TEXT,
            temp_name TEXT,
            temp_address TEXT,
            temp_decision_contact TEXT,
            temp_comment TEXT
        )
    ''')

    # safe ALTER if old DB lacks columns
    cur.execute("PRAGMA table_info(hotels)")
    cols = [r[1] for r in cur.fetchall()]
    if "decision_contact" not in cols:
        try:
            cur.execute("ALTER TABLE hotels ADD COLUMN decision_contact TEXT")
        except Exception:
            pass

    cur.execute("PRAGMA table_info(pending)")
    cols2 = [r[1] for r in cur.fetchall()]
    if "temp_decision_contact" not in cols2:
        try:
            cur.execute("ALTER TABLE pending ADD COLUMN temp_decision_contact TEXT")
        except Exception:
            pass

    conn.commit()
    conn.close()

init_db_and_migrate()

# ---------------- DB helper ----------------
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

# ---------------- UTILITIES ----------------
def normalize(s: str) -> str:
    return " ".join(s.lower().strip().split()) if s else ""

def similar_name(search, names, cutoff=0.7):
    if not names:
        return None
    norm_map = {normalize(n): n for n in names}
    matches = get_close_matches(normalize(search), list(norm_map.keys()), n=1, cutoff=cutoff)
    if matches:
        return norm_map[matches[0]]
    return None

# ---------------- SHEET + DB BUSINESS ----------------
def get_all_sheet_hotels():
    """Return list of hotel names from Google Sheet (original spelling). Expects header in row1."""
    try:
        if sheet:
            col = sheet.col_values(1)  # column A
            return [v for v in col[1:] if v and v.strip()]  # skip header
    except Exception as e:
        print("⚠️ Error reading sheet names:", e)
    return []

def hotel_exists_in_db(name: str):
    n = normalize(name)
    rows = db_execute("SELECT id, name, address FROM hotels WHERE LOWER(name)=?", (n,), fetch=True)
    return rows[0] if rows else None

def add_hotel(name, address, decision_contact, comment, agent):
    ts = int(time.time())
    db_execute(
        "INSERT INTO hotels (name, address, decision_contact, comment, agent, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (name.strip(), address.strip() if address else None, decision_contact.strip() if decision_contact else None, comment.strip() if comment else None, agent.strip() if agent else None, ts)
    )
    # Append to Google Sheet in exact column order that you use:
    # hotel name | address | comment | Contact | agent | date
    if sheet:
        try:
            sheet.append_row([
                name.strip(),
                address.strip() if address else "",
                comment.strip() if comment else "",
                decision_contact.strip() if decision_contact else "",
                agent.strip() if agent else "",
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            ], value_input_option="USER_ENTERED")
        except Exception as e:
            print("⚠️ Failed to append to Google Sheet:", e)

# ---------------- PENDING HELPERS ----------------
def set_pending(chat_id, state, temp_name=None, temp_address=None, temp_decision_contact=None, temp_comment=None):
    db_execute(
        "REPLACE INTO pending (chat_id, state, temp_name, temp_address, temp_decision_contact, temp_comment) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, state, temp_name, temp_address, temp_decision_contact, temp_comment)
    )

def get_pending(chat_id):
    rows = db_execute("SELECT state, temp_name, temp_address, temp_decision_contact, temp_comment FROM pending WHERE chat_id=?", (chat_id,), fetch=True)
    if rows:
        return rows[0]
    return (None, None, None, None, None)

def clear_pending(chat_id):
    db_execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))

# ---------------- TELEGRAM HELPERS ----------------
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=15)
        return r.json()
    except Exception as e:
        print("⚠️ Telegram send error:", e)
        return None

def keyboard_main():
    return {"keyboard": [[{"text": "მოძებნე. 🔍"}, {"text": "დაწყება / start. 🚀"}], [{"text": "/myhotels"}]], "resize_keyboard": True}

def keyboard_search_only():
    return {"keyboard": [[{"text": "მოძებნე. 🔍"}]], "resize_keyboard": True}

def keyboard_start_only():
    return {"keyboard": [[{"text": "დაწყება / start. 🚀"}]], "resize_keyboard": True}

# ---------------- WEBHOOK ----------------
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

    # /myhotels command - shows stored rows (DB)
    if text.lower() in ('/myhotels', 'myhotels'):
        rows = db_execute("SELECT name, address, decision_contact, comment, agent, created_at FROM hotels ORDER BY created_at DESC", fetch=True)
        if not rows:
            send_message(chat_id, "📭 ჩანაწერები არ მოიძებნა.", reply_markup=keyboard_main())
        else:
            out = "<b>ჩაწერილი კორპორაციები / სასტუმროები:</b>\n"
            for name, address, decision_contact, comment, agent, ts in rows:
                dt = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
                out += f"\n🏷️ <b>{name}</b>\n📍 {address or '-'}\n📞 {decision_contact or '-'}\n📝 {comment or '-'}\n👤 {agent or '-'}\n⏱ {dt}\n"
            send_message(chat_id, out, reply_markup=keyboard_main())
        return jsonify({"ok": True})

    # Start search
    if text in ("მოძებნე. 🔍", "მოძებნე", "მოძებნე 🔍"):
        set_pending(chat_id, "awaiting_search")
        send_message(chat_id, "გთხოვთ ჩაწერეთ სასტუმროს/კორპორაციის სახელი საძიებლად.", reply_markup=keyboard_search_only())
        return jsonify({"ok": True})

    # Manual start registration
    if text in ("დაწყება / start. 🚀", "/start", "start"):
        set_pending(chat_id, "awaiting_name")
        send_message(chat_id, "გთხოვთ ჩაწერეთ — <b>კორპორაციის დასახელება. 🏢</b>", reply_markup=keyboard_start_only())
        return jsonify({"ok": True})

    # Get pending state
    state, temp_name, temp_address, temp_decision_contact, temp_comment = get_pending(chat_id)

    # SEARCH FLOW
    if state == "awaiting_search":
        search_raw = text
        search = normalize(search_raw)

        # 1) check Google Sheet first
        sheet_names = get_all_sheet_hotels()
        if sheet_names:
            # exact match?
            if any(normalize(n) == search for n in sheet_names):
                send_message(chat_id, "❌ ამ აბონენტისთვის наша შეთავაზება უკვე გაგზავნილია.", reply_markup=keyboard_main())
                # NOTE: user asked Georgian text; ensure correct text
                # (we'll send corrected Georgian below)
                # But to be exact, send proper Georgian:
                send_message(chat_id, "❌ ამ აბონენტისთვის ჩვენი შეთავაზება უკვე გაგზავნილია.", reply_markup=keyboard_main())
                clear_pending(chat_id)
                return jsonify({"ok": True})

            # similar?
            similar = similar_name(search_raw, sheet_names, cutoff=0.7)
            if similar:
                send_message(chat_id, f"🔎 მსგავსი სახელით სასტუმრო მოიძებნა (Sheet): <b>{similar}</b>\nგთხოვთ გადაამოწმოთ, შეიძლება იგივე იყოს.", reply_markup=keyboard_main())
                clear_pending(chat_id)
                return jsonify({"ok": True})

        # 2) check local DB
        if hotel_exists_in_db(search_raw):
            send_message(chat_id, "❌ ამ აბონენტისთვის ჩვენი შეთავაზება უკვე გაგზავნილია.", reply_markup=keyboard_main())
            clear_pending(chat_id)
            return jsonify({"ok": True})

        # 3) not found -> offer to register
        set_pending(chat_id, "ready_to_register", temp_name=search_raw)
        send_message(chat_id, "✅ ეს კორპორაცია თავისუფალია. თუ გსურთ რეგისტრაცია დააწკაპუნეთ \"დაწყება / start. 🚀\".", reply_markup=keyboard_main())
        return jsonify({"ok": True})

    # REGISTRATION FLOW: name -> address -> decision_contact -> comment -> agent
    if state == "awaiting_name":
        set_pending(chat_id, "awaiting_address", temp_name=text)
        send_message(chat_id, "სახელი მიღებულია. გთხოვთ ჩაწეროთ — <b>მისამართი. 📍</b>", reply_markup=keyboard_start_only())
        return jsonify({"ok": True})

    if state == "awaiting_address":
        set_pending(chat_id, "awaiting_decision_contact", temp_name=temp_name, temp_address=text)
        send_message(chat_id, "გთხოვთ მიუთითოთ გადამწყვეტი პირის კონტაქტი (ტელეფონი ან მეილი). 📞✉️", reply_markup=keyboard_start_only())
        return jsonify({"ok": True})

    if state == "awaiting_decision_contact":
        set_pending(chat_id, "awaiting_comment", temp_name=temp_name, temp_address=temp_address, temp_decision_contact=text)
        send_message(chat_id, "კონტაქტი მიღებულია. გთხოვთ ჩაწეროთ — <b>კომენტარი. 📝</b>", reply_markup=keyboard_start_only())
        return jsonify({"ok": True})

    if state == "awaiting_comment":
        set_pending(chat_id, "awaiting_agent", temp_name=temp_name, temp_address=temp_address, temp_decision_contact=temp_decision_contact, temp_comment=text)
        send_message(chat_id, "კომენტარი მიღებულია. გთხოვთ ჩაწეროთ — <b>აგენტის სახელი და გვარი. 👩‍💻</b>", reply_markup=keyboard_start_only())
        return jsonify({"ok": True})

    if state == "awaiting_agent":
        add_hotel(temp_name, temp_address or "", temp_decision_contact or "", temp_comment or "", text or "")
        clear_pending(chat_id)
        send_message(chat_id, "✅ მონაცემები შენახულია. OK TV გისურვებთ წარმატებულ დღეს! 🥰", reply_markup=keyboard_main())
        return jsonify({"ok": True})

    # default
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
        print("Webhook error:", e)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
