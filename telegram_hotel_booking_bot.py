# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-

import os
import re
import json
import logging
import difflib
import unicodedata
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
# 4) TEXT NORMALIZATION & MATCHING
# =========================

KA_ADDR_EQUIV = {
    # frequent abbreviations / variants -> canonical
    "ქ.": "ქუჩა", "ქ": "ქუჩა",
    "ქუჩ.": "ქუჩა",
    "გამზ.": "გამზირი", "გამზ": "გამზირი",
    "ბულვ.": "ბულვარი",
    "ბათუმის ბულვარი": "ბათუმის ბულვარი",
    "რესპ.": "რესპუბლიკა",
    "№": "", "ნ.": "", "N": "",
}

KA_ADDR_STOPWORDS = {
    # generic words that don't change identity
    "საქართველო","ქალაქი","სადგური","მიკრორაიონი","მ/რ","უბანი",
    "სოფელი","სოფ.","სოფ","დასრულდა","აღმართი","ჩასახვევი","შესახვევი",
    "კორპუსი","კორპ.","კორპ","კომერციული","შენობა","სქაიტელი","სკაიტელი",
    # very common cities to soften over-strictness
    "თბილისი","ბათუმი","ქუთაისი","გუდაური","ბაკურიანი","ბორჯომი","ყაზბეგი","მცხეთა","თელავი"
}

def _unicode_simplify(s: str) -> str:
    if not s: return ""
    s = unicodedata.normalize("NFKC", s)
    return s

def normalize_text_generic(s: str) -> str:
    """Lower, normalize unicode, collapse whitespace, strip punctuation (keep ka/latin/digits)."""
    if not s: return ""
    s = _unicode_simplify(s).lower()
    # unify some special dashes & quotes
    s = s.replace("–", "-").replace("—", "-").replace("‚", "'").replace("’", "'")
    # collapse multiple spaces/newlines/tabs
    s = re.sub(r"\s+", " ", s)
    # keep letters (ka + latin) and digits and spaces
    s = re.sub(r"[^\w\u10A0-\u10FF -]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def normalize_name_en(s: str) -> str:
    """For hotel names (English/Latin)."""
    s = normalize_text_generic(s)
    # remove most punctuation/dashes for strict compare
    s = re.sub(r"[-_]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def normalize_address_ka(s: str) -> str:
    """For Georgian addresses: expand abbreviations, remove noise, canonical tokens."""
    s = normalize_text_generic(s)
    # expand abbreviations
    for k, v in KA_ADDR_EQUIV.items():
        s = s.replace(k, v)
    # remove commas / dashes used as separators
    s = s.replace(",", " ").replace("/", " ").replace("-", " ")
    # collapse again
    s = re.sub(r"\s+", " ", s).strip()
    return s

def tokens_ka(s: str):
    s = normalize_address_ka(s)
    toks = [t for t in s.split() if t and t not in KA_ADDR_STOPWORDS]
    return toks

def jaccard(a_tokens, b_tokens) -> float:
    if not a_tokens or not b_tokens:
        return 0.0
    A, B = set(a_tokens), set(b_tokens)
    inter = len(A & B)
    union = len(A | B)
    return inter / union if union else 0.0

def similarity_soft(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()

def looks_like_same_hotel(input_name, input_addr, row_name, row_addr) -> (bool, float, float, float):
    """
    Returns (is_same, score_total, score_name, score_addr)
    Combines strict equality (after strong normalization), soft similarity and token Jaccard for addresses.
    """
    in_name = normalize_name_en(input_name)
    in_addr = normalize_address_ka(input_addr)
    r_name = normalize_name_en(row_name or "")
    r_addr = normalize_address_ka(row_addr or "")

    # very strict equality (ignore spaces & punctuation)
    def strip_strict(x): return re.sub(r"[^\w\u10A0-\u10FF]+", "", x)
    if strip_strict(in_name) and strip_strict(in_name) == strip_strict(r_name) \
       and strip_strict(in_addr) and strip_strict(in_addr) == strip_strict(r_addr):
        return True, 1.0, 1.0, 1.0

    # name & address soft scores
    name_soft = similarity_soft(in_name, r_name)              # 0..1
    addr_soft = similarity_soft(in_addr, r_addr)              # 0..1
    jacc = jaccard(tokens_ka(in_addr), tokens_ka(r_addr))     # 0..1

    # aggregate: name dominates; address uses max(soft, jacc)
    addr_score = max(addr_soft, jacc)
    total = (name_soft * 0.65) + (addr_score * 0.35)

    # decision rules tuned for your data:
    # 1) very high name and decent addr
    if name_soft >= 0.92 and addr_score >= 0.60:
        return True, total, name_soft, addr_score
    # 2) strong total (covers minor typos, order flips)
    if total >= 0.82:
        return True, total, name_soft, addr_score
    # 3) exact name, loose addr with good token overlap
    if strip_strict(in_name) == strip_strict(r_name) and jacc >= 0.65:
        return True, total, name_soft, addr_score

    return False, total, name_soft, addr_score

# =========================
# 5) TELEGRAM HELPERS
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

# =========================
# 6) INPUT VALIDATION
# =========================
def is_valid_name_en(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text)) and len(text.strip()) >= 2

def is_valid_addr_ka(text: str) -> bool:
    return bool(re.search(r"[\u10A0-\u10FF]", text)) and len(text.strip()) >= 3

def looks_like_any_phone(s: str) -> bool:
    """Accepts one or many phone numbers separated by any separators."""
    s = _unicode_simplify(s)
    phones = re.findall(r"\+?\d{7,15}", s)
    return len(phones) >= 1

def looks_like_any_email(s: str) -> bool:
    return bool(re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", s))

# =========================
# 7) SHEETS I/O
# =========================
def get_all_hotels():
    if not sheet:
        return []
    try:
        return sheet.get_all_records()  # handles multiline cells too
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

# =========================
# 8) STATE (in-memory)
# =========================
user_state = {}
# per chat_id:
# {
#   step: None | search_name | search_addr | search_similar | form_comment | form_contact | form_agent
#   name_en, addr_ka
#   candidates: [ {row, score}, ... ]
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
# 9) CORE FLOW
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

        in_name_raw = st["name_en"]
        in_addr_raw = st["addr_ka"]

        exact_found_row = None
        similar_candidates = []

        for row in hotels:
            r_name = str(row.get("hotel name", "") or "").strip()
            r_addr = str(row.get("address", "") or "").strip()

            is_same, total, name_s, addr_s = looks_like_same_hotel(in_name_raw, in_addr_raw, r_name, r_addr)
            if is_same:
                exact_found_row = row
                break

            # keep top-3 similar for user confirmation (but below "same" threshold)
            if total >= 0.67:
                similar_candidates.append({"row": row, "score": round(float(total), 3)})

        if exact_found_row:
            comment = str(exact_found_row.get("comment", "") or "—")
            send_message(
                chat_id,
                f"{red_x()} <b>ეს სასტუმრო უკვე გამოკვლეულია</b> და ბაზაშია.\n"
                f"კომენტარი: <i>{comment}</i>\n\n"
                f"ჩატი დასრულდა.",
                kbd_main()
            )
            reset_state(chat_id)
            return jsonify({"ok": True})

        if similar_candidates:
            # sort & show max 3
            similar_candidates.sort(key=lambda x: x["score"], reverse=True)
            st["candidates"] = similar_candidates[:3]
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
        send_message(chat_id, "ჩაწერე <b>გადამწყვეტის საკონტაქტო</b> — ტელეფონი(ები) <i>ან</i> ელფოსტა. მაგ.: +9955XXXXXXX ან name@domain.com")
        return jsonify({"ok": True})

    if st.get("step") == "form_contact":
        if not (looks_like_any_phone(t) or looks_like_any_email(t)):
            send_message(chat_id, "⛔️ ფორმატი არასწორია. მიუთითე <b>ტელეფონი</b> ან <b>ელფოსტა</b> (შეიძლება რამდენიმე ნომერიც).")
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
# 10) WEBHOOK SETUP (idempotent, exact token route)
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
# 11) LOCAL RUN (dev only)
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
