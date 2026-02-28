import os
import json
import logging
import re
from datetime import datetime, date, timedelta
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
AUTHORIZED_USER = int(os.getenv("TELEGRAM_USER_ID", "0"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Conversation states
ASKING_TIME     = "ASKING_TIME"
ASKING_MESSAGE  = "ASKING_MESSAGE"
ASKING_REPEAT   = "ASKING_REPEAT"
AWAITING_CHECKIN = "AWAITING_CHECKIN"

# Cycle constants
COFFEE_TARGET  = 2   # target cups per coffee day
COFFEE_FLAG    = 4   # flag if >= this for 3 consecutive coffee days
NICOTINE_FLAG  = 5   # flag if >= this for 3 consecutive nicotine days

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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_config (
                    chat_id          BIGINT PRIMARY KEY,
                    cycle_start_date DATE NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_log (
                    id             SERIAL PRIMARY KEY,
                    chat_id        BIGINT NOT NULL,
                    date           DATE NOT NULL,
                    phase          TEXT NOT NULL,
                    consumed_units INTEGER,
                    logged_at      TIMESTAMP,
                    note           TEXT,
                    UNIQUE(chat_id, date)
                )
            """)


try:
    init_db()
    logger.info("Database ready")
except Exception as e:
    logger.warning("DB init skipped: %s", e)


# --- Conversation state ---

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


# --- Generic reminders ---

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


# --- Cycle config ---

def get_cycle_start(chat_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cycle_start_date FROM cycle_config WHERE chat_id = %s",
                (chat_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def set_cycle_start(chat_id: int, start_date: date) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cycle_config (chat_id, cycle_start_date)
                VALUES (%s, %s)
                ON CONFLICT (chat_id) DO UPDATE
                SET cycle_start_date = EXCLUDED.cycle_start_date
                """,
                (chat_id, start_date),
            )


# --- Daily log ---

def log_day(chat_id: int, log_date: date, phase: str, units, note: str = None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO daily_log (chat_id, date, phase, consumed_units, logged_at, note)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (chat_id, date) DO UPDATE
                SET consumed_units = EXCLUDED.consumed_units,
                    logged_at      = EXCLUDED.logged_at,
                    note           = EXCLUDED.note
                """,
                (
                    chat_id, log_date, phase, units,
                    datetime.now(TZ) if units is not None else None,
                    note,
                ),
            )


def get_today_log(chat_id: int, today: date) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT consumed_units, note, logged_at FROM daily_log WHERE chat_id = %s AND date = %s",
                (chat_id, today),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_recent_logs(chat_id: int, limit: int = 14) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT date, phase, consumed_units, note
                FROM daily_log WHERE chat_id = %s
                ORDER BY date DESC LIMIT %s
                """,
                (chat_id, limit),
            )
            return cur.fetchall()


# ---------------------------------------------------------------------------
# Days parsing (generic reminders)
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
# Cycle engine
# ---------------------------------------------------------------------------

def get_cycle_info(cycle_start: date, today: date) -> dict:
    """Returns cycle_day (1-7), phase, phase_day, days_remaining (including today)."""
    cycle_day = ((today - cycle_start).days % 7) + 1
    if cycle_day <= 4:
        phase = "coffee"
        phase_day = cycle_day
        days_remaining = 4 - cycle_day + 1
    else:
        phase = "nicotine"
        phase_day = cycle_day - 4
        days_remaining = 7 - cycle_day + 1
    return {
        "cycle_day": cycle_day,
        "phase": phase,
        "phase_day": phase_day,
        "days_remaining": days_remaining,
    }


def format_morning_message(info: dict) -> str:
    day = info["cycle_day"]
    rem = info["days_remaining"]
    rem_str = f"{rem} day{'s' if rem != 1 else ''} remaining in this phase"
    if info["phase"] == "coffee":
        return (
            f"☕ Day {day} — Coffee Phase ({rem_str})\n"
            f"Target: 2 cups of coffee today. No nicotine.\n"
            f"Check-in tonight at 9 PM."
        )
    return (
        f"🟢 Day {day} — Nicotine Phase ({rem_str})\n"
        f"Target: 3–4 pieces of 2mg nicotine gum. No coffee.\n"
        f"Check-in tonight at 9 PM."
    )


def format_checkin_prompt(info: dict) -> str:
    substance = "cups" if info["phase"] == "coffee" else "pieces"
    return (
        f"📋 Daily check-in — how did today go?\n"
        f"Reply with the number of {substance} you had today, "
        f"or add a note (e.g. '2 felt good')."
    )


def parse_checkin_reply(text: str) -> tuple:
    match = re.match(r"^(\d+)\s*(.*)?$", text.strip())
    if match:
        return int(match.group(1)), match.group(2).strip() or None
    return None, None


def check_tolerance(chat_id: int, phase: str) -> str | None:
    threshold = COFFEE_FLAG if phase == "coffee" else NICOTINE_FLAG
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT consumed_units FROM daily_log
                WHERE chat_id = %s AND phase = %s AND consumed_units IS NOT NULL
                ORDER BY date DESC LIMIT 3
                """,
                (chat_id, phase),
            )
            rows = cur.fetchall()
    if len(rows) < 3 or not all(r[0] >= threshold for r in rows):
        return None
    substance = "cups" if phase == "coffee" else "pieces"
    target = "2–3" if phase == "coffee" else "3–4"
    return (
        f"⚠️ You've been at {threshold}+ {substance} for 3 days. "
        f"Consider staying at {target} to preserve sensitivity."
    )


def format_status(chat_id: int) -> str:
    today = datetime.now(TZ).date()
    cycle_start = get_cycle_start(chat_id)
    if not cycle_start:
        return "No cycle started. Send /start to begin."

    info = get_cycle_info(cycle_start, today)
    emoji = "☕" if info["phase"] == "coffee" else "🟢"
    phase_name = "Coffee Phase" if info["phase"] == "coffee" else "Nicotine Phase"
    rem = info["days_remaining"]
    lines = [
        f"{emoji} Day {info['cycle_day']} — {phase_name}",
        f"{rem} day{'s' if rem != 1 else ''} remaining in this phase",
    ]
    today_log = get_today_log(chat_id, today)
    if today_log and today_log["consumed_units"] is not None:
        substance = "cups" if info["phase"] == "coffee" else "pieces"
        lines.append(f"Today's log: {today_log['consumed_units']} {substance}")
        if today_log["note"]:
            lines.append(f"Note: {today_log['note']}")
    else:
        lines.append("Today: not yet logged")
    return "\n".join(lines)


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
    today = datetime.now(TZ).date()
    cycle_start = get_cycle_start(chat_id)
    if not cycle_start:
        set_cycle_start(chat_id, today)
        send_message(
            chat_id,
            "👋 Welcome! Your cycle starts today.\n\n"
            + format_status(chat_id) + "\n\n"
            "Commands:\n"
            "/status — current day & phase\n"
            "/log [n] — log today's consumption\n"
            "/history — last 14 days\n"
            "/cycle — 7-day schedule\n"
            "/skip — jump to next phase\n"
            "/add — add a custom timed reminder\n"
            "/list — view reminders\n"
            "/delete — remove a reminder",
        )
    else:
        send_message(chat_id, "👋 Welcome back!\n\n" + format_status(chat_id))


def handle_status(chat_id: int) -> None:
    send_message(chat_id, format_status(chat_id))


def handle_log_command(chat_id: int, args: str) -> None:
    cycle_start = get_cycle_start(chat_id)
    if not cycle_start:
        send_message(chat_id, "No cycle started. Use /start first.")
        return
    units, note = parse_checkin_reply(args)
    if units is None:
        send_message(chat_id, "Usage: /log 2  or  /log 2 felt great")
        return
    today = datetime.now(TZ).date()
    info = get_cycle_info(cycle_start, today)
    log_day(chat_id, today, info["phase"], units, note)
    substance = "cups" if info["phase"] == "coffee" else "pieces"
    send_message(chat_id, f"✅ Logged: {units} {substance} today.")
    warning = check_tolerance(chat_id, info["phase"])
    if warning:
        send_message(chat_id, warning)


def handle_history(chat_id: int) -> None:
    rows = get_recent_logs(chat_id, 14)
    if not rows:
        send_message(chat_id, "No history yet.")
        return

    lines = ["📊 Last 14 days:\n"]
    for log_date, phase, units, note in rows:
        emoji = "☕" if phase == "coffee" else "🟢"
        units_str = str(units) if units is not None else "—"
        note_str = f"  ({note})" if note else ""
        lines.append(f"{emoji} {log_date}  {units_str}{note_str}")

    # Averages and trend
    coffee_units = [r[2] for r in rows if r[1] == "coffee" and r[2] is not None]
    nic_units    = [r[2] for r in rows if r[1] == "nicotine" and r[2] is not None]

    stats = []
    for label, emoji, data in [("coffee", "☕", coffee_units), ("nicotine", "🟢", nic_units)]:
        if not data:
            continue
        avg = sum(data) / len(data)
        trend = ""
        if len(data) >= 6:
            if sum(data[:3]) / 3 > sum(data[3:6]) / 3 + 0.5:
                trend = " ↑ trending up"
        unit = "cups" if label == "coffee" else "pieces"
        stats.append(f"{emoji} Avg {label}: {avg:.1f} {unit}{trend}")

    # Longest on-target streak
    all_logged = [(r[1], r[2]) for r in rows if r[2] is not None]
    streak = best = 0
    for phase, units in reversed(all_logged):
        target = COFFEE_TARGET if phase == "coffee" else 4
        if units <= target:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    if best:
        stats.append(f"🔥 Longest on-target streak: {best} days")

    if stats:
        lines.append("\n" + "\n".join(stats))
    send_message(chat_id, "\n".join(lines))


def handle_cycle(chat_id: int) -> None:
    cycle_start = get_cycle_start(chat_id)
    if not cycle_start:
        send_message(chat_id, "No cycle started. Use /start first.")
        return
    today = datetime.now(TZ).date()
    current_day = get_cycle_info(cycle_start, today)["cycle_day"]
    lines = ["📅 7-day cycle:\n"]
    for d in range(1, 8):
        label = "☕ Coffee (target: 2 cups)" if d <= 4 else "🟢 Nicotine (target: 3–4 pieces)"
        marker = " ← today" if d == current_day else ""
        lines.append(f"Day {d}: {label}{marker}")
    send_message(chat_id, "\n".join(lines))


def handle_skip(chat_id: int) -> None:
    cycle_start = get_cycle_start(chat_id)
    if not cycle_start:
        send_message(chat_id, "No cycle started. Use /start first.")
        return
    today = datetime.now(TZ).date()
    info = get_cycle_info(cycle_start, today)
    if info["phase"] == "coffee":
        new_start = today - timedelta(days=4)   # make today = day 5 (nicotine)
        jumped_to = "Nicotine Phase"
    else:
        new_start = today                        # make today = day 1 (new coffee cycle)
        jumped_to = "Coffee Phase (new cycle)"
    set_cycle_start(chat_id, new_start)
    send_message(chat_id, f"⏭ Skipped to {jumped_to}.\n\n{format_status(chat_id)}")


# --- Generic reminder handlers ---

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


# ---------------------------------------------------------------------------
# Text handler (conversation states)
# ---------------------------------------------------------------------------

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

    elif state == AWAITING_CHECKIN:
        cycle_start = get_cycle_start(chat_id)
        if not cycle_start:
            set_conv(chat_id)
            return
        units, note = parse_checkin_reply(text)
        if units is None:
            send_message(chat_id, "Please reply with a number, e.g. '2' or '2 felt good'")
            return
        today = datetime.now(TZ).date()
        info = get_cycle_info(cycle_start, today)
        log_day(chat_id, today, info["phase"], units, note)
        set_conv(chat_id)
        substance = "cups" if info["phase"] == "coffee" else "pieces"
        send_message(chat_id, f"✅ Logged: {units} {substance} today. Good work!")
        warning = check_tolerance(chat_id, info["phase"])
        if warning:
            send_message(chat_id, warning)

    else:
        send_message(chat_id, "Use /status to see your current day, or /log [n] to log today.")


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

    if AUTHORIZED_USER and chat_id != AUTHORIZED_USER:
        send_message(chat_id, "This is a private bot.")
        return jsonify({"ok": True})

    if text.startswith("/start"):
        handle_start(chat_id)
    elif text.startswith("/status"):
        handle_status(chat_id)
    elif text.startswith("/log"):
        handle_log_command(chat_id, text[4:].strip())
    elif text.startswith("/history"):
        handle_history(chat_id)
    elif text.startswith("/cycle"):
        handle_cycle(chat_id)
    elif text.startswith("/skip"):
        handle_skip(chat_id)
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
    """Called every minute by cron-job.org."""
    now = datetime.now(TZ)
    current_time = now.strftime("%H:%M")
    today = now.date()
    sent = 0

    # Generic reminders
    for chat_id, time_str, message, days_str in get_all_reminders():
        if time_str != current_time:
            continue
        day_list, _ = parse_days(days_str)
        if day_list is not None and now.weekday() not in day_list:
            continue
        send_message(chat_id, f"⏰ {message}")
        sent += 1

    if AUTHORIZED_USER:
        cycle_start = get_cycle_start(AUTHORIZED_USER)

        # 07:00 — morning message
        if cycle_start and current_time == "07:00":
            info = get_cycle_info(cycle_start, today)
            send_message(AUTHORIZED_USER, format_morning_message(info))
            sent += 1

        # 21:00 — evening check-in (only if not already logged)
        if cycle_start and current_time == "21:00":
            today_log = get_today_log(AUTHORIZED_USER, today)
            if not today_log or today_log["consumed_units"] is None:
                info = get_cycle_info(cycle_start, today)
                send_message(AUTHORIZED_USER, format_checkin_prompt(info))
                set_conv(AUTHORIZED_USER, state=AWAITING_CHECKIN)
                sent += 1

        # 22:00 — nudge if still not logged
        if cycle_start and current_time == "22:00":
            today_log = get_today_log(AUTHORIZED_USER, today)
            if not today_log or today_log["consumed_units"] is None:
                info = get_cycle_info(cycle_start, today)
                substance = "cups" if info["phase"] == "coffee" else "pieces"
                send_message(AUTHORIZED_USER, f"🔔 Still waiting for your check-in — how many {substance} today?")
                set_conv(AUTHORIZED_USER, state=AWAITING_CHECKIN)
                sent += 1

        # 23:55 — auto log no-data if still nothing
        if cycle_start and current_time == "23:55":
            today_log = get_today_log(AUTHORIZED_USER, today)
            if not today_log or today_log["consumed_units"] is None:
                info = get_cycle_info(cycle_start, today)
                log_day(AUTHORIZED_USER, today, info["phase"], None, "auto: no data")
                set_conv(AUTHORIZED_USER)

    logger.info("Cron at %s — sent %d", current_time, sent)
    return jsonify({"ok": True, "time": current_time, "sent": sent})


@app.route("/setup")
def setup():
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
