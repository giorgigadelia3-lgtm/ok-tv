# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-

import os
import re
import json
import time
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
APP_BASE_URL  = os.environ.get("APP_BASE_URL")               # e.g. https://ok-tv-1.onrender.com
BOT_TOKEN     = os.environ.get("TELEGRAM_TOKEN")             # BotFather token
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")            # Google Sheet ID
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

if not APP_BASE_URL or not BOT_TOKEN:
    raise RuntimeError("❌ Set APP_BASE_URL and TELEGRAM_TOKEN in environment.")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(level=logging.INFO, format="%(levelname)s:hotel-bot:%(message)s")
log = logging.getLogger("hotel-bot")

# =========================
# 2) GOOGLE SHEETS CONNECT
# =========================
sheet = None
sheet_headers = []  # cached, lower-cased
try:
    creds_dict = json.loads(GOOGLE_CREDS_JSON or "{}")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    # ALWAYS use first worksheet to avoid title mismatches
    sh = client.open_by_key(SPREADSHEET_ID)
    sheet = sh.get_worksheet(0)
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

def reply_kbd_main():
    return {
        "keyboard": [
            [{"text": "🔍 მოძებნა"}],
            [{"text": "🔁 თავიდან"}],
        ],
        "resize_keyboard": True
    }

def normalize_text(s: str) -> str:
    """Lowercase, trim, strip punctuation; keep latin, digits and Georgian."""
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
    """Return mapping of known headers to indices, lowercase-insensitive."""
    base = {h: idx for idx, h in enumerate(sheet_headers)}
    return {
        "hotel name": base.get("hotel name"),
        "address": base.get("address"),
        "comment": base.get("comment"),
        "contact": base.get("contact"),
        "agent": base.get("agent"),
        "name": base.get("name"),  # we'll store timestamp here if present
    }

def append_hotel_row(hotel_name, address, comment="", contact="", agent="", timestamp_str=None):
    """Append row preserving sheet header order."""
    if not sheet:
        return False, "Sheet unavailable"

    cols = headers_map()
    width = max(len(sheet_headers), 6)
    row = [""] * width

    def put(colkey, val):
        idx = cols.get(colkey)
        if idx is not None:
            # expand if sheet has more columns than we expected
            while idx >= len(row):
                row.append("")
            row[idx] = val

    put("hotel name", hotel_name)
    put("address", address)
    put("comment", comment)
    put("contact", contact)
    put("agent", agent)
    put("name", timestamp_str or datetime.now().strftime("%Y-%m-%d %H:%M"))

    # If header row missing for some reason, fallback default order
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
    """EN hotel name — must contain latin letter."""
    return bool(re.search(r"[A-Za-z]", text)) and len(text.strip()) >= 2

def is_valid_addr_ka(text: str) -> bool:
    """KA address — must include Georgian letters."""
    return bool(re.search(r"[\u10A0-\u10FF]", text)) and len(text.strip()) >= 3

def looks_like_phone(text: str) -> bool:
    s = re.sub(r"[^\d+]", "", text)
    # allow +9955xxxxxxx etc. or 5xxxxxxxx
    return bool(re.fullmatch(r"(\+?\d{9,15})", s))

def looks_like_email(text: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text.strip()))

# =========================
# 5) STATE (in-memory)
# =========================
user_state = {}
# per chat_id:
# {
#   step: one of
#     ask_name_en -> ask_addr_ka -> similar_offer? -> ask_comment -> ask_contact -> ask_agent -> done
#   name_en, addr_ka
#   candidates: [ {row, score}, ... ]
# }

def reset_state(chat_id):
    user_state[chat_id] = {"step": None}

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

    st = user_state.get(chat_id) or {"step": None}
    user_state[chat_id] = st
    t = text.strip()

    # ===== Commands / main buttons
    if t == "/start" or t == "🔁 თავიდან":
        reset_state(chat_id)
        send_message(chat_id, "აირჩიე მოქმედება 👇", reply_kbd_main())
        return jsonify({"ok": True})

    if t == "🔍 მოძებნა" and st.get("step") is None:
        st.update({
            "step": "ask_name_en",
            "candidates": []
        })
        send_message(chat_id, "გთხოვ, ჩაწერე სასტუმროს <b>ოფიციალური სახელი</b> ინგლისურად (მაგ.: <i>Radisson Blu Batumi</i>).")
        return jsonify({"ok": True})

    # ===== Step: ask_name_en
    if st.get("step") == "ask_name_en":
        if not is_valid_name_en(t):
            send_message(chat_id, "⛔️ საეჭვო ფორმატია. ჩაწერე <b>ინგლისურად</b> სასტუმროს ოფიციალური სახელი (ლათინური ასოებით).")
            return jsonify({"ok": True})
        st["name_en"] = t
        st["step"] = "ask_addr_ka"
        send_message(chat_id, "ახლა ჩაწერე <b>ოფიციალური მისამართი</b> ქართულად (ქალაქი, ქუჩა, ნომერი).")
        return jsonify({"ok": True})

    # ===== Step: ask_addr_ka
    if st.get("step") == "ask_addr_ka":
        if not is_valid_addr_ka(t):
            send_message(chat_id, "⛔️ მისამართი უნდა შეიცავდეს <b>ქართულ</b> ასოებს. გთხოვ, გამოასწორე და ხელახლა შეიყვანე.")
            return jsonify({"ok": True})

        st["addr_ka"] = t

        hotels = get_all_hotels()
        if not hotels:
            send_message(
                chat_id,
                "⚠️ Hotels Sheet ვერ ვიპოვე. გადაამოწმე <b>SPREADSHEET_ID</b>, Service Account-ის წვდომა და worksheet-ის პირველი ფურცელი.",
                reply_kbd_main()
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
                f"{red_x()} <b>ეს სასტუმრო უკვე გამოკითხულია.</b>\n"
                f"კომენტარი: <i>{comment}</i>\n\nჩატი დასრულდა.",
                reply_kbd_main()
            )
            reset_state(chat_id)
            return jsonify({"ok": True})

        # Not exact — maybe similar?
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
            st["step"] = "similar_offer"
            return jsonify({"ok": True})

        # No candidates at all → go on with form
        send_message(chat_id, "✅ ბაზაში ეს ჩანაწერი არ იძებნება. გავაგრძელოთ კითხვარი.\n\nჩაწერე <b>კომენტარი</b>.")
        st["step"] = "ask_comment"
        return jsonify({"ok": True})

    # ===== Step: similar_offer
    if st.get("step") == "similar_offer":
        if t in {"1", "2", "3"} and st.get("candidates"):
            idx = int(t) - 1
            if 0 <= idx < len(st["candidates"]):
                row = st["candidates"][idx]["row"]
                comment = str(row.get("comment", "") or "—")
                send_message(
                    chat_id,
                    f"{red_x()} <b>ეს სასტუმრო უკვე გამოკითხულია.</b>\n"
                    f"კომენტარი: <i>{comment}</i>\n\nჩატი დასრულდა.",
                    reply_kbd_main()
                )
                reset_state(chat_id)
                return jsonify({"ok": True})

        if t == "სხვა სასტუმროა":
            send_message(chat_id, "კარგია. ჩაწერე <b>კომენტარი</b> (სიტუაცია, სტატუსი და ა.შ.).")
            st["step"] = "ask_comment"
            return jsonify({"ok": True})

        # If user typed something else, remind choices (do not break the chat)
        send_message(chat_id, "აირჩიე 1, 2, 3 ან 'სხვა სასტუმროა'.")
        return jsonify({"ok": True})

    # ===== Step: ask_comment
    if st.get("step") == "ask_comment":
        st["comment"] = t
        st["step"] = "ask_contact"
        send_message(
            chat_id,
            "ჩაწერე <b>გადამწყვეტის საკონტაქტო</b> — ტელეფონის ნომერი <i>ან</i> ელფოსტა.\n"
            "მაგ.: +9955XXXXXXX ან name@domain.com"
        )
        return jsonify({"ok": True})

    # ===== Step: ask_contact
    if st.get("step") == "ask_contact":
        if not (looks_like_phone(t) or looks_like_email(t)):
            send_message(chat_id, "⛔️ ფორმატი არასწორია. ჩაწერე <b>ტელეფონი</b> ან <b>ელფოსტა</b> სწორ ფორმატში.")
            return jsonify({"ok": True})
        st["contact"] = t
        st["step"] = "ask_agent"
        send_message(chat_id, "ჩაწერე <b>აგენტის სახელი და გვარი</b> (ვინც ამატებს ჩანაწერს).")
        return jsonify({"ok": True})

    # ===== Step: ask_agent
    if st.get("step") == "ask_agent":
        # accept almost anything human-like
        if len(t) < 2:
            send_message(chat_id, "⛔️ ძალიან მოკლეა. ჩაწერე <b>სახელი და გვარი</b>.")
            return jsonify({"ok": True})

        st["agent"] = t

        # Ready to write
        hotel_name = st.get("name_en")
        address    = st.get("addr_ka")
        comment    = st.get("comment", "")
        contact    = st.get("contact", "")
        agent      = st.get("agent", "")
        timestamp  = datetime.now().strftime("%Y-%m-%d %H:%M")

        ok, err = append_hotel_row(
            hotel_name=hotel_name,
            address=address,
            comment=comment,
            contact=contact,
            agent=agent,
            timestamp_str=timestamp
        )
        if ok:
            send_message(chat_id, "✅ ჩანაწერი წარმატებით დაემატა Sheet-ში. წარმატებები! 🎉", reply_kbd_main())
        else:
            send_message(chat_id, f"⚠️ ჩანაწერის დამატება ვერ მოხერხდა: <i>{err}</i>", reply_kbd_main())

        reset_state(chat_id)
        return jsonify({"ok": True})

    # ===== Fallback
    if st.get("step") is None:
        send_message(chat_id, "აირჩიე მენიუდან 👇", reply_kbd_main())
    else:
        # remind user the current expected action implicitly
        send_message(chat_id, "გაგრძელებისთვის გამოიყენე ეკრანზე მოცემული ღილაკები ან დაასრულე '🔁 თავიდან'.")
    return jsonify({"ok": True})

# =========================
# 7) WEBHOOK SETUP (idempotent)
# =========================
def set_webhook():
    try:
        url = f"{APP_BASE_URL}/webhook/{BOT_TOKEN}"
        resp = requests.get(
            f"{API_URL}/setWebhook",
            params={
                "url": url,
                "max_connections": 4,
                "allowed_updates": json.dumps(["message"])
            },
            timeout=10
        )
        ok = resp.ok and resp.json().get("ok", False)
        log.info(f"Webhook set to {url}: {ok}")
    except Exception as e:
        log.error(f"Failed to set webhook: {e}")

set_webhook()

# =========================
# 8) LOCAL RUN
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
