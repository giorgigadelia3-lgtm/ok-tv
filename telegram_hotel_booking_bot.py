#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OK TV — Telegram Chatbot for corporate-offers management
Author: Generated (assistant)
Requirements: python-telegram-bot>=20.0, APScheduler (optional), python-dotenv
DB: SQLite (file-based, portable)
Usage:
  - create .env with BOT_TOKEN and ADMIN_CHAT_ID (or comma-separated IDs)
  - python3 bot.py
"""

import os
import sqlite3
import csv
from datetime import datetime
from functools import wraps

from telegram import (
    __version__ as TG_VER,
)
# ensure using telegram v20+ API
try:
    from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton
    from telegram.ext import (
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
        ConversationHandler,
    )
except Exception as e:
    raise RuntimeError("python-telegram-bot v20+ is required. Install with: pip install python-telegram-bot --upgrade") from e

# ---- CONFIGURATION (environment variables) ----
BOT_TOKEN = os.getenv("BOT_TOKEN", "")  # required
ADMIN_CHAT_IDS = os.getenv("ADMIN_CHAT_ID", "")  # comma separated chat id(s)
DB_PATH = os.getenv("DB_PATH", "oktv_offers.db")

if not BOT_TOKEN:
    raise SystemExit("Please set BOT_TOKEN environment variable (e.g., in .env)")

# parse admin ids
ADMIN_IDS = []
if ADMIN_CHAT_IDS:
    for s in ADMIN_CHAT_IDS.split(","):
        s = s.strip()
        if s:
            try:
                ADMIN_IDS.append(int(s))
            except ValueError:
                print(f"Warning: invalid ADMIN_CHAT_ID value: {s}")

# ---- Database helpers ----
def init_db(path=DB_PATH):
    conn = sqlite3.connect(path, check_same_thread=False)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hotel_name TEXT UNIQUE,
            corp_name TEXT,
            address TEXT,
            comment TEXT,
            agent_name TEXT,
            submitted_by INTEGER,
            submitted_at TEXT
        )
        """
    )
    conn.commit()
    return conn

DB = init_db(DB_PATH)

def hotel_exists(hotel_name: str) -> bool:
    c = DB.cursor()
    c.execute("SELECT 1 FROM offers WHERE lower(hotel_name)=lower(?) LIMIT 1", (hotel_name.strip(),))
    return c.fetchone() is not None

def save_offer(hotel_name, corp_name, address, comment, agent_name, submitted_by):
    c = DB.cursor()
    now = datetime.utcnow().isoformat()
    try:
        c.execute(
            "INSERT INTO offers (hotel_name, corp_name, address, comment, agent_name, submitted_by, submitted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (hotel_name.strip(), corp_name.strip(), address.strip(), comment.strip(), agent_name.strip(), submitted_by, now),
        )
        DB.commit()
        return True
    except sqlite3.IntegrityError:
        # already exists (race condition)
        return False

def list_offers(limit=100):
    c = DB.cursor()
    c.execute("SELECT id, hotel_name, corp_name, address, comment, agent_name, submitted_by, submitted_at FROM offers ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    return rows

def export_offers_csv(path="oktv_offers_export.csv"):
    rows = list_offers(limit=1000000)
    header = ["id","hotel_name","corp_name","address","comment","agent_name","submitted_by","submitted_at"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)
    return path

# ---- Conversation states ----
(
    STATE_WAIT_SEARCH,       # after /start -> we show "მოძებნე. 🔍"
    STATE_WAIT_HOTEL_NAME,   # user types hotel name to check
    STATE_CORP_NAME,         # ask "კორპორაციის დასახელება. 🏢"
    STATE_ADDRESS,           # ask "მისამართი. 📍"
    STATE_COMMENT,           # ask "კომენტარი. 📩"
    STATE_AGENT_NAME,        # ask "აგენტის სახელი და გვარი. 👩‍💻"
) = range(6)

# ---- Admin only decorator ----
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in ADMIN_IDS:
            await update.message.reply_text("გვერდზე წვდომა მხოლოდ ადმინისტრატორს აქვს.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# ---- Bot Handlers ----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point: send the initial button 'მოძებნე. 🔍'"""
    # create simple keyboard with single button
    kb = ReplyKeyboardMarkup([[KeyboardButton("მოძებნე. 🔍")]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(
        "გამარჯობა! გამოიყენე ღილაკი ან დაწერე 'მოძებნე. 🔍' — სასტუმროს დასახელების შესამოწმებლად.",
        reply_markup=kb
    )
    return STATE_WAIT_HOTEL_NAME

async def search_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """When user presses the search button or sends any text intended as hotel name."""
    text = update.message.text.strip()
    # If user literally pressed button, we ask them to type the hotel name
    if text == "მოძებნე. 🔍":
        await update.message.reply_text("გთხოვთ, დაწერეთ სასტუმროს სახელი (სახელით).", reply_markup=ReplyKeyboardRemove())
        return STATE_WAIT_HOTEL_NAME

    # Otherwise, treat incoming text as hotel name directly (user typed it)
    return await handle_hotel_name(update, context, text=text)

async def handle_hotel_name(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None):
    """Main check: if hotel exists -> end with ❌. Else -> continue sequence."""
    if text is None:
        text = update.message.text.strip()

    hotel_name = text.strip()
    if not hotel_name:
        await update.message.reply_text("სახელის ველი ცარიელია — გთხოვთ, მიაწოდოთ სასტუმროს სახელი.")
        return STATE_WAIT_HOTEL_NAME

    # Check DB
    if hotel_exists(hotel_name):
        await update.message.reply_text("კორპორაციისთვის შეთავაზება მიწოდებულია. ❌️", reply_markup=ReplyKeyboardRemove())
        # conversation ends
        return ConversationHandler.END
    else:
        # new — inform and continue
        await update.message.reply_text("კორპორაცია თავისუფალია, გისურვებთ წარმატებებს. ✅️", reply_markup=ReplyKeyboardRemove())
        # store initial hotel_name in user_data
        context.user_data['hotel_name'] = hotel_name
        # Next prompt sequence as requested
        await update.message.reply_text("კორპორაციის დასახელება. 🏢")
        return STATE_CORP_NAME

async def corp_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data['corp_name'] = text
    await update.message.reply_text("მისამართი. 📍")
    return STATE_ADDRESS

async def address_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data['address'] = text
    await update.message.reply_text("კომენტარი. 📩")
    return STATE_COMMENT

async def comment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data['comment'] = text
    await update.message.reply_text("აგენტის სახელი და გვარი. 👩‍💻")
    return STATE_AGENT_NAME

async def agent_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data['agent_name'] = text

    # Gather all saved info
    hotel_name = context.user_data.get('hotel_name') or ""
    corp_name = context.user_data.get('corp_name') or hotel_name
    address = context.user_data.get('address') or ""
    comment = context.user_data.get('comment') or ""
    agent_name = context.user_data.get('agent_name') or ""
    submitted_by = update.effective_user.id if update.effective_user else None

    # Save to DB
    saved = save_offer(hotel_name=hotel_name, corp_name=corp_name, address=address, comment=comment, agent_name=agent_name, submitted_by=submitted_by)
    if not saved:
        # conflict (race)
        await update.message.reply_text("მოხდა შეცდომა: მსგავსი კორპორაცია უკვე დამატებულია. სტატი ფარავს. ❌️")
        return ConversationHandler.END

    # Notify admin(s) with full details
    msg = (
        f"📥 ახალი ნასტავსება დარეგისტრირდა:\n\n"
        f"🏨 სასტუმრო/კორპორაცია: {hotel_name}\n"
        f"🏢 კორპორაციის დასახელება: {corp_name}\n"
        f"📍 მისამართი: {address}\n"
        f"📩 კომენტარი: {comment}\n"
        f"👩‍💻 აგენტი: {agent_name}\n"
        f"🆔 შეტყობინების ავტორი (TG id): {submitted_by}\n"
        f"🕒 დრო (UTC): {datetime.utcnow().isoformat()}\n"
    )
    # send to each admin if set
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=aid, text=msg)
        except Exception:
            print(f"Warning: couldn't send admin notification to {aid}")

    # Final user message and end conversation
    await update.message.reply_text("OK TV გისურვებთ წარმატებულ დღეს. 🥰")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ოპერაცია გაუქმდა.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ---- Admin commands ----
@admin_only
async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_offers(limit=50)
    if not rows:
        await update.message.reply_text("DB ცარიელია — არ მოხვედრილა ჩანაწერი.")
        return
    texts = []
    for r in rows:
        (id_, hotel_name, corp_name, address, comment, agent_name, submitted_by, submitted_at) = r
        texts.append(f"{id_}. {hotel_name} | {corp_name} | {agent_name} | {submitted_at}")
    # send in chunks if long
    chunk_size = 10
    for i in range(0, len(texts), chunk_size):
        await update.message.reply_text("\n".join(texts[i:i+chunk_size]))

@admin_only
async def admin_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    path = export_offers_csv()
    await update.message.reply_text(f"ექსპორტი მზად: {path}")
    # send file as document
    try:
        await context.bot.send_document(chat_id=update.effective_chat.id, document=open(path, "rb"))
    except Exception as e:
        await update.message.reply_text(f"ფაილის გაგზავნა ვერ მოხერხდა: {e}")

# ---- Build application and handlers ----
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            STATE_WAIT_HOTEL_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_button_pressed)
            ],
            STATE_CORP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, corp_name_handler)],
            STATE_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, address_handler)],
            STATE_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment_handler)],
            STATE_AGENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, agent_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)

    # admin commands
    app.add_handler(CommandHandler("list", admin_list))
    app.add_handler(CommandHandler("export", admin_export))
    app.add_handler(CommandHandler("cancel", cancel))

    print("Bot started. Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == "__main__":
    main()
