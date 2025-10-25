# HotelClaimBot (Render + Telegram Webhook)

ENV:
- TELEGRAM_TOKEN
- APP_BASE_URL (e.g. https://ok-tv-1.onrender.com)
- SPREADSHEET_ID
- GOOGLE_SERVICE_ACCOUNT_JSON
- SHEET_NAME (optional, default "Hotels")

Routes:
- GET /           -> health check
- POST /<TOKEN>   -> telegram webhook endpoint

Buttons:
- "🔎 მოძებნა"  -> ჯერ სასტუმროს სახელი (EN), შემდეგ მისამართი (KA), მოძებნა Google Sheet-ში, fuzzy match-ით.
- "▶️ სტარტი"   -> (თუ არ მოიძებნა) იწყებს კითხვარს და ახალ ჩანაწერს წერს Sheet-ში.
