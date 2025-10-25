# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-
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
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process

# -------------------- ლოგირება --------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hotel-bot")

# -------------------- ENV -------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
APP_BASE_URL   = os.environ.get("APP_BASE_URL")                 # напр: https://ok-tv-1.onrender.com
SHEET_ID       = os.environ.get("SPREADSHEET_ID")               # Google Sheet ID
SERVICE_JSON   = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")  # service account JSON (string)

missing = [k for k,v in {
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "APP_BASE_URL": APP_BASE_URL,
    "SPREADSHEET_ID": SHEET_ID,
    "GOOGLE_SERVICE_ACCOUNT_JSON": SERVICE_JSON,
}.items() if not v]
if missing:
    raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

SERVICE_INFO = json.loads(SERVICE_JSON)

# -------------------- Flask + TeleBot --------------
app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True, num_threads=4, skip_pending=True)

# -------------------- Google Sheets ----------------
"""
Google Sheet:  “HotelClaimBot_Data”
TAB/Worksheet: “1 ცხრილი”  (ზუსტად ასე წერია შენთან)
Columns (A..F): hotel name | address | comment | Contact | agent | name
"""
def _gc_client():
    creds = Credentials.from_service_account_info(
        SERVICE_INFO,
        scopes=["https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"],
    )
    return gspread.authorize(creds)

def _open_ws():
    gc = _gc_client()
    sh = gc.open_by_key(SHEET_ID)
    # ⚠️ აქაა მთავარი – შენთან worksheet ჰქვია „1 ცხრილი“
    return sh.worksheet("1 ცხრილი")

# მარტივი cache რომ ყოველ მესიჯზე არ წავიკითხოთ მთელი ფურცელი
_SHEET_CACHE: Dict[str, Any] = {"rows": [], "ts": 0}
_CACHE_TTL_SEC = 90

def load_rows(force: bool = False) -> List[Dict[str, Any]]:
    now = time.time()
    if (not force) and _SHEET_CACHE["rows"] and (now - _SHEET_CACHE["ts"] < _CACHE_TTL_SEC):
        return _SHEET_CACHE["rows"]
    ws = _open_ws()
    rows = ws.get_all_records()  # list[dict] with keys exactly as headers
    _SHEET_CACHE["rows"] = rows
    _SHEET_CACHE["ts"] = now
    logger.info(f"Loaded {len(rows)} rows from sheet.")
    return rows

def append_row_new(hotel_name: str, address: str, comment: str, contact: str, agent_name: str):
    ws = _open_ws()
    # Columns: hotel name | address | comment | Contact | agent | name
    timestamp = time.strftime("%d.%m.%y, %H:%M")
    ws.append_row([hotel_name, address, comment, contact, agent_name, timestamp],
                  value_input_option="USER_ENTERED")

# -------------------- Session (FSM) ----------------
@dataclass
class Session:
    stage: str = "idle"  # idle -> ask_name -> ask_address -> checking -> suggest -> ready_to_start -> confirm_fixed -> questionnaire
    hotel_name_en: Optional[str] = None
    address_ka: Optional[str] = None
    best_match: Optional[Dict[str, Any]] = None
    score_name: int = 0
    score_addr: int = 0
    answers: Dict[str, Any] = field(default_factory=dict)

SESSIONS: Dict[int, Session] = {}

def sess(chat_id: int) -> Session:
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = Session()
    return SESSIONS[chat_id]

# -------------------- UI --------------------------
def kb_main() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("🔍 მოძებნა"))
    return kb

def kb_start() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("▶️ სტარტი"))
    kb.add(KeyboardButton("⬅️ უკან მენიუში"))
    return kb

# -------------------- Helpers ---------------------
def norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())

def find_best(hotel_name_en: str, address_ka: str) -> Tuple[Optional[Dict[str, Any]], int, int]:
    rows = load_rows()
    names = [norm(r.get("hotel name", "")) for r in rows]
    addrs = [norm(r.get("address", "")) for r in rows]

    # RapidFuzz top-1 search by both fields
    nm = process.extractOne(norm(hotel_name_en), names, scorer=fuzz.token_set_ratio)
    am = process.extractOne(norm(address_ka),   addrs, scorer=fuzz.token_set_ratio)

    best = None
    nscore = 0
    ascore = 0

    if nm:
        _, nscore, idx_n = nm
        best = rows[idx_n]
        nscore = int(nscore)

    if am:
        _, ascore, idx_a = am
        ascore = int(ascore)
        if best is None or idx_a != rows.index(best):
            alt = rows[idx_a]
            alt_name_score = int(fuzz.token_set_ratio(norm(hotel_name_en), norm(alt.get("hotel name",""))))
            cur_addr_score = int(fuzz.token_set_ratio(norm(address_ka), norm((best or {}).get("address","")))) if best else 0
            if (alt_name_score + ascore) > (nscore + cur_addr_score):
                best = alt
                nscore = alt_name_score

    return best, nscore, ascore

# -------------------- Bot handlers ----------------
@bot.message_handler(commands=['start'])
def cmd_start(message):
    SESSIONS[message.chat.id] = Session(stage="idle")
    bot.send_message(message.chat.id, "აირჩიე მოქმედება 👇", reply_markup=kb_main())

@bot.message_handler(func=lambda m: m.text == "⬅️ უკან მენიუში")
def back_to_menu(message):
    SESSIONS[message.chat.id] = Session(stage="idle")
    bot.send_message(message.chat.id, "დაბრუნდი მთავარ მენიუში.", reply_markup=kb_main())

@bot.message_handler(func=lambda m: m.text == "🔍 მოძებნა")
def search_entry(message):
    s = sess(message.chat.id)
    s.stage = "ask_name"
    bot.send_message(
        message.chat.id,
        "გთხოვ, შეიყვანე სასტუმროს <b>ოფიციალური სახელი ინგლისურად</b> (მაგ.: <i>Radisson Blu Batumi</i>).",
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda m: sess(m.chat.id).stage == "ask_name")
def ask_address(message):
    s = sess(message.chat.id)
    s.hotel_name_en = message.text.strip()
    s.stage = "ask_address"
    bot.send_message(
        message.chat.id,
        "ახლა შეიყვანე ამავე სასტუმროს <b>ოფიციალური მისამართი ქართულად</b> (ქალაქი, ქუჩა, №).",
        parse_mode="HTML"
    )

@bot.message_handler(func=lambda m: sess(m.chat.id).stage == "ask_address")
def check_sheet(message):
    chat_id = message.chat.id
    s = sess(chat_id)
    s.address_ka = message.text.strip()
    s.stage = "checking"

    best, nscore, ascore = find_best(s.hotel_name_en, s.address_ka)
    s.best_match, s.score_name, s.score_addr = best, nscore, ascore

    EXACT_BOTH = 95
    SIMILAR_ONE = 80

    if best:
        name = best.get("hotel name", "")
        addr = best.get("address", "")
        comment = best.get("comment", "") or "—"
        contact = best.get("Contact", "") or "—"
        agent = best.get("agent", "") or "—"

        if nscore >= EXACT_BOTH and ascore >= EXACT_BOTH:
            # ზუსტი დამთხვევა → უკვე გვაქვს ბაზაში → დასრულება
            bot.send_message(
                chat_id,
                (
                    "❌ <b>ეს სასტუმრო უკვე გამოკითხულია</b>.\n"
                    f"🏨 <b>{name}</b>\n"
                    f"📍 {addr}\n"
                    f"💬 კომენტარი: <i>{comment}</i>\n"
                    f"👤 აგენტი: {agent} | ☎️ {contact}\n\n"
                    "ჩატი დასრულდა."
                ),
                parse_mode="HTML",
                reply_markup=kb_main()
            )
            SESSIONS[chat_id] = Session(stage="idle")
            return

        if nscore >= SIMILAR_ONE or ascore >= SIMILAR_ONE:
            # მსგავსი ჩანაწერი → შევთავაზოთ დადასტურება
            im = InlineKeyboardMarkup()
            im.add(
                InlineKeyboardButton("✔️ დიახ, ეს სასტუმროა", callback_data="match_yes"),
                InlineKeyboardButton("✏️ არა, სხვაა", callback_data="match_no")
            )
            bot.send_message(
                chat_id,
                (
                    "მოვძებნე <b>მსგავსი</b> ჩანაწერი, ხომ არ გულისხმობ ამას?\n\n"
                    f"🏨 <b>{name}</b>  (ქულა სახელზე: {nscore})\n"
                    f"📍 {addr}  (ქულა მისამართზე: {ascore})\n"
                    f"💬 კომენტარი: <i>{comment}</i>"
                ),
                parse_mode="HTML",
                reply_markup=im
            )
            s.stage = "suggest"
            return

    # ვერ ვიპოვეთ (ზუსტი/მსგავსიც არა) → მივცეთ გაგრძელება
    bot.send_message(
        chat_id,
        (
            "ამ სახელზე/მისამართზე <b>ზუსტი ჩანაწერი ვერ ვიპოვე</b>.\n"
            "შეგიძლია დაუკავშირდე ამ სასტუმროს, ან გააგრძელო ახალი ჩანაწერის შევსება.\n\n"
            "გაგრძელებისთვის დააჭირე <b>▶️ სტარტი</b>."
        ),
        parse_mode="HTML",
        reply_markup=kb_start()
    )
    s.stage = "ready_to_start"

@bot.callback_query_handler(func=lambda c: c.data in ("match_yes","match_no"))
def on_suggestion_choice(call):
    chat_id = call.message.chat.id
    s = sess(chat_id)

    if call.data == "match_yes" and s.best_match:
        # თუ მსგავსია, მაგრამ არ იყო EXACT → მაინც მივცეთ გაგრძელება (შეავსოს ახალი ინფორმაცია თუ საჭიროა)
        bot.edit_message_text(
            chat_id=chat_id, message_id=call.message.message_id,
            text=(
                "ეს ჩანაწერი <b>არსებობს</b> შიტში, მაგრამ ზუსტი დამთხვევა არ იყო.\n"
                "თუ გინდა, შეგიძლია გააგრძელო მონაცემების შევსება.\n"
                "დააჭირე <b>▶️ სტარტი</b>."
            ),
            parse_mode="HTML"
        )
        s.stage = "ready_to_start"
        bot.send_message(chat_id, "გაგრძელება:", reply_markup=kb_start())
        return

    # match_no ან საერთოდ ვერ იპოვეს → ახალი ჩანაწერის შექმნა
    bot.edit_message_text(
        chat_id=chat_id, message_id=call.message.message_id,
        text="გასაგებია — შევქმნათ ახალი ჩანაწერი.\nდააჭირე <b>▶️ სტარტი</b> რომ გაგრძელდეს.",
        parse_mode="HTML"
    )
    s.stage = "ready_to_start"
    bot.send_message(chat_id, "გაგრძელება:", reply_markup=kb_start())

@bot.message_handler(func=lambda m: sess(m.chat.id).stage == "ready_to_start" and m.text == "▶️ სტარტი")
def start_questionnaire(message):
    chat_id = message.chat.id
    s = sess(chat_id)

    # უსაფრთხოება — ორივე ველი უნდა გქონდეს შეყვანილი ძებნამდე
    if not s.hotel_name_en or not s.address_ka:
        s.stage = "ask_name"
        bot.send_message(chat_id, "ჯერ შეიყვანე სასტუმროს სახელი ინგლისურად.", parse_mode="HTML")
        return

    # დამატებითი კონტროლი: თანამშრომელმა იგივე სახელი/მისამართი შეიყვანოს დადასტურებისთვის
    s.stage = "confirm_fixed"
    bot.send_message(chat_id, "გაიმეორე სასტუმროს <b>ოფიციალური სახელი (EN)</b> დასადასტურებლად:", parse_mode="HTML")

@bot.message_handler(func=lambda m: sess(m.chat.id).stage == "confirm_fixed" and "fix_name" not in sess(m.chat.id).answers)
def confirm_name(message):
    s = sess(message.chat.id)
    s.answers["fix_name"] = message.text.strip()
    # შევადაროთ მოძიებულს (თუ იყო) ან პირველ შეყვანილს
    base = s.best_match.get("hotel name") if s.best_match else s.hotel_name_en
    if fuzz.token_set_ratio(norm(s.answers["fix_name"]), norm(base)) < 85:
        bot.send_message(message.chat.id,
                         "⚠️ შეყვანილი სახელი <b>არ ემთხვევა</b> მოძიებულს/შეყვანილს. გასწორე და კიდევ შეიყვანე.",
                         parse_mode="HTML")
        s.answers.pop("fix_name", None)
        return
    bot.send_message(message.chat.id, "ახლა ჩაწერე იგივე <b>მისამართი (KA)</b> დასადასტურებლად:", parse_mode="HTML")
    s.stage = "confirm_fixed_addr"

@bot.message_handler(func=lambda m: sess(m.chat.id).stage == "confirm_fixed_addr")
def confirm_addr(message):
    s = sess(message.chat.id)
    fix_addr = message.text.strip()
    base_addr = s.best_match.get("address") if s.best_match else s.address_ka
    if fuzz.token_set_ratio(norm(fix_addr), norm(base_addr)) < 85:
        bot.send_message(message.chat.id,
                         "⚠️ შეყვანილი მისამართი <b>არ ემთხვევა</b> მოძიებულს/შეყვანილს. გასწორე და თავიდან ჩაწერე.",
                         parse_mode="HTML")
        return

    # გავაგრძელოთ მინიმალური კითხვარი — (შენს ცხრილში არის: comment, Contact, agent)
    s.answers["fix_addr"] = fix_addr
    s.stage = "q_comment"
    bot.send_message(message.chat.id, "📝 კომენტარი (არასაექსპრესიად, სურვილის მიხედვით — ან ჩაწერე „—“):")

@bot.message_handler(func=lambda m: sess(m.chat.id).stage == "q_comment")
def q_comment(message):
    s = sess(message.chat.id)
    s.answers["comment"] = message.text.strip()
    s.stage = "q_contact"
    bot.send_message(message.chat.id, "☎️ საკონტაქტო ნომერი/სახელი (მაგ.: 555123456 გიორგი):")

@bot.message_handler(func=lambda m: sess(m.chat.id).stage == "q_contact")
def q_contact(message):
    chat_id = message.chat.id
    s = sess(chat_id)
    s.answers["contact"] = message.text.strip()
    agent = (message.from_user.username and f"@{message.from_user.username}") or f"id:{message.from_user.id}"

    # ჩავწეროთ იმავე "1 ცხრილი" worksheet-ში ახალ სტრიქონად
    try:
        append_row_new(
            hotel_name = s.hotel_name_en,
            address    = s.address_ka,
            comment    = s.answers.get("comment","—"),
            contact    = s.answers.get("contact","—"),
            agent_name = agent
        )
        bot.send_message(chat_id, "✅ ინფორმაცია წარმატებით ჩაიწერა Google Sheet-ში. მადლობა!", reply_markup=kb_main())
    except Exception as e:
        logger.exception("Append error: %s", e)
        bot.send_message(chat_id, "⚠️ ჩაწერის შეცდომა Google Sheets-ში. სცადე კიდევ ერთხელ.", reply_markup=kb_main())

    SESSIONS[chat_id] = Session(stage="idle")

# fallback
@bot.message_handler(content_types=['text'])
def fallback(message):
    s = sess(message.chat.id)
    if s.stage == "idle":
        bot.send_message(message.chat.id, "აირჩიე მოქმედება მენიუდან.", reply_markup=kb_main())
    else:
        bot.send_message(message.chat.id, "გაგვიზიარე მოსალოდნელი ინფორმაცია ან დაბრუნდი მენიუში.", reply_markup=kb_main())

# -------------------- Flask routes -----------------
@app.route("/", methods=["GET"])
def health():
    return "HotelClaimBot — alive", 200

# ვებუქი — მხოლოდ ერთი მისამართი, რომ 429 აღარ დაგივარდეს
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    if request.headers.get('content-type') == 'application/json':
        update = telebot.types.Update.de_json(request.data.decode("utf-8"))
        bot.process_new_updates([update])
        return "OK", 200
    abort(403)

# -------------------- Webhook რეგისტრაცია ----------
def set_webhook():
    try:
        url = f"{APP_BASE_URL.rstrip('/')}/{TELEGRAM_TOKEN}"
        bot.remove_webhook()
        time.sleep(1.0)
        ok = bot.set_webhook(url=url, max_connections=5, allowed_updates=["message", "callback_query"])
        logger.info(f"Webhook set to {url}: {ok}")
    except Exception as e:
        logger.exception("Failed to set webhook: %s", e)

# gunicorn-ის წამოდგომისას ერთხელ გაეშვას
set_webhook()
