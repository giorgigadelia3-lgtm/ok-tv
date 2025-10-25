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

# ---------------------------
# კონფიგი და საწყისი დაყენება
# ---------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hotel-bot")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
APP_BASE_URL   = os.environ.get("APP_BASE_URL")   # e.g. https://ok-tv-1.onrender.com
SHEET_ID       = os.environ.get("SPREADSHEET_ID") # Google Sheet ID
SERVICE_JSON   = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")  # JSON string

if not (TELEGRAM_TOKEN and APP_BASE_URL and SHEET_ID and SERVICE_JSON):
    raise RuntimeError("One or more required env vars are missing.")

SERVICE_INFO = json.loads(SERVICE_JSON)

# Flask + TeleBot (webhook mode)
app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True, num_threads=3, skip_pending=True)

# ---------------------------
# Google Sheets helper-ები
# ---------------------------
def _gc_client():
    gc = gspread.service_account_from_dict(SERVICE_INFO)
    return gc

def _open_hotels_ws():
    """
    გახსენი Worksheet, რომელშიც სასტუმროებია.
    დააყენე ზუსტი სახელწოდება, თუ სხვაგვარად გქვია.
    """
    gc = _gc_client()
    sh = gc.open_by_key(SHEET_ID)
    # ❗️ჩაანაცვლე, თუ სხვა worksheet-ს იყენებ:
    ws = sh.worksheet("Hotels")  # Columns: name_en | address_ka | status | comment
    return ws

def _open_leads_ws():
    """
    Worksheet სადაც ჩაწერება ხდება ახალი ჩანაწერების (არაგამოკითხული სასტუმროები + შეფასების პასუხები).
    """
    gc = _gc_client()
    sh = gc.open_by_key(SHEET_ID)
    # ❗️ჩაანაცვლე, თუ სხვა worksheet-ს იყენებ:
    ws = sh.worksheet("Leads")
    return ws

# მარტივი cache რომ შიტი ყოველ მესიჯზე არ წავიკითხოთ
_HOTELS_CACHE: Dict[str, Any] = {"rows": [], "ts": 0}
_CACHE_TTL = 120  # sec

def load_hotels(force: bool = False) -> List[Dict[str, Any]]:
    now = time.time()
    if (not force) and (now - _HOTELS_CACHE["ts"] < _CACHE_TTL) and _HOTELS_CACHE["rows"]:
        return _HOTELS_CACHE["rows"]
    ws = _open_hotels_ws()
    rows = ws.get_all_records()  # list of dicts
    _HOTELS_CACHE["rows"] = rows
    _HOTELS_CACHE["ts"] = now
    logger.info(f"Loaded {len(rows)} hotels from sheet.")
    return rows

def append_lead_row(data: Dict[str, Any]):
    ws = _open_leads_ws()
    # სტანდარტული ველები — სურვილით დაამატე/შეცვალე
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

# ---------------------------
# სესიის მდგომარეობა (FSM)
# ---------------------------
@dataclass
class Session:
    stage: str = "idle"  # idle -> ask_name -> ask_address -> checking -> suggest -> ready_to_start -> questionnaire
    hotel_name_en: Optional[str] = None
    address_ka: Optional[str] = None
    # მოძიებული მსგავსი/ზუსტი ჰიტები
    best_match: Optional[Dict[str, Any]] = None
    best_score_name: int = 0
    best_score_addr: int = 0
    # „შენი ძველი კითხვარის“ პასუხები
    answers: Dict[str, Any] = field(default_factory=dict)

SESSIONS: Dict[int, Session] = {}  # chat_id -> Session

def get_session(chat_id: int) -> Session:
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = Session()
    return SESSIONS[chat_id]

# ---------------------------
# UI helpers
# ---------------------------
def main_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("🔍 მოძებნა"))
    return kb

def start_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("▶️ სტარტი"))
    kb.add(KeyboardButton("⬅️ უკან მენიუში"))
    return kb

# ---------------------------
# სერვისული ლოგიკა — ძიება
# ---------------------------
def normalize(s: str) -> str:
    return (s or "").strip().lower()

def find_best_hotel(hotel_name_en: str, address_ka: str) -> Tuple[Optional[Dict[str, Any]], int, int]:
    """ მოძებნე საუკეთესო დამთხვევა სახელით და მისამართით rapidfuzz-ით. """
    rows = load_hotels()
    names = [r.get("name_en", "") for r in rows]
    addrs = [r.get("address_ka", "") for r in rows]

    name_match = process.extractOne(
        hotel_name_en, names,
        scorer=fuzz.token_set_ratio
    )
    addr_match = process.extractOne(
        address_ka, addrs,
        scorer=fuzz.token_set_ratio
    )

    bm = None
    name_score = 0
    addr_score = 0

    if name_match:
        name_str, name_score, name_idx = name_match
        bm = rows[name_idx]
        name_score = int(name_score)

    if addr_match:
        addr_str, addr_score, addr_idx = addr_match
        addr_score = int(addr_score)
        # თუ მისამართი სხვა რიგზე დაემთხვა, ავიღოთ ის, რომელიც უკეთესი ჯამური იქნება
        if bm is None or addr_idx != rows.index(bm):
            # შევამოწმოთ, რომელს აქვს მეტი „საერთო“ ქულა ჯამში
            alt = rows[addr_idx]
            # ალტერნატიული სახელის ქულა
            alt_name_score = fuzz.token_set_ratio(
                hotel_name_en, alt.get("name_en", "")
            )
            # გადავწყვიტოთ საუკეთესო
            if (alt_name_score + addr_score) > (name_score + (fuzz.token_set_ratio(address_ka, bm.get("address_ka", "")) if bm else 0)):
                bm = alt
                name_score = int(alt_name_score)

    return bm, name_score, addr_score

# ---------------------------
# კომანდები და ჰენდლერები
# ---------------------------
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    SESSIONS[chat_id] = Session(stage="idle")  # reset
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

    # მოძებნა შიტში
    bm, nscore, ascore = find_best_hotel(s.hotel_name_en, s.address_ka)
    s.best_match = bm
    s.best_score_name = nscore
    s.best_score_addr = ascore

    # ზუსტი/ფაქტობრივი ზღვარი — შეგიძლია დაარეგულირო
    EXACT = 90
    SIMILAR = 75

    if bm:
        name_en = bm.get("name_en", "")
        addr_ka = bm.get("address_ka", "")
        status  = normalize(bm.get("status", ""))  # expected: "surveyed" / "done" etc.
        comment = bm.get("comment", "")

        # 1) უკვე გამოკითხულია — მაღალი დამთხვევა სახელზეც და მისამართზეც
        if nscore >= EXACT and ascore >= EXACT and status in ("done", "surveyed", "completed", "აღებულია", "გაკეთებულია"):
            txt = (f"❌ ეს სასტუმრო უკვე **გამოკითხულია**.\n"
                   f"სახელი: {name_en}\nმისამართი: {addr_ka}\n\n"
                   f"კომენტარი (შიტიდან): {comment if comment else '—'}\n\n"
                   f"ჩატი ავტომატურად დასრულდა.")
            bot.send_message(chat_id, txt, reply_markup=main_menu(), parse_mode="Markdown")
            SESSIONS[chat_id] = Session(stage="idle")
            return

        # 2) შესაძლოა იგივეა — შევთავაზოთ „ეს ხომ არ გაქვს მხედველობაში?“
        if nscore >= SIMILAR or ascore >= SIMILAR:
            im = InlineKeyboardMarkup()
            im.add(
                InlineKeyboardButton("✔️ დიახ, ეს სასტუმროა", callback_data="confirm_match"),
                InlineKeyboardButton("✏️ არა, სხვაა", callback_data="reject_match")
            )
            txt = (f"მივაგენით **მსგავს** ჩანაწერს. ხომ არ გულისხმობ ამას?\n\n"
                   f"სახელი: *{name_en}*  (ქულა: {nscore})\n"
                   f"მისამართი: *{addr_ka}* (ქულა: {ascore})")
            bot.send_message(chat_id, txt, reply_markup=im, parse_mode="Markdown")
            s.stage = "suggest"
            return

    # 3) ვერ ვიპოვეთ — ვაძლევთ გაგრძელების საშუალებას
    bot.send_message(
        chat_id,
        "ამ სახელზე/მისამართზე **ზუსტი ჩანაწერი ვერ ვიპოვე**.\n"
        "შეგიძლია დაუკავშირდე ამ სასტუმროს ან გააგრძელო ღირსეული შეთავაზების შევსება.\n\n"
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
            # უკვე გამოკითხულია -> ავტომატურად დასრულდეს
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
            # მსგავსი მაგრამ არა „დასრულებული“ -> ვანახებთ, რომ შესაძლებელია განვაგრძოთ
            bot.edit_message_text(
                chat_id=chat_id, message_id=call.message.message_id,
                text=("ეს ჩანაწერი **არსებობს**, მაგრამ არ არის დასრულებულად მონიშნული.\n"
                      "თუ ეს სასტუმროა, შეგიძლია გააგრძელო მონაცემების შევსება.\n"
                      "დააჭირე „▶️ სტარტი“."),
                parse_mode="Markdown"
            )
            s.stage = "ready_to_start"
            bot.send_message(chat_id, "გაგრძელება:", reply_markup=start_menu())
            return

    # უარყოფილია ან ვერ ვიპოვეთ -> მისცეს სტარტი
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

    # უსაფრთხოება: კიდევ ერთხელ დავაზღვიოთ, რომ სახელიც და მისამართიც შევსებულია
    if not s.hotel_name_en or not s.address_ka:
        s.stage = "ask_name"
        bot.send_message(chat_id, "ჯერ შეიყვანე სასტუმროს ოფიციალური **სახელი ინგლისურად**.", parse_mode="Markdown")
        return

    # აქ იწყება **შენი არსებული კითხვარი**.
    # --------------------------------------------------
    # ქვემოთ არის მინიმალური, პროფესიონალურად მოწყობილი შაბლონი,
    # სადაც მარტივად ჩაანაცვლებ შენს რეალურ კითხვებს/დამუშავებას.
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

    # --- აქ მოხდა კითხვარის დასასრული (შენი ვერსიაში ჩაამატე ყველაფერი რაც გაქვს) ---
    # ჩაწერა Leads-ში
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
        bot.send_message(chat_id, "⚠️ ჩაწერის შეცდომა Google Sheets-ში. სცადე ხელახლა ან გამოგვიგზავე სკრინი.", reply_markup=main_menu())

    SESSIONS[chat_id] = Session(stage="idle")

# fallback — ტექსტები, რომლებსაც Stage არ ემთხვევა
@bot.message_handler(content_types=['text'])
def fallback(message):
    chat_id = message.chat.id
    s = get_session(chat_id)
    if s.stage in ("idle",):
        bot.send_message(chat_id, "აირჩიე მოქმედება მენიუდან.", reply_markup=main_menu())
    else:
        bot.send_message(chat_id, "გაგვიგზავნე მოსალოდნელი ინფორმაცია ან დაბრუნდი მენიუში.", reply_markup=main_menu())

# ---------------------------
# Webhook სერვერი
# ---------------------------
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        json_str = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return "OK", 200
    else:
        abort(403)

@app.route("/", methods=["GET"])
def health():
    return "OK TV HotelClaimBot — alive", 200

def set_webhook():
    url = f"{APP_BASE_URL}/{TELEGRAM_TOKEN}"
    ok = bot.set_webhook(url=url, max_connections=3, allowed_updates=["message","callback_query"])
    logger.info(f"Webhook set to {url}: {ok}")

# Render იწყებს gunicorn-ით; set_webhook გამოვიძახოთ ერთხელ
set_webhook()

# app ობიექტს იყენებს gunicorn
# gunicorn startcmd:  gunicorn telegram_hotel_booking_bot:app --bind 0.0.0.0:$PORT --timeout 120
