# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-
import os
import json
import time
import logging
from typing import Dict, Any, Optional, Tuple, List

from flask import Flask, request, jsonify
import requests

import gspread
from google.oauth2.service_account import Credentials
from rapidfuzz import fuzz, process

# =========================
# ლოგირება
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hotel-bot")

# =========================
# ENV
# =========================
BOT_TOKEN  = os.environ.get("TELEGRAM_TOKEN")  # BotFather token
BASE_URL   = os.environ.get("APP_BASE_URL")    # e.g. https://ok-tv-1.onrender.com
SHEET_ID   = os.environ.get("SPREADSHEET_ID")
SA_JSON    = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")  # full JSON string

for k, v in {
    "TELEGRAM_TOKEN": BOT_TOKEN,
    "APP_BASE_URL": BASE_URL,
    "SPREADSHEET_ID": SHEET_ID,
    "GOOGLE_SERVICE_ACCOUNT_JSON": SA_JSON,
}.items():
    if not v:
        raise RuntimeError(f"Missing ENV: {k}")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# =========================
# Flask
# =========================
app = Flask(__name__)

# =========================
# Google Sheets
# =========================
GC = None
WS_HOTELS = None
WS_LEADS  = None

def connect_sheets():
    """Try connect once on boot; re-try lazily later if needed."""
    global GC, WS_HOTELS, WS_LEADS
    try:
        creds_info = json.loads(SA_JSON)
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        GC = gspread.authorize(creds)
        sh = GC.open_by_key(SHEET_ID)
        WS_HOTELS = sh.worksheet("Hotels")
        WS_LEADS  = sh.worksheet("Leads")
        log.info("✅ Google Sheets connected (Hotels, Leads).")
    except Exception as e:
        WS_HOTELS = None
        WS_LEADS  = None
        log.warning("⚠️ Google Sheets connect error: %s", e)

connect_sheets()

# =========================
# Caching Hotels to reduce API calls
# =========================
_HOTELS_CACHE = {"rows": [], "ts": 0}
_CACHE_TTL = 120  # sec

def load_hotels(force: bool = False) -> List[Dict[str, Any]]:
    global WS_HOTELS
    now = time.time()
    if not force and _HOTELS_CACHE["rows"] and (now - _HOTELS_CACHE["ts"] < _CACHE_TTL):
        return _HOTELS_CACHE["rows"]

    if WS_HOTELS is None:
        connect_sheets()
    if WS_HOTELS is None:
        raise RuntimeError("Sheets not connected")

    rows = WS_HOTELS.get_all_records()  # [{'name_en':..., 'address_ka':..., 'status':..., 'comment':...}, ...]
    _HOTELS_CACHE["rows"] = rows
    _HOTELS_CACHE["ts"] = now
    log.info("Loaded %d hotels from sheet.", len(rows))
    return rows

def append_lead_row(data: Dict[str, Any]):
    global WS_LEADS
    if WS_LEADS is None:
        connect_sheets()
    if WS_LEADS is None:
        raise RuntimeError("Sheets not connected")
    row = [
        data.get("created_at", time.strftime("%Y-%m-%d %H:%M:%S")),
        data.get("agent_username", ""),
        data.get("hotel_name_en", ""),
        data.get("address_ka", ""),
        data.get("matched", ""),
        data.get("matched_comment", ""),
        json.dumps(data.get("answers", {}), ensure_ascii=False),
    ]
    WS_LEADS.append_row(row, value_input_option="USER_ENTERED")

# =========================
# Helpers
# =========================
def send_message(chat_id: int, text: str, reply_markup: Optional[Dict]=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        requests.post(f"{API_URL}/sendMessage", json=payload, timeout=15)
    except Exception as e:
        log.warning("send_message failed: %s", e)

def answer_callback(callback_id: str, text: str):
    try:
        requests.post(f"{API_URL}/answerCallbackQuery", json={"callback_query_id": callback_id, "text": text, "show_alert": False}, timeout=10)
    except Exception:
        pass

def kb_main():
    return {"keyboard": [[{"text":"🔍 მოძებნა"}]], "resize_keyboard": True}

def kb_start():
    return {"keyboard": [[{"text":"▶️ სტარტი"}], [{"text":"⬅️ უკან"}]], "resize_keyboard": True}

def inline_yes_no():
    return {
        "inline_keyboard":[
            [{"text":"✔️ დიახ, ეს სასტუმროა","callback_data":"confirm_match"},
             {"text":"✏️ არა, სხვაა","callback_data":"reject_match"}]
        ]
    }

def normalize(s: str) -> str:
    return " ".join((s or "").strip().lower().split())

def find_best(hotel_name_en: str, address_ka: str) -> Tuple[Optional[Dict[str,Any]], int, int]:
    rows = load_hotels()
    names = [normalize(r.get("name_en","")) for r in rows]
    addrs = [normalize(r.get("address_ka","")) for r in rows]

    nm = process.extractOne(normalize(hotel_name_en), names, scorer=fuzz.token_set_ratio)
    am = process.extractOne(normalize(address_ka), addrs, scorer=fuzz.token_set_ratio)

    best = None
    name_score = 0
    addr_score = 0

    if nm:
        _, name_score, idx = nm
        best = rows[idx]
        name_score = int(name_score)

    if am:
        _, addr_score, idx = am
        addr_score = int(addr_score)
        if best is None or idx != rows.index(best):
            alt = rows[idx]
            alt_name_score = int(fuzz.token_set_ratio(normalize(hotel_name_en), normalize(alt.get("name_en",""))))
            cur_addr = normalize((best or {}).get("address_ka",""))
            cur_addr_score = int(fuzz.token_set_ratio(normalize(address_ka), cur_addr)) if best else 0
            if (alt_name_score + addr_score) > (name_score + cur_addr_score):
                best = alt
                name_score = alt_name_score

    return best, name_score, addr_score

# =========================
# Session (simple in-memory FSM)
# =========================
SESSIONS: Dict[int, Dict[str, Any]] = {}
def session(chat_id: int) -> Dict[str, Any]:
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = {"stage":"idle","answers":{}}
    return SESSIONS[chat_id]

# =========================
# Webhook setup
# =========================
def set_webhook():
    url = f"{BASE_URL.rstrip('/')}/webhook/{BOT_TOKEN}"
    try:
        r = requests.get(f"{API_URL}/setWebhook", params={"url": url}, timeout=10)
        j = r.json()
        log.info("Webhook set to %s: %s", url, j.get("result", j))
    except Exception as e:
        log.warning("set_webhook failed (ignored): %s", e)

# =========================
# Routes
# =========================
@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def tg_webhook():
    upd = request.get_json(force=True)
    if "message" in upd:
        handle_message(upd["message"])
    elif "callback_query" in upd:
        handle_callback(upd["callback_query"])
    return jsonify({"ok":True})

# =========================
# Telegram handlers
# =========================
EXACT  = 90
SIMILAR= 75

def handle_message(msg: Dict[str,Any]):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    st = session(chat_id)

    # commands
    if text == "/start":
        st.clear(); st.update({"stage":"idle","answers":{}})
        send_message(chat_id, "გამარჯობა! აირჩიე მოქმედება 👇", kb_main())
        return

    if text == "⬅️ უკან":
        st.clear(); st.update({"stage":"idle","answers":{}})
        send_message(chat_id, "დაბრუნდი მთავარ მენიუში.", kb_main())
        return

    # entry: search
    if text == "🔍 მოძებნა":
        st.update({"stage":"ask_name","hotel_name_en":None,"address_ka":None,
                   "best":None,"name_score":0,"addr_score":0})
        send_message(chat_id, "გთხოვ, ჩაწერე სასტუმროს <b>ოფიციალური სახელი ინგლისურად</b> (მაგ.: Radisson Blu Batumi).")
        return

    # flow
    if st["stage"] == "ask_name":
        st["hotel_name_en"] = text
        st["stage"] = "ask_addr"
        send_message(chat_id, "ახლა ჩაწერე <b>ოფიციალური მისამართი ქართულად</b> (მაგ.: ბათუმი, შ. ხიმშიაშვილის ქ. 1).")
        return

    if st["stage"] == "ask_addr":
        st["address_ka"] = text
        st["stage"] = "checking"
        # search
        try:
            bm, ns, as_ = find_best(st["hotel_name_en"], st["address_ka"])
            st["best"], st["name_score"], st["addr_score"] = bm, ns, as_
        except Exception as e:
            log.warning("search error: %s", e)
            send_message(chat_id, "⚠️ ვერ წავიკვეეთ Hotels ტაბი. გადაამოწმე SPREADSHEET_ID/Service Account და სცადე თავიდან.", kb_main())
            st.clear(); st.update({"stage":"idle","answers":{}})
            return

        bm = st["best"]
        if bm:
            status  = normalize(bm.get("status",""))
            comment = bm.get("comment","") or "—"
            name_en = bm.get("name_en","")
            addr_ka = bm.get("address_ka","")

            # exact surveyed/completed -> end
            if st["name_score"]>=EXACT and st["addr_score"]>=EXACT and status in ("done","surveyed","completed","აღებულია","გაკეთებულია"):
                send_message(chat_id,
                    f"❌ <b>ეს სასტუმრო უკვე გამოკითხულია</b>.\n"
                    f"• სახელი: {name_en}\n"
                    f"• მისამართი: {addr_ka}\n"
                    f"• კომენტარი: <i>{comment}</i>\n\n"
                    f"ჩატი დასრულებულია.")
                st.clear(); st.update({"stage":"idle","answers":{}})
                send_message(chat_id, "დაბრუნდი მთავარ მენიუში.", kb_main())
                return

            # similar -> suggest
            if st["name_score"]>=SIMILAR or st["addr_score"]>=SIMILAR:
                send_message(chat_id,
                    f"მოვძებნე <b>მსგავსი</b> ჩანაწერი:\n"
                    f"• სახელი: <i>{name_en}</i> (ქულა {st['name_score']})\n"
                    f"• მისამართი: <i>{addr_ka}</i> (ქულა {st['addr_score']})\n\n"
                    f"ეს ხომ არ არის ის, რასაც ეძებ?", reply_markup=inline_yes_no())
                st["stage"]="suggest"
                return

        # not found -> ready to start
        send_message(chat_id,
            "ბაზაში ზუსტი ჩანაწერი ვერ ვიპოვე.\n"
            "შეგიძლია გააგრძელო კითხვარი. დააჭირე „▶️ სტარტი“. ", kb_start())
        st["stage"] = "ready_to_start"
        return

    # start questionnaire
    if st["stage"] == "ready_to_start" and text == "▶️ სტარტი":
        # before Q, re-validate employee re-types same values
        st["stage"] = "confirm_name"
        send_message(chat_id, "კითხვარის დასაწყებად განმეორებით ჩაწერე <b>სასტუმროს სახელი ინგლისურად</b> ზუსტად ისე, როგორც ადრე შეიყვანე.")
        return

    if st["stage"] == "confirm_name":
        typed = normalize(text)
        prev  = normalize(st.get("hotel_name_en",""))
        if typed != prev:
            send_message(chat_id, "⚠️ შეყვანილი სახელი <b>არ ემთხვევა</b> ძიებისას შეყვანილს. შეასწორე და კიდევ ერთხელ ჩაწერე ზუსტად.")
            return
        st["stage"] = "confirm_addr"
        send_message(chat_id, "ახლა ჩაწერე <b>მისამართი ქართულად</b> — ზუსტად იგივე, რაც ძიებისას შეიყვანე.")
        return

    if st["stage"] == "confirm_addr":
        typed = normalize(text)
        prev  = normalize(st.get("address_ka",""))
        if typed != prev:
            send_message(chat_id, "⚠️ შეყვანილი მისამართი <b>არ ემთხვევა</b> ძიებისას შეყვანილს. გთხოვ სწორად ჩაწერო.")
            return
        # continue Q
        st["answers"] = {}
        st["stage"] = "q1"
        send_message(chat_id, "Q1) ვინ არის საკონტაქტო პირი? (სახელი და ტელეფონი)")
        return

    if st["stage"] == "q1":
        st["answers"]["Q1_contact"] = text
        st["stage"] = "q2"
        send_message(chat_id, "Q2) სურვილის შემთხვევაში დაამატე კომენტარი (ან დაწერე „არა“).")
        return

    if st["stage"] == "q2":
        st["answers"]["Q2_comment"] = text
        # write to Leads
        data = {
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "agent_username": (msg.get("from",{}).get("username") or f"id:{msg.get('from',{}).get('id')}"),
            "hotel_name_en": st.get("hotel_name_en",""),
            "address_ka": st.get("address_ka",""),
            "matched": "YES" if st.get("best") else "NO",
            "matched_comment": f"name_score={st.get('name_score',0)}, addr_score={st.get('addr_score',0)}",
            "answers": st.get("answers",{})
        }
        try:
            append_lead_row(data)
            send_message(chat_id, "✅ ინფორმაცია წარმატებით ჩაიწერა Google Sheets-ში.", kb_main())
        except Exception as e:
            log.warning("append_lead error: %s", e)
            send_message(chat_id, "⚠️ ჩაწერის შეცდომა Sheets-ში. გადაამოწმე „Leads“ ტაბი/უფლებები.", kb_main())
        st.clear(); st.update({"stage":"idle","answers":{}})
        return

    # default
    if st["stage"] == "idle":
        send_message(chat_id, "აირჩიე მოქმედება 👇", kb_main())
    else:
        send_message(chat_id, "გაგრძელებისთვის გამოიყენე ღილაკები.", kb_main())

def handle_callback(cb: Dict[str,Any]):
    chat_id = cb["message"]["chat"]["id"]
    data = cb.get("data","")
    st = session(chat_id)

    if st.get("stage") != "suggest":
        answer_callback(cb["id"], "ჩამორჩენილი ქოლბექი.")
        return

    if data == "confirm_match":
        bm = st.get("best") or {}
        status  = normalize(bm.get("status",""))
        comment = bm.get("comment","") or "—"
        name_en = bm.get("name_en","")
        addr_ka = bm.get("address_ka","")

        if status in ("done","surveyed","completed","აღებულია","გაკეთებულია"):
            answer_callback(cb["id"], "ეს სასტუმრო უკვე გამოკითხულია.")
            send_message(chat_id,
                f"❌ <b>ეს სასტუმრო უკვე გამოკითხულია</b>.\n"
                f"• სახელი: {name_en}\n"
                f"• მისამართი: {addr_ka}\n"
                f"• კომენტარი: <i>{comment}</i>\n\nჩატი დასრულებულია.")
            st.clear(); st.update({"stage":"idle","answers":{}})
            send_message(chat_id, "დაბრუნდი მთავარ მენიუში.", kb_main())
            return
        else:
            answer_callback(cb["id"], "გაგრძელე კითხვარი „▶️ სტარტი“-თ")
            st["stage"]="ready_to_start"
            send_message(chat_id, "ეს ჩანაწერი გვიპოვია, შეგიძლია გააგრძელო კითხვარი. დააჭირე „▶️ სტარტი“. ", kb_start())
            return

    if data == "reject_match":
        answer_callback(cb["id"], "კარგი, შევქმნათ ახალი ჩანაწერი.")
        st["stage"]="ready_to_start"
        send_message(chat_id, "შევქმნათ ახალი ჩანაწერი. დააჭირე „▶️ სტარტი“. ", kb_start())
        return

# =========================
# App start: set webhook
# =========================
set_webhook()

# =========================
# Gunicorn entrypoint expects `app`
# =========================
# CMD on Render should be:
# gunicorn telegram_hotel_booking_bot.py:app --bind 0.0.0.0:$PORT --timeout 120
