import os
import json
import logging
import threading
import asyncio
from typing import Dict, Any, List, Tuple, Optional

from flask import Flask, request, jsonify, Response

from fuzzywuzzy import fuzz, process

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
)

# =========================
# Logging
# =========================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("hotel_bot")

# =========================
# ENV
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
GOOGLE_SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN env")
if not APP_BASE_URL:
    raise RuntimeError("Missing APP_BASE_URL env")
if not SPREADSHEET_ID:
    raise RuntimeError("Missing SPREADSHEET_ID env")
if not GOOGLE_SA_JSON:
    raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON env")

# =========================
# Flask
# =========================
app = Flask(__name__)

# =========================
# Google Sheets helper
# =========================

SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

def _sa_client():
    data = json.loads(GOOGLE_SA_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(data, scopes=SCOPE)
    gc = gspread.authorize(creds)
    return gc

def open_sheet():
    gc = _sa_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    # პირველი worksheet — შეგიძლია შეცვალო სახელით თუ გჭირდება
    ws = sh.sheet1
    return ws

# ვიგულვოთ სვეტების სტრუქტურა:
# A: Hotel Name (EN)
# B: Address (KA)
# C: Status  (e.g., ✅ Surveyed / ❌ Already / NEW)
# D: Comment
# E+: სხვა ველები (ჩასაწერი ბოტიდან როცა ახალია)

def read_all_hotels() -> List[Dict[str, Any]]:
    ws = open_sheet()
    values = ws.get_all_records()
    # მოამზადე სტანდარტული ფორმატი
    normalized = []
    for row in values:
        normalized.append({
            "name": str(row.get("Hotel Name", "")).strip(),
            "address": str(row.get("Address", "")).strip(),
            "status": str(row.get("Status", "")).strip(),
            "comment": str(row.get("Comment", "")).strip(),
            "_raw": row,
        })
    return normalized

def append_new_row(payload: Dict[str, Any]) -> None:
    ws = open_sheet()
    # აკურატულად შეავსე — თუ გაგაჩნია სხვა სვეტებიც, დაამატე აქ
    ws.append_row(
        [
            payload.get("name", ""),
            payload.get("address", ""),
            payload.get("status", "NEW"),
            payload.get("comment", ""),
            payload.get("contact_name", ""),
            payload.get("contact_phone", ""),
            payload.get("notes", ""),
        ],
        value_input_option="USER_ENTERED",
    )

# =========================
# Helpers
# =========================

def normalize(s: str) -> str:
    return " ".join(s.lower().strip().split())

def best_matches(
    hotels: List[Dict[str, Any]], name: str, address: str, limit: int = 5
) -> List[Tuple[Dict[str, Any], int]]:
    """
    აბრუნებს საუკეთესო მსგავსებებს name + address-ზე დაყრდნობით.
    ქულა = საშუალო(token_set_ratio(name), token_set_ratio(address))
    """
    res = []
    for h in hotels:
        nscore = fuzz.token_set_ratio(normalize(name), normalize(h["name"]))
        ascore = fuzz.token_set_ratio(normalize(address), normalize(h["address"]))
        score = (nscore + ascore) // 2
        res.append((h, score))
    res.sort(key=lambda x: x[1], reverse=True)
    return res[:limit]

def is_strong_match(score: int) -> bool:
    # 90%-ზე მეტი — ვთვლით ზუსტ ან თითქმის ზუსტ დამთხვევად
    return score >= 90

def is_close_match(score: int) -> bool:
    # ახლოსაა, მაგრამ არა აბსოლუტურად ზუსტი
    return score >= 70

def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔎 მოძებნა"), KeyboardButton("▶️ Start")],
            [KeyboardButton("ℹ️ დახმარება")],
        ],
        resize_keyboard=True,
    )

def red_x() -> str:
    return "❌"

def green_check() -> str:
    return "✅"

# =========================
# Conversation states
# =========================

# Search flow
S_NAME, S_ADDR, S_CONFIRM = range(3)

# New (Start) flow
N_NAME, N_ADDR, N_CONTACT, N_PHONE, N_NOTES, N_CONFIRM = range(6)

# =========================
# PTB Application — background loop
# =========================

application: Application
loop: asyncio.AbstractEventLoop
_app_ready = threading.Event()

async def _build_and_start_application():
    global application
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # --- Handlers registration ---
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(MessageHandler(filters.Regex("^ℹ️ დახმარება$"), help_cmd))
    application.add_handler(MessageHandler(filters.Regex("^🔎 მოძებნა$"), search_entry))
    application.add_handler(MessageHandler(filters.Regex("^▶️ Start$"), new_entry))

    # Search conversation
    application.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^🔎 მოძებნა$"), search_entry)],
            states={
                S_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_collect_name)],
                S_ADDR: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_collect_addr)],
                S_CONFIRM: [
                    CallbackQueryHandler(search_pick_suggestion, pattern=r"^pick_\d+$"),
                    CallbackQueryHandler(search_decline_suggestions, pattern=r"^pick_none$"),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_any)],
            name="search_conv",
            persistent=False,
        )
    )

    # New / Start conversation
    application.add_handler(
        ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^▶️ Start$"), new_entry)],
            states={
                N_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_collect_name)],
                N_ADDR: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_collect_addr)],
                N_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_collect_contact)],
                N_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_collect_phone)],
                N_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_collect_notes)],
                N_CONFIRM: [
                    CallbackQueryHandler(new_confirm_yes, pattern=r"^new_ok$"),
                    CallbackQueryHandler(new_confirm_no, pattern=r"^new_cancel$"),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_any)],
            name="new_conv",
            persistent=False,
        )
    )

    # Default fallbacks
    application.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))
    application.add_handler(MessageHandler(filters.ALL, fallback_router))

    # --- Start bot internal services (without polling) ---
    await application.initialize()
    await application.start()
    _app_ready.set()
    log.info("Telegram application started")

def start_background_loop():
    global loop
    loop = asyncio.new_event_loop()
    threading.Thread(target=lambda: loop.run_until_complete(_build_and_start_application()), daemon=True).start()
    _app_ready.wait()

start_background_loop()

# =========================
# Bot handlers
# =========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "გამარჯობა! აირჩიე მოქმედება 👇",
        reply_markup=main_menu(),
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_chat.send_message(
        "🔎 *მოძებნა* — ჯერ შეიყვანე სასტუმროს ოფიციალური სახელი (ინგლisch), შემდეგ მისი მისამართი (ქართული). "
        "ბოტი შეადარებს Sheets-ში არსებულ მონაცემებს და გეტყვის უკვე გამოკითხულია თუ არა.\n\n"
        "▶️ *Start* — დაიწყო ახალ ობიექტზე შეკითხვები და შედეგი ჩაიწერება Sheet-ში.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=main_menu(),
    )

async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("ბრძანება ვერ გავიგე. აირჩიე მენიუდან ⬇️", reply_markup=main_menu())

async def fallback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # თუ ტექსტი მოვიდა უშუალოდ — გადამისამართე მენიუზე
    await update.effective_message.reply_text("აირჩიე მოქმედება ⬇️", reply_markup=main_menu())

# ----- SEARCH FLOW -----

async def search_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text(
        "მომეცი *სასტუმროს ოფიციალური სახელი (EN)* — მაგალითად: `Radisson Blu Iveria`",
        parse_mode=ParseMode.MARKDOWN,
    )
    return S_NAME

async def search_collect_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_message.text.strip()
    context.user_data["search_name_en"] = name
    await update.effective_message.reply_text(
        "ახლა მომეცი *სასტუმროს ოფიციალური მისამართი ქართულად* — მაგ.: `თბილისი, კოსტავას 14`",
        parse_mode=ParseMode.MARKDOWN,
    )
    return S_ADDR

async def search_collect_addr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = update.effective_message.text.strip()
    context.user_data["search_addr_ka"] = addr

    # მოძებნე Sheet-ში
    hotels = read_all_hotels()
    matches = best_matches(hotels, context.user_data["search_name_en"], addr, limit=5)

    if not matches:
        await update.effective_message.reply_text(
            "ვერ ვიპოვე მსგავსი ჩანაწერი Sheet-ში. შეგიძლიათ გააგრძელოთ ▶️ *Start* ღილაკით.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu(),
        )
        # შევინახოთ მოკლე „მოლოდინები“ რათა Start-ზე შევადაროთ
        context.user_data["expected_name"] = context.user_data["search_name_en"]
        context.user_data["expected_addr"] = context.user_data["search_addr_ka"]
        return ConversationHandler.END

    # თუ ძალიან ძლიერი დამთხვევაა — მიგვაჩნია, რომ უკვე არსებობს
    best_hotel, score = matches[0]
    if is_strong_match(score):
        comment = best_hotel.get("comment") or "კომენტარი არ არის."
        await update.effective_message.reply_text(
            f"{red_x()} *სასტუმრო უკვე გამოკითხულია.*\n\n"
            f"*სახელი:* {best_hotel['name']}\n"
            f"*მისამართი:* {best_hotel['address']}\n"
            f"*კომენტარი:* _{comment}_",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu(),
        )
        return ConversationHandler.END

    # სხვა შემთხვევაში — შევთავაზოთ „ეს ხომ არ არის?“ ვარიანტები
    buttons = []
    text_lines = ["შეიძლება इनमें ერთ-ერთს გულისხმობდე?"]
    for idx, (h, sc) in enumerate(matches, start=1):
        text_lines.append(f"{idx}) {h['name']} — {h['address']} (სიმსგავსე {sc}%)")
        buttons.append(
            [InlineKeyboardButton(f"{idx}) აირჩიე", callback_data=f"pick_{idx-1}")]
        )
    buttons.append([InlineKeyboardButton("არაფერი არ ემთხვევა", callback_data="pick_none")])

    context.user_data["search_suggestions"] = matches

    await update.effective_message.reply_text(
        "\n".join(text_lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return S_CONFIRM

async def search_pick_suggestion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    idx = int(q.data.split("_")[1])
    matches: List[Tuple[Dict[str, Any], int]] = context.user_data.get("search_suggestions", [])
    if idx < 0 or idx >= len(matches):
        await q.edit_message_text("არასწორი არჩევანი.")
        return ConversationHandler.END

    hotel, score = matches[idx]
    comment = hotel.get("comment") or "კომენტარი არ არის."

    await q.edit_message_text(
        f"{red_x()} *სასტუმრო უკვე გამოკითხულია.*\n\n"
        f"*სახელი:* {hotel['name']}\n"
        f"*მისამართი:* {hotel['address']}\n"
        f"*კომენტარი:* _{comment}_",
        parse_mode=ParseMode.MARKDOWN,
    )
    # დასრულება — ჩატი ავტომატურად მთავრდება „უკვე გამოკითხულია“ შემთხვევაში
    return ConversationHandler.END

async def search_decline_suggestions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    # შევინახოთ რომ „ვარიანტები არ ემთხვეოდა“ — და მივცეთ Start
    context.user_data["expected_name"] = context.user_data.get("search_name_en")
    context.user_data["expected_addr"] = context.user_data.get("search_addr_ka")

    await q.edit_message_text(
        "ოკ! მაშინ შეგიძლიათ გააგრძელოთ ▶️ *Start* ღილაკით და შევავსოთ ახალი ჩანაწერი.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ConversationHandler.END

# ----- NEW / START FLOW -----

async def new_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "დავიწყოთ ახალი ჩანაწერი.\n\n"
        "გთხოვ, ისევ შეიყვანე *სასტუმროს ოფიციალური სახელი (EN)*:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return N_NAME

async def new_collect_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_message.text.strip()
    context.user_data["new_name"] = name

    # თუ Search-იდან იყო მოლოდინი — შევადაროთ
    exp = context.user_data.get("expected_name")
    if exp and normalize(exp) != normalize(name):
        await update.effective_message.reply_text(
            f"ℹ️ შენ მიერ შეყვანილი სახელი ({name}) განსხვავდება ადრე მოძიებულისგან ({exp}). "
            "დარწმუნდე, რომ სწორად წერ. თუ ყველაფერი სწორია, გავაგრძელოთ.",
        )

    await update.effective_message.reply_text(
        "ახლა შეიყვანე *სასტუმროს ოფიციალური მისამართი ქართულად*:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return N_ADDR

async def new_collect_addr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    addr = update.effective_message.text.strip()
    context.user_data["new_addr"] = addr

    exp = context.user_data.get("expected_addr")
    if exp and normalize(exp) != normalize(addr):
        await update.effective_message.reply_text(
            f"ℹ️ შენ მიერ შეყვანილი მისამართი ({addr}) განსხვავდება ადრე მოძიებულისგან ({exp}). "
            "გთხოვ გადაამოწმე. თუ სწორია, გავაგრძელოთ.",
        )

    await update.effective_message.reply_text("კონტაქტის სახელი (ვინ გვპასუხობს?):")
    return N_CONTACT

async def new_collect_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["contact_name"] = update.effective_message.text.strip()
    await update.effective_message.reply_text("კონტაქტის ტელეფონი:")
    return N_PHONE

async def new_collect_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["contact_phone"] = update.effective_message.text.strip()
    await update.effective_message.reply_text("შენიშვნები / კომენტარი:")
    return N_NOTES

async def new_collect_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["notes"] = update.effective_message.text.strip()

    name = context.user_data["new_name"]
    addr = context.user_data["new_addr"]
    contact = context.user_data.get("contact_name", "")
    phone = context.user_data.get("contact_phone", "")
    notes = context.user_data.get("notes", "")

    preview = (
        f"*შესაჯამებელი:*\n"
        f"• სახელი (EN): {name}\n"
        f"• მისამართი (KA): {addr}\n"
        f"• კონტაქტი: {contact} | {phone}\n"
        f"• შენიშვნა: {notes}\n\n"
        "დავადასტუროთ ჩაწერა Sheet-ში?"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ დადასტურება", callback_data="new_ok")],
            [InlineKeyboardButton("❌ გაუქმება", callback_data="new_cancel")],
        ]
    )
    await update.effective_message.reply_text(preview, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    return N_CONFIRM

async def new_confirm_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    payload = {
        "name": context.user_data["new_name"],
        "address": context.user_data["new_addr"],
        "status": "NEW",
        "comment": context.user_data.get("notes", ""),
        "contact_name": context.user_data.get("contact_name", ""),
        "contact_phone": context.user_data.get("contact_phone", ""),
        "notes": context.user_data.get("notes", ""),
    }
    append_new_row(payload)

    await q.edit_message_text(
        f"{green_check()} ჩანაწერი წარმატებით ჩაიწერა Sheet-ში. გმადლობთ!",
    )
    context.user_data.clear()
    return ConversationHandler.END

async def new_confirm_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("გაუქმებულია.")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_any(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("გაუქმებულია.", reply_markup=main_menu())
    return ConversationHandler.END

# =========================
# Flask routes
# =========================

@app.get("/")
def health() -> Response:
    return Response("OK", status=200)

@app.get("/set_webhook")
def set_webhook():
    url = f"{APP_BASE_URL}/webhook/{TELEGRAM_TOKEN}"
    async def _do():
        await application.bot.set_webhook(url=url, drop_pending_updates=True)
    fut = asyncio.run_coroutine_threadsafe(_do(), loop)
    fut.result(timeout=15)
    log.info("Webhook set (masked): %s/*** -> True", APP_BASE_URL)
    return jsonify(ok=True, url=url)

@app.post(f"/webhook/{TELEGRAM_TOKEN}")
def telegram_webhook():
    # მიიღე update და გადააწოდე PTB-ს
    update_json = request.get_json(force=True, silent=True)
    if not update_json:
        return jsonify(ok=False)
    update = Update.de_json(update_json, application.bot)

    async def _process():
        await application.process_update(update)

    asyncio.run_coroutine_threadsafe(_process(), loop)
    return jsonify(ok=True)

# აპის გაშვებისას ერთი ჯერ მოვახდინოთ webhook-ის დაყენება
with app.app_context():
    try:
        url = f"{APP_BASE_URL}/webhook/{TELEGRAM_TOKEN}"
        async def _do():
            await application.bot.set_webhook(url=url, drop_pending_updates=True)
        fut = asyncio.run_coroutine_threadsafe(_do(), loop)
        fut.result(timeout=20)
        log.info("Webhook set (masked): %s/*** -> True", APP_BASE_URL)
    except Exception as e:
        log.warning("Webhook set failed initially: %s", e)

# =========================
# End of file
# =========================
