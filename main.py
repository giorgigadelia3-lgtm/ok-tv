import os
import telebot
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# --- Telegram Token და Sheet ID ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

bot = telebot.TeleBot(BOT_TOKEN)

# --- Google Sheets ავტორიზაცია ---
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("service_account.json", scope)
client = gspread.authorize(creds)
sheet = client.open_by_key(SPREADSHEET_ID).sheet1

# --- მომხმარებლის დროებითი მონაცემების შესანახად ---
user_data = {}

# --- საწყისი ბრძანება ---
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "გამარჯობა! 🏨 გთხოვ მიუთითო სასტუმროს სახელი:")
    bot.register_next_step_handler(message, get_hotel_name)

def get_hotel_name(message):
    user_data[message.chat.id] = {"hotel_name": message.text}
    bot.send_message(message.chat.id, "📍 ჩაწერე სასტუმროს მისამართი:")
    bot.register_next_step_handler(message, get_address)

def get_address(message):
    user_data[message.chat.id]["address"] = message.text
    bot.send_message(message.chat.id, "💬 შეიყვანე კომენტარი:")
    bot.register_next_step_handler(message, get_comment)

def get_comment(message):
    user_data[message.chat.id]["comment"] = message.text
    bot.send_message(message.chat.id, "👤 შენი სახელი (აგენტის სახელი):")
    bot.register_next_step_handler(message, get_agent)

def get_agent(message):
    user_data[message.chat.id]["agent"] = message.text
    user_data[message.chat.id]["date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    data = user_data[message.chat.id]

    # --- მონაცემების ჩაწერა Sheet-ში ---
    sheet.append_row([
        data["hotel_name"],
        data["address"],
        data["comment"],
        data["agent"],
        message.from_user.first_name,
        data["date"]
    ])

    bot.send_message(message.chat.id, "✅ ინფორმაცია წარმატებით ჩაიწერა Google Sheet-ში!")
    del user_data[message.chat.id]

# --- ბოტის გაშვება ---
bot.polling(non_stop=True)
