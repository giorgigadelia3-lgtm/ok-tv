import os
from flask import Flask, request
import telebot
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Flask app for webhook
app = Flask(__name__)

# Environment Variables
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
SPREADSHEET_KEY = os.getenv("GSPREAD_SHEET_KEY")
APP_URL = os.getenv("APP_BASE_URL")  # https://ok-tv-1.onrender.com

# Telegram Bot Initialization
bot = telebot.TeleBot(BOT_TOKEN)

# Google Sheet Connection
def connect_google_sheet():
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        eval(creds_json),
        ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    )
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_KEY).sheet1

# Basic start command
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "ბოტი წარმატებით ფუნქციონირებს ✅\nმომწერე 'მოგესალმე' ტესტისთვის 😊")

# Example message handler
@bot.message_handler(func=lambda message: True)
def echo_message(message):
    if "მოგესალმე" in message.text:
        bot.reply_to(message, "გაგიმარჯოს! 😎 ყველაფერი მუშაობს 🚀")
    else:
        bot.reply_to(message, "შენი შეტყობინება მივიღე ✅\nმაგრამ ჯერ მხოლოდ ტესტ რეჟიმში ვარ 🤖")

# Flask webhook endpoint
@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

# Home page for testing
@app.route("/", methods=["GET"])
def home():
    return "Bot is running ✅", 200

# Run Flask locally
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
