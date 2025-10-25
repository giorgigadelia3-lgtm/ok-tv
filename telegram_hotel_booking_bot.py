import os
import json
import logging
import asyncio
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple

from flask import Flask, request, jsonify

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from rapidfuzz import fuzz, process
import gspread
from google.oauth2.service_account import Credentials

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s]: %(message)s",
)
log = logging.getLogger("hotel_bot")

# ---------------- Env ----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")  # https://ok-tv-1.onrender.com
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
SHEET_NAME = os.environ.get("SHEET_NAME", "Hotels")  # შეგიძლია შეცვალო სურვილისამებრ

if not (TELEGRAM_TOKEN and APP_BASE_URL and SPREADSHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON):
    log.warning("Some env vars are missing. Make sure TELEGRAM_TOKEN, APP_BASE_URL, SPREADSHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON are set.")

# ---------------- Flask ----------------
app = Flask(__name__)

# ---------------- Google Sheets helper ----------------
def _sheet_client():
    """Authorize and return (gc, worksheet)"""
    info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        # შევქმნათ default სქემა
        ws = sh.add_worksheet(title=SHEET_NAME, rows=1000, cols=20)
        ws.append_row(["Name_EN", "Address_KA", "Status", "Comment", "CreatedBy", "CreatedAt"])
    return ws

def _read_hotels() -> List[Dict[str, str]]:
    ws = _sheet_client()
    rows = ws.get_all_records()
    # normalize headers
    normalized = []
    for r in rows:
        normalized.append({
            "Name_EN": str(r.get("Name_EN", "")).strip(),
            "Address_KA": str(r.get("Address_KA", "")).strip(),
            "Status": str(r.get("Status", "")).strip(),
            "Comment": str(r.get("Comment", "")).strip(),
        })
    return normalized

def _append_hotel(name_en: str, address_ka: str, status: str, comment: str, user: str):
    ws = _sheet_client()
    from datetime import datetime
    ws.append_row([name_en, address_ka, status, comment, user, datetime.utcnow().isoformat(timespec="seconds") + "Z"])

# ---------------- Fuzzy match ----------------
@dataclass
class MatchResult:
    found_exact: bool = False
    exact_row: Optional[Dict[str, str]] = None
    suggestions: List[Tuple[Dict[str,str], int]] = field(default_factory=list)  # (row, score)

def find_hotel(name_en: str, address_ka: str) -> MatchResult:
    hotels = _read_hotels()
    result = MatchResult()
    # Try exact-ish first
    for h in hotels:
        if h["Name_EN"].lower() == name_en.lower() and h["Address_KA"] == address_ka:
            result.found_exact = True
            result.exact_row = h
            return result

    # Fuzzy: combine name/address
    candidates = []
    for h in hotels:
        name_score = fuzz.WRatio(name_en.lower(), h["Name_EN"].lower())
        addr_score = fuzz.WRatio(address_ka, h["Address_KA"])
        combined = int(0.65 * name_score + 0.35 * addr_score)  # name heavier
        if combined >= 80:
            candidates.append((h, combined))

    # Sort by score desc
    candidates.sort(key=lambda x: x[1], reverse=True)
    result.suggestions = candidates[:5]
    return result

# ---------------- Conversation state (simple FSM via user_data) ----------------
SEARCH_BTN = "🔎 მოძებნა"
START_BTN  = "▶️ სტარტი"

ASK_NAME = "ASK_NAME"
ASK_ADDR = "ASK_ADDR"
WAIT_CONFIRM_SUGGEST = "WAIT_CONFIRM_SUGGEST"
FILL_FLOW = "FILL_FLOW"
CONFIRM_NAME = "CONFIRM_NAME"
CONFIRM_ADDR = "CONFIRM_ADDR"

# აქ ჩამოწერე შენი საბოლოო კითხვარი – 1:1 შეცვლადი სიით.
QUESTIONS: List[Tuple[str, str]] = [
    # (key, prompt)
    ("contact_person", "კონტაქტი (სახელი გვარი):"),
    ("phone", "ტელეფონის ნომერი:"),
    ("notes", "დამატებითი შენიშვნა:"),
]

def home_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(SEARCH_BTN)], [KeyboardButton(START_BTN)]],
        resize_keyboard=True
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "გამარჯობა! აირჩიე ქმედება 👇",
        reply_markup=home_keyboard()
    )
    context.user_data.clear()

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # 1) საწყისი მენიუ
    if text == SEARCH_BTN:
        context.user_data.clear()
        context.user_data["mode"] = "search"
        await update.message.reply_text("სასტუმროს ოფიციალური სახელი შეიყვანე ინგლისურად:")
        context.user_data["step"] = ASK_NAME
        return

    if text == START_BTN:
        # თუ უკვე გვაქ სახელ/მისამართი ძიებიდან და არ იყო გამოკითხული -> გადავამოწმოთ შესაბამისობა
        mode = context.user_data.get("mode")
        if mode == "search_not_found":
            await update.message.reply_text(
                "ახლა კიდევ ერთხელ შეიყვანე იგივე სასტუმროს ოფიციალური სახელი (EN):"
            )
            context.user_data["step"] = CONFIRM_NAME
            return
        # თორემ პირდაპირ დავიწყოთ სვლა ნულიდან
        await start_fill_flow(update, context)
        return

    # 2) Search flow
    step = context.user_data.get("step")
    if step == ASK_NAME:
        context.user_data["hotel_name_en"] = text
        await update.message.reply_text("ახლა იგივე სასტუმროს ოფიციალური მისამართი ჩაწერე ქართულად:")
        context.user_data["step"] = ASK_ADDR
        return

    if step == ASK_ADDR:
        context.user_data["hotel_addr_ka"] = text
        name_en = context.user_data["hotel_name_en"]
        addr_ka = context.user_data["hotel_addr_ka"]

        mr = find_hotel(name_en, addr_ka)
        # ზუსტად მოიძებნა
        if mr.found_exact and mr.exact_row:
            row = mr.exact_row
            comment = row.get("Comment", "")
            await update.message.reply_text(
                "ეს სასტუმრო უკვე გვაქვს გამოკითხული. ვანიშნავ სტატუსს ❌\n"
                f"კომენტარი: {comment or '—'}\n\nჩატი დასრულებულია.",
                reply_markup=home_keyboard()
            )
            context.user_data.clear()
            return

        # ზუსტი არა, მაგრამ მსგავსები მოიძებნა
        if mr.suggestions:
            # შევთავაზოთ
            buttons = []
            for i, (row, score) in enumerate(mr.suggestions, start=1):
                n = row.get("Name_EN","")
                a = row.get("Address_KA","")
                buttons.append([InlineKeyboardButton(f"{i}) {n} | {a} (≈{score}%)", callback_data=f"suggest:{i-1}")])
            buttons.append([InlineKeyboardButton("ვერ ვპოულობ – გავაგრძელოთ ახალი ჩანაწერი", callback_data="suggest:none")])
            await update.message.reply_text(
                "მსგავსი ჩანაწერები ვიპოვე – რომელს გულისხმობ? (შეამოწმე მართლწერა)\n"
                "თუ არცერთი არ არის, აირჩიე ბოლო ვარიანტი:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            context.user_data["suggestions"] = mr.suggestions
            context.user_data["step"] = WAIT_CONFIRM_SUGGEST
            return

        # საერთოდ ვერ ვიპოვეთ – მივცეთ გაგრძელების უფლება
        await update.message.reply_text(
            "ეს სასტუმრო ბაზაში ვერ ვიპოვე. შეგიძლია დაუკავშირდე სასტუმროს "
            "ან ახალი ჩანაწერი შექმნა — დააჭირე „▶️ სტარტი“.",
            reply_markup=home_keyboard()
        )
        context.user_data["mode"] = "search_not_found"
        return

    # 3) ძიების შემდეგ დასტური – სახელი/მისამართი შევადაროთ
    if step == CONFIRM_NAME:
        entered_name = text
        found_name = context.user_data.get("hotel_name_en","")
        if fuzz.WRatio(entered_name.lower(), found_name.lower()) < 90:
            await update.message.reply_text(
                "შეყვანილი სახელი არ ემთხვევა საძიებო ეტაპზე შეყვანილს. "
                "გთხოვ, გაასწორე და ზუსტად ჩაწერე (EN):"
            )
            return
        context.user_data["hotel_name_en"] = entered_name
        await update.message.reply_text("კარგი. ახლა მისამართი (KA):")
        context.user_data["step"] = CONFIRM_ADDR
        return

    if step == CONFIRM_ADDR:
        entered_addr = text
        found_addr = context.user_data.get("hotel_addr_ka","")
        if fuzz.WRatio(entered_addr, found_addr) < 90:
            await update.message.reply_text(
                "მისამართი არ ემთხვევა საძიებო ეტაპზე შეყვანილს. "
                "გთხოვ, გადაამოწმე და ზუსტად ჩაწერე ქართული მისამართი:"
            )
            return
        context.user_data["hotel_addr_ka"] = entered_addr
        # გადავიდეთ შეკითხვებზე
        await start_fill_flow(update, context)
        return

    # 4) კითხვარის პერიოდში პასუხები
    if step == FILL_FLOW:
        q_index = context.user_data.get("q_index", 0)
        key, _prompt = QUESTIONS[q_index]
        context.user_data.setdefault("answers", {})[key] = text

        q_index += 1
        if q_index >= len(QUESTIONS):
            # ვწერთ შიტში ახალ ჩანაწერს ✅
            name_en = context.user_data.get("hotel_name_en", "")
            addr_ka = context.user_data.get("hotel_addr_ka", "")
            comment = context.user_data["answers"].get("notes", "")
            # ახალზე – სტატუსად ✅ გამოვიყენოთ (ან ცარიელი)
            _append_hotel(
                name_en=name_en,
                address_ka=addr_ka,
                status="✅ NEW",
                comment=comment,
                user=update.effective_user.full_name if update.effective_user else "unknown",
            )
            await update.message.reply_text(
                "ინფორმაცია წარმატებით ჩაიწერა Google Sheet-ში. მადლობა!\nჩატი დასრულებულია.",
                reply_markup=home_keyboard()
            )
            context.user_data.clear()
            return
        else:
            context.user_data["q_index"] = q_index
            key, prompt = QUESTIONS[q_index]
            await update.message.reply_text(prompt)
            return

    # სხვა ტექსტი – საწყისისკენ
    await update.message.reply_text("აირჩიე 👇", reply_markup=home_keyboard())


async def start_fill_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["step"] = FILL_FLOW
    context.user_data["q_index"] = 0
    context.user_data.setdefault("answers", {})
    # თუ search-ით არ მოსულა, ახლა ვთხოვოთ აუცილებელი ორი ველი:
    if "hotel_name_en" not in context.user_data or "hotel_addr_ka" not in context.user_data:
        await update.message.reply_text("ჯერ სასტუმროს ოფიციალური სახელი (EN) ჩაწერე:")
        context.user_data["step"] = ASK_NAME
        return
    # თორემ პირდაპირ პირველ შეკითხვაზე გადავიდეთ
    first_prompt = QUESTIONS[0][1]
    await update.message.reply_text(first_prompt)

# ---------------- Callbacks ----------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("suggest:"):
        val = data.split(":",1)[1]
        if val == "none":
            # ახალი ჩანაწერის გზა
            await query.edit_message_text(
                "არცერთი არ არის. შეგიძლია ახალი ჩანაწერი დაიწყო – დააჭირე „▶️ სტარტი“."
            )
            context.user_data["mode"] = "search_not_found"
            return
        try:
            idx = int(val)
        except ValueError:
            return
        suggestions = context.user_data.get("suggestions", [])
        if not suggestions or idx >= len(suggestions):
            return
        row, score = suggestions[idx]
        # ეს უკვე ბაზაშია – დავასრულოთ
        comment = row.get("Comment","")
        await query.edit_message_text(
            "ეს სასტუმრო უკვე გამოკითხულია. სტატუსი: ❌\n"
            f"კომენტარი: {comment or '—'}\n\nჩატი დასრულებულია."
        )
        context.user_data.clear()
        return

# ---------------- Telegram app bootstrap ----------------
tg_app: Optional[Application] = None
loop = asyncio.get_event_loop()

async def _build_and_start_application():
    global tg_app
    tg_app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    # Handlers
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CallbackQueryHandler(on_callback))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Set webhook (masked in logs)
    webhook_url = f"{APP_BASE_URL}/{TELEGRAM_TOKEN}"
    ok = await tg_app.bot.set_webhook(
        url=webhook_url,
        allowed_updates=["message","callback_query"]
    )
    log.info("Webhook set (masked): %s/*** -> %s", APP_BASE_URL, ok)

    await tg_app.initialize()
    await tg_app.start()

# Kick off the telegram application in background
loop.create_task(_build_and_start_application())

# ---------------- Flask routes ----------------
@app.route("/", methods=["GET"])
def health():
    return "OK"

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    if request.method == "POST":
        try:
            update = Update.de_json(request.get_json(force=True), tg_app.bot)
            # put update into PTB queue
            tg_app.update_queue.put_nowait(update)
        except Exception as e:
            log.exception("webhook error: %s", e)
            return jsonify({"ok": False}), 500
        return jsonify({"ok": True})
    return "Method Not Allowed", 405
