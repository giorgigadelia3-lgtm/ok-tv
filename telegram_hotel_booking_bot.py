import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN") or "შენი_ბოტის_ტოკენი"
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# მომხმარებლების სტეპების დროებითი საცავი (შეგიძლია მერე DB-თაც შეცვალო)
user_state = {}

# === HELPER FUNCTIONS ===
def send_message(chat_id, text, reply_markup=None):
    """აგზავნის შეტყობინებას Telegram-ში"""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{API_URL}/sendMessage", json=payload)

def build_start_keyboard():
    """დასაწყისის ღილაკი"""
    return {
        "keyboard": [[{"text": "დავიწყოთ / Start"}]],
        "resize_keyboard": True,
        "one_time_keyboard": True
    }

# === ROUTES ===
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = request.get_json()
    print("📩 Received update:", update)

    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "").strip()

        # თუ ახალი მომხმარებელია — იწყება "დავიწყოთ / Start"
        if text in ["/start", "დავიწყოთ / Start"]:
            user_state[chat_id] = "awaiting_corporation"
            send_message(chat_id, "შეიყვანეთ კორპორაციის სახელი. 🏢")
            return jsonify({"ok": True})

        # === საფეხური 1: კორპორაციის სახელი ===
        elif user_state.get(chat_id) == "awaiting_corporation":
            user_state[chat_id] = {
                "step": "awaiting_address",
                "corporation": text
            }
            send_message(chat_id, "შეიყვანეთ მისამართი. 📍")
            return jsonify({"ok": True})

        # === საფეხური 2: მისამართი ===
        elif isinstance(user_state.get(chat_id), dict) and user_state[chat_id].get("step") == "awaiting_address":
            user_state[chat_id]["address"] = text
            user_state[chat_id]["step"] = "awaiting_comment"
            send_message(chat_id, "კომენტარი. 📩")
            return jsonify({"ok": True})

        # === საფეხური 3: კომენტარი ===
        elif isinstance(user_state.get(chat_id), dict) and user_state[chat_id].get("step") == "awaiting_comment":
            user_data = user_state[chat_id]
            corporation = user_data.get("corporation")
            address = user_data.get("address")
            comment = text

            # აქ შეგიძლია დაამატო შენახვა DB-ში თუ გინდა
            print(f"✅ ახალი ჩანაწერი:\nკორპორაცია: {corporation}\nმისამართი: {address}\nკომენტარი: {comment}\n")

            send_message(chat_id, "მადლობა OK TV-სგან. 🥰")

            # conversation დასრულდა
            del user_state[chat_id]
            return jsonify({"ok": True})

        else:
            send_message(chat_id, "დააჭირე 'დავიწყოთ / Start' დასაწყებად. 🚀", reply_markup=build_start_keyboard())
            return jsonify({"ok": True})

    return jsonify({"ok": True})


@app.route("/")
def home():
    return "🏨 HotelClaimBot is active and ready!"


# === MAIN ===
if __name__ == "__main__":
    webhook_url = f"https://ok-tv-1.onrender.com/{BOT_TOKEN}"
    set_hook = requests.get(f"{API_URL}/setWebhook?url={webhook_url}")
    print("🔗 Webhook response:", set_hook.text)

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

