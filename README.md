# OK-TV Telegram Bot (Webhook + Google Sheets)

Env ცვლადები Render-ზე:
- TELEGRAM_TOKEN
- APP_BASE_URL           # напр.: https://ok-tv-1.onrender.com
- SPREADSHEET_ID         # თქვენი Google Sheet-ის ID
- GOOGLE_SERVICE_ACCOUNT_JSON  # Service Account JSON მთლიანად (როგორც Text secret)

Deploy:
- `requirements.txt` + `Procfile`
- Start Command: `gunicorn telegram_hotel_booking_bot:app --bind 0.0.0.0:$PORT --timeout 120`

Webhook:
- აპი ავტომატურად დააყენებს ვებჰუქს APP_BASE_URL + `/webhook/<TOKEN>`

