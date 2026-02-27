import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TZ = ZoneInfo(os.getenv("TIMEZONE", "UTC"))
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Conversation states
ASKING_TIME = "ASKING_TIME"
ASKING_MESSAGE = "ASKING_MESSAGE"
ASKING_REPEAT = "ASKING_REPEAT"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id      SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    time    TEXT NOT NULL,
                    message TEXT NOT NULL,
                    days    TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    chat_id      BIGINT PRIMARY KEY,
                    state        TEXT,
                    temp_time    TEXT,
                    temp_message TEXT
                )
            """)


try:
    init_db()
    logger.info("Database ready")
except Exception as e:
    logger.warning("DB init skipped: %s", e)


def get_conv(chat_id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT state, temp_time, temp_message FROM conversations WHERE chat_id = %s",
                (chat_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else {"state": None, "temp_time": None, "temp_message": None}


def set_conv(chat_id: int, state=None, temp_time=None, temp_message=None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO conversations (chat_id, state, temp_time, temp_message)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (chat_id) DO UPDATE
                SET state = EXCLUDED.state,
                    temp_time = EXCLUDED.temp_time,
                    temp_message = EXCLUDED.temp_message
                """,
                (chat_id, state, temp_time, temp_message),
            )


def save_reminder(chat_id: int, time_str: str, message: str, days: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reminders (chat_id, time, message, days) VALUES (%s, %s, %s, %s) RETURNING id",
                (chat_id, time_str, message, days),
            )
            return cur.fetchone()[0]


def get_reminders(chat_id: int) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, time, message, days FROM reminders WHERE chat_id = %s ORDER BY time",
                (chat_id,),
            )
            return cur.fetchall()


def get_all_reminders() -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT chat_id, time, message, days FROM reminders")
            return cur.fetchall()


def delete_reminder_db(reminder_id: int, chat_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM reminders WHERE id = %s AND chat_id = %s",
                (reminder_id, chat_id),
            )


# ---------------------------------------------------------------------------
# Days parsing
# ---------------------------------------------------------------------------

_DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_DAY_MAP = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}

_PRESETS = {
    "daily": None,
    "weekdays": [0, 1, 2, 3, 4],
    "weekends": [5, 6],
}


def parse_days(text: str) -> tuple:
    """Returns (weekday_int_list_or_None, normalized_str_or_None_if_invalid)."""
    text = text.strip().lower()
    if text in _PRESETS:
        return _PRESETS[text], text
    parts = [p.strip() for p in text.replace(" ", ",").split(",") if p.strip()]
    indices = []
    for p in parts:
        if p not in _DAY_MAP:
            return None, None
        indices.append(_DAY_MAP[p])
    if not indices:
        return None, None
    indices = sorted(set(indices))
    return indices, ",".join(_DAY_NAMES[i] for i in indices)


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

def send_message(chat_id: int, text: str, reply_markup=None) -> None:
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)


def edit_message(chat_id: int, message_id: int, text: str) -> None:
    requests.post(
        f"{TELEGRAM_API}/editMessageText",
        json={"chat_id": chat_id, "message_id": message_id, "text": text},
        timeout=10,
    )


def answer_callback(callback_query_id: str) -> None:
    requests.post(
        f"{TELEGRAM_API}/answerCallbackQuery",
        json={"callback_query_id": callback_query_id},
        timeout=10,
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def handle_start(chat_id: int) -> None:
    set_conv(chat_id)
    send_message(
        chat_id,
        "Hi! I'll send you reminders on your schedule.\n\n"
        "/add — add a new reminder\n"
        "/list — see your reminders\n"
        "/delete — remove a reminder\n"
        "/cancel — cancel current action",
    )


def handle_add(chat_id: int) -> None:
    set_conv(chat_id, state=ASKING_TIME)
    send_message(chat_id, "Let's add a reminder!\n\nWhat time? (24h format, e.g. 09:30)")


def handle_list(chat_id: int) -> None:
    rows = get_reminders(chat_id)
    if not rows:
        send_message(chat_id, "You have no reminders. Use /add to create one.")
        return
    lines = ["Your reminders:\n"]
    for rid, t, msg, days in rows:
        lines.append(f"#{rid}  {t}  [{days}]\n    {msg}")
    send_message(chat_id, "\n\n".join(lines))


def handle_delete(chat_id: int) -> None:
    rows = get_reminders(chat_id)
    if not rows:
        send_message(chat_id, "You have no reminders to delete.")
        return
    keyboard = {
        "inline_keyboard": [
            [{"text": f"#{rid}  {t}  {msg[:30]}", "callback_data": f"del_{rid}"}]
            for rid, t, msg, _ in rows
        ] + [[{"text": "Cancel", "callback_data": "del_cancel"}]]
    }
    send_message(chat_id, "Which reminder do you want to delete?", reply_markup=keyboard)


def handle_cancel(chat_id: int) -> None:
    set_conv(chat_id)
    send_message(chat_id, "Cancelled.")


def handle_text(chat_id: int, text: str) -> None:
    conv = get_conv(chat_id)
    state = conv["state"]

    if state == ASKING_TIME:
        try:
            parts = text.strip().split(":")
            h, m = int(parts[0]), int(parts[1])
            assert 0 <= h < 24 and 0 <= m < 60
        except Exception:
            send_message(chat_id, "Please use HH:MM format, e.g. 14:30")
            return
        set_conv(chat_id, state=ASKING_MESSAGE, temp_time=f"{h:02d}:{m:02d}")
        send_message(chat_id, "Got it! What should I remind you about?")

    elif state == ASKING_MESSAGE:
        set_conv(chat_id, state=ASKING_REPEAT, temp_time=conv["temp_time"], temp_message=text.strip())
        send_message(
            chat_id,
            "How often?\n\n• daily\n• weekdays\n• weekends\n• or specific days like: mon, wed, fri",
        )

    elif state == ASKING_REPEAT:
        days, normalized = parse_days(text)
        if normalized is None:
            send_message(chat_id, "Try: daily, weekdays, weekends, or days like mon,wed,fri")
            return
        save_reminder(chat_id, conv["temp_time"], conv["temp_message"], normalized)
        set_conv(chat_id)
        send_message(
            chat_id,
            f"Reminder set!\n\nTime: {conv['temp_time']}\nMessage: {conv['temp_message']}\nRepeat: {normalized}",
        )

    else:
        send_message(chat_id, "Use /add to set a reminder, /list to view, /delete to remove.")


def handle_callback(callback_query_id: str, chat_id: int, message_id: int, data: str) -> None:
    answer_callback(callback_query_id)
    if data == "del_cancel":
        edit_message(chat_id, message_id, "Cancelled.")
        return
    if data.startswith("del_"):
        reminder_id = int(data.split("_", 1)[1])
        delete_reminder_db(reminder_id, chat_id)
        edit_message(chat_id, message_id, f"Reminder #{reminder_id} deleted.")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": True})

    if "callback_query" in data:
        cq = data["callback_query"]
        handle_callback(
            cq["id"],
            cq["message"]["chat"]["id"],
            cq["message"]["message_id"],
            cq["data"],
        )
        return jsonify({"ok": True})

    msg = data.get("message")
    if not msg or "text" not in msg:
        return jsonify({"ok": True})

    chat_id = msg["chat"]["id"]
    text = msg["text"]

    if text.startswith("/start"):
        handle_start(chat_id)
    elif text.startswith("/add"):
        handle_add(chat_id)
    elif text.startswith("/list"):
        handle_list(chat_id)
    elif text.startswith("/delete"):
        handle_delete(chat_id)
    elif text.startswith("/cancel"):
        handle_cancel(chat_id)
    else:
        handle_text(chat_id, text)

    return jsonify({"ok": True})


@app.route("/cron", methods=["GET"])
def cron():
    """Called every minute by cron-job.org to fire due reminders."""
    now = datetime.now(TZ)
    current_time = now.strftime("%H:%M")
    today = now.weekday()

    sent = 0
    for chat_id, time_str, message, days_str in get_all_reminders():
        if time_str != current_time:
            continue
        day_list, _ = parse_days(days_str)
        if day_list is not None and today not in day_list:
            continue
        send_message(chat_id, f"⏰ {message}")
        sent += 1

    logger.info("Cron at %s — sent %d reminder(s)", current_time, sent)
    return jsonify({"ok": True, "time": current_time, "sent": sent})


@app.route("/setup")
def setup():
    """Register the webhook URL with Telegram. Visit once after deploying."""
    base_url = request.args.get("url", "").rstrip("/")
    if not base_url:
        return jsonify({"error": "Pass ?url=https://your-app.onrender.com"}), 400
    resp = requests.post(
        f"{TELEGRAM_API}/setWebhook",
        json={"url": f"{base_url}/webhook"},
        timeout=10,
    )
    return jsonify(resp.json())


@app.route("/")
def health():
    return jsonify({"status": "running"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
