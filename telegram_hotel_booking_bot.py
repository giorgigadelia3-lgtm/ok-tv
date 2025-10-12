import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN") or "შენი_ტოკენი_აქ_ჩასვი"

# ======================
#   1. ბოტის Webhook Route
# ======================
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def receive_update():
    update = request.get_json()
    print("📩 Received update:", update)

    # აიღე მესიჯი და უპასუხე
    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")

        reply = f"✅ შენი მესიჯი მივიღე: {text}"
        send_message(chat_id, reply)

    return jsonify({"ok": True})

# ======================
#   2. მთავარ გვერდზე სტატუსის შემოწმება
# ======================
@app.route('/')
def home():
    return "🏨 HotelClaimBot is running and webhook is active!"

# ======================
#   3. მესიჯის გაგზავნის ფუნქცია
# ======================
def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    requests.post(url, json=payload)

# ======================
#   4. Flask-ის გაშვება + Webhook დაყენება
# ======================
if __name__ == '__main__':
    webhook_url = f"https://ok-tv-1.onrender.com/{BOT_TOKEN}"
    set_hook = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}")
    print("🔗 Webhook response:", set_hook.text)

    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
