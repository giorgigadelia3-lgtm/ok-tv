import os
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple

from flask import Flask, request
import telebot
from telebot import types

# ===== Google Sheets =====
import gspread
from google.oauth2.service_account import Credentials

# ===== Fuzzy matching =====
from rapidfuzz import fuzz

# ---------------------------
# LOGGING
# ---------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ---------------------------
# ENV / CONFIG
# ---------------------------
# Required
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
SHEET_KEY = os.environ["GSPREAD_SHEET_KEY"]  # Spreadsheet ID

# Optional
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
AUTO_SET_WEBHOOK = os.getenv("AUTO_SET_WEBHOOK", "1") == "1"

HOTELS_WS = os.getenv("HOTELS_WORKSHEET", "Hotels")
RESPONSES_WS = os.getenv("RESPONSES_WORKSHEET", "Responses")

COL_NAME_EN = os.getenv("HOTELS_NAME_COLUMN", "name_en")
COL_ADDR_GE = os.getenv("HOTELS_ADDRESS_COLUMN", "address_ge")
COL_STATUS = os.getenv("HOTELS_STATUS_COLUMN", "status")
COL_COMMENT = os.getenv("HOTELS_COMMENT_COLUMN", "comment")

# Fuzzy thresholds
EXACT_THRESHOLD = int(os.getenv("MATCH_EXACT_THRESHOLD", "90"))      # must match to confirm
SUGGEST_THRESHOLD = int(os.getenv("MATCH_SUGGEST_THRESHOLD", "70"))  # show suggestions above this

# ---------------------------
# BOT / WEB
# ---------------------------
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ---------------------------
# STATE
# ---------------------------
@dataclass
class PendingHotel:
    name_en: Optional[str] = None
    addr_ge: Optional[str] = None
    candidate_from_sheet: Optional[Dict[str, Any]] = None  # best suggestion (if any)
    found_status: Optional[str] = None  # "surveyed" | "unsurveyed" | "not_found"

@dataclass
class SurveyState:
    step: str = "IDLE"
    pending: PendingHotel = field(default_factory=PendingHotel)
    answers: Dict[str, Any] = field(default_factory=dict)
    current_q_idx: int = 0

user_state: Dict[int, SurveyState] = {}  # chat_id -> state


# ---------------------------
# GOOGLE SHEETS HELPERS
# ---------------------------
def gsheet_client():
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    log.info("✅ Google Sheets connected.")
    return gc

def get_hotels_records() -> List[Dict[str, Any]]:
    gc = gsheet_client()
    ws = gc.open_by_key(SHEET_KEY).worksheet(HOTELS_WS)
    return ws.get_all_records()  # list[dict] based on header row

def ensure_responses_ws():
    gc = gsheet_client()
    sh = gc.open_by_key(SHEET_KEY)
    try:
        ws = sh.worksheet(RESPONSES_WS)
        return ws
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=RESPONSES_WS, rows=2000, cols=50)
        ws.update("1:1", [[
            "timestamp", "name_en", "address_ge",
            "matched_name_en", "matched_address_ge", "matched_status"
        ]])
        return ws

def ensure_headers(ws, required_headers: List[str]):
    headers = ws.row_values(1)
    if not headers:
        ws.update("1:1", [required_headers])
        return required_headers

    changed = False
    for h in required_headers:
        if h not in headers:
            headers.append(h)
            changed = True
    if changed:
        ws.update("1:1", [headers])
    return headers

def append_response_row(state: SurveyState):
    ws = ensure_responses_ws()
    # make sure all question keys exist in header
    q_keys = [k for k, _ in QUESTIONS]
    base = [
        "timestamp", "name_en", "address_ge",
        "matched_name_en", "matched_address_ge", "matched_status"
    ]
    headers = ensure_headers(ws, base + q_keys)

    row_map = {h: "" for h in headers}
    row_map["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
    row_map["name_en"] = state.pending.name_en or ""
    row_map["address_ge"] = state.pending.addr_ge or ""
    row_map["matched_name_en"] = (state.pending.candidate_from_sheet or {}).get(COL_NAME_EN, "")
    row_map["matched_address_ge"] = (state.pending.candidate_from_sheet or {}).get(COL_ADDR_GE, "")
    row_map["matched_status"] = state.pending.found_status or ""
    for k, v in state.answers.items():
        row_map[k] = v

    ws.append_row([row_map[h] for h in headers], value_input_option="USER_ENTERED")


# ---------------------------
# MATCHING
# ---------------------------
def _normalize(s: str) -> str:
    return "".join(ch for ch in s.lower().strip() if ch.isalnum() or ch.isspace())

def _is_surveyed(status_cell: str) -> bool:
    s = (status_cell or "").strip().lower()
    return s in {"x", "✓", "✅", "yes", "true", "done", "surveyed"}

def match_hotels(name_en: str, addr_ge: str):
    """
    Returns: (found_status, best_row_or_None, best_score_int, suggestions_list[(score,name_score,addr_score,row),...])
    found_status ∈ {"surveyed","unsurveyed","not_found"}
    """
    records = get_hotels_records()
    if not records:
        return "not_found", None, 0, []

    cands = []
    n1 = _normalize(name_en)
    a1 = _normalize(addr_ge)

    for row in records:
        r_name = str(row.get(COL_NAME_EN, "") or "")
        r_addr = str(row.get(COL_ADDR_GE, "") or "")

        name_score = fuzz.WRatio(n1, _normalize(r_name))
        addr_score = fuzz.WRatio(a1, _normalize(r_addr)) if a1 else 0
        combo = int(0.7 * name_score + 0.3 * addr_score)
        cands.append((combo, name_score, addr_score, row))

    cands.sort(key=lambda x: x[0], reverse=True)
    best_combo, best_name, best_addr, best_row = cands[0]

    suggestions = [c for c in cands[:5] if c[0] >= SUGGEST_THRESHOLD]
    status_cell = str(best_row.get(COL_STATUS, "") or "")

    if best_combo >= EXACT_THRESHOLD:
        return ("surveyed" if _is_surveyed(status_cell) else "unsurveyed"), best_row, best_combo, suggestions
    else:
        # not a strong match
        if suggestions:
            # might still show top suggestion as hint
            return ("surveyed" if _is_surveyed(status_cell) else "not_found"), best_row, best_combo, suggestions
        return "not_found", None, 0, []


# ---------------------------
# QUESTIONS (can be overridden via QUESTIONS_JSON env)
# ---------------------------
def load_questions_from_env() -> List[Tuple[str, str]]:
    raw = os.getenv("QUESTIONS_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        out = []
        for item in data:
            # item: {"key": "...", "text": "..."}
            out.append((item["key"], item["text"]))
        return out
    except Exception as e:
        log.warning("QUESTIONS_JSON parse error: %s", e)
        return []

QUESTIONS: List[Tuple[str, str]] = load_questions_from_env() or [
    ("contact_person", "კონტაქტის სახელი და გვარი?"),
    ("phone", "ტელეფონის ნომერი?"),
    ("rooms_count", "რამდენი ნომერია სასტუმროში?"),
    ("email", "ელფოსტა?"),
    ("notes", "დამატებითი კომენტარი?"),
]


# ---------------------------
# KEYBOARDS
# ---------------------------
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(types.KeyboardButton("🔎 მოძებნა"), types.KeyboardButton("🧾 Start"))
    return kb

def start_only_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.row(types.KeyboardButton("🧾 Start"), types.KeyboardButton("↩️ დაბრუნება მენიუში"))
    return kb


# ---------------------------
# BOT HANDLERS
# ---------------------------
@bot.message_handler(commands=["start"])
def on_start(msg: types.Message):
    st = user_state.setdefault(msg.chat.id, SurveyState())
    st.step = "IDLE"
    st.pending = PendingHotel()
    st.answers = {}
    st.current_q_idx = 0
    bot.send_message(
        msg.chat.id,
        "გამარჯობა! აირჩიე ქმედება 👇",
        reply_markup=main_menu()
    )

@bot.message_handler(func=lambda m: m.text == "🔎 მოძებნა")
def on_search_button(msg: types.Message):
    st = user_state.setdefault(msg.chat.id, SurveyState())
    st.step = "ASK_NAME_EN"
    st.pending = PendingHotel()
    bot.send_message(
        msg.chat.id,
        "გთხოვ, შეიყვანე <b>სასტუმროს ოფიციალური სახელი (ინგლისურად)</b>."
    )

@bot.message_handler(func=lambda m: user_state.get(m.chat.id, SurveyState()).step == "ASK_NAME_EN")
def ask_address(msg: types.Message):
    st = user_state[msg.chat.id]
    st.pending.name_en = msg.text.strip()
    st.step = "ASK_ADDR_GE"
    bot.send_message(
        msg.chat.id,
        "ახლა შეიყვანე <b>სასტუმროს ოფიციალური მისამართი (ქართულად)</b>."
    )

@bot.message_handler(func=lambda m: user_state.get(m.chat.id, SurveyState()).step == "ASK_ADDR_GE")
def perform_lookup(msg: types.Message):
    st = user_state[msg.chat.id]
    st.pending.addr_ge = msg.text.strip()

    name_en = st.pending.name_en or ""
    addr_ge = st.pending.addr_ge or ""

    bot.send_message(msg.chat.id, "ძებნა მიმდინარეობს… ერთი წამი 🔎")
    status, best_row, score, suggestions = match_hotels(name_en, addr_ge)
    st.pending.candidate_from_sheet = best_row
    st.pending.found_status = status

    if status == "surveyed":
        comment = ""
        if best_row and COL_COMMENT in best_row and best_row[COL_COMMENT]:
            comment = f"\nკომენტარი: <i>{best_row[COL_COMMENT]}</i>"
        pretty = (
            f"ნაპოვნია: <b>{best_row.get(COL_NAME_EN,'')}</b>\n"
            f"მისამართი: {best_row.get(COL_ADDR_GE,'')}\n"
            f"სტატუსი: ❌ უკვე გამოკითხულია.{comment}"
        )
        bot.send_message(msg.chat.id, pretty, reply_markup=main_menu())
        # დასრულება
        st.step = "IDLE"
        return

    # unsurveyed or not_found
    text_lines = []
    if status == "unsurveyed":
        text_lines.append("ნაპოვნია მსგავსი სასტუმრო, მაგრამ <b>არ არის გამოკითხული</b> (შიტში არ აქვს 'X').")
    else:
        text_lines.append("ასეთი სასტუმრო შიტში <b>ვერ ვიპოვე</b> ან ზუსტი დამთხვევა არ არის.")

    if suggestions:
        text_lines.append("\nახლოს მყოფი ვარიანტები:")
        for i, (combo, nsc, asc, row) in enumerate(suggestions, start=1):
            mark = "❌" if _is_surveyed(str(row.get(COL_STATUS, ""))) else "🟢"
            text_lines.append(f"{i}) {row.get(COL_NAME_EN,'')} — {row.get(COL_ADDR_GE,'')}  [{mark}] ({combo}%)")
        text_lines.append(
            "\nთუ ზემოთ მოცემული უკვე '❌' აღნიშვნითაა — გამოკითხულია და დასრულებულია.\n"
            "თუ არა — დააჭირე <b>Start</b> და გააგრძელე შევსება."
        )

    bot.send_message(msg.chat.id, "\n".join(text_lines), reply_markup=start_only_kb())
    st.step = "WAIT_START_OR_BACK"

@bot.message_handler(func=lambda m: user_state.get(m.chat.id, SurveyState()).step == "WAIT_START_OR_BACK")
def wait_start(msg: types.Message):
    st = user_state[msg.chat.id]
    if msg.text == "🧾 Start":
        st.step = "CONFIRM_NAME"
        bot.send_message(
            msg.chat.id,
            "სტარტი ✅\nგაიმეორე სასტუმროს <b>სახელი (ინგლისურად)</b>, ზუსტად ის, რასაც ეძებდი."
        )
    else:
        # back to main
        st.step = "IDLE"
        st.pending = PendingHotel()
        bot.send_message(msg.chat.id, "დაბრუნდი მთავარ მენიუში.", reply_markup=main_menu())

@bot.message_handler(func=lambda m: user_state.get(m.chat.id, SurveyState()).step == "CONFIRM_NAME")
def confirm_name(msg: types.Message):
    st = user_state[msg.chat.id]
    typed = msg.text.strip()
    expected = st.pending.name_en or ""
    score = fuzz.WRatio(_normalize(typed), _normalize(expected))
    if score < EXACT_THRESHOLD:
        bot.send_message(
            msg.chat.id,
            f"შეყვანილი სახელი <b>არ ემთხვევა</b> საძიებო მნიშვნელობას ({score}%).\n"
            "გთხოვ, ჩასწორე ან ხელახლა შეიყვანე ზუსტად."
        )
        return
    st.pending.name_en = typed  # lock
    st.step = "CONFIRM_ADDR"
    bot.send_message(msg.chat.id, "ახლა გაიმეორე <b>მისამართი (ქართულად)</b>.")

@bot.message_handler(func=lambda m: user_state.get(m.chat.id, SurveyState()).step == "CONFIRM_ADDR")
def confirm_addr(msg: types.Message):
    st = user_state[msg.chat.id]
    typed = msg.text.strip()
    expected = st.pending.addr_ge or ""
    score = fuzz.WRatio(_normalize(typed), _normalize(expected))
    if score < EXACT_THRESHOLD:
        bot.send_message(
            msg.chat.id,
            f"მისამართი <b>არ ემთხვევა</b> საძიებო მნიშვნელობას ({score}%).\n"
            "გთხოვ, ჩასწორე ან ხელახლა შეიყვანე ზუსტად."
        )
        return
    st.pending.addr_ge = typed  # lock
    # proceed to first question
    st.step = "ASK_Q"
    st.current_q_idx = 0
    ask_next_question(msg.chat.id)

def ask_next_question(chat_id: int):
    st = user_state[chat_id]
    if st.current_q_idx >= len(QUESTIONS):
        # done
        append_response_row(st)
        bot.send_message(
            chat_id,
            "მადლობა! ინფორმაცია ჩაიწერა შიტში. ✅",
            reply_markup=main_menu()
        )
        st.step = "IDLE"
        st.answers = {}
        st.current_q_idx = 0
        return

    key, text = QUESTIONS[st.current_q_idx]
    bot.send_message(chat_id, text)

@bot.message_handler(func=lambda m: user_state.get(m.chat.id, SurveyState()).step == "ASK_Q")
def on_answer(msg: types.Message):
    st = user_state[msg.chat.id]
    key, _ = QUESTIONS[st.current_q_idx]
    st.answers[key] = msg.text.strip()
    st.current_q_idx += 1
    ask_next_question(msg.chat.id)


# ---------------------------
# WEBHOOK / HEALTH
# ---------------------------
@app.get("/")
def health():
    return "ok", 200

@app.post(f"/{TELEGRAM_TOKEN}")
def telegram_webhook():
    json_update = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_update)
    bot.process_new_updates([update])
    return "!", 200

def maybe_set_webhook():
    if not APP_BASE_URL or not AUTO_SET_WEBHOOK:
        return
    url = f"{APP_BASE_URL}/{TELEGRAM_TOKEN}"
    try:
        ok = bot.set_webhook(url=url)
        log.info("Webhook set to %s -> %s", url, ok)
    except Exception as e:
        log.warning("Webhook set failed: %s", e)

# Set webhook once on import (gunicorn workers may call multiple times; it's idempotent)
maybe_set_webhook()

if __name__ == "__main__":
    # Local dev only. On Render use gunicorn via startCommand.
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
