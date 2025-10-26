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
APP_BASE_URL = os.environ.get("APP_BASE_URL")              # e.g. https://ok-tv-1.onrender.com
BOT_TOKEN     = os.environ.get("TELEGRAM_TOKEN")           # BotFather token
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")          # Google Sheet ID
GOOGLE_CREDS_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

if not APP_BASE_URL or not BOT_TOKEN:
    raise RuntimeError("❌ Set APP_BASE_URL and TELEGRAM_TOKEN in environment.")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:hotel-bot:%(message)s"
)
log = logging.getLogger("hotel-bot")


# =========================
# 2) GOOGLE SHEETS CONNECT
# =========================
sheet = None
sheet_headers = []  # cache headers in lower-case
try:
    creds_dict = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)

    # open by key, take the FIRST worksheet (index 0) to avoid title mismatches
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
            [{"text": "🔍 მოძებნა"}]
        ],
        "resize_keyboard": True
    }


def normalize_text(s: str) -> str:
    """Lowercase, remove spaces & punctuation for strict matching."""
    if not s:
        return ""
    s = s.strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\u10A0-\u10FF ]+", "", s)  # keep latin, digits, Georgian letters, spaces
    return s


def soft_key(text: str) -> str:
    """Softer key for fuzzy matching: just lower & condense spaces."""
    if not text:
        return ""
    s = text.strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, soft_key(a), soft_key(b)).ratio()


def get_all_hotels():
    """Return list of dict rows. If sheet is missing, return []."""
    if not sheet:
        return []
    try:
        return sheet.get_all_records()
    except Exception as e:
        log.warning(f"get_all_hotels error: {e}")
        return []


def headers_map():
    """
    Map known headers -> column index.
    We support: hotel name, address, comment, Contact, agent, name
    """
    base = {h: idx for idx, h in enumerate(sheet_headers)}
    return {
        "hotel name": base.get("hotel name"),
        "address": base.get("address"),
        "comment": base.get("comment"),
        "contact": base.get("contact"),
        "agent": base.get("agent"),
        "name": base.get("name"),
    }


def append_hotel_row(hotel_name, address, comment="", contact="", agent="", name=""):
    """Append row preserving column order."""
    if not sheet:
        return False, "Sheet unavailable"

    cols = headers_map()
    # if headers empty, fallback dumb order
    row = [""] * max(6, len(sheet_headers))

    def put(key, val):
        idx = cols.get(key)
        if idx is not None and idx < len(row):
            row[idx] = val

    put("hotel name", hotel_name)
    put("address", address)
    put("comment", comment)
    put("contact", contact)
    put("agent", agent)
    put("name", name)

    # If we don't have headers (empty sheet): write in default order
    if not sheet_headers:
        row = [hotel_name, address, comment, contact, agent, name]

    try:
        sheet.append_row(row, value_input_option="USER_ENTERED")
        return True, None
    except Exception as e:
        return False, str(e)


def red_x() -> str:
    # Using red circle + X visually (Telegram may not render red X emoji reliably)
    return "🔴✖️"


# =========================
# 5) STATE
# =========================
# In-memory state – fine for single dyno bots.
user_state = {}
# Structure per chat_id:
# {
#   "step": "ask_name_en" | "ask_addr_ka" | "confirm_for_start" | None
#   "name_en": "...",
#   "addr_ka": "...",
#   "search_suggestions": [ {row, score}, ...],   # optional
#   "search_exact_found": {row} | None
#   "pending_name": "...",  # used in start confirmation
#   "pending_addr": "..."
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
    # This exact route is used by set_webhook(); accept immediately
    return _process_update()

@app.route("/webhook/<token>", methods=["POST"])
def telegram_webhook(token):
    # Generic route – accept only if token matches
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

    # Commands
    if text == "/start":
        reset_state(chat_id)
        send_message(chat_id, "აირჩიე მოქმედება 👇", reply_kbd_main())
        return jsonify({"ok": True})

    # MAIN BUTTON
    if text.strip() == "🔍 მოძებნა":
        st["step"] = "ask_name_en"
        st["search_suggestions"] = []
        st["search_exact_found"] = None
        send_message(chat_id, "გთხოვ, ჩაწერე სასტუმროს <b>ოფიციალური სახელი</b> ინგლისურად (მაგ.: <i>Radisson Blu Batumi</i>).")
        return jsonify({"ok": True})

    # STEP: ask_name_en
    if st.get("step") == "ask_name_en":
        st["name_en"] = text.strip()
        st["step"] = "ask_addr_ka"
        send_message(chat_id, "ახლა ჩაწერე <b>ოფიციალური მისამართი</b> ქართულად (ქალაქი, ქუჩა, ნომერი).")
        return jsonify({"ok": True})

    # STEP: ask_addr_ka
    if st.get("step") == "ask_addr_ka":
        st["addr_ka"] = text.strip()

        # SEARCH in sheet
        hotels = get_all_hotels()
        if not hotels:
            send_message(chat_id,
                         "⚠️ გაქვს ჩართული Hotels შიტი? გადაამოწმე <b>SPREADSHEET_ID</b>/Service Account და წვდომა. "
                         "ჯერჯერობით ვერ ვნახე მონაცემები.", reply_kbd_main())
            reset_state(chat_id)
            return jsonify({"ok": True})

        in_name = st["name_en"]
        in_addr = st["addr_ka"]

        in_name_norm = normalize_text(in_name)
        in_addr_norm = normalize_text(in_addr)

        exact_row = None
        candidates = []
        for row in hotels:
            r_name = str(row.get("hotel name", "")).strip()
            r_addr = str(row.get("address", "")).strip()

            if normalize_text(r_name) == in_name_norm and normalize_text(r_addr) == in_addr_norm:
                exact_row = row
                break

            # fuzzy collect
            score = (similarity(r_name, in_name) * 0.6) + (similarity(r_addr, in_addr) * 0.4)
            if score >= 0.65:  # threshold of "slight" similarity
                candidates.append({"row": row, "score": round(score, 3)})

        if exact_row:
            # Already surveyed
            comment = str(exact_row.get("comment", "") or "—")
            send_message(
                chat_id,
                f"{red_x()} <b>ეს სასტუმრო უკვე გამოკითხულია</b>.\n"
                f"კომენტარი: <i>{comment}</i>\n\n"
                f"ჩატი დასრულდა."
            )
            reset_state(chat_id)
            return jsonify({"ok": True})

        # No exact: maybe suggestions?
        if candidates:
            # sort by score desc, show up to 3
            candidates.sort(key=lambda x: x["score"], reverse=True)
            st["search_suggestions"] = candidates[:3]
            sug_lines = []
            for i, c in enumerate(st["search_suggestions"], start=1):
                r = c["row"]
                sug_lines.append(f"{i}) <b>{r.get('hotel name','')}</b>\n📍 {r.get('address','')}")
            send_message(
                chat_id,
                "ზუსტად ვერ ვიპოვე. მაგრამ არის <b>მსგავსი</b> ჩანაწერები:\n\n" +
                "\n\n".join(sug_lines) +
                "\n\nთუ इनमें დიდად ემთხვევა – ალბათ <b>უკვე გამოკითხულია</b>.\n"
                "თუ არა – შეგიძლია დაუკავშირდე ამ სასტუმროს ან გააგრძელო კითხვარი 👉 <b>▶️ სტარტი</b>."
            )
        else:
            send_message(
                chat_id,
                "✅ ბაზაში ეს სასტუმრო <b>არ არის</b>.\n"
                "შეგიძლია დაუკავშირდე სასტუმროს ან გააგრძელო კითხვარი 👉 <b>▶️ სტარტი</b>."
            )

        # Ask for start
        st["step"] = "confirm_for_start"
        st["pending_name"] = in_name
        st["pending_addr"] = in_addr

        # Inline keyboard for START + if suggestions exist – quick confirm buttons
        kb = {
            "keyboard": [
                [{"text": "▶️ სტარტი"}],
                [{"text": "🔍 მოძებნა"}]
            ],
            "resize_keyboard": True
        }
        send_message(chat_id, "რას იზამ? აირჩიე:", kb)
        return jsonify({"ok": True})

    # STEP: confirm_for_start
    if st.get("step") == "confirm_for_start":
        # if user selects one of the suggested (by typing its index or name),
        # treat as already surveyed
        txt = text.strip().lower()
        if txt in {"1", "2", "3"} and st.get("search_suggestions"):
            idx = int(txt) - 1
            if 0 <= idx < len(st["search_suggestions"]):
                row = st["search_suggestions"][idx]["row"]
                comment = str(row.get("comment", "") or "—")
                send_message(
                    chat_id,
                    f"{red_x()} <b>ეს სასტუმრო უკვე გამოკითხულია</b>.\n"
                    f"კომენტარი: <i>{comment}</i>\n\n"
                    f"ჩატი დასრულდა."
                )
                reset_state(chat_id)
                return jsonify({"ok": True})

        if txt in {"▶️ სტარტი", "სტარტი", "start", "/start"}:
            # Before continuing, one more confirmation that they will use the same name/address
            send_message(
                chat_id,
                "დავიწყოთ.\n\n"
                f"გთხოვ, <b>გაიმეორე სასტუმროს სახელი (EN)</b> ზუსტად ისე, როგორც შეიყვანე:\n"
                f"<i>{st.get('pending_name')}</i>"
            )
            st["step"] = "confirm_name_again"
            return jsonify({"ok": True})

        # Any other text – allow re-search
        if txt == "🔍 მოძებნა":
            st["step"] = "ask_name_en"
            send_message(chat_id, "კარგი. თავიდან დავიწყოთ – ჩაწერე <b>სახელი</b> ინგლისურად.")
            return jsonify({"ok": True})

        # else ignore & show options again
        send_message(chat_id, "აირჩიე: ▶️ სტარტი ან 🔍 მოძებნა", reply_kbd_main())
        return jsonify({"ok": True})

    # STEP: confirm_name_again
    if st.get("step") == "confirm_name_again":
        provided = text.strip()
        if similarity(provided, st.get("pending_name")) < 0.87:
            send_message(chat_id,
                         "შეყვანილი სახელი <b>არ ემთხვევა</b> ძიებისას გამოყენებულს. "
                         "გთხოვ, გადაამოწმე ორთოგრაფია და ახლიდან დაწერე ზუსტად იგივე.")
            return jsonify({"ok": True})

        st["confirmed_name"] = provided
        st["step"] = "confirm_addr_again"
        send_message(
            chat_id,
            "კარგია ✅\nახლა <b>გაიმეორე მისამართი (KA)</b> ზუსტად ისე, როგორც შეიყვანე:\n"
            f"<i>{st.get('pending_addr')}</i>"
        )
        return jsonify({"ok": True})

    # STEP: confirm_addr_again
    if st.get("step") == "confirm_addr_again":
        provided = text.strip()
        if similarity(provided, st.get("pending_addr")) < 0.87:
            send_message(chat_id,
                         "შეყვანილი მისამართი <b>არ ემთხვევა</b> ძიებისას გამოყენებულს. "
                         "გთხოვ, გადაამოწმე და შეასწორე.")
            return jsonify({"ok": True})

        # All good – write to sheet
        hotel_name = st.get("confirmed_name") or st.get("pending_name")
        address = provided

        # Optional meta
        agent = ""     # სურვილის შემთხვევაში აქ შეგიძლია ჩააწერინო ოპერატორის სახელი
        contact = ""   # ასევე საკონტაქტო ნომერი
        comment = f"დაემატა ბოტიდან {datetime.now().strftime('%d.%m.%y, %H:%M')}"

        ok, err = append_hotel_row(hotel_name, address, comment=comment, contact=contact, agent=agent, name="")
        if ok:
            send_message(chat_id, "✅ ჩანაწერი წარმატებით დაემატა Sheet-ში.\nმადლობა! ჩატი დასრულდა.", reply_kbd_main())
        else:
            send_message(chat_id, f"⚠️ ჩანაწერის დამატება ვერ მოხერხდა: <i>{err}</i>", reply_kbd_main())

        reset_state(chat_id)
        return jsonify({"ok": True})

    # fallback
    send_message(chat_id, "აირჩიე მენიუდან 👇", reply_kbd_main())
    return jsonify({"ok": True})


# =========================
# 7) WEBHOOK SETUP
# =========================
def set_webhook():
    """Idempotent webhook setter – avoids 429 spam and handles both routes."""
    try:
        # Use the exact route
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
# 8) APP RUN (local dev)
# =========================
if __name__ == "__main__":
    # For local tests only
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
