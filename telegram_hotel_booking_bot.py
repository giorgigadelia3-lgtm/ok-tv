import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN") or "შენი_ტოკენი"

user_state = {}
registered_corps = []

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def receive_update():
    update = request.get_json()
    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")

        # =============== ახალი დიალოგის ლოგიკა ===============
        if text in ["დაწყება / start. 🚀", "დაწყება 🚀", "/start", "start"]:
            user_state[chat_id] = "enter_corp_name"
            send_message(chat_id, "შეიყვანეთ კორპორაციის სახელი. 🏢")

        elif user_state.get(chat_id) == "enter_corp_name":
            corp_name = text.strip()
            if corp_name.lower() in [c.lower() for c in registered_corps]:
                send_message(chat_id, "კორპორაციისთვის შეთავაზება მიწოდებულია. ❌️")
                user_state.pop(chat_id, None)
            else:
                registered_corps.append(corp_name)
                send_message(chat_id, "კორპორაცია თავისუფალია, გისურვებთ წარმატებებს. ✅️")
                user_state[chat_id] = "enter_address"
                send_message(chat_id, "შეიყვანეთ მისამართი. 📍")

        elif user_state.get(chat_id) == "enter_address":
            address = text.strip()
            user_state[chat_id] = "enter_comment"
            send_message(chat_id, "კომენტარი. 📩")

        elif user_state.get(chat_id) == "enter_comment":
            comment = text.strip()
            user_state[chat_id] = "enter_agent"
            send_message(chat_id, "აგენტის სახელი და გვარი. 👩‍💻")

        elif user_state.get(chat_id) == "enter_agent":
            agent = text.strip()
            user_state.pop(chat_id, None)
            send_message(chat_id, "OK TV გისურვებთ წარმატებულ დღეს. 🥰")

    return jsonify({"ok": True})


@app.route('/')
def home():
    return "🏨 HotelClaimBot is running and webhook is active!"


def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    requests.post(url, json=payload)


if __name__ == '__main__':
    webhook_url = f"https://ok-tv-1.onrender.com/{BOT_TOKEN}"
    set_hook = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}")
    print("🔗 Webhook response:", set_hook.text)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
