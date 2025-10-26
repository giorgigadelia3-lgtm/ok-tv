# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-

import os
import re
import json
import logging
from datetime import datetime

import requests
from flask import Flask, request, jsonify, abort

import gspread
from google.oauth2.service_account import Credentials

# ✅ ახალი მოდული — მხოლოდ ძებნაზეა პასუხისმგებელი
from hotel_checker import check_hotel  # <— მთავარი ცვლილება

# =========================
# 1) ENV & LOGGING
# =========================
APP_BASE_URL   = os.environ.get("APP_BASE_URL")             # e.g. https://ok-tv-1.onrender.com
BOT_TOKEN      = os.environ.get("TELEGRAM_TOKEN")           # BotFather token
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")           # Google Sheet ID
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

if not APP_BASE_URL or not BOT_TOKEN:
    raise RuntimeError("❌ Set APP_BASE_URL and TELEGRAM_TOKEN in environment.")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(level=logging.INFO, format="%(levelname)s:hotel-bot:%(message)s")
log = logging.getLogger("hotel-bot")

# =========================
# 2) GOOGLE SHEETS CONNECT (always first worksheet)
# — ბოტისთვის მხოლოდ append-ს ვიყენებთ; ძებნას აკეთებს hotel_checker.py
# =========================
sheet = None
sheet_headers = []
try:
    creds_dict = json.loads(GOOGLE_CREDS_JSON or "{}")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    sh = client.open_by_key(SPREADSHEET_ID)
    sheet = sh.get_worksheet(0)  # FIRST worksheet – avoids title mismatches
    headers = sheet.row_values(1)
    sheet_headers = [h.strip().lower() for h in headers]
    log.info("✅ Google Sheets connected (first worksheet).")
except Exception as e:
    log.warning(f"⚠️ Google Sheets connect error: {e}")

# =========================
# 3) FLASK
# =========================
app = Flask(__name__)

# =========================
# 4) HELPERS
# =========================
def send_message(chat_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    try:
        r = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"send_message error: {e}")

def kbd_main():
    return {
        "keyboard": [
            [{"text": "🔍 მოძებნა"}],
            [{"text": "▶️ სტარტი"}],
            [{"text": "🔁 თავიდან"}],
        ],
        "resize_keyboard": True
    }

def red_x() -> str:
    return "🔴✖️"

def is_valid_name_en(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text)) and len(text.strip()) >= 2

def is_valid_addr_ka(text: str) -> bool:
    return bool(re.search(r"[\u10A0-\u10FF]", text)) and len(text.strip()) >= 3

def looks_like_phone(text: str) -> bool:
    s = re.sub(r"[^\d+]", "", text)
    return bool(re.fullmatch(r"(\+?\d{9,15})", s))

def looks_like_email(text: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text.strip()))

# Append helper
def headers_map():
    base = {h: idx for idx, h in enumerate(sheet_headers)}
    # შენს შიტში timestamp ინახება სვეტში „name“ (ასე გქონდა)
    return {
        "hotel name": base.get("hotel name"),
        "address": base.get("address"),
        "comment": base.get("comment"),
        "contact": base.get("contact"),
        "agent": base.get("agent"),
        "name": base.get("name"),
    }

def append_hotel_row(hotel_name, address, comment="", contact="", agent="", timestamp_str=None):
    if not sheet:
        return False, "Sheet unavailable"

    cols = headers_map()
    width = max(len(sheet_headers), 6)
    row = [""] * width

    def put(colkey, val):
        idx = cols.get(colkey)
        if idx is None:
            return
        while idx >= len(row):
            row.append("")
        row[idx] = val

    put("hotel name", hotel_name)
    put("address", address)
    put("comment", comment)
    put("contact", contact)
    put("agent", agent)
    put("name", timestamp_str or datetime.now().strftime("%Y-%m-%d %H:%M"))

    if not sheet_headers:
        row = [hotel_name, address, comment, contact, agent, (timestamp_str or datetime.now().strftime("%Y-%m-%d %H:%M"))]

    try:
        sheet.append_row(row, value_input_option="USER_ENTERED")
        return True, None
    except Exception as e:
        return False, str(e)

# =========================
# 5) STATE (in-memory)
# =========================
user_state = {}
# {
#   step: None | search_name | search_addr | search_similar | form_comment | form_contact | form_agent
#   name_en, addr_ka
#   candidates: [ {hotel_name, address, comment, score, score_name, score_addr}, ... ]
#   search_ready_for_form: bool
# }

def reset_state(cid):
    user_state[cid] = {
        "step": None,
        "candidates": [],
        "search_ready_for_form": False,
        "name_en": "",
        "addr_ka": "",
        "comment": "",
        "contact": "",
        "agent": "",
    }

# =========================
# 6) CORE FLOW
# =========================
@app.route("/", methods=["GET"])
def index():
    return "HotelClaimBot is running."

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook_exact():
    return _process_update()

@app.route("/webhook/<token>", methods=["POST"])
def telegram_webhook_generic(token):
    if token != BOT_TOKEN:
        abort(404)
    return _process_update()

def _process_update():
    try:
        update = request.get_json(force=True, silent=True) or {}
    except Exception:
        update = {}

    message = update.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text", "")

    if not chat_id or not text:
        return jsonify({"ok": True})

    st = user_state.get(chat_id)
    if not st:
        reset_state(chat_id)
        st = user_state[chat_id]

    t = text.strip()

    # ===== Commands / main
    if t == "/start" or t == "🔁 თავიდან":
        reset_state(chat_id)
        send_message(chat_id, "აირჩიე მოქმედება 👇", kbd_main())
        return jsonify({"ok": True})

    # FIRST do search, then allow START
    if t == "▶️ სტარტი" and not st.get("search_ready_for_form", False):
        send_message(chat_id, "საწყისად დააჭირე <b>🔍 მოძებნა</b> — ჯერ ბაზაში გადავამოწმოთ, შემდეგ გაგრძელდება 'სტარტი'.", kbd_main())
        return jsonify({"ok": True})

    if t == "🔍 მოძებნა" and st.get("step") is None:
        st["step"] = "search_name"
        send_message(chat_id, "ჩაწერე სასტუმროს <b>ოფიციალური სახელი</b> ინგლისურად (მაგ.: <i>Radisson Blu Batumi</i>).")
        return jsonify({"ok": True})

    # ===== SEARCH name
    if st.get("step") == "search_name":
        if not is_valid_name_en(t):
            send_message(chat_id, "⛔️ ჩაწერე <b>ინგლისურად</b> ოფიციალური სახელი (ლათინური ასოებით).")
            return jsonify({"ok": True})
        st["name_en"] = t
        st["step"] = "search_addr"
        send_message(chat_id, "ახლა ჩაწერე <b>ოფიციალური მისამართი</b> ქართულად (ქალაქი, ქუჩა, ნომერი).")
        return jsonify({"ok": True})

    # ===== SEARCH address
    if st.get("step") == "search_addr":
        if not is_valid_addr_ka(t):
            send_message(chat_id, "⛔️ მისამართი უნდა შეიცავდეს <b>ქართულ</b> ასოებს. გთხოვ, გამოასწორე და თავიდან ჩაწერე.")
            return jsonify({"ok": True})
        st["addr_ka"] = t

        # ✅ კრიტიკული ცვლილება: ძებნას აკეთებს hotel_checker.py
        try:
            result = check_hotel(st["name_en"], st["addr_ka"])
        except Exception as e:
            send_message(chat_id,
                f"⚠️ მოძებნის შეცდომა: <i>{e}</i>\nგადაამოწმე SPREADSHEET_ID/წვდომები.",
                kbd_main()
            )
            reset_state(chat_id)
            return jsonify({"ok": True})

        status = result.get("status")
        if status == "exact":
            exact = result.get("exact_row") or {}
            comment = str(exact.get("comment", "") or "—")
            send_message(
                chat_id,
                f"{red_x()} <b>ეს სასტუმრო უკვე გამოკითხულია.</b>\n"
                f"კომენტარი: <i>{comment}</i>\n\nჩატი დასრულდა.",
                kbd_main()
            )
            reset_state(chat_id)
            return jsonify({"ok": True})

        if status == "similar":
            cands = result.get("candidates", [])[:3]
            st["candidates"] = cands
            lines = []
            kb_rows = []
            for i, c in enumerate(cands, start=1):
                lines.append(f"{i}) <b>{c.get('hotel_name','')}</b>\n   📍 {c.get('address','')}")
                kb_rows.append([{"text": str(i)}])
            kb_rows.append([{"text": "სხვა სასტუმროა"}])
            send_message(
                chat_id,
                "ზუსტად ვერ ვიპოვე, მაგრამ არის <b>მსგავსი</b> ჩანაწერები. რომელიმეს ეძებ?\n\n" + "\n\n".join(lines),
                {"keyboard": kb_rows, "resize_keyboard": True}
            )
            st["step"] = "search_similar"
            return jsonify({"ok": True})

        # none
        st["search_ready_for_form"] = True
        st["step"] = None
        send_message(chat_id, "✅ ბაზაში ასეთი ჩანაწერი <b>არ არის</b>. ახლა შეგიძლია გააგრძელო.\nდააჭირე 👉 <b>▶️ სტარტი</b>.", kbd_main())
        return jsonify({"ok": True})

    # ===== SEARCH similar choice
    if st.get("step") == "search_similar":
        if t in {"1", "2", "3"} and st.get("candidates"):
            idx = int(t) - 1
            cands = st["candidates"]
            if 0 <= idx < len(cands):
                cm = cands[idx].get("comment") or "—"
                send_message(
                    chat_id,
                    f"{red_x()} <b>ეს სასტუმრო უკვე მსგავს ჩანაწერებშია.</b>\n"
                    f"კომენტარი: <i>{cm}</i>\n\nჩატი დასრულდა.",
                    kbd_main()
                )
                reset_state(chat_id)
                return jsonify({"ok": True})

        if t == "სხვა სასტუმროა":
            st["search_ready_for_form"] = True
            st["step"] = None
            send_message(chat_id, "გასაგებია. ახლა შეგიძლია შეავსო ინფორმაცია. დააჭირე 👉 <b>▶️ სტარტი</b>.", kbd_main())
            return jsonify({"ok": True})

        send_message(chat_id, "აირჩიე 1, 2, 3 ან 'სხვა სასტუმროა'.")
        return jsonify({"ok": True})

    # ===== FORM (available only after search_ready_for_form=True)
    if t == "▶️ სტარტი" and st.get("search_ready_for_form", False):
        st["step"] = "form_comment"
        send_message(chat_id, "ჩაწერე <b>კომენტარი</b> (სტატუსი/შენიშვნა).")
        return jsonify({"ok": True})

    if st.get("step") == "form_comment":
        st["comment"] = t
        st["step"] = "form_contact"
        send_message(chat_id, "ჩაწერე <b>გადამწყვეტის საკონტაქტო</b> — ტელეფონი <i>ან</i> ელფოსტა. მაგ.: +9955XXXXXXX ან name@domain.com")
        return jsonify({"ok": True})

    if st.get("step") == "form_contact":
        if not (looks_like_phone(t) or looks_like_email(t)):
            send_message(chat_id, "⛔️ ფორმატი არასწორია. მიუთითე <b>ტელეფონი</b> ან <b>ელფოსტা</b> სწორად.")
            return jsonify({"ok": True})
        st["contact"] = t
        st["step"] = "form_agent"
        send_message(chat_id, "ჩაწერე <b>აგენტის სახელი და გვარი</b> (ვინც ამატებს ჩანაწერს).")
        return jsonify({"ok": True})

    if st.get("step") == "form_agent":
        if len(t) < 2:
            send_message(chat_id, "⛔️ ძალიან მოკლეა. ჩაწერე <b>სახელი და გვარი</b>.")
            return jsonify({"ok": True})
        st["agent"] = t

        ok, err = append_hotel_row(
            hotel_name=st.get("name_en"),
            address=st.get("addr_ka"),
            comment=st.get("comment", ""),
            contact=st.get("contact", ""),
            agent=st.get("agent", ""),
            timestamp_str=datetime.now().strftime("%Y-%m-%d %H:%M")
        )
        if ok:
            send_message(chat_id, "✅ ჩანაწერი წარმატებით დაემატა Sheet-ში. წარმატებები! 🎉", kbd_main())
        else:
            send_message(chat_id, f"⚠️ ჩანაწერის დამატება ვერ მოხერხდა: <i>{err}</i>", kbd_main())

        reset_state(chat_id)
        return jsonify({"ok": True})

    # ===== Fallback
    if st.get("step") is None:
        send_message(chat_id, "აირჩიე მოქმედება 👇", kbd_main())
    else:
        send_message(chat_id, "გაგრძელებისთვის გამოიყენე ეკრანზე მოცემული ღილაკები ან '🔁 თავიდან'.")
    return jsonify({"ok": True})

# =========================
# 7) WEBHOOK SETUP (idempotent, exact token route)
# =========================
def set_webhook():
    try:
        url = f"{APP_BASE_URL}/webhook/{BOT_TOKEN}"
        resp = requests.get(
            f"{API_URL}/setWebhook",
            params={"url": url, "max_connections": 4, "allowed_updates": json.dumps(["message"])},
            timeout=10
        )
        ok = resp.ok and resp.json().get("ok", False)
        log.info(f"Webhook set to {url}: {ok}")
    except Exception as e:
        log.error(f"Failed to set webhook: {e}")

set_webhook()

# =========================
# 8) LOCAL RUN (dev only)
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
