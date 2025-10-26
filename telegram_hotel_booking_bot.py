# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-

import os
import re
import json
import logging
import difflib
from datetime import datetime

import requests
from flask import Flask, request, jsonify, abort

import gspread
from google.oauth2.service_account import Credentials

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

def normalize_text(s: str) -> str:
    """Lowercase; collapse spaces; strip punctuation except Georgian/latin/digits."""
    if not s:
        return ""
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\u10A0-\u10FF ]+", "", s)
    return s

def soft_key(s: str) -> str:
    if not s: return ""
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s

def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, soft_key(a), soft_key(b)).ratio()

def get_all_hotels():
    if not sheet:
        return []
    try:
        return sheet.get_all_records()
    except Exception as e:
        log.warning(f"get_all_hotels error: {e}")
        return []

def headers_map():
    base = {h: idx for idx, h in enumerate(sheet_headers)}
    return {
        "hotel name": base.get("hotel name"),
        "address": base.get("address"),
        "comment": base.get("comment"),
        "contact": base.get("contact"),
        "agent": base.get("agent"),
        "name": base.get("name"),  # timestamp column at your sheet
    }

def append_hotel_row(hotel_name, address, comment="", contact="", agent="", timestamp_str=None):
    """Append row respecting the current header order of the first worksheet."""
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

# =========================
# 5) STATE (in-memory)
# =========================
user_state = {}
# per chat_id:
# {
#   step: None | search_name | search_addr | search_similar | form_comment | form_contact | form_agent
#   name_en, addr_ka
#   candidates: [ {row, score}, ... ]
#   search_ready_for_form: bool  # becomes True ONLY when „სხვა სასტუმროა“ ან საერთოდ ვერ ვიპოვეთ
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

    # Enforce workflow: first SEARCH, then START (unless already allowed)
    if t == "▶️ სტარტი" and not st.get("search_ready_for_form", False):
        # Not allowed yet – push them to search
        send_message(chat_id, "საწყისად გამოიყენე <b>🔍 მოძებნა</b>, რომ გადავამოწმოთ სასტუმრო ბაზაშია თუ არა. მერე გაგრძელდება სტარტი.", kbd_main())
        return jsonify({"ok": True})

    if t == "🔍 მოძებნა" and st.get("step") is None:
        st["step"] = "search_name"
        send_message(chat_id, "ჩაწერე სასტუმროს <b>ოფიციალური სახელი</b> ინგლისურად (მაგ.: <i>Radisson Blu Batumi</i>).")
        return jsonify({"ok": True})

    # ===== SEARCH: name
    if st.get("step") == "search_name":
        if not is_valid_name_en(t):
            send_message(chat_id, "⛔️ ჩაწერე <b>ინგლისურად</b> ოფიციალური სახელი (ლათინური ასოებით).")
            return jsonify({"ok": True})
        st["name_en"] = t
        st["step"] = "search_addr"
        send_message(chat_id, "ახლა ჩაწერე <b>ოფიციალური მისამართი</b> ქართულად (ქალაქი, ქუჩა, ნომერი).")
        return jsonify({"ok": True})

    # ===== SEARCH: address
    if st.get("step") == "search_addr":
        if not is_valid_addr_ka(t):
            send_message(chat_id, "⛔️ მისამართი უნდა შეიცავდეს <b>ქართულ</b> ასოებს. გთხოვ, გამოასწორე და თავიდან ჩაწერე.")
            return jsonify({"ok": True})
        st["addr_ka"] = t

        hotels = get_all_hotels()
        if not hotels:
            send_message(chat_id,
                "⚠️ Hotels Sheet ვერ ვიპოვე. გადაამოწმე <b>SPREADSHEET_ID</b>, Service Account-ის წვდომა და პირველი worksheet.",
                kbd_main()
            )
            reset_state(chat_id)
            return jsonify({"ok": True})

        in_name = st["name_en"]
        in_addr = st["addr_ka"]
        name_norm = normalize_text(in_name)
        addr_norm = normalize_text(in_addr)

        exact_row = None
        cands = []
        for row in hotels:
            r_name = str(row.get("hotel name", "")).strip()
            r_addr = str(row.get("address", "")).strip()

            if normalize_text(r_name) == name_norm and normalize_text(r_addr) == addr_norm:
                exact_row = row
                break

            score = (similarity(r_name, in_name) * 0.6) + (similarity(r_addr, in_addr) * 0.4)
            if score >= 0.67:
                cands.append({"row": row, "score": round(score, 3)})

        if exact_row:
            comment = str(exact_row.get("comment", "") or "—")
            send_message(
                chat_id,
                f"{red_x()} <b>ეს სასტუმრო უკვე გამოკვლეულია</b> და ბაზაშია.\n"
                f"კომენტარი: <i>{comment}</i>\n\n"
                f"ჩატი დასრულდა.",
                kbd_main()
            )
            reset_state(chat_id)
            return jsonify({"ok": True})

        if cands:
            cands.sort(key=lambda x: x["score"], reverse=True)
            st["candidates"] = cands[:3]
            lines = []
            kb_rows = []
            for i, c in enumerate(st["candidates"], start=1):
                r = c["row"]
                lines.append(f"{i}) <b>{r.get('hotel name','')}</b>\n   📍 {r.get('address','')}")
                kb_rows.append([{"text": str(i)}])
            kb_rows.append([{"text": "სხვა სასტუმროა"}])
            send_message(
                chat_id,
                "ზუსტად ვერ ვიპოვე, მაგრამ არის <b>მსგავსი</b> ჩანაწერები. რომელიმეს ეძებ?\n\n" + "\n\n".join(lines),
                {"keyboard": kb_rows, "resize_keyboard": True}
            )
            st["step"] = "search_similar"
            return jsonify({"ok": True})

        # no candidates at all – allow START
        st["search_ready_for_form"] = True
        st["step"] = None
        send_message(chat_id, "✅ ბაზაში ასეთი ჩანაწერი <b>არ არის</b>. ახლა შეგიძლია გაფორმო.\nდააჭირე 👉 <b>▶️ სტარტი</b>.", kbd_main())
        return jsonify({"ok": True})

    # ===== SEARCH: similar choose
    if st.get("step") == "search_similar":
        if t in {"1", "2", "3"} and st.get("candidates"):
            idx = int(t) - 1
            if 0 <= idx < len(st["candidates"]):
                row = st["candidates"][idx]["row"]
                comment = str(row.get("comment", "") or "—")
                send_message(
                    chat_id,
                    f"{red_x()} <b>ეს სასტუმრო უკვე გამოკვლეულია</b> და ბაზაშია.\n"
                    f"კომენტარი: <i>{comment}</i>\n\nჩატი დასრულდა.",
                    kbd_main()
                )
                reset_state(chat_id)
                return jsonify({"ok": True})

        if t == "სხვა სასტუმროა":
            # allow to continue with the form
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
            send_message(chat_id, "⛔️ ფორმატი არასწორია. მიუთითე <b>ტელეფონი</b> ან <b>ელფოსტა</b> სწორად.")
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

        # WRITE to sheet
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
