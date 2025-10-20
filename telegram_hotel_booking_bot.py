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

# ---------------- DB INIT ----------------
def init_db_and_migrate():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

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

    # add columns if missing
    cur.execute("PRAGMA table_info(hotels)")
    cols = [r[1] for r in cur.fetchall()]
    if "decision_contact" not in cols:
        try:
            cur.execute("ALTER TABLE hotels ADD COLUMN decision_contact TEXT")
        except:
            pass

    cur.execute("PRAGMA table_info(pending)")
    cols2 = [r[1] for r in cur.fetchall()]
    if "temp_decision_contact" not in cols2:
        try:
            cur.execute("ALTER TABLE pending ADD COLUMN temp_decision_contact TEXT")
        except:
            pass

    conn.commit()
    conn.close()

init_db_and_migrate()

# ---------------- DB EXEC ----------------
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

def combine_norm(name, address):
    return normalize(f"{name} | {address}")

def similar_name_and_address(search_name, search_address, records, cutoff=0.7):
    if not records:
        return None
    combined_map, combined_list = {}, []
    for r in records:
        key = combine_norm(r.get("name", ""), r.get("address", ""))
        combined_map[key] = r
        combined_list.append(key)
    matches = get_close_matches(combine_norm(search_name, search_address), combined_list, n=1, cutoff=cutoff)
    return combined_map[matches[0]] if matches else None

# ---------------- SHEET HELPERS ----------------
def read_sheet_records():
    results = []
    if not sheet:
        return results
    try:
        records = sheet.get_all_records()
        for row in records:
            results.append({
                "name": str(row.get("hotel name") or row.get("name") or "").strip(),
                "address": str(row.get("address") or "").strip(),
                "comment": str(row.get("comment") or "").strip(),
                "contact": str(row.get("Contact") or "").strip(),
                "agent": str(row.get("agent") or "").strip(),
                "date": str(row.get("date") or "").strip()
            })
    except Exception as e:
        print("⚠️ read_sheet_records error:", e)
    return results

# ---------------- DB BUSINESS ----------------
def hotel_exists_in_db_by_name_and_address(name, address):
    n, a = normalize(name), normalize(address)
    rows = db_execute("SELECT id, name, address FROM hotels WHERE LOWER(name)=? AND LOWER(address)=?", (n, a), fetch=True)
    return rows[0] if rows else None

def add_hotel(name, address, decision_contact, comment, agent):
    ts = int(time.time())
    db_execute(
        "INSERT INTO hotels (name, address, decision_contact, comment, agent, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (name, address, decision_contact, comment, agent, ts)
    )
    if sheet:
        try:
            sheet.append_row([
                name,
                address,
                comment,
                decision_contact,
                agent,
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            ], value_input_option="USER_ENTERED")
        except Exception as e:
            print("⚠️ Failed to append to Google Sheet:", e)

# ---------------- PENDING ----------------
def set_pending(chat_id, state, temp_name=None, temp_address=None, temp_decision_contact=None, temp_comment=None):
    db_execute(
        "REPLACE INTO pending (chat_id, state, temp_name, temp_address, temp_decision_contact, temp_comment) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, state, temp_name, temp_address, temp_decision_contact, temp_comment)
    )

def get_pending(chat_id):
    rows = db_execute("SELECT state, temp_name, temp_address, temp_decision_contact, temp_comment FROM pending WHERE chat_id=?", (chat_id,), fetch=True)
    return rows[0] if rows else (None, None, None, None, None)

def clear_pending(chat_id):
    db_execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))

# ---------------- TELEGRAM ----------------
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{API_URL}/sendMessage", json=payload, timeout=15)
    except Exception as e:
        print("⚠️ Telegram send error:", e)

def keyboard_main():
    return {"keyboard": [[{"text": "მოძებნე. 🔍"}, {"text": "დაწყება / start. 🚀"}]], "resize_keyboard": True}

# ---------------- WEBHOOK ----------------
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json(force=True)
    msg = update.get('message', {})
    chat_id = msg['chat']['id']
    text = msg.get('text', '').strip()

    if text in ("მოძებნე. 🔍", "მოძებნე"):
        set_pending(chat_id, "awaiting_search_name")
        send_message(chat_id, "გთხოვთ ჩაწერეთ სასტუმროს სახელი.", reply_markup=keyboard_main())
        return jsonify(ok=True)

    state, temp_name, temp_address, temp_decision_contact, temp_comment = get_pending(chat_id)

    if state == "awaiting_search_name":
        set_pending(chat_id, "awaiting_search_address", temp_name=text)
        send_message(chat_id, "შეიყვანეთ სასტუმროს ოფიციალური მისამართი ზუსტი იდენტიფიკაციისთვის.", reply_markup=keyboard_main())
        return jsonify(ok=True)

    if state == "awaiting_search_address":
        search_name = temp_name
        search_address = text
        records = read_sheet_records()

        # 1) ზუსტი მატჩი Google Sheet-ში
        for r in records:
            if normalize(r["name"]) == normalize(search_name) and normalize(r["address"]) == normalize(search_address):
                comment = r["comment"] or "— კომენტარი არ არის მითითებული —"
                send_message(chat_id, f"❌ <b>ეს სასტუმრო უკვე გამოკითხულია.</b>\n\n💬 კომენტარი: <i>{comment}</i>", reply_markup=keyboard_main())
                clear_pending(chat_id)
                return jsonify(ok=True)

        # 2) მსგავსების ძიება
        similar = similar_name_and_address(search_name, search_address, records, cutoff=0.7)
        if similar:
            send_message(chat_id, f"🔎 ბაზაში მოიძებნა მსგავსი ჩანაწერი:\n<b>{similar['name']}</b>\n📍 {similar['address']}", reply_markup=keyboard_main())
            clear_pending(chat_id)
            return jsonify(ok=True)

        set_pending(chat_id, "ready_to_register", temp_name=search_name, temp_address=search_address)
        send_message(chat_id, "✅ ეს სასტუმრო ბაზაში არ მოიძებნა, შეგიძლიათ რეგისტრაცია დაიწყოთ ღილაკით \"დაწყება / start. 🚀\"", reply_markup=keyboard_main())
        return jsonify(ok=True)

    return jsonify(ok=True)

@app.route('/')
def index():
    return "OK TV Bot is running."

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
