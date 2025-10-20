# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-
"""
HotelClaimBot — improved matching:
- robust header mapping for Google Sheets
- exact address priority: if address matches exactly -> treat as match
- combined fuzzy matching with logging
- DEBUG_MODE env var prints extra info to logs and (optionally) replies to user
"""
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
    raise RuntimeError("Please set BOT_TOKEN environment variable")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_PATH = os.path.join(os.getcwd(), "data.db")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
DEBUG_MODE = os.environ.get("DEBUG_MODE", "0") == "1"

# ---------------- CONNECT SHEETS ----------------
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
        sheet = None
        print("⚠️ Google Sheets connection failed:", e)
else:
    print("⚠️ SPREADSHEET_ID or GOOGLE_APPLICATION_CREDENTIALS_JSON not configured.")

app = Flask(__name__)

# ---------------- DB init ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
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

# ---------------- UTILITIES ----------------
def normalize(s: str) -> str:
    if not s:
        return ""
    s = str(s)
    s = s.replace("\n", " ").replace("\r", " ")
    s = s.strip().lower()
    # remove common Georgian abbreviations that may break matching
    s = s.replace("ქ.", "").replace("ქალაქი", "").replace("ს წ", "").strip()
    return " ".join(s.split())

def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()

def debug_log(*args, **kwargs):
    if DEBUG_MODE:
        print("[DEBUG]", *args, **kwargs)

# ---------------- SHEET helpers (robust) ----------------
def normalize_header(h: str) -> str:
    if not h:
        return ""
    k = h.strip().lower()
    k = k.replace("\n", " ").strip()
    # map various header names to canonical keys
    if "hotel" in k and ("name" in k or "სახელი" in k):
        return "name"
    if k in ("hotel name", "name", "hotel"):
        return "name"
    if "address" in k or "მისამართ" in k:
        return "address"
    if "comment" in k or "კომენტ" in k:
        return "comment"
    if "contact" in k or "contact" in k or "კონტაქტ" in k:
        return "contact"
    if "agent" in k or "აგენტ" in k:
        return "agent"
    if "date" in k or "time" in k:
        return "date"
    # fallback to raw normalized header
    return k

def read_sheet_records():
    """
    Read all records and return list of dicts with keys:
    name, address, comment, contact, agent, date
    Robust to header naming and extra whitespace.
    """
    results = []
    if not sheet:
        debug_log("Sheet not connected")
        return results
    try:
        raw = sheet.get_all_values()
        if not raw or len(raw) < 1:
            return results
        header_row = raw[0]
        # map headers
        header_map = {}
        for idx, h in enumerate(header_row):
            header_map[idx] = normalize_header(str(h))
        # read data rows
        for row in raw[1:]:
            # build row dict
            r = {"name": "", "address": "", "comment": "", "contact": "", "agent": "", "date": ""}
            for idx, val in enumerate(row):
                key = header_map.get(idx, "")
                if key in r:
                    r[key] = str(val).strip()
                else:
                    # ignore unknown cols
                    pass
            # skip totally empty rows
            if any(v for v in r.values()):
                results.append(r)
        debug_log(f"Read {len(results)} rows from sheet")
    except Exception as e:
        print("⚠️ read_sheet_records error:", e)
    return results

# ---------------- matching logic ----------------
def find_best_match(name_input: str, address_input: str, records: list):
    """
    Priority logic:
    1) If any record has exact normalized address == normalized input address -> prefer it (high score)
       - if name also similar enough (>=0.4) -> treat as exact
    2) Otherwise compute combined score = 0.65*name_sim + 0.35*address_sim
    3) Return best_record and score
    """
    if not records:
        return None, 0.0

    name_norm = normalize(name_input)
    addr_norm = normalize(address_input)

    best = None
    best_score = 0.0

    for r in records:
        rn = r.get("name", "")
        ra = r.get("address", "")
        rn_norm = normalize(rn)
        ra_norm = normalize(ra)

        # exact address match priority
        if ra_norm and addr_norm and ra_norm == addr_norm:
            # compute name similarity to decide whether it's same hotel
            name_sim = similarity(name_input, rn)
            score = 0.9 + (name_sim * 0.09)  # near 0.9-0.99
            debug_log(f"Exact address match candidate: {rn} | {ra} name_sim={name_sim:.3f} score={score:.3f}")
            if score > best_score:
                best = r
                best_score = score
            continue

        # otherwise fuzzy combine
        name_sim = similarity(name_input, rn)
        addr_sim = similarity(address_input, ra)
        combined = 0.65 * name_sim + 0.35 * addr_sim
        debug_log(f"Candidate: {rn} | {ra} name_sim={name_sim:.3f} addr_sim={addr_sim:.3f} combined={combined:.3f}")
        if combined > best_score:
            best = r
            best_score = combined

    return best, best_score

# ---------------- DB pending helpers ----------------
def db_execute(query, params=(), fetch=False):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(query, params)
    if fetch:
        data = cur.fetchall()
    else:
        data = None
    conn.commit()
    conn.close()
    return data

def set_pending(chat_id, state, temp_name=None, temp_address=None, temp_decision_contact=None, temp_comment=None):
    db_execute("REPLACE INTO pending (chat_id, state, temp_name, temp_address, temp_decision_contact, temp_comment) VALUES (?, ?, ?, ?, ?, ?)",
               (chat_id, state, temp_name, temp_address, temp_decision_contact, temp_comment))

def get_pending(chat_id):
    rows = db_execute("SELECT state, temp_name, temp_address, temp_decision_contact, temp_comment FROM pending WHERE chat_id=?", (chat_id,), fetch=True)
    if rows:
        return rows[0]
    return (None, None, None, None, None)

def clear_pending(chat_id):
    db_execute("DELETE FROM pending WHERE chat_id=?", (chat_id,))

# ---------------- Telegram helpers ----------------
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=15)
        debug_log("Telegram sendMessage response:", r.status_code if r is not None else None)
        return r.json() if r is not None else None
    except Exception as e:
        print("⚠️ Telegram send error:", e)
        return None

def keyboard_main():
    return {"keyboard": [[{"text": "მოძებნე. 🔍"}, {"text": "დაწყება / start. 🚀"}], [{"text": "/myhotels"}]], "resize_keyboard": True}

# ---------------- WEBHOOK ----------------
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json(force=True)
    if not update:
        return jsonify(ok=True)
    if 'message' not in update:
        return jsonify(ok=True)

    msg = update['message']
    chat_id = msg['chat']['id']
    text = msg.get('text', '').strip()
    if not text:
        return jsonify(ok=True)

    # commands
    if text.lower() in ('/myhotels', 'myhotels'):
        rows = read_sheet_records()
        if not rows:
            send_message(chat_id, "ჩანაწერები არ მოიძებნა.", reply_markup=keyboard_main())
            return jsonify(ok=True)
        out = "<b>ჩაწერილი სასტუმროები (Sheet):</b>\n"
        for r in rows[-50:][::-1]:  # show up to last 50
            out += f"\n🏷️ <b>{r.get('name') or '-'} </b>\n📍 {r.get('address') or '-'}\n💬 {r.get('comment') or '-'}\n"
        send_message(chat_id, out, reply_markup=keyboard_main())
        return jsonify(ok=True)

    # start search
    if text in ("მოძებნე. 🔍", "მოძებნე", "მოძებნე 🔍"):
        set_pending(chat_id, "awaiting_search_name")
        send_message(chat_id, "გთხოვთ შეიყვანოთ სასტუმროს სახელი საძიებლად.", reply_markup=keyboard_main())
        return jsonify(ok=True)

    # registration start
    if text in ("დაწყება / start. 🚀", "/start", "start"):
        set_pending(chat_id, "awaiting_name")
        send_message(chat_id, "დავიწყოთ რეგისტრაცია. გთხოვთ შეიყვანოთ — <b>კორპორაციის/სასტუმროს დასახელება</b>.", reply_markup=keyboard_main())
        return jsonify(ok=True)

    # pending state handling
    state, temp_name, temp_address, temp_decision_contact, temp_comment = get_pending(chat_id)

    # search: got name -> ask address
    if state == "awaiting_search_name":
        set_pending(chat_id, "awaiting_search_address", temp_name=text)
        send_message(chat_id, "გთხოვთ შეიყვანოთ სასტუმროს <b>ოფიციალური მისამართი</b> (შეეცადეთ ჩაწეროთ პრობლების გარეშე). 📍", reply_markup=keyboard_main())
        return jsonify(ok=True)

    # search: got address -> perform robust checks
    if state == "awaiting_search_address":
        search_name = temp_name or ""
        search_address = text
        debug_log("Search request:", search_name, "|", search_address)

        records = read_sheet_records()
        if not records:
            send_message(chat_id, "⚠️ ვერ წავიკითხე Google Sheet-ს. სცადეთ მოგვიანებით.", reply_markup=keyboard_main())
            clear_pending(chat_id)
            return jsonify(ok=True)

        # 1) exact normalized name+address in sheet
        for r in records:
            if normalize(r.get("name")) == normalize(search_name) and normalize(r.get("address")) == normalize(search_address):
                comment = r.get("comment") or "კომენტარი არ არის მითითებული."
                send_message(chat_id, f"❌ ეს სასტუმრო უკვე გამოკითხულია.\n\n🏨 <b>{r.get('name')}</b>\n📍 {r.get('address')}\n💬 კომენტარი: <i>{comment}</i>", reply_markup=keyboard_main())
                clear_pending(chat_id)
                return jsonify(ok=True)

        # 2) if any record has exact address match (even if name slightly different) -> show it (high confidence)
        for r in records:
            if normalize(r.get("address")) == normalize(search_address) and r.get("address"):
                comment = r.get("comment") or "კომენტარი არ არის მითითებული."
                send_message(chat_id, f"❌ მისამართით მოიძებნა ჩანაწერი: \n🏨 <b>{r.get('name')}</b>\n📍 {r.get('address')}\n💬 კომენტარი: <i>{comment}</i>", reply_markup=keyboard_main())
                clear_pending(chat_id)
                return jsonify(ok=True)

        # 3) fuzzy combined check via find_best_match
        best, score = find_best_match(search_name, search_address, records)
        debug_log("Best candidate:", best, "score=", score)

        if best and score >= 0.85:
            # very likely same
            comment = best.get("comment") or "კომენტარი არ არის მითითებული."
            send_message(chat_id, f"❌ პოპულური შესაბამისობა: <b>{best.get('name')}</b>\n📍 {best.get('address')}\n💬 კომენტარი: <i>{comment}</i>\n(შეერთების ხარისხი: {score:.2f})", reply_markup=keyboard_main())
        elif best and score >= 0.55:
            # similar
            comment = best.get("comment") or "არ არის"
            send_message(chat_id, f"🔎 მოიძებნა მსგავსი ჩანაწერი: <b>{best.get('name')}</b>\n📍 {best.get('address')}\n💬 კომენტარი: <i>{comment}</i>\n(მსგავსების სკორი: {score:.2f})", reply_markup=keyboard_main())
        else:
            # nothing similar
            send_message(chat_id, "✅ ამ სასტუმროსთვის ჩვენი ჩანაწერები არ მოიძებნა. თუ გსურთ, დააწკაპუნეთ \"დაწყება / start. 🚀\" რეგისტრაციისთვის.", reply_markup=keyboard_main())

        clear_pending(chat_id)
        return jsonify(ok=True)

    # registration flow: name -> address -> decision_contact -> comment -> agent
    if state == "awaiting_name":
        set_pending(chat_id, "awaiting_address", temp_name=text)
        send_message(chat_id, "სახელი მიღებულია. გთხოვთ დაწეროთ — <b>მისამართი. 📍</b>", reply_markup=keyboard_main())
        return jsonify(ok=True)

    if state == "awaiting_address":
        set_pending(chat_id, "awaiting_decision_contact", temp_name=temp_name, temp_address=text)
        send_message(chat_id, "გთხოვთ მიუთითოთ გადამწყვეტი პირის კონტაქტი (ტელეფონი ან ელ.ფოსტა). 📞✉️", reply_markup=keyboard_main())
        return jsonify(ok=True)

    if state == "awaiting_decision_contact":
        set_pending(chat_id, "awaiting_comment", temp_name=temp_name, temp_address=temp_address, temp_decision_contact=text)
        send_message(chat_id, "კონტაქტი მიღებულია. გთხოვთ ჩაწეროთ — <b>კომენტარი. 📝</b>", reply_markup=keyboard_main())
        return jsonify(ok=True)

    if state == "awaiting_comment":
        set_pending(chat_id, "awaiting_agent", temp_name=temp_name, temp_address=temp_address, temp_decision_contact=temp_decision_contact, temp_comment=text)
        send_message(chat_id, "კომენტარი მიღებულია. გთხოვთ ჩაწეროთ — <b>აგენტის სახელი და გვარი. 👤</b>", reply_markup=keyboard_main())
        return jsonify(ok=True)

    if state == "awaiting_agent":
        # write to sheet & db via earlier add routine (we'll append to sheet using current header order)
        # Simple append to sheet: try to append as columns [name, address, comment, contact, agent, date]
        # Note: if your sheet header is different, adapt order in future.
        name_final = temp_name
        address_final = temp_address or ""
        contact_final = temp_decision_contact or ""
        comment_final = temp_comment or ""
        agent_final = text or ""
        ts = datetime.fromtimestamp(int(time.time())).strftime("%Y-%m-%d %H:%M")
        try:
            if sheet:
                sheet.append_row([name_final, address_final, comment_final, contact_final, agent_final, ts], value_input_option="USER_ENTERED")
        except Exception as e:
            print("⚠️ append to sheet failed:", e)
        # also optionally keep a DB copy (not mandatory)
        try:
            db_execute("INSERT INTO pending (chat_id, state, temp_name, temp_address, temp_decision_contact, temp_comment) VALUES (?, ?, ?, ?, ?, ?)",
                       (chat_id, None, name_final, address_final, contact_final, comment_final))
        except Exception:
            # ignore
            pass

        clear_pending(chat_id)
        send_message(chat_id, "✅ ჩანაწერი დამატებულია Sheets-ში. მადლობა! 🥰", reply_markup=keyboard_main())
        return jsonify(ok=True)

    # default fallback
    send_message(chat_id, "გთხოვთ გამოიყენოთ ღილაკი \"მოძებნე. 🔍\" ან /myhotels", reply_markup=keyboard_main())
    return jsonify(ok=True)

# ---------------- index ----------------
@app.route('/')
def index():
    return "HotelClaimBot is running."

# ---------------- main ----------------
if __name__ == "__main__":
    webhook_host = os.environ.get("WEBHOOK_HOST", "https://ok-tv-1.onrender.com")
    webhook_url = f"{webhook_host.rstrip('/')}/{BOT_TOKEN}"
    print("Setting webhook to:", webhook_url)
    try:
        r = requests.get(f"{API_URL}/setWebhook?url={webhook_url}", timeout=10)
        print("Webhook set response:", getattr(r, "text", str(r)))
    except Exception as e:
        print("Webhook error:", e)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
