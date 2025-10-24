import os
import json
import time
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple

from flask import Flask, request
import telebot
from telebot import types

# === Google Sheets ===
import gspread
from google.oauth2.service_account import Credentials

# === Fuzzy matching ===
from rapidfuzz import fuzz, process

# ---------------------------
# ENVIRONMENT / CONFIG
# ---------------------------
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]

# Service Account JSON მთელი ტექსტით env-ში:
#   GOOGLE_SERVICE_ACCOUNT_JSON = {...}
GSERVICE_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
SHEET_KEY = os.environ["GSPREAD_SHEET_KEY"]  # Spreadsheet ID
HOTELS_WS = os.getenv("HOTELS_WORKSHEET", "Hotels")  # worksheet name hotels list
RESPONSES_WS = os.getenv("RESPONSES_WORKSHEET", "Responses")

# Column names (header row-ს მიხედვით). შეცვლადი env-ებით.
COL_NAME_EN = os.getenv("HOTELS_NAME_COLUMN", "name_en")
COL_ADDR_GE = os.getenv("HOTELS_ADDRESS_COLUMN", "address_ge")
COL_STATUS = os.getenv("HOTELS_STATUS_COLUMN", "status")   # 'X', '✅' etc. = surveyed
COL_COMMENT = os.getenv("HOTELS_COMMENT_COLUMN", "comment")

# Matching thresholds
EXACT_THRESHOLD = int(os.getenv("MATCH_EXACT_THRESHOLD", "90"))
SUGGEST_THRESHOLD = int(os.getenv("MATCH_SUGGEST_THRESHOLD", "70"))

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
    candidate_from_sheet: Optional[Dict[str, Any]] = None  # top suggestion (if any)
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
    info = json.loads(GSERVICE_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)

def get_hotels_records() -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Return (records, header_index_map)."""
    gc = gsheet_client()
    ws = gc.open_by_key(SHEET_KEY).worksheet(HOTELS_WS)
    rows = ws.get_all_records()  # list[dict] using header row
    # map lower headers
    headers = {k.strip().lower(): k for k in rows[0].keys()} if rows else {}
    # but safer: grab header row directly
    header_row = ws.row_values(1)
    header_idx = {h.strip().lower(): i for i, h in enumerate(header_row)}
    return rows, header_idx

def ensure_responses_header():
    gc = gsheet_client()
    sh = gc.open_by_key(SHEET_KEY)
    try:
        ws = sh.worksheet(RESPONSES_WS)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=RESPONSES_WS, rows=2000, cols=50)
        ws.append_row([
            "timestamp", "name_en", "address_ge",
            "matched_name_en", "matched_address_ge", "matched_status",
            # dynamic Q headers appended later
        ])
    return ws

def append_response_row(state: SurveyState):
    ws = ensure_responses_header()
    # ensure headers contain question keys (add if missing)
    headers = ws.row_values(1)
    q_keys = [k for k, _ in QUESTIONS]
    missing = [k for k in q_keys if k not in headers]
    if missing:
        ws.add_cols(len(missing))
        headers += missing
        ws.update('A1', [headers])

    row = [
        time.strftime("%Y-%m-%d %H:%M:%S"),
        state.pending.name_en or "",
        state.pending.addr_ge or "",
        (state.pending.candidate_from_sheet or {}).get(COL_NAME_EN, ""),
        (state.pending.candidate_from_sheet or {}).get(COL_ADDR_GE, ""),
        state.pending.found_status or "",
    ]
    # pad to current headers length, then fill answers in correct columns
    values = {**{k: "" for k in headers}}
    # base columns:
    values["timestamp"] = row[0]
    values["name_en"] = row[1]
    values["address_ge"] = row[2]
    values["matched_name_en"] = row[3]
    values["matched_address_ge"] = row[4]
    values["matched_status"] = row[5]
    # answers:
    for k in state.answers:
        values[k] = state.answers[k]

    ws.append_row([values.get(h, "") for h in headers], value_input_option="USER_ENTERED")

def normalize(s: str) -> str:
    return "".join(ch for ch in s.lower().strip() if ch.isalnum() or ch.isspace())

def match_hotels(name_en: str, addr_ge: str):
    """Return best match info: (found_status, best_row_or_None, best_score, suggestions)"""
    records, _ = get_hotels_records()
    if not records:
        return "not_found", None, 0, []

    cand_scores = []
    for row in records:
        r_name = str(row.get(COL_NAME_EN, "") or "")
        r_addr = str(row.get(COL_ADDR_GE, "") or "")
        name_score = fuzz.WRatio(normalize(name_en), normalize(r_name))
        addr_score = fuzz.WRatio(normalize(addr_ge), normalize(r_addr)) if addr_ge else 0
        # weighted combo: name 70%, address 30%
        combo = int(0.7 * name_score + 0.3 * addr_score)
        cand_scores.append((combo, name_score, addr_score, row))

    cand_scores.sort(reverse=True, key=lambda x: x[0])
    best = cand_scores[0]
    suggestions = [c for c in cand_scores[:5] if c[0] >= SUGGEST_THRESHOLD]

    best_combo, best_name, best_addr, best_row = best
    status_cell = str(best_row.get(COL_STATUS, "") or "").strip().lower()
    is_surveyed = status_cell in ("x", "✅", "yes", "true", "done", "surveyed")

    if best_combo >= EXACT_THRESHOLD:
        return ("surveyed" if is_surveyed else "unsurveyed"), best_row, best_combo, suggestions
    else:
        # no strong match
        if suggestions:
            # still present "similar" list for human check
            return ("surveyed" if is_surveyed and best_combo >= SUGGEST_THRESHOLD else "not_found"), best_row, best_combo, suggestions
        return "not_found", None, 0, []


# ---------------------------
# QUESTIONS (შენი არსებული ბლოკის ადგილი)
# სურვილისამებრ ჩაანაცვლე/დაამატე
# ---------------------------
QUESTIONS: List[Tuple[str, str]] = [
    ("contact_person", "კონტაქტის სახელი და გვარი?"),
    ("phone", "ტელეფონის ნომერი?"),
    ("rooms_count", "რამდენი ნომერია სასტუმროში?"),
    ("email", "ელფოსტა?"),
    ("notes", "დამატებითი კომენტარი?"),
]
# თუ გინდა მთლიანად შენი ბლოკი — უბრალოდ შეცვალე QUESTIONS.

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
        pretty = f"ნაპოვნია: <b>{best_row.get(COL_NAME_EN,'')}</b>\nმისამართი: {best_row.get(COL_ADDR_GE,'')}\nსტატუსი: ❌ უკვე გამოკითხულია.{comment}"
        bot.send_message(msg.chat.id, pretty, reply_markup=main_menu())
        # დასრულება
        st.step = "IDLE"
        return

    if status in ("unsurveyed", "not_found"):
        text = []
        if status == "unsurveyed":
            text.append("ნაპოვნია მსგავსი სასტუმრო, მაგრამ <b>არ არის გამოკითხული</b> (შიტში არ აქვს 'X').")
        else:
            text.append("ასეთი სასტუმრო შიტში <b>დე ფაქტო ვერ ვიპოვე</b>.")

        if suggestions:
            text.append("\nახლოს მყოფი ვარიანტები:")
            for i, (combo, nsc, asc, row) in enumerate(suggestions, start=1):
                status_cell = str(row.get(COL_STATUS, "") or "").strip()
                mark = "❌" if status_cell.lower() in ("x", "✅", "yes", "true", "done", "surveyed") else "🟢"
                text.append(f"{i}) {row.get(COL_NAME_EN,'')} — {row.get(COL_ADDR_GE,'')}  [{mark}] ({combo}%)")
            text.append("\nთუ ზემოთ მოცემული უკვე '❌' აღნიშვნითაა — გამოკითხულია და დასრულებულია.\n"
                        "თუ არა — შეგიძლია დააჭირო <b>Start</b> და გააგრძელო შევსება.")

        bot.send_message(msg.chat.id, "\n".join(text), reply_markup=start_only_kb())
        st.step = "WAIT_START_OR_BACK"
        return

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
    score = fuzz.WRatio(normalize(typed), normalize(expected))
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
    score = fuzz.WRatio(normalize(typed), normalize(expected))
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

# --------------- Utilities ---------------
@app.get("/")
def health():
    return "ok", 200

@app.post(f"/{TELEGRAM_TOKEN}")
def telegram_webhook():
    json_update = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_update)
    bot.process_new_updates([update])
    return "!", 200

if __name__ == "__main__":
    # local run (Render-ზე მუშაობს gunicorn-ით, აქ მხოლოდ dev)
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
