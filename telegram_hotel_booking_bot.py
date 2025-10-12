# telegram_hotel_bot.py
import os
import requests
import sqlite3
import time
from datetime import datetime
from flask import Flask, request, jsonify

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Please set BOT_TOKEN environment variable")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_PATH = "data.db"

app = Flask(__name__)

# ---------- DATABASE HELPERS ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''
    CREATE TABLE IF NOT EXISTS hotels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        hotel_name TEXT NOT NULL,
        address TEXT,
        comment TEXT,
        agent_name TEXT,
        agent_chat_id INTEGER,
        timestamp INTEGER
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

# ---------- TELEGRAM HELPERS ----------
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    r = requests.post(f"{API_URL}/sendMessage", json=payload)
    return r.json()

def build_main_keyboard():
    # მთავარი ორი ღილაკი: მოსაძებნე და დაწყება
    keyboard = {
        "keyboard": [
            [{"text": "მოძებნე. 🔍"}, {"text": "დაწყება / start. 🚀"}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }
    return keyboard

def build_start_keyboard():
    return {"keyboard": [[{"text": "დაწყება / start. 🚀"}]], "resize_keyboard": True, "one_time_keyboard": True}

# ---------- BUSINESS LOGIC ----------
def hotel_exists_by_name(name):
    # უბრალოდ წრთმულია: ამოწმებს იგივე სახელი (case-insensitive)
    n = name.strip().lower()
    rows = db_execute("SELECT id, hotel_name, address, agent_name FROM hotels WHERE LOWER(hotel_name)=?", (n,), fetch=True)
    return rows[0] if rows else None

def add_hotel_record(hotel_name, address, comment, agent_name, agent_chat_id):
    ts = int(time.time())
    db_execute(
        "INSERT INTO hotels (hotel_name, address, comment, agent_name, agent_chat_id, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (hotel_name.strip(), address.strip(), comment.strip() if comment else "", agent_name.strip(), agent_chat_id, ts)
    )
    return True

def list_agent_records(agent_chat_id):
    rows = db_execute("SELECT id, hotel_name, address, comment, timestamp FROM hotels WHERE agent_chat_id=? ORDER BY timestamp DESC", (agent_chat_id,), fetch=True)
    return rows

# ---------- PENDING STATE (simple in-memory) ----------
# ნოტა: ამ ინმემორი სტრუქტურას შეგიძლია დროთა განმავლობაში გადააკეთო DB pending table-ზე.
pending = {}
# pending[chat_id] = {"step": "awaiting_search_name"} ან {"step":"awaiting_corp", "data": {...}}

# ---------- WEBHOOK HANDLER ----------
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    # print("DEBUG update:", update)
    if "message" not in update:
        return jsonify({"ok": True})
    msg = update["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()

    # თუ ჯერ არ აქვთ არაფერი — ვაგზავნით ძირითადი ღილაკები
    if chat_id not in pending:
        # გავაგზავნოთ მთავარი კლავიატურა (მოძებნე + დაწყება)
        pending[chat_id] = {"step": None}
        send_message(chat_id, "აირჩიე მოქმედება:", reply_markup=build_main_keyboard())
        # თუ მოსთხოვა უფლება /start ან სხვა, მერე მოდის ქვემოთ
        # fallthrough allowed
    # =====================
    # 1) ძებნა - მოძებნე. 🔍
    # =====================
    if text == "მოძებნე. 🔍":
        pending[chat_id] = {"step": "awaiting_search_name"}
        send_message(chat_id, "გთხოვე ჩაწერეთ სასტუმროს / კორპორაციის სახელი, რომელიც გსურთ გადაამოწმოთ. 🔎")
        return jsonify({"ok": True})

    if pending.get(chat_id, {}).get("step") == "awaiting_search_name":
        # შემოდის სტუმრის(სასტუმროს) სახელი — ამოწმებს DB-ში
        hotel_name = text
        existing = hotel_exists_by_name(hotel_name)
        if existing:
            send_message(chat_id, "კორპორაციისთვის შეთავაზება მიწოდებულია. ❌️")
        else:
            send_message(chat_id, "კორპორაცია თავისუფალია, გისურვებთ წარმატებებს. ✅️")
        # შემდეგ ავრთავ Start ღილაკს
        pending[chat_id] = {"step": None}
        send_message(chat_id, "გსურთ ახალი შეტყობინების დაწყება? დააჭირეთ:", reply_markup=build_start_keyboard())
        return jsonify({"ok": True})

    # =====================
    # 2) დაწყება /start -> flow
    # =====================
    if text in ["/start", "დაწყება / start. 🚀"]:
        pending[chat_id] = {"step": "awaiting_corporation", "data": {}}
        # თავში ვიგზავნით ერთი მოკლე შეტყობინება და შეგვიძლია მყისიერი reply keyboard წარვადგინოთ
        send_message(chat_id, "დავიწყოთ. პირველი: შეიყვანეთ კორპორაციის სახელი. 🏢")
        return jsonify({"ok": True})

    # საფეხური: კორპორაცია
    state = pending.get(chat_id, {})
    step = state.get("step")
    if step == "awaiting_corporation":
        hotel_name = text
        state["data"]["hotel_name"] = hotel_name
        state["step"] = "awaiting_address"
        pending[chat_id] = state
        send_message(chat_id, "მშვენიერია. ახლა შეიყვანეთ მისამართი. 📍")
        return jsonify({"ok": True})

    # საფეხური: მისამართი
    if step == "awaiting_address":
        address = text
        state["data"]["address"] = address
        state["step"] = "awaiting_comment"
        pending[chat_id] = state
        send_message(chat_id, "გმადლობთ. შეიყვანეთ კომენტარი. 📩")
        return jsonify({"ok": True})

    # საფეხური: კომენტარი
    if step == "awaiting_comment":
        comment = text
        state["data"]["comment"] = comment
        state["step"] = "awaiting_agent"
        pending[chat_id] = state
        send_message(chat_id, "ჩვენი უკანასკნელი ინფორმაცია: გთხოვთ ჩაწეროთ თქვენი სახელი და გვარი (აგენტის ინფორმაცია). 👩‍💻")
        return jsonify({"ok": True})

    # საფეხური: აგენტი (სახელი და გვარი)
    if step == "awaiting_agent":
        agent_name = text
        data = state.get("data", {})
        hotel_name = data.get("hotel_name", "")
        address = data.get("address", "")
        comment = data.get("comment", "")
        # შენახვა DB-ში
        add_hotel_record(hotel_name, address, comment, agent_name, chat_id)

        # დასრულება და მადლობა
        send_message(chat_id, "OK TV გისურვებთ წარმატებულ დღეს. 🥰")
        # optional: ვუგზავნით მოკლე რეზიუმეს თანამშრომელს
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        summary = (f"შეტყობინება მიღებულია:\n\n"
                   f"კორპორაცია: {hotel_name}\n"
                   f"მისამართი: {address}\n"
                   f"კომენტარი: {comment}\n"
                   f"აგენტის სახელი: {agent_name}\n"
                   f"დრო: {ts}")
        send_message(chat_id, summary)

        # დავბლოკოთ pending
        pending.pop(chat_id, None)
        # დაბრუნება მთავარი კლავიატურა
        send_message(chat_id, "სურვილები? აირჩიე:", reply_markup=build_main_keyboard())
        return jsonify({"ok": True})

    # =====================
    # დამატებითი ბრძანებები (დამხმარე)
    # =====================
    if text == "/myentries":
        rows = list_agent_records(chat_id)
        if not rows:
            send_message(chat_id, "თქვენ არ გაქვთ შენახული ჩანაწერები.")
        else:
            text_out = "თქვენი ჩანაწერები:\n"
            for r in rows:
                hid, hname, addr, comm, ts = r
                dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                text_out += f"\nID:{hid} • {hname} • {addr}\n{comm}\nclaimed: {dt}\n"
            send_message(chat_id, text_out)
        return jsonify({"ok": True})

    # Default: თუ არაფერი ამოიცა — ვაგზავნით მთავარ კლავიატურას
    send_message(chat_id, "სურვილი? აირჩიე მოქმედება:", reply_markup=build_main_keyboard())
    return jsonify({"ok": True})

# ---------- HEALTH CHECK ----------
@app.route("/")
def index():
    return "HotelClaimBot (OK TV) is running."

# ---------- START (webhook set) ----------
if __name__ == "__main__":
    # set webhook on startup (idempotent)
    webhook_url = f"https://ok-tv-1.onrender.com/{BOT_TOKEN}"
    resp = requests.get(f"{API_URL}/setWebhook?url={webhook_url}")
    print("Set webhook response:", resp.text)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
