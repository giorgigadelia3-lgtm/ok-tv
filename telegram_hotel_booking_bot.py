import os
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple

from flask import Flask, request, abort
import telebot
from telebot.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)

import gspread
from rapidfuzz import fuzz, process

# =========================
# ლოგირება
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hotel-bot")

# =========================
# ENV ცვლადები
# =========================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
APP_BASE_URL   = os.environ.get("APP_BASE_URL")                 # напр: https://ok-tv-1.onrender.com
SHEET_ID       = os.environ.get("SPREADSHEET_ID")               # Google Sheet ID
SERVICE_JSON   = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")  # service account JSON (როგორც სტრინგი)

missing = [k for k,v in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "APP_BASE_URL": APP_BASE_URL,
    "SPREADSHEET_ID": SHEET_ID,
    "GOOGLE_SERVICE_ACCOUNT_JSON": SERVICE_JSON,
}.items() if not v]
if missing:
    raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

SERVICE_INFO = json.loads(SERVICE_JSON)

# =========================
# Flask + TeleBot (webhook)
# =========================
app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True, num_threads=4, skip_pending=True)

# =========================
# Google Sheets helper-ები
# =========================
def _gc_client():
    return gspread.service_account_from_dict(SERVICE_INFO)

def _open_hotels_ws():
    """
    Worksheet, სადაც არის სასტუმროების კატალოგი.
    ჩამოყალიბებული სვეტები:
      - name_en
      - address_ka
      - status           (done/surveyed/completed/აღებულია/გაკეთებულია)
      - comment
    თუ worksheet სხვა სახელით გაქვს, ჩაანაცვლე აქ.
    """
    gc = _gc_client()
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet("Hotels")

def _open_leads_ws():
    """
    Worksheet, სადაც ახალი ჩანაწერები/კითხვარის პასუხები იწერება.
    მინიმუმ ეს სვეტები შექმენი ამ რიგით:
      created_at | agent_username | hotel_name_en | address_ka | matched | matched_comment | answers
    """
    gc = _gc_client()
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet("Leads")

# მცირე cache, რომ ყოველ მესიჯზე არ წავიკითხოთ მთელი შიტი
_HOTELS_CACHE: Dict[str, Any] = {"rows": [], "ts": 0}
_CACHE_TTL_SEC = 120

def load_hotels(force: bool = False) -> List[Dict[str, Any]]:
    now = time.time()
    if (not force) and _HOTELS_CACHE["rows"] and (now - _HOTELS_CACHE["ts"] < _CACHE_TTL_SEC):
        return _HOTELS_CACHE["rows"]
    ws = _open_hotels_ws()
    rows = ws.get_all_records()  # list[dict]
    _HOTELS_CACHE["rows"] = rows
    _HOTELS_CACHE["ts"] = now
    logger.info(f"Loaded {len(rows)} hotels from sheet.")
    return rows

def append_lead_row(data: Dict[str, Any]):
    ws = _open_leads_ws()
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

# =========================
# Session (FSM)
# =========================
@dataclass
class Session:
    stage: str = "idle"  # idle -> ask_name -> ask_address -> checking -> suggest -> ready_to_start -> questionnaire
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
# UI helpers
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
# ძიების ლოგიკა
# =========================
def normalize(s: str) -> str:
    return (s or "").strip().lower()

def find_best_hotel(hotel_name_en: str, address_ka: str) -> Tuple[Optional[Dict[str, Any]], int, int]:
    rows = load_hotels()
    names = [r.get("name_en", "") for r in rows]
    addrs = [r.get("address_ka", "") for r in rows]

    name_match = process.extractOne(hotel_name_en, names, scorer=fuzz.token_set_ratio)
    addr_match = process.extractOne(address_ka,   addrs, scorer=fuzz.token_set_ratio)

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
            alt_name_score = int(fuzz.token_set_ratio(hotel_name_en, alt.get("name_en", "")))
            cur_addr_score = int(fuzz.token_set_ratio(address_ka, (best or {}).get("address_ka", ""))) if best else 0
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
        "გამარჯობა! მე ვარ OK TV-ის HotelClaimBot.\nაირჩიე მოქმედება:",
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
        "გთხოვ, შეიყვანე სასტუმროს **ოფიციალური სახელი ინგლისურად** (მაგ.: *Radisson Blu Batumi*).",
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda m: get_session(m.chat.id).stage == "ask_name")
def ask_address_next(message):
    chat_id = message.chat.id
    s = get_session(chat_id)
    s.hotel_name_en = message.text.strip()
    s.stage = "ask_address"
    bot.send_message(
        chat_id,
        "ახლა შეიყვანე **ოფიციალური მისამართი ქართულად** (მაგ.: *ბათუმი, შ. ხიმშიაშვილის ქ. 1*).",
        parse_mode="Markdown"
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

    EXACT = 90
    SIMILAR = 75

    if bm:
        name_en = bm.get("name_en", "")
        addr_ka = bm.get("address_ka", "")
        status  = normalize(bm.get("status", ""))
        comment = bm.get("comment", "")

        # უკვე გამოკითხულია → ავტომატური დასრულება
        if nscore >= EXACT and ascore >= EXACT and status in ("done", "surveyed", "completed", "აღებულია", "გაკეთებულია"):
            txt = (f"❌ ეს სასტუმრო უკვე **გამოკითხულია**.\n"
                   f"სახელი: {name_en}\nმისამართი: {addr_ka}\n\n"
                   f"კომენტარი (შიტიდან): {comment if comment else '—'}\n\n"
                   f"ჩატი ავტომატურად დასრულდა.")
            bot.send_message(chat_id, txt, reply_markup=main_menu(), parse_mode="Markdown")
            SESSIONS[chat_id] = Session(stage="idle")
            return

        # მსგავსი → შემოთავაზება
        if nscore >= SIMILAR or ascore >= SIMILAR:
            im = InlineKeyboardMarkup()
            im.add(
                InlineKeyboardButton("✔️ დიახ, ეს სასტუმროა", callback_data="confirm_match"),
                InlineKeyboardButton("✏️ არა, სხვაა", callback_data="reject_match")
            )
            txt = (f"მოვძებნე **მსგავსი** ჩანაწერი. ხომ არ გულისხმობ ამას?\n\n"
                   f"სახელი: *{name_en}*  (ქულა: {nscore})\n"
                   f"მისამართი: *{addr_ka}* (ქულა: {ascore})")
            bot.send_message(chat_id, txt, reply_markup=im, parse_mode="Markdown")
            s.stage = "suggest"
            return

    # ვერ ვიპოვეთ → „სტარტი“
    bot.send_message(
        chat_id,
        "ამ სახელზე/მისამართზე **ზუსტი ჩანაწერი ვერ ვიპოვე**.\n"
        "შეგიძლია დაუკავშირდე ამ სასტუმროს, ან გააგრძელო კითხვარი ჩვენს შიტში ჩასაწერად.\n\n"
        "გაგრძელებისთვის დააჭირე „▶️ სტარტი“.",
        reply_markup=start_menu()
    )
    s.stage = "ready_to_start"

@bot.callback_query_handler(func=lambda c: c.data in ("confirm_match", "reject_match"))
def on_suggestion_choice(call):
    chat_id = call.message.chat.id
    s = get_session(chat_id)

    if call.data == "confirm_match" and s.best_match:
        bm = s.best_match
        status  = normalize(bm.get("status", ""))
        comment = bm.get("comment", "")
        name_en = bm.get("name_en", "")
        addr_ka = bm.get("address_ka", "")

        if status in ("done", "surveyed", "completed", "აღებულია", "გაკეთებულია"):
            bot.edit_message_text(
                chat_id=chat_id, message_id=call.message.message_id,
                text=(f"❌ ეს სასტუმრო უკვე **გამოკითხულია**.\n"
                      f"სახელი: {name_en}\nმისამართი: {addr_ka}\n\n"
                      f"კომენტარი (შიტიდან): {comment if comment else '—'}\n\n"
                      f"ჩატი ავტომატურად დასრულდა."),
                parse_mode="Markdown"
            )
            bot.send_message(chat_id, "დაბრუნდი მთავარ მენიუში.", reply_markup=main_menu())
            SESSIONS[chat_id] = Session(stage="idle")
            return
        else:
            bot.edit_message_text(
                chat_id=chat_id, message_id=call.message.message_id,
                text=("ეს ჩანაწერი **არსებობს**, მაგრამ დასრულებულად არ არის მონიშნული.\n"
                      "თუ ეს სასტუმროა, შეგიძლია გააგრძელო მონაცემების შევსება.\n"
                      "დააჭირე „▶️ სტარტი“."),
                parse_mode="Markdown"
            )
            s.stage = "ready_to_start"
            bot.send_message(chat_id, "გაგრძელება:", reply_markup=start_menu())
            return

    # უარყოფილი ან ვერ ვიპოვეთ → სტარტი
    bot.edit_message_text(
        chat_id=chat_id, message_id=call.message.message_id,
        text=("გასაგებია — გავაგრძელოთ ახალი ჩანაწერის შექმნა.\n"
              "დააჭირე „▶️ სტარტი“ რომ კითხვარი გააგრძელო."),
        parse_mode="Markdown"
    )
    s.stage = "ready_to_start"
    bot.send_message(chat_id, "გაგრძელება:", reply_markup=start_menu())

@bot.message_handler(func=lambda m: get_session(m.chat.id).stage == "ready_to_start" and m.text == "▶️ სტარტი")
def start_questionnaire(message):
    chat_id = message.chat.id
    s = get_session(chat_id)

    # კონტროლი — სახელიც და მისამართიც უნდა ჰქონდეს
    if not s.hotel_name_en or not s.address_ka:
        s.stage = "ask_name"
        bot.send_message(chat_id, "ჯერ შეიყვანე სასტუმროს ოფიციალური **სახელი ინგლისურად**.", parse_mode="Markdown")
        return

    # აქ იწყება შენი კითხ—რავი (მაგალითით)
    s.stage = "questionnaire"
    s.answers = {}

    bot.send_message(
        chat_id,
        ("კარგი, ვაგრძელებთ კითხვარს.\n"
         "_ქვემოთ არის მაგალითი 2 შეკითხვის; ჩაანაცვლე შენი სრული ბლოკით._\n\n"
         "Q1) რამდენი ნომერია სასტუმროში? (ჩაწერე რიცხვი)"),
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda m: get_session(m.chat.id).stage == "questionnaire" and "Q1" not in get_session(m.chat.id).answers)
def q1_rooms(message):
    chat_id = message.chat.id
    s = get_session(chat_id)
    s.answers["Q1"] = message.text.strip()
    bot.send_message(chat_id, "Q2) ვინ არის საკონტაქტო პირი? (სახელი, ტელეფონი)")

@bot.message_handler(func=lambda m: get_session(m.chat.id).stage == "questionnaire" and "Q1" in get_session(m.chat.id).answers and "Q2" not in get_session(m.chat.id).answers)
def q2_contact(message):
    chat_id = message.chat.id
    s = get_session(chat_id)
    s.answers["Q2"] = message.text.strip()

    # დასრულება — ჩაწერა Leads-ში
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
        bot.send_message(chat_id, "✅ ინფორმაცია წარმატებით შეინახა შიტში. მადლობა!", reply_markup=main_menu())
    except Exception as e:
        logger.exception(e)
        bot.send_message(chat_id, "⚠️ ჩაწერის შეცდომა Google Sheets-ში. სცადე ხელახლა ან მოგვწერე.", reply_markup=main_menu())

    SESSIONS[chat_id] = Session(stage="idle")

# fallback
@bot.message_handler(content_types=['text'])
def fallback(message):
    chat_id = message.chat.id
    s = get_session(chat_id)
    if s.stage == "idle":
        bot.send_message(chat_id, "აირჩიე მოქმედება მენიუდან.", reply_markup=main_menu())
    else:
        bot.send_message(chat_id, "გაგვიზიარე მოსალოდნელი ინფორმაცია ან დაბრუნდი მენიუში.", reply_markup=main_menu())

# =========================
# Flask routes (webhook + health)
# =========================
@app.route("/", methods=["GET"])
def health():
    return "OK TV HotelClaimBot — alive", 200

# ორივე მისამართი მივიღოთ — /<TOKEN> და /webhook/<TOKEN>
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.data.decode("utf-8"))
        bot.process_new_updates([update])
        return "OK", 200
    abort(403)

# =========================
# Webhook რეგისტრაცია
# =========================
def set_webhook():
    try:
        url_plain   = f"{APP_BASE_URL}/{TELEGRAM_TOKEN}"
        url_webhook = f"{APP_BASE_URL}/webhook/{TELEGRAM_TOKEN}"

        # ძველი წაშალე, მერე დააყენე (ორი გზაც დავარეგისტრიროთ — რომელი დაგიწერია BotFather-ში, ორივე იმუშავებს)
        bot.remove_webhook()
        time.sleep(1.0)
        ok1 = bot.set_webhook(url=url_plain, max_connections=4, allowed_updates=["message", "callback_query"])
        # პარალელურად ალტერნატიული მისამართიც
        ok2 = bot.set_webhook(url=url_webhook, max_connections=4, allowed_updates=["message", "callback_query"])
        logger.info(f"Webhook set to {url_plain}: {ok1} | {url_webhook}: {ok2}")
    except Exception as e:
        logger.exception("Failed to set webhook: %s", e)

# gunicorn-ის წამოდგომისას ერთხელ გაეშვას
set_webhook()

# app-ს იყენებს gunicorn:
# Start command on Render:
#   gunicorn telegram_hotel_booking_bot:app --bind 0.0.0.0:$PORT --timeout 120
