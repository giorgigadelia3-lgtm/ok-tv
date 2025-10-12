from flask import Flask, request, jsonify
import os
import requests
import sqlite3
import time
from datetime import datetime

# ---------- CONFIG ----------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    raise RuntimeError('Please set BOT_TOKEN environment variable')

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
DB_PATH = 'data.db'

app = Flask(__name__)

# ---------- DATABASE ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS hotels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        address TEXT NOT NULL,
        agent_username TEXT,
        agent_chat_id INTEGER,
        timestamp INTEGER
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS pending (
        chat_id INTEGER PRIMARY KEY,
        step TEXT,
        temp_name TEXT
    )''')
    conn.commit()
    conn.close()

def db_execute(query, params=(), fetch=False):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(query, params)
    data = None
    if fetch:
        data = cur.fetchall()
    conn.commit()
    conn.close()
    return data

init_db()

# ---------- TELEGRAM HELPERS ----------
def send_message(chat_id, text):
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'}
    requests.post(f'{API_URL}/sendMessage', json=payload)

# ---------- BUSINESS LOGIC ----------
def normalize(s: str) -> str:
    return ' '.join(s.strip().lower().split())

def hotel_exists(name, address):
    n = normalize(name)
    a = normalize(address)
    rows = db_execute('SELECT id, name, address, agent_username FROM hotels WHERE LOWER(name)=? AND LOWER(address)=?', (n, a), fetch=True)
    return rows[0] if rows else None

def add_hotel(name, address, agent_username, agent_chat_id):
    ts = int(time.time())
    db_execute('INSERT INTO hotels (name, address, agent_username, agent_chat_id, timestamp) VALUES (?, ?, ?, ?, ?)',
               (name.strip(), address.strip(), agent_username, agent_chat_id, ts))
    rows = db_execute('SELECT last_insert_rowid()', fetch=True)
    return rows

def list_agent_hotels(agent_chat_id):
    rows = db_execute('SELECT id, name, address, timestamp FROM hotels WHERE agent_chat_id=? ORDER BY timestamp DESC',
                      (agent_chat_id,), fetch=True)
    return rows

def release_hotel(hotel_id, agent_chat_id):
    rows = db_execute('SELECT agent_chat_id FROM hotels WHERE id=?', (hotel_id,), fetch=True)
    if not rows:
        return False, 'No such hotel'
    owner = rows[0][0]
    if owner != agent_chat_id:
        return False, 'You are not the owner of this claim.'
    db_execute('DELETE FROM hotels WHERE id=?', (hotel_id,))
    return True, 'Released'

# ---------- PENDING STATE ----------
def set_pending(chat_id, step, temp_name=None):
    db_execute('REPLACE INTO pending (chat_id, step, temp_name) VALUES (?, ?, ?)', (chat_id, step, temp_name))

def get_pending(chat_id):
    rows = db_execute('SELECT step, temp_name FROM pending WHERE chat_id=?', (chat_id,), fetch=True)
    return rows[0] if rows else (None, None)

def clear_pending(chat_id):
    db_execute('DELETE FROM pending WHERE chat_id=?', (chat_id,))

# ---------- COMMAND HANDLERS ----------
def handle_addhotel(chat_id):
    set_pending(chat_id, 'awaiting_name')
    send_message(chat_id, "Please enter the hotel name you are about to call (or send /cancel to stop).")

def handle_cancel(chat_id):
    clear_pending(chat_id)
    send_message(chat_id, "Operation cancelled.")

def handle_myhotels(chat_id):
    rows = list_agent_hotels(chat_id)
    if not rows:
        send_message(chat_id, "You have no claimed hotels.")
        return
    text = "Your claimed hotels:\n"
    for r in rows:
        hid, name, address, ts = r
        dt = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
        text += f"\nID: {hid}\n*{name}*\n{address}\nClaimed: {dt}\n"
    send_message(chat_id, text)

# ---------- WEBHOOK ----------
@app.route(f'/webhook/{BOT_TOKEN}', methods=['POST'])
def webhook():
    update = request.get_json()
    if 'message' in update:
        message = update['message']
        chat_id = message['chat']['id']
        from_username = message['from'].get('username') or message['from'].get('first_name', 'unknown')
        text = message.get('text', '')

        if text.startswith('/'):
            cmd = text.split()[0].lower()
            if cmd in ['/start', '/addhotel']:
                handle_addhotel(chat_id)
                return jsonify({'ok': True})
            elif cmd == '/cancel':
                handle_cancel(chat_id)
                return jsonify({'ok': True})
            elif cmd == '/myhotels':
                handle_myhotels(chat_id)
                return jsonify({'ok': True})
            elif cmd == '/release':
                parts = text.split()
                if len(parts) < 2:
                    send_message(chat_id, "Usage: /release <hotel_id>")
                else:
                    try:
                        hid = int(parts[1])
                        ok, msg = release_hotel(hid, chat_id)
                        send_message(chat_id, msg)
                    except ValueError:
                        send_message(chat_id, "Invalid hotel id")
                return jsonify({'ok': True})
            else:
                send_message(chat_id, "Unknown command. Use /addhotel to start.")
                return jsonify({'ok': True})

        step, temp_name = get_pending(chat_id)
        if step == 'awaiting_name':
            name = text.strip()
            set_pending(chat_id, 'awaiting_address', name)
            send_message(chat_id, "Now please enter the address of the hotel.")
            return jsonify({'ok': True})

        if step == 'awaiting_address':
            address = text.strip()
            name = temp_name
            existing = hotel_exists(name, address)
            if existing:
                existing_id = existing[0]
                existing_agent = existing[3] or 'unknown'
                send_message(chat_id, f"❌ Already claimed (ID {existing_id}) by @{existing_agent}")
                clear_pending(chat_id)
                return jsonify({'ok': True})

            add_hotel(name, address, from_username, chat_id)
            send_message(chat_id, f"✅ Hotel {name} at {address} has been claimed by you.")
            clear_pending(chat_id)
            return jsonify({'ok': True})

        send_message(chat_id, "Send /addhotel to claim a hotel. Use /myhotels to view your claimed hotels.")
    return jsonify({'ok': True})

@app.route('/')
def index():
    return 'Telegram Hotel Claim Bot is running.'

if __name__ == '__main__':
    # Register webhook with Telegram when the app starts
    webhook_url = f"https://ok-tv-1.onrender.com/{BOT_TOKEN}"
    set_resp = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}")
    print("Set webhook response:", set_resp.text)

    # Run Flask server
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000))
