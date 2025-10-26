# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-

import os
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple

from flask import Flask, request, abort, jsonify

import telebot
from telebot.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)

import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process

# =========================
# ლოგირება
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hotel-bot")

# =========================
# ENV ცვლადები (Render > Environment)
# =========================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
APP_BASE_URL   = os.environ.get("APP_BASE_URL")                 # напр: https://ok-tv-1.onrender.com
SHEET_ID       = os.environ.get("SPREADSHEET_ID")               # Google Sheet ID
SERVICE_JSON   = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")  # service account JSON (string)

missing = [k for k, v in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "APP_BASE_URL": APP_BASE_URL,
    "SPREADSHEET_ID": SHEET_ID,
    "GOOGLE_SERVICE_ACCOUNT_JSON": SERVICE_JSON,
}.items() if not v]
if missing:
    raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

SERVICE_INFO = json.loads(SERVICE_JSON)
API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# =========================
# Flask + TeleBot (webhook)
# =========================
app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True, num_threads=4, skip_pending=True)

# =========================
# Google Sheets helpers
# =========================
def _gs_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(SERVICE_INFO, scopes=scopes)
    return gspread.authorize(creds)

def _open_spreadsheet():
    gc = _gs_client()
    return gc.open_by_key(SHEET_ID)

def _open_hotels_ws():
    """
    Hotels worksheet (კატალოგი). ვიღებთ პირველ ტაბს, რომ ტაბის სახელი არ შეგვეშალოს.
    აუცილებელი სვეტები:
      - hotel name
      - address
      - comment  (არასავალდებულო, მაგრამ თუაა, გამოვაჩენთ)
    """
    sh = _open_spreadsheet()
    return sh.sheet1

def _open_or_create_leads_ws():
    """
    Leads worksheet, სადაც ახალი ჩანაწერები იწერება:
      created_at | agent_username | hotel_name_en | address_ka | matched | matched_comment | answers
    თუ არ არსებობს — ვქმნით.
    """
    sh = _open_spreadsheet()
    try:
        ws = sh.worksheet("Leads")
        return ws
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Leads", rows=1000, cols=10)
        ws.update("A1:G1", [[
            "created_at", "agent_username", "hotel_name_en", "address_ka",
            "matched", "matched_comment", "answers"
        ]])
        return ws

# Cache for Hotels
_HOTELS_CACHE: Dict[str, Any] = {"rows": [], "ts": 0}
_CACHE_TTL_SEC = 120

def load_hotels(force: bool = False) -> List[Dict[str, Any]]:
    now = time.time()
    if (not force) and _HOTELS_CACHE["rows"] and (now - _HOTELS_CACHE["ts"] < _CACHE_TTL_SEC):
        return _HOTELS_CACHE["rows"]
    try:
        ws = _open_hotels_ws()
        rows = ws.get_all_records()  # list[dict]
        _HOTELS_CACHE["rows"] = rows
        _HOTELS_CACHE["ts"] = now
        logger.info(f"Loaded {len(rows)} hotels from sheet.")
        return rows
    except Exception as e:
        logger.exception("Google Sheets connect error: %s", e)
        return []

def append_lead_row(data: Dict[str, Any]):
    try:
        ws = _open_or_create_leads_ws()
        row = [
            data.get("created_at", time.strftime("%Y-%m-%d %H:%M:%S")),
            data.get("agent_username", ""),
            data.get("hotel_name_en", ""),
            data.get("address_ka", ""),
            data.get("matched", ""),
            data.get("matched_comment", ""),
            json.dumps(data.get("answers", {}), ensure_ascii=False),
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        logger.exception("Append Lead failed: %s", e)
        raise

# =========================
# Session (FSM)
# =========================
@dataclass
class Session:
    stage: str = "idle"
    hotel_name_en: Optional[str] = None
    address_ka: Optional[str] = None
    best_match: Optional[Dict[str, Any]] = None
    best_score_name: int = 0
    best_score_addr: int = 0
    answers: Dict[str, Any] = field(default_factory=dict)

SESSIONS: Dict[int, Session] = {}

def get_session(chat_id: int) -> Session:
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = Session()
    return SESSIONS[chat_id]

# =========================
# UI keyboards
# =========================
def main_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("🔍 მოძებნა"))
    return kb

def start_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("▶️ სტარტი"))
    kb.add(KeyboardButton("⬅️ უკან მენიუში"))
    return kb

# =========================
# Matching Logic
# =========================
def normalize(s: str) -> str:
    return (s or "").strip().lower()

def find_best_hotel(hotel_name_en: str, address_ka: str) -> Tuple[Optional[Dict[str, Any]], int, int]:
    """
    ვეძებთ საუკეთესო დამთხვევას "hotel name" + "address" ველებში.
    ვ იყენებთ RapidFuzz token_set_ratio-ს — საუკეთესო შედეგს ვაბრუნებთ.
    """
    rows = load_hotels()
    if not rows:
        return None, 0, 0

    names = [r.get("hotel name", "") for r in rows]
    addrs = [r.get("address", "") for r in rows]

    name_match = process.extractOne(hotel_name_en, names, scorer=fuzz.token_set_ratio)
    addr_match = process.extractOne(address_ka, addrs, scorer=fuzz.token_set_ratio)

    best = None
    name_score = 0
    addr_score = 0

    if name_match:
        _, name_score, idx = name_match
        best = rows[idx]
        name_score = int(name_score)

    if addr_match:
        _, addr_score, idx = addr_match
        addr_score = int(addr_score)
        if best is None or idx != rows.index(best):
            alt = rows[idx]
            alt_name_score = int(fuzz.token_set_ratio(hotel_name_en, alt.get("hotel name", "")))
            cur_addr = (best or {}).get("address", "")
            cur_addr_score = int(fuzz.token_set_ratio(address_ka, cur_addr)) if best else 0
            if (alt_name_score + addr_score) > (name_score + cur_addr_score):
                best = alt
                name_score = alt_name_score

    return best, name_score, addr_score

# =========================
# Bot handlers
# =========================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    SESSIONS[chat_id] = Session(stage="idle")
    bot.send_message(
        chat_id,
        "აირჩიე მოქმედება 👇",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda m: m.text == "⬅️ უკან მენიუში")
def back_to_menu(message):
    SESSIONS[message.chat.id] = Session(stage="idle")
    bot.send_message(message.chat.id, "დაბრუნდი მთავარ მენიუში.", reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == "🔍 მოძებნა")
def search_entry(message):
    chat_id = message.chat.id
    s = get_session(chat_id)
    s.stage = "ask_name"
    bot.send_message(
        chat_id,
        "გთხოვ, შეიყვანე სასტუმროს <b>ოფიციალური სახელი ინგლისურად</b> (მაგ.: <i>Radisson Blu Batumi</i>).",
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda m: get_session(m.chat.id).stage == "ask_name")
def ask_address_next(message):
    chat_id = message.chat.id
    s = get_session(chat_id)
    s.hotel_name_en = message.text.strip()
    s.stage = "ask_address"
    bot.send_message(
        chat_id,
        "ახლა შეიყვანე <b>ოფიციალური მისამართი ქართულად</b> (ქალაქი, ქუჩა, ნომერი).",
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda m: get_session(m.chat.id).stage == "ask_address")
def check_in_sheet(message):
    chat_id = message.chat.id
    s = get_session(chat_id)
    s.address_ka = message.text.strip()
    s.stage = "checking"

    bm, nscore, ascore = find_best_hotel(s.hotel_name_en, s.address_ka)
    s.best_match = bm
    s.best_score_name = nscore
    s.best_score_addr = ascore

    # ზუსტი / მსგავსი ზღვარი
    EXACT = 90
    SIMILAR = 75

    if bm:
        name_en = bm.get("hotel name", "")
        addr_ka = bm.get("address", "")
        comment = bm.get("comment", "")

        # ზუსტი დამთხვევა → უკვე გამოკითხულია
        if nscore >= EXACT and ascore >= EXACT:
            txt = (f"❌ ეს სასტუმრო უკვე <b>გამოკითხულია</b>.\n"
                   f"🏨 სახელი: {name_en}\n"
                   f"📍 მისამართი: {addr_ka}\n\n"
                   f"📝 კომენტარი: <i>{comment or '—'}</i>\n\n"
                   f"ჩატი დასრულებულია.")
            bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=main_menu())
            SESSIONS[chat_id] = Session(stage="idle")
            return

        # მსგავსი სასტუმრო → „დიახ / არა“
        if nscore >= SIMILAR or ascore >= SIMILAR:
            im = InlineKeyboardMarkup()
            im.add(
                InlineKeyboardButton("✔️ დიახ, ეს სასტუმროა", callback_data="confirm_match"),
                InlineKeyboardButton("✏️ არა, სხვაა", callback_data="reject_match")
            )
            txt = (f"მოვძებნე <b>მსგავსი</b> სასტუმრო.\n\n"
                   f"🏨 სავარაუდო სახელი: <i>{name_en}</i> (ქულა {nscore})\n"
                   f"📍 სავარაუდო მისამართი: <i>{addr_ka}</i> (ქულა {ascore})\n\n"
                   f"ეს ხომ არ არის ის, რასაც ეძებ?")
            bot.send_message(chat_id, txt, reply_markup=im, parse_mode="HTML")
            s.stage = "suggest"
            return

    # ვერ ვიპოვეთ → სტარტი
    bot.send_message(
        chat_id,
        "ამ სახელზე/მისამართზე <b>ზუსტი ჩანაწერი ვერ ვიპოვე</b>.\n"
        "შეგიძლია დაუკავშირდე ამ სასტუმროს.\n\n"
        "თუ გინდა კითხვარის გაგრძელება, დააჭირე <b>▶️ სტარტი</b>.",
        reply_markup=start_menu(),
        parse_mode="HTML"
    )
    s.stage = "ready_to_start"

@bot.callback_query_handler(func=lambda c: c.data in ("confirm_match", "reject_match"))
def on_suggestion_choice(call):
    chat_id = call.message.chat.id
    s = get_session(chat_id)

    if call.data == "confirm_match" and s.best_match:
        bm = s.best_match
        name_en = bm.get("hotel name", "")
        addr_ka = bm.get("address", "")
        comment = bm.get("comment", "")

        # მსგავსი = ფაქტობრივად ბაზაშია → დასრულება
        bot.edit_message_text(
            chat_id=chat_id, message_id=call.message.message_id,
            text=(f"❌ ეს სასტუმრო უკვე <b>გამოკითხულია</b>.\n"
                  f"🏨 {name_en}\n📍 {addr_ka}\n\n"
                  f"📝 კომენტარი: <i>{comment or '—'}</i>\n\n"
                  f"ჩატი დასრულდა."),
            parse_mode="HTML"
        )
        bot.send_message(chat_id, "დაბრუნდი მთავარ მენიუში.", reply_markup=main_menu())
        SESSIONS[chat_id] = Session(stage="idle")
        return

    # „არა, სხვაა“ → ახალი ჩანაწერი
    bot.edit_message_text(
        chat_id=chat_id, message_id=call.message.message_id,
        text="კარგი — შევქმნათ ახალი ჩანაწერი.\nდააჭირე <b>▶️ სტარტი</b> რომ გაგრძელდეს.",
        parse_mode="HTML"
    )
    s.stage = "ready_to_start"
    bot.send_message(chat_id, "გაგრძელებისთვის:", reply_markup=start_menu())

@bot.message_handler(func=lambda m: get_session(m.chat.id).stage == "ready_to_start" and m.text == "▶️ სტარტი")
def start_questionnaire(message):
    chat_id = message.chat.id
    s = get_session(chat_id)

    if not s.hotel_name_en or not s.address_ka:
        s.stage = "ask_name"
        bot.send_message(chat_id, "ჯერ შეიყვანე სასტუმროს სახელი თავიდან.", parse_mode="HTML")
        return

    # უსაფრთხოება — თანამშრომელმა იგივე შეიყვანოს, რაც ძებნაში მიანიშნა
    s.stage = "confirm_inputs_name"
    bot.send_message(
        chat_id,
        "გაიმეორე სასტუმროს <b>ოფიციალური სახელი (EN)</b> დასადასტურებლად:",
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda m: get_session(m.chat.id).stage == "confirm_inputs_name")
def confirm_name_again(message):
    chat_id = message.chat.id
    s = get_session(chat_id)
    name_again = message.text.strip()

    if fuzz.token_set_ratio(name_again, s.hotel_name_en) < 90:
        bot.send_message(
            chat_id,
            "⚠️ შენს მიერ შეყვანილი სახელი <b>განსხვავდება</b> საწყისისგან.\n"
            "გამოსწორე და ჩაწერე თავიდან, ან დააჭირე „⬅️ უკან მენიუში“.",
            parse_mode="HTML"
        )
        return

    s.stage = "confirm_inputs_addr"
    bot.send_message(
        chat_id,
        "ახლა გაიმეორე <b>მისამართი (KA)</b> დასადასტურებლად:",
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda m: get_session(m.chat.id).stage == "confirm_inputs_addr")
def confirm_addr_again(message):
    chat_id = message.chat.id
    s = get_session(chat_id)
    addr_again = message.text.strip()

    if fuzz.token_set_ratio(addr_again, s.address_ka) < 90:
        bot.send_message(
            chat_id,
            "⚠️ შეყვანილი მისამართი <b>განსხვავდება</b> საწყისისგან.\n"
            "გამოსწორე და ჩაწერე თავიდან, ან დააჭირე „⬅️ უკან მენიუში“.",
            parse_mode="HTML"
        )
        return

    # გადავიდეთ კითხვებზე (შენების მინიმუმი — Q1/Q2; შეგიძლია გააფართოვო)
    s.stage = "questionnaire"
    s.answers = {}
    bot.send_message(
        chat_id,
        "Q1) რამდენი ნომერია სასტუმროში? (რიცხვი)",
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda m: get_session(m.chat.id).stage == "questionnaire" and "Q1" not in get_session(m.chat.id).answers)
def q1_rooms(message):
    chat_id = message.chat.id
    s = get_session(chat_id)
    s.answers["Q1"] = message.text.strip()
    bot.send_message(chat_id, "Q2) საკონტაქტო პირი (სახელი, ტელ.):", parse_mode="HTML")

@bot.message_handler(func=lambda m: get_session(m.chat.id).stage == "questionnaire" and 
                      "Q1" in get_session(m.chat.id).answers and 
                      "Q2" not in get_session(m.chat.id).answers)
def q2_contact(message):
    chat_id = message.chat.id
    s = get_session(chat_id)
    s.answers["Q2"] = message.text.strip()

    data = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "agent_username": message.from_user.username or f"id:{message.from_user.id}",
        "hotel_name_en": s.hotel_name_en,
        "address_ka": s.address_ka,
        "matched": "YES" if s.best_match else "NO",
        "matched_comment": f"name_score={s.best_score_name}, addr_score={s.best_score_addr}",
        "answers": s.answers
    }

    try:
        append_lead_row(data)
        bot.send_message(chat_id, "✅ ინფორმაცია ჩაიწერა Google Sheet-ში.", reply_markup=main_menu())
    except Exception:
        bot.send_message(chat_id, "⚠️ ჩაწერის შეცდომა! სცადე ხელახლა.", reply_markup=main_menu())

    SESSIONS[chat_id] = Session(stage="idle")

# =========================
# Fallback
# =========================
@bot.message_handler(content_types=['text'])
def fallback(message):
    chat_id = message.chat.id
    s = get_session(chat_id)
    if s.stage == "idle":
        bot.send_message(chat_id, "აირჩიე მოქმედება მენიუდან.", reply_markup=main_menu())
    else:
        bot.send_message(chat_id, "გაგრძელე მიმდინარე პროცესი ან დააჭირე '⬅️ უკან მენიუში'.", reply_markup=main_menu())

# =========================
# Flask routes (Webhook + Health)
# =========================
@app.route("/", methods=["GET"])
def health():
    return "HotelClaimBot — alive", 200

# Telegram Webhook — მხოლოდ /webhook/<TOKEN> მისამართით
@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        update_json = request.data.decode("utf-8")
        update = telebot.types.Update.de_json(update_json)
        bot.process_new_updates([update])
        return "OK", 200
    return abort(403)

# =========================
# Webhook რეგისტრაცია (სწორი URL-ით) — ავტომატურად Deploy-ზე
# =========================
def set_webhook():
    try:
        # წავშალოთ ძველი, მერე დავაყენოთ სწორი
        bot.remove_webhook()
        time.sleep(1.0)
        url_webhook = f"{APP_BASE_URL}/webhook/{TELEGRAM_TOKEN}"
        ok2 = bot.set_webhook(url=url_webhook, max_connections=4, allowed_updates=["message", "callback_query"])
        logger.info(f"Webhook set to {url_webhook}: {ok2}")
    except Exception as e:
        logger.exception("Failed to set webhook: %s", e)

# გაშვებისას ავტომატურად webhook დაემატოს
set_webhook()

# =========================
# Gunicorn-ისთვის app
# =========================
# Start Command Render-ზე:
# gunicorn telegram_hotel_booking_bot:app --bind 0.0.0.0:$PORT --timeout 120
