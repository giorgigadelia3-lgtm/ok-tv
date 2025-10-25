# telegram_hotel_booking_bot.py
# -*- coding: utf-8 -*-

import os
import json
import time
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

import requests
from flask import Flask, request, jsonify

# Google Sheets
import gspread
from google.oauth2.service_account import Credentials

# Fuzzy match
from rapidfuzz import fuzz, process

# =========================
# ENV
# =========================
BOT_TOKEN        = os.environ.get("TELEGRAM_TOKEN")  # BotFather token
APP_BASE_URL     = os.environ.get("APP_BASE_URL")    # e.g. https://ok-tv-1.onrender.com
SPREADSHEET_ID   = os.environ.get("SPREADSHEET_ID")  # Google Sheet ID
SERVICE_JSON_STR = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")  # service account JSON string

if not BOT_TOKEN or not APP_BASE_URL or not SPREADSHEET_ID or not SERVICE_JSON_STR:
    raise RuntimeError("Missing env vars. Set TELEGRAM_TOKEN, APP_BASE_URL, SPREADSHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON")

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# =========================
# Google Sheets: open 2 tabs
#   Hotels:   name_en | address_ka | status | comment
#   Leads:    created_at | agent | name_en | address_ka | matched | name_score | addr_score | comment | answers_json
# =========================
hotels_ws = None
leads_ws  = None

def _open_sheets():
    global hotels_ws, leads_ws
    creds = Credentials.from_service_account_info(json.loads(SERVICE_JSON_STR),
                                                  scopes=["https://www.googleapis.com/auth/spreadsheets",
                                                          "https://www.googleapis.com/auth/drive"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    hotels_ws = sh.worksheet("Hotels")
    leads_ws  = sh.worksheet("Leads")

try:
    _open_sheets()
    print("✅ Google Sheets connected.")
except Exception as e:
    print("⚠️ Google Sheets connect error:", e)

# =========================
# Flask
# =========================
app = Flask(__name__)

# =========================
# Helpers
# =========================
def send_message(chat_id: int, text: str, keyboard: Optional[Dict]=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    try:
        requests.post(f"{API_URL}/sendMessage", json=payload, timeout=15)
    except Exception as e:
        print("send_message error:", e)

def send_inline(chat_id: int, text: str, buttons: List[List[Dict[str, str]]]):
    kb = {"inline_keyboard": buttons}
    send_message(chat_id, text, kb)

def kb_main():
    return {"keyboard": [[{"text":"🔍 მოძებნა"}]], "resize_keyboard": True}

def kb_start():
    return {"keyboard": [[{"text":"▶️ სტარტი"}], [{"text":"⬅️ უკან"}]], "resize_keyboard": True}

def norm(s: str) -> str:
    return (s or "").strip().lower()

def load_hotels() -> List[Dict[str, Any]]:
    """Read all hotels (cached by Google’s servers; fast enough)."""
    try:
        return hotels_ws.get_all_records() if hotels_ws else []
    except Exception as e:
        print("load_hotels error:", e)
        try:
            _open_sheets()
            return hotels_ws.get_all_records()
        except:
            return []

def fuzzy_best(name_en: str, address_ka: str) -> Tuple[Optional[Dict[str,Any]], int, int]:
    """Return best matching row + scores by name & address (RapidFuzz)."""
    rows = load_hotels()
    if not rows:
        return None, 0, 0

    name_list = [r.get("name_en","") for r in rows]
    addr_list = [r.get("address_ka","") for r in rows]

    name_match = process.extractOne(name_en, name_list, scorer=fuzz.token_set_ratio)
    addr_match = process.extractOne(address_ka, addr_list, scorer=fuzz.token_set_ratio)

    best=None; nscore=0; ascore=0
    if name_match:
        _, nscore, idx = name_match
        best = rows[idx]; nscore=int(nscore)

    if addr_match:
        _, ascore, idx2 = addr_match
        ascore=int(ascore)
        if best is None or idx2 != rows.index(best):
            alt = rows[idx2]
            alt_n = int(fuzz.token_set_ratio(name_en, alt.get("name_en","")))
            cur_a = int(fuzz.token_set_ratio(address_ka, (best or {}).get("address_ka",""))) if best else 0
            if (alt_n + ascore) > (nscore + cur_a):
                best = alt; nscore = alt_n

    return best, nscore, ascore

def append_lead(agent: str, name_en: str, addr_ka: str,
                matched: str, nscore: int, ascore: int,
                comment: str, answers: Dict[str,Any]):
    try:
        leads_ws.append_row(
            [
                datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                agent,
                name_en,
                addr_ka,
                matched,
                nscore,
                ascore,
                comment,
                json.dumps(answers, ensure_ascii=False)
            ],
            value_input_option="USER_ENTERED"
        )
    except Exception as e:
        print("append_lead error:", e)
        _open_sheets()
        leads_ws.append_row(
            [
                datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                agent,
                name_en,
                addr_ka,
                matched,
                nscore,
                ascore,
                comment,
                json.dumps(answers, ensure_ascii=False)
            ],
            value_input_option="USER_ENTERED"
        )

# =========================
# In-memory session (FSM)
# =========================
# stage:
#   idle -> ask_name -> ask_address -> checking -> choice(suggest) -> ready_to_start
#   -> confirm_name -> confirm_address -> questionnaire -> done
SESS: Dict[int, Dict[str, Any]] = {}

def session(chat_id: int) -> Dict[str, Any]:
    if chat_id not in SESS:
        SESS[chat_id] = {"stage":"idle", "answers":{}}
    return SESS[chat_id]

# =========================
# Telegram webhook
# =========================
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def tg_webhook():
    upd = request.get_json(force=True)
    msg = upd.get("message") or {}
    cq  = upd.get("callback_query")

    if msg:
        _on_message(msg)
    elif cq:
        _on_callback(cq)

    return jsonify({"ok": True})

def _on_message(m: Dict[str,Any]):
    chat_id = m["chat"]["id"]
    text = (m.get("text") or "").strip()
    st = session(chat_id)

    if text == "/start" or text == "⬅️ უკან":
        SESS[chat_id] = {"stage":"idle","answers":{}}
        send_message(chat_id, "გამარჯობა! აირჩიე მოქმედება 👇", kb_main())
        return

    # 1) მოძებნა
    if text == "🔍 მოძებნა" and st["stage"] in ("idle","done"):
        st.update({"stage":"ask_name","answers":{}})
        send_message(chat_id, "გთხოვ, შეიყვანე სასტუმროს <b>ოფიციალური სახელი ინგლისურად</b> (მაგ.: Radisson Blu Batumi).")
        return

    if st["stage"] == "ask_name":
        st["hotel_name_en"] = text
        st["stage"] = "ask_address"
        send_message(chat_id, "ახლა ჩაწერე <b>ოფიციალური მისამართი ქართულად</b> (ქალაქი, ქუჩა, ნომერი).")
        return

    if st["stage"] == "ask_address":
        st["address_ka"] = text
        st["stage"] = "checking"

        # მოძებნა შიტში (ზუსტი/მსგავსი)
        best, ns, as_ = fuzzy_best(st["hotel_name_en"], st["address_ka"])
        st["best"] = best; st["name_score"]=ns; st["addr_score"]=as_

        EXACT, SIMILAR = 92, 75

        if best:
            status  = norm(best.get("status",""))
            name_en = best.get("name_en","")
            addr_ka = best.get("address_ka","")
            comment = best.get("comment","")

            # თუ ზუსტი + უკვე გამოკითხულია -> დასრულება
            if ns>=EXACT and as_>=EXACT and status in ("done","surveyed","completed","აღებულია","გაკეთებულია"):
                send_message(
                    chat_id,
                    f"❌ <b>ეს სასტუმრო უკვე გამოკითხულია.</b>\n"
                    f"• სახელი: {name_en}\n"
                    f"• მისამართი: {addr_ka}\n"
                    f"• კომენტარი: <i>{comment or '—'}</i>\n\n"
                    f"ჩატი დასრულდა.",
                    kb_main()
                )
                SESS[chat_id] = {"stage":"done","answers":{}}
                return

            # თუ მსგავსია -> შესთავაზე
            if ns>=SIMILAR or as_>=SIMILAR:
                send_inline(
                    chat_id,
                    ("მოიძებნა <b>მსგავსი</b> სასტუმრო. ხომ ეს არის?\n\n"
                     f"• სახელი: <i>{name_en}</i>  (ქულა {ns})\n"
                     f"• მისამართი: <i>{addr_ka}</i> (ქულა {as_})"),
                    [[
                        {"text":"✔️ დიახ","callback_data":"match_yes"},
                        {"text":"✏️ არა","callback_data":"match_no"}
                    ]]
                )
                st["stage"]="choice"
                return

        # საერთოდ ვერ ვიპოვეთ → სტარტი
        send_message(
            chat_id,
            ("ამ სახელზე/მისამართზე ზუსტი ჩანაწერი ვერ მოიძებნა.\n"
             "შეგიძლია დაუკავშირდე ამ სასტუმროს ან გააგრძელო კითხვარი.\n\n"
             "გასაგრძელებლად დააჭირე <b>▶️ სტარტი</b>."),
            kb_start()
        )
        st["stage"]="ready_to_start"
        return

    # „სტარტი“ – მხოლოდ როცა მზად ვართ
    if text == "▶️ სტარტი" and st["stage"] == "ready_to_start":
        # მოთხოვნილი ვალიდაცია: თავიდან შეიყვანოს სახელი და მისამართი და შევამოწმოთ ემთხვევა თუ არა მოძებნილს (თუ იყო)
        st["stage"]="confirm_name"
        send_message(chat_id, "გაიმეორე სასტუმროს <b>სახელი (EN)</b> დასადასტურებლად:")
        return

    # კონფირმაციები
    if st["stage"] == "confirm_name":
        typed = text.strip()
        st["confirm_name"] = typed
        # თუ ძებნაში „best“ გვქონდა და ეს სასტუმრო არ იყო გამოკითხული — უნდა ემთხვეოდეს სახელიც
        if st.get("best") and norm(st["best"].get("name_en","")) != norm(typed):
            send_message(chat_id,
                "⚠️ შეყვანილი სახელი <b>არ ემთხვევა</b> მოძიებულ სასტუმროს სახელს. შეასწორე და მიუთითე ზუსტად.")
            return
        st["stage"]="confirm_address"
        send_message(chat_id, "ახლა გაიმეორე <b>მისამართი (KA)</b> დასადასტურებლად:")
        return

    if st["stage"] == "confirm_address":
        typed = text.strip()
        st["confirm_address"] = typed
        if st.get("best") and norm(st["best"].get("address_ka","")) != norm(typed):
            send_message(chat_id,
                "⚠️ შეყვანილი მისამართი <b>არ ემთხვევა</b> მოძიებულ მისამართს. გთხოვ, შეასწორე.")
            return
        # იწყება კითხვარი
        st["stage"]="q_rooms"
        send_message(chat_id, "Q1) რამდენი ნომერია სასტუმროში? (რიცხვი)")
        return

    # კითხვარი — მაგალითი (ჩაანაცვლებ შენი ბლოკით)
    if st["stage"] == "q_rooms":
        st["answers"]["rooms"] = text.strip()
        st["stage"] = "q_contact"
        send_message(chat_id, "Q2) საკონტაქტო პირი (სახელი, ტელ):")
        return

    if st["stage"] == "q_contact":
        st["answers"]["contact"] = text.strip()

        # ჩანაწერის ჩაწერა Leads-ში
        agent = m["from"].get("username") or f"id:{m['from']['id']}"
        name_en = st.get("confirm_name") or st.get("hotel_name_en","")
        addr_ka = st.get("confirm_address") or st.get("address_ka","")
        matched = "YES" if st.get("best") else "NO"
        name_score = st.get("name_score",0)
        addr_score = st.get("addr_score",0)
        comment = (st.get("best") or {}).get("comment","") or ""

        try:
            append_lead(agent, name_en, addr_ka, matched, name_score, addr_score, comment, st["answers"])
            send_message(chat_id, "✅ ინფორმაცია წარმატებით ჩაიწერა შიტში. გმადლობთ!", kb_main())
        except Exception as e:
            print("write lead error:", e)
            send_message(chat_id, "⚠️ ჩაწერის შეცდომა Google Sheets-ში. სცადეთ ხელახლა.", kb_main())

        SESS[chat_id] = {"stage":"done","answers":{}}
        return

    # სხვა ტექსტები
    if st["stage"] in ("idle","done"):
        send_message(chat_id, "აირჩიე მოქმედება 👇", kb_main())
    else:
        send_message(chat_id, "გაგრძელე მიმდინარე პროცესი ან დააჭირე „⬅️ უკან“.", kb_main())

def _on_callback(cq: Dict[str,Any]):
    chat_id = cq["message"]["chat"]["id"]
    data = cq.get("data")
    st = session(chat_id)

    if data == "match_yes" and st.get("best"):
        # თუ best-ს სტატუსი done/surveyed — დავასრულოთ
        status = norm(st["best"].get("status",""))
        name_en = st["best"].get("name_en","")
        addr_ka = st["best"].get("address_ka","")
        comment = st["best"].get("comment","")
        if status in ("done","surveyed","completed","აღებულია","გაკეთებულია"):
            send_message(
                chat_id,
                f"❌ <b>ეს სასტუმრო უკვე გამოკითხულია.</b>\n"
                f"• {name_en}\n• {addr_ka}\nკომენტარი: <i>{comment or '—'}</i>\n\n"
                f"ჩატი დასრულდა.",
                kb_main()
            )
            SESS[chat_id] = {"stage":"done","answers":{}}
            return

        # სხვა შემთხვევაში — შეგვიძლია გავაგრძელოთ
        st["stage"]="ready_to_start"
        send_message(chat_id, "კარგი, გავაგრძელოთ. დააჭირე <b>▶️ სტარტი</b>.", kb_start())
        return

    if data == "match_no":
        st["stage"]="ready_to_start"
        send_message(chat_id, "გასაგებია. ახალი ჩანაწერის შესაქმნელად დააჭირე <b>▶️ სტარტი</b>.", kb_start())
        return

# =========================
# Health
# =========================
@app.route("/", methods=["GET"])
def health():
    return "HotelClaimBot — alive", 200

# =========================
# Webhook setup
# =========================
def set_webhook():
    url = f"{APP_BASE_URL}/{BOT_TOKEN}"
    try:
        # მოკლედ: ჯერ წაშლა, მერე დაყენება
        requests.get(f"{API_URL}/deleteWebhook", timeout=10)
        time.sleep(1)
        r = requests.get(f"{API_URL}/setWebhook", params={"url": url}, timeout=10)
        print("Webhook:", r.text)
    except Exception as e:
        print("set_webhook error:", e)

set_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
