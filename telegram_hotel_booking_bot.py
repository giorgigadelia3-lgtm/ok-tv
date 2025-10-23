# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-
"""
HotelClaimBot - production-ready:
- SQLite: WAL, check_same_thread=False, timeout, retry
- Google Sheets: connection, header mapping, caching, safe append
- Search: exact address priority, fuzzy matching (SequenceMatcher)
- Flow: search (name->address) + registration (name->address->decision contact->comment->agent)
- Duplicate prevention (exact + fuzzy threshold)
- Georgian messages
"""

import os
import json
import sqlite3
import time
import threading
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from difflib import SequenceMatcher
import logging

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# ---------------- CONFIG ----------------
logging.basicConfig(level=logging.INFO)
DEBUG_MODE = os.environ.get("DEBUG_MODE", "0") == "1"

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Please set BOT_TOKEN environment variable")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_PATH = os.path.join(os.getcwd(), "data.db")

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")

SHEET_CACHE_TTL = int(os.environ.get("SHEET_CACHE_TTL", "60"))  # seconds
# fuzzy thresholds
FUZZY_STRONG = float(os.environ.get("FUZZY_STRONG", "0.88"))
FUZZY_MEDIUM = float(os.environ.get("FUZZY_MEDIUM", "0.58"))

app = Flask(__name__)

# ---------------- Google Sheets connection + cache ----------------
sheet = None
_sheet_lock = threading.Lock()
_sheet_cache = {"ts": None, "rows": []}

def connect_sheet():
    global sheet
    if not GOOGLE_CREDS_JSON or not SPREADSHEET_ID:
        logging.info("Google Sheets not configured (SPREADSHEET_ID or GOOGLE_APPLICATION_CREDENTIALS_JSON missing).")
        sheet = None
        return
    try:
        creds_dict = json.loads(GOOGLE_CREDS_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(SPREADSHEET_ID).sheet1
        logging.info("✅ Google Sheets connected.")
    except Exception as e:
        sheet = None
        logging.exception("⚠️ Google Sheets connection failed:")

connect_sheet()

def normalize_header(h: str) -> str:
    if not h:
        return ""
    k = h.strip().lower()
    if "hotel" in k and ("name" in k or "სახ" in k):
        return "name"
    if "address" in k or "მისამ" in k:
        return "address"
    if "comment" in k or "კომენტ" in k:
        return "comment"
    if "contact" in k or "კონტაქტ" in k:
        return "contact"
    if "agent" in k or "აგენტ" in k:
        return "agent"
    if "date" in k or "time" in k:
        return "date"
    return k

def read_sheet_cached(force=False):
    """Read all rows from sheet with TTL cache."""
    global _sheet_cache
    if not sheet:
        return []
    now = time.time()
    if not force and _sheet_cache["ts"] and (now - _sheet_cache["ts"] < SHEET_CACHE_TTL):
        if DEBUG_MODE:
            logging.info("Using sheet cache")
        return _sheet_cache["rows"]
    with _sheet_lock:
        try:
            raw = sheet.get_all_values()
            if not raw or len(raw) < 1:
                rows = []
            else:
                header = raw[0]
                header_map = {i: normalize_header(header[i]) for i in range(len(header))}
                rows = []
                for row in raw[1:]:
                    r = {"name": "", "address": "", "comment": "", "contact": "", "agent": "", "date": ""}
                    for i, val in enumerate(row):
                        key = header_map.get(i, "")
                        if key in r:
                            r[key] = str(val).strip()
                    if any(v for v in r.values()):
                        rows.append(r)
            _sheet_cache["rows"] = rows
            _sheet_cache["ts"] = now
            if DEBUG_MODE:
                logging.info(f"Read {len(rows)} rows from sheet (refreshed).")
            return rows
        except Exception:
            logging.exception("⚠️ read_sheet_cached error")
            return _sheet_cache.get("rows", [])

def clear_sheet_cache():
    global _sheet_cache
    with _sheet_lock:
        _sheet_cache = {"ts": None, "rows": []}

# ---------------- SQLite helpers (WAL + retry) ----------------
def get_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        pass
    return conn

def db_execute(query, params=(), fetch=False, retries=6, retry_delay=0.5):
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
            msg = str(e).lower()
            if "database is locked" in msg:
                if DEBUG_MODE:
                    logging.warning(f"DB locked (attempt {attempt+1}/{retries}), retrying...")
                time.sleep(retry_delay)
                continue
            else:
                logging.exception("DB operational error")
                raise
    logging.error("DB execute failed after retries: %s %s", query, params)
    return None

def init_db():
    conn = get_connection()
    cur = conn.cursor()
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
    conn.commit()
    conn.close()

init_db()

# ---------------- Utilities & fuzzy matching ----------------
def normalize(text: str) -> str:
    if not text:
        return ""
    s = str(text).strip().lower()
    s = s.replace("\n", " ").replace("\r", " ")
    for token in ["სასტუმრო", "ქ.", "ქალაქი"]:
        s = s.replace(token, "")
    return " ".join(s.split())

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()

def find_best_sheet_match(name_input: str, address_input: str, rows: list):
    if not rows:
        return None, 0.0
    n_in = normalize(name_input)
    a_in = normalize(address_input)

    best = None
    best_score = 0.0

    for r in rows:
        rn = r.get("name", "") or ""
        ra = r.get("address", "") or ""
        rn_n = normalize(rn)
        ra_n = normalize(ra)

        # exact address priority
        if ra_n and a_in and ra_n == a_in:
            ns = similarity(n_in, rn_n)
            score = 0.88 + (ns * 0.11)
            if score > best_score:
                best = r
                best_score = score
            continue

        # combined fuzzy
        name_sim = similarity(n_in, rn_n)
        addr_sim = similarity(a_in, ra_n)
        combined = 0.65 * name_sim + 0.35 * addr_sim
        if combined > best_score:
            best = r
            best_score = combined

    return best, best_score

# ---------------- Telegram helpers ----------------
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=15)
        if DEBUG_MODE:
            logging.info("Telegram send status: %s %s", getattr(r, "status_code", None), getattr(r, "text", None))
    except Exception:
        logging.exception("⚠️ Telegram send error")

def keyboard_search_only():
    return {"keyboard": [[{"text": "მოძებნე. 🔍"}]], "resize_keyboard": True, "one_time_keyboard": False}

def keyboard_main():
    return {"keyboard": [[{"text": "მოძებნე. 🔍"}, {"text": "დაწყება / start. 🚀"}], [{"text": "/myhotels"}]], "resize_keyboard": True}

# ---------------- Pending helpers ----------------
def set_pending(chat_id, state, temp_name=None, temp_address=None, temp_decision_contact=None, temp_comment=None):
    db_execute("REPLACE INTO pending (chat_id, state, temp_name, temp_address, temp_decision_contact, temp_comment) VALUES (?, ?, ?, ?, ?, ?)",
               (chat_id, state, temp_name, temp_address, temp_decision_contact, temp_comment))

def get_pending(chat_id):
    res = db_execute("SELECT state, temp_name, temp_address, temp_decision_contact, temp_comment FROM pending WHERE chat_id=?", (chat_id,), fetch=True)
    return res[0] if res else (None, None, None, None, None)

def clear_pending(chat_id):
    db_execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))

# ---------------- Sheet append with duplicate prevention ----------------
def sheet_has_duplicate(name, address, rows=None):
    if rows is None:
        rows = read_sheet_cached()
    best, score = find_best_sheet_match(name, address, rows)
    if best and score >= FUZZY_STRONG:
        return True, best, score
    return False, best, score

def append_to_sheet_safe(name, address, comment, contact, agent):
    if not sheet:
        return False, "Sheet not configured"
    rows = read_sheet_cached(force=True)
    is_dup, best, score = sheet_has_duplicate(name, address, rows)
    if is_dup:
        return False, f"duplicate (score={score:.2f})"
    try:
        with _sheet_lock:
            sheet.append_row([name, address, comment, contact, agent, datetime.now().strftime("%Y-%m-%d %H:%M")], value_input_option="USER_ENTERED")
            clear_sheet_cache()
        return True, "ok"
    except Exception as e:
        logging.exception("⚠️ Sheet append error")
        return False, str(e)

# ---------------- Webhook handler ----------------
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json(force=True)
    if not update:
        return jsonify({"ok": True})
    if 'message' not in update:
        return jsonify({"ok": True})

    msg = update['message']
    chat_id = msg['chat']['id']
    text = msg.get('text', '').strip()
    if not text:
        return jsonify({"ok": True})

    state, temp_name, temp_address, temp_decision_contact, temp_comment = get_pending(chat_id)

    # /myhotels command
    if text.strip().lower() in ('/myhotels', 'myhotels'):
        rows = read_sheet_cached()
        if not rows:
            send_message(chat_id, "ჩანაწერები არ მოიძებნა.", reply_markup=keyboard_main())
            return jsonify({"ok": True})
        out = "<b>ჩაწერილი სასტუმროები (ბოლო):</b>\n"
        for r in rows[-40:]:
            out += f"\n🏷️ <b>{r.get('name') or '-'}</b>\n📍 {r.get('address') or '-'}\n💬 {r.get('comment') or '-'}\n"
        send_message(chat_id, out, reply_markup=keyboard_main())
        return jsonify({"ok": True})

    # start search
    if text in ("მოძებნე. 🔍", "მოძებნე", "მოძებნე 🔍"):
        set_pending(chat_id, "awaiting_search_name")
        send_message(chat_id, "გთხოვთ ჩაწეროთ სასტუმროს <b>სახელი</b> საძიებლად.", reply_markup=keyboard_search_only())
        return jsonify({"ok": True})

    # start registration
    if text in ("დაწყება / start. 🚀", "დაწყება", "/start", "start"):
        set_pending(chat_id, "awaiting_name")
        send_message(chat_id, "დავიწყოთ რეგისტრაცია. გთხოვთ ჩაწეროთ — <b>სასტუმროს დასახელება</b>.", reply_markup=keyboard_main())
        return jsonify({"ok": True})

    # SEARCH FLOW: name -> address -> check
    if state == "awaiting_search_name":
        set_pending(chat_id, "awaiting_search_address", temp_name=text)
        send_message(chat_id, "შეიყვანეთ სასტუმროს <b>ოფიციალური მისამართი</b> (ქუჩა, ნომერი, ქალაქი), მეტი სიზუსტისთვის.", reply_markup=keyboard_main())
        return jsonify({"ok": True})

    if state == "awaiting_search_address":
        search_name = temp_name or ""
        search_address = text or ""
        if not sheet:
            send_message(chat_id, "⚠️ Google Sheet არ არის კონფიგურირებული. ვერ შევამოწმებ ბაზას.", reply_markup=keyboard_main())
            clear_pending(chat_id)
            return jsonify({"ok": True})

        rows = read_sheet_cached()
        # exact name + address
        for r in rows:
            if normalize(r.get("name")) == normalize(search_name) and normalize(r.get("address")) == normalize(search_address):
                comment = r.get("comment") or "კომენტარი არ არის."
                send_message(chat_id,
                             f"❌ ამ აბონენტისთვის უკვე გაგზავნილია ჩვენი შეთავაზება.\n\n🏨 <b>{r.get('name')}</b>\n📍 {r.get('address')}\n💬 <i>{comment}</i>",
                             reply_markup=keyboard_main())
                clear_pending(chat_id)
                return jsonify({"ok": True})
        # exact address
        for r in rows:
            if normalize(r.get("address")) == normalize(search_address) and r.get("address"):
                comment = r.get("comment") or "კომენტარი არ არის."
                send_message(chat_id,
                             f"❌ მისამართით მოიძებნა ჩანაწერი:\n🏨 <b>{r.get('name')}</b>\n📍 {r.get('address')}\n💬 <i>{comment}</i>",
                             reply_markup=keyboard_main())
                clear_pending(chat_id)
                return jsonify({"ok": True})
        # fuzzy combined
        best, score = find_best_sheet_match(search_name, search_address, rows)
        if best and score >= FUZZY_STRONG:
            comment = best.get("comment") or "კომენტარი არ არის."
            send_message(chat_id, f"❌ ძალიან მსგავს ჩანაწერს აქვს: <b>{best.get('name')}</b>\n📍 {best.get('address')}\n💬 <i>{comment}</i>\n(შეფასება: {score:.2f})", reply_markup=keyboard_main())
        elif best and score >= FUZZY_MEDIUM:
            send_message(chat_id, f"🔎 ნაპოვნია მსგავსი ჩანაწერი: <b>{best.get('name')}</b>\n📍 {best.get('address')}\n💬 <i>{best.get('comment') or 'არ არის'}</i>\n(მსგავსების სკორი: {score:.2f})", reply_markup=keyboard_main())
        else:
            send_message(chat_id, "✅ ამ სასტუმროსთვის ჩანაწერი არ მოიძებნა. თუ გსურთ, დააწკაპუნეთ \"დაწყება / start. 🚀\" რეგისტრაციისთვის.", reply_markup=keyboard_main())

        clear_pending(chat_id)
        return jsonify({"ok": True})

    # REGISTRATION FLOW: name -> address -> decision contact -> comment -> agent -> append
    if state == "awaiting_name":
        set_pending(chat_id, "awaiting_address", temp_name=text)
        send_message(chat_id, "სახელი მიღებულია. გთხოვთ ჩაწეროთ — <b>მისამართი. 📍</b>", reply_markup=keyboard_main())
        return jsonify({"ok": True})

    if state == "awaiting_address":
        set_pending(chat_id, "awaiting_decision_contact", temp_name=temp_name, temp_address=text)
        send_message(chat_id, "მისამართი მიღებულია. გთხოვთ მიუთითოთ — <b>გადამწყვეტი პირის კონტაქტი (ტელეფონი ან მეილი)</b>.", reply_markup=keyboard_main())
        return jsonify({"ok": True})

    if state == "awaiting_decision_contact":
        set_pending(chat_id, "awaiting_comment", temp_name=temp_name, temp_address=temp_address, temp_decision_contact=text)
        send_message(chat_id, "კონტაქტი მიღებულია. ახლა დაწერეთ — <b>კომენტარი. 📝</b>", reply_markup=keyboard_main())
        return jsonify({"ok": True})

    if state == "awaiting_comment":
        set_pending(chat_id, "awaiting_agent", temp_name=temp_name, temp_address=temp_address, temp_decision_contact=temp_decision_contact, temp_comment=text)
        send_message(chat_id, "კომენტარი მიღებულია. გთხოვთ ჩაწეროთ — <b>აგენტის სახელი და გვარი. 👤</b>", reply_markup=keyboard_main())
        return jsonify({"ok": True})

    if state == "awaiting_agent":
        agent = text.strip()
        name_final = temp_name or ""
        address_final = temp_address or ""
        contact_final = temp_decision_contact or ""
        comment_final = temp_comment or ""
        agent_final = agent or ""

        logging.info(f"Attempt append: {name_final} | {address_final} | {agent_final}")

        rows = read_sheet_cached()
        is_dup, best, score = sheet_has_duplicate(name_final, address_final, rows)
        if is_dup:
            comment = best.get("comment") or "კომენტარი არ არის."
            send_message(chat_id, f"❌ ამ აბონენტისთვის უკვე გაგზავნილია ჩვენი შეთავაზება.\n💬 <i>{comment}</i>", reply_markup=keyboard_main())
            clear_pending(chat_id)
            return jsonify({"ok": True})

        ok, info = append_to_sheet_safe(name_final, address_final, comment_final, contact_final, agent_final)
        logging.info("Append result: %s %s", ok, info)
        if ok:
            send_message(chat_id, "✅ ჩანაწერი წარმატებით დაემატა Google Sheet-ში. 🥰", reply_markup=keyboard_main())
        else:
            send_message(chat_id, f"⚠️ ჩანაწერის დამატება ვერ მოხერხდა: {info}", reply_markup=keyboard_main())
        clear_pending(chat_id)
        return jsonify({"ok": True})

    # fallback
    send_message(chat_id, "გთხოვთ დაიწყოთ ღილაკით \"მოძებნე. 🔍\" ან გამოიყენეთ /myhotels რათა ნახოთ ჩანაწერები.", reply_markup=keyboard_main())
    return jsonify({"ok": True})

# ---------------- INDEX ----------------
@app.route('/')
def index():
    return "HotelClaimBot is running."

# ---------------- MAIN ----------------
if __name__ == '__main__':
    webhook_host = os.environ.get("WEBHOOK_HOST", "https://ok-tv-1.onrender.com")
    webhook_url = f"{webhook_host.rstrip('/')}/{BOT_TOKEN}"
    logging.info("Setting webhook to: %s", webhook_url)
    try:
        r = requests.get(f"{API_URL}/setWebhook?url={webhook_url}", timeout=10)
        logging.info("Webhook set response: %s", getattr(r, "text", str(r)))
    except Exception:
        logging.exception("⚠️ Webhook set failed")
    # for production consider gunicorn; this is ok for small Render services
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
