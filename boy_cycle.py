import os
import json
import logging
import re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

# matplotlib is only used for history charts and may not be present in
# minimal deployments. Import it in a try/except so the rest of the bot can
# start even if the package is missing.  Use the Agg backend for headless
# operation and build the font cache eagerly to avoid the ``building the
# font cache'' message showing up in the logs every time the first reminder
# is sent.
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from io import BytesIO

    # trigger font cache build immediately (it happens on first figure)
    try:
        import matplotlib.font_manager
        matplotlib.font_manager._rebuild()
    except Exception:
        # if rebuilding fails, we'll just let the first plot do it normally
        pass

    HAS_MATPLOTLIB = True
except ImportError:
    # missing optional dependency; disable chart generation
    HAS_MATPLOTLIB = False
    plt = None
    mdates = None
    BytesIO = None

import requests
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

# ensure matplotlib has a stable config directory for its font cache. This
# prevents the “building the font cache” message from reappearing when the
# process restarts.  If the environment variable is already set by the host
# we honour it; otherwise we pick a writable folder inside the project and
# export it so that matplotlib will use it.
if not os.getenv("MPLCONFIGDIR"):
    default_mpl = os.path.join(os.getcwd(), ".mplconfig")
    try:
        os.makedirs(default_mpl, exist_ok=True)
        os.environ["MPLCONFIGDIR"] = default_mpl
    except Exception:
        # fall back to whatever matplotlib chooses (usually /tmp)
        pass

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TZ = ZoneInfo(os.getenv("TIMEZONE", "UTC"))
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"
AUTHORIZED_USER = int(os.getenv("TELEGRAM_USER_ID", "0"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Conversation states
ASKING_TIME          = "ASKING_TIME"
ASKING_MESSAGE       = "ASKING_MESSAGE"
ASKING_REPEAT        = "ASKING_REPEAT"
AWAITING_CHECKIN     = "AWAITING_CHECKIN"
ASKING_MORNING_TIME  = "ASKING_MORNING_TIME"
ASKING_EVENING_TIME  = "ASKING_EVENING_TIME"
ASKING_COFFEE_DOSE   = "ASKING_COFFEE_DOSE"
ASKING_NICOTINE_DOSE = "ASKING_NICOTINE_DOSE"

# Default cycle settings (overridable per user)
DEFAULT_MORNING_TIME    = "07:00"
DEFAULT_EVENING_TIME    = "21:00"
DEFAULT_COFFEE_TARGET   = 2
DEFAULT_NICOTINE_TARGET = 3

# Persistent reply keyboards
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 Status"}, {"text": "📝 Log"}, {"text": "📈 History"}],
        [{"text": "🔄 Cycle"},  {"text": "⏭ Skip"}, {"text": "🔁 Reset Cycle"}],
        [{"text": "⏰ Set Time"}, {"text": "💊 Set Dose"}, {"text": "⏸ Pause"}],
    ],
    "resize_keyboard": True,
    "persistent": True,
}

PAUSE_KEYBOARD = {
    "keyboard": [[{"text": "▶️ Resume"}]],
    "resize_keyboard": True,
    "persistent": True,
}

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
                    cycle_start_date DATE NOT NULL,
                    morning_time     TEXT,
                    evening_time     TEXT,
                    coffee_target    INTEGER,
                    nicotine_target  INTEGER,
                    paused           BOOLEAN DEFAULT FALSE
                )
            """)
            # Migrate existing deployments: add new columns if they don't exist yet
            for col, typedef in [
                ("morning_time",    "TEXT"),
                ("evening_time",    "TEXT"),
                ("coffee_target",   "INTEGER"),
                ("nicotine_target", "INTEGER"),
                ("paused",          "BOOLEAN DEFAULT FALSE"),
            ]:
                try:
                    cur.execute(
                        f"ALTER TABLE cycle_config ADD COLUMN IF NOT EXISTS {col} {typedef}"
                    )
                except Exception:
                    pass
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


def get_user_config(chat_id: int) -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """SELECT morning_time, evening_time, coffee_target, nicotine_target, paused
                   FROM cycle_config WHERE chat_id = %s""",
                (chat_id,),
            )
            row = cur.fetchone()
            if not row:
                return {
                    "morning_time":    DEFAULT_MORNING_TIME,
                    "evening_time":    DEFAULT_EVENING_TIME,
                    "coffee_target":   DEFAULT_COFFEE_TARGET,
                    "nicotine_target": DEFAULT_NICOTINE_TARGET,
                    "paused":          False,
                }
            return {
                "morning_time":    row["morning_time"]    or DEFAULT_MORNING_TIME,
                "evening_time":    row["evening_time"]    or DEFAULT_EVENING_TIME,
                "coffee_target":   row["coffee_target"]   if row["coffee_target"]   is not None else DEFAULT_COFFEE_TARGET,
                "nicotine_target": row["nicotine_target"] if row["nicotine_target"] is not None else DEFAULT_NICOTINE_TARGET,
                "paused":          bool(row["paused"]),
            }


def update_user_config(chat_id: int, **kwargs) -> None:
    allowed = {"morning_time", "evening_time", "coffee_target", "nicotine_target", "paused"}
    kwargs = {k: v for k, v in kwargs.items() if k in allowed}
    if not kwargs:
        return
    set_parts = ", ".join(f"{k} = %s" for k in kwargs)
    values = list(kwargs.values()) + [chat_id]
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE cycle_config SET {set_parts} WHERE chat_id = %s",
                values,
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


def get_all_logs(chat_id: int) -> list:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT date, phase, consumed_units
                FROM daily_log WHERE chat_id = %s AND consumed_units IS NOT NULL
                ORDER BY date ASC
                """,
                (chat_id,),
            )
            return cur.fetchall()


def generate_history_chart(chat_id: int, config: dict) -> BytesIO | None:
    if not HAS_MATPLOTLIB:  # optional dependency
        logger.warning("history chart requested but matplotlib is not installed")
        return None

    rows = get_all_logs(chat_id)
    if not rows:
        return None

    coffee_dict = {}
    nicotine_dict = {}
    for log_date, phase, units in rows:
        if phase == "coffee":
            coffee_dict[log_date] = units
        else:
            nicotine_dict[log_date] = units

    min_date = rows[0][0]
    max_date = rows[-1][0]
    date_range = [min_date + timedelta(days=i) for i in range((max_date - min_date).days + 1)]
    # Convert to datetime so matplotlib date formatters work reliably
    dt_range = [datetime(d.year, d.month, d.day) for d in date_range]

    coffee_vals  = [coffee_dict.get(d,   float('nan')) for d in date_range]
    nicotine_vals = [nicotine_dict.get(d, float('nan')) for d in date_range]

    ct = config["coffee_target"]
    nt = config["nicotine_target"]

    fig, ax = plt.subplots(figsize=(10, 4))

    ax.plot(dt_range, coffee_vals,   color='#7B3F00', linewidth=1.5,
            marker='o', markersize=3, label=f'☕ Coffee (target: {ct})')
    ax.plot(dt_range, nicotine_vals, color='#2E8B57', linewidth=1.5,
            marker='o', markersize=3, label=f'◽ Nicotine (target: {nt})')

    ax.axhline(y=ct, color='#7B3F00', linestyle='--', alpha=0.35, linewidth=1)
    ax.axhline(y=nt, color='#2E8B57', linestyle='--', alpha=0.35, linewidth=1)

    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
    fig.autofmt_xdate()
    ax.set_ylabel('Units consumed')
    ax.set_title('Consumption history')
    ax.legend()
    ax.grid(True, alpha=0.25)
    fig.tight_layout()

    buf = BytesIO()
    try:
        fig.savefig(buf, format='png', dpi=110)
    except Exception as e:
        logger.warning("failed to render history chart: %s", e)
        plt.close(fig)
        return None
    plt.close(fig)
    buf.seek(0)
    return buf


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


def format_morning_message(info: dict, config: dict) -> str:
    day = info["cycle_day"]
    rem = info["days_remaining"]
    rem_str = f"{rem} day{'s' if rem != 1 else ''} remaining"
    ev = config["evening_time"]
    if info["phase"] == "coffee":
        ct = config["coffee_target"]
        cups = f"{ct} cup{'s' if ct != 1 else ''}"
        return (
            f"☕ Day {day} — Coffee Phase ({rem_str})\n"
            f"📌 Recommended today: {cups} of coffee. No nicotine.\n"
            f"Check-in tonight at {ev}."
        )
    nt = config["nicotine_target"]
    return (
        f"◽ Day {day} — Nicotine Phase ({rem_str})\n"
        f"📌 Recommended today: {nt}–{nt + 1} pieces of 2mg nicotine gum. No coffee.\n"
        f"Check-in tonight at {ev}."
    )


def format_checkin_prompt(info: dict) -> str:
    substance = "cups" if info["phase"] == "coffee" else "pieces"
    return (
        f"📋 Daily check-in — how did today go?\n"
        f"Reply with the number of {substance} you had, or add a note (e.g. '2 felt good')."
    )


def parse_checkin_reply(text: str) -> tuple:
    match = re.match(r"^(\d+)\s*(.*)?$", text.strip())
    if match:
        return int(match.group(1)), match.group(2).strip() or None
    return None, None


def check_tolerance(chat_id: int, phase: str, config: dict) -> str | None:
    threshold = (
        config["coffee_target"] + 1 if phase == "coffee"
        else config["nicotine_target"] + 2
    )
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
    if phase == "coffee":
        target_str = str(config["coffee_target"])
    else:
        nt = config["nicotine_target"]
        target_str = f"{nt}–{nt + 1}"
    return (
        f"⚠️ You've been at {threshold}+ {substance} for 3 days. "
        f"Consider staying at {target_str} to preserve sensitivity."
    )


def format_status(chat_id: int) -> str:
    today = datetime.now(TZ).date()
    cycle_start = get_cycle_start(chat_id)
    if not cycle_start:
        return "No cycle started. Send /start to begin."
    info = get_cycle_info(cycle_start, today)
    emoji = "☕" if info["phase"] == "coffee" else "◽"
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


def send_photo(chat_id: int, buf: BytesIO, reply_markup=None) -> None:
    data = {"chat_id": chat_id}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    requests.post(
        f"{TELEGRAM_API}/sendPhoto",
        data=data,
        files={"photo": ("chart.png", buf, "image/png")},
        timeout=30,
    )


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
# Command handlers — cycle
# ---------------------------------------------------------------------------

def handle_start(chat_id: int) -> None:
    set_conv(chat_id)
    today = datetime.now(TZ).date()
    cycle_start = get_cycle_start(chat_id)
    if not cycle_start:
        set_cycle_start(chat_id, today)
        send_message(
            chat_id,
            "👋 Welcome! Your cycle starts today.\n\n" + format_status(chat_id),
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        send_message(
            chat_id,
            "👋 Welcome back!\n\n" + format_status(chat_id),
            reply_markup=MAIN_KEYBOARD,
        )


def handle_status(chat_id: int) -> None:
    send_message(chat_id, format_status(chat_id), reply_markup=MAIN_KEYBOARD)


def handle_log_command(chat_id: int, args: str) -> None:
    cycle_start = get_cycle_start(chat_id)
    if not cycle_start:
        send_message(chat_id, "No cycle started. Use /start first.", reply_markup=MAIN_KEYBOARD)
        return
    if not args:
        today = datetime.now(TZ).date()
        info = get_cycle_info(cycle_start, today)
        substance = "cups" if info["phase"] == "coffee" else "pieces"
        set_conv(chat_id, state=AWAITING_CHECKIN)
        send_message(chat_id, f"How many {substance} today? (e.g. '2' or '2 felt good')")
        return
    units, note = parse_checkin_reply(args)
    if units is None:
        send_message(chat_id, "Usage: /log 2  or  /log 2 felt great", reply_markup=MAIN_KEYBOARD)
        return
    today = datetime.now(TZ).date()
    info = get_cycle_info(cycle_start, today)
    log_day(chat_id, today, info["phase"], units, note)
    substance = "cups" if info["phase"] == "coffee" else "pieces"
    send_message(chat_id, f"✅ Logged: {units} {substance} today.", reply_markup=MAIN_KEYBOARD)
    config = get_user_config(chat_id)
    warning = check_tolerance(chat_id, info["phase"], config)
    if warning:
        send_message(chat_id, warning)


def handle_history(chat_id: int) -> None:
    rows = get_recent_logs(chat_id, 14)
    if not rows:
        send_message(chat_id, "No history yet.", reply_markup=MAIN_KEYBOARD)
        return

    config = get_user_config(chat_id)
    ct = config["coffee_target"]
    nt = config["nicotine_target"]

    lines = [f"📊 Last 14 days (targets: ☕ {ct} cups / ◽ {nt}–{nt + 1} pieces):\n"]
    for log_date, phase, units, note in rows:
        emoji = "☕" if phase == "coffee" else "◽"
        units_str = str(units) if units is not None else "—"
        note_str = f"  ({note})" if note else ""
        lines.append(f"{emoji} {log_date}  {units_str}{note_str}")

    coffee_units = [r[2] for r in rows if r[1] == "coffee"   and r[2] is not None]
    nic_units    = [r[2] for r in rows if r[1] == "nicotine" and r[2] is not None]

    stats = []
    for label, emoji, data, target in [
        ("coffee",   "☕", coffee_units, ct),
        ("nicotine", "◽", nic_units,    nt),
    ]:
        if not data:
            continue
        avg = sum(data) / len(data)
        trend = ""
        if len(data) >= 6 and sum(data[:3]) / 3 > sum(data[3:6]) / 3 + 0.5:
            trend = " ↑ trending up"
        unit = "cups" if label == "coffee" else "pieces"
        stats.append(f"{emoji} Avg {label}: {avg:.1f} {unit} (target: {target}){trend}")

    all_logged = [(r[1], r[2]) for r in rows if r[2] is not None]
    streak = best = 0
    for phase, units in reversed(all_logged):
        target = ct if phase == "coffee" else nt
        if units <= target:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    if best:
        stats.append(f"🔥 Longest on-target streak: {best} days")

    if stats:
        lines.append("\n" + "\n".join(stats))
    send_message(chat_id, "\n".join(lines), reply_markup=MAIN_KEYBOARD)

    chart = generate_history_chart(chat_id, config)
    if chart:
        send_photo(chat_id, chart)


def handle_cycle(chat_id: int) -> None:
    cycle_start = get_cycle_start(chat_id)
    if not cycle_start:
        send_message(chat_id, "No cycle started. Use /start first.", reply_markup=MAIN_KEYBOARD)
        return
    config = get_user_config(chat_id)
    ct = config["coffee_target"]
    nt = config["nicotine_target"]
    today = datetime.now(TZ).date()
    current_day = get_cycle_info(cycle_start, today)["cycle_day"]
    lines = ["📅 7-day cycle:\n"]
    for d in range(1, 8):
        if d <= 4:
            label = f"☕ Coffee (target: {ct} cup{'s' if ct != 1 else ''})"
        else:
            label = f"◽ Nicotine (target: {nt}–{nt + 1} pieces)"
        marker = " ← today" if d == current_day else ""
        lines.append(f"Day {d}: {label}{marker}")
    send_message(chat_id, "\n".join(lines), reply_markup=MAIN_KEYBOARD)


def handle_skip(chat_id: int) -> None:
    cycle_start = get_cycle_start(chat_id)
    if not cycle_start:
        send_message(chat_id, "No cycle started. Use /start first.", reply_markup=MAIN_KEYBOARD)
        return
    today = datetime.now(TZ).date()
    info = get_cycle_info(cycle_start, today)
    if info["phase"] == "coffee":
        new_start = today - timedelta(days=4)
        jumped_to = "Nicotine Phase"
    else:
        new_start = today
        jumped_to = "Coffee Phase (new cycle)"
    set_cycle_start(chat_id, new_start)
    send_message(chat_id, f"⏭ Skipped to {jumped_to}.\n\n{format_status(chat_id)}", reply_markup=MAIN_KEYBOARD)


def handle_reset(chat_id: int) -> None:
    cycle_start = get_cycle_start(chat_id)
    if not cycle_start:
        send_message(chat_id, "No cycle started. Use /start first.", reply_markup=MAIN_KEYBOARD)
        return
    today = datetime.now(TZ).date()
    info = get_cycle_info(cycle_start, today)
    if info["phase"] == "coffee":
        new_start = today
    else:
        new_start = today - timedelta(days=4)
    set_cycle_start(chat_id, new_start)
    phase_name = "Coffee Phase" if info["phase"] == "coffee" else "Nicotine Phase"
    send_message(chat_id, f"🔁 Reset to Day 1 of {phase_name}.\n\n{format_status(chat_id)}", reply_markup=MAIN_KEYBOARD)


def handle_set_time(chat_id: int) -> None:
    if not get_cycle_start(chat_id):
        send_message(chat_id, "No cycle started. Use /start first.", reply_markup=MAIN_KEYBOARD)
        return
    config = get_user_config(chat_id)
    set_conv(chat_id, state=ASKING_MORNING_TIME)
    send_message(
        chat_id,
        f"⏰ Set notification times.\n\n"
        f"Current morning time: {config['morning_time']}\n"
        f"Current evening time: {config['evening_time']}\n\n"
        f"What time should the morning message be sent? (HH:MM, 24h format)",
    )


def handle_set_dose(chat_id: int) -> None:
    if not get_cycle_start(chat_id):
        send_message(chat_id, "No cycle started. Use /start first.", reply_markup=MAIN_KEYBOARD)
        return
    config = get_user_config(chat_id)
    set_conv(chat_id, state=ASKING_COFFEE_DOSE)
    send_message(
        chat_id,
        f"💊 Set daily dose targets.\n\n"
        f"Current coffee target: {config['coffee_target']} cups "
        f"(warning fires at {config['coffee_target'] + 1}+)\n"
        f"Current nicotine target: {config['nicotine_target']} pieces "
        f"(warning fires at {config['nicotine_target'] + 2}+)\n\n"
        f"What is your daily coffee target? (number of cups, e.g. 2)",
    )


def handle_pause(chat_id: int) -> None:
    if not get_cycle_start(chat_id):
        send_message(chat_id, "No cycle started. Use /start first.", reply_markup=MAIN_KEYBOARD)
        return
    update_user_config(chat_id, paused=True)
    set_conv(chat_id)
    send_message(
        chat_id,
        "⏸ Bot paused. No notifications will be sent until you resume.",
        reply_markup=PAUSE_KEYBOARD,
    )


def handle_resume(chat_id: int) -> None:
    update_user_config(chat_id, paused=False)
    set_conv(chat_id)
    send_message(
        chat_id,
        "▶️ Welcome back! Resuming your cycle.\n\n" + format_status(chat_id),
        reply_markup=MAIN_KEYBOARD,
    )


# --- Generic reminder handlers ---

def handle_add(chat_id: int) -> None:
    set_conv(chat_id, state=ASKING_TIME)
    send_message(chat_id, "➕ Let's add a reminder!\n\nWhat time? (24h format, e.g. 09:30)")


def handle_list(chat_id: int) -> None:
    rows = get_reminders(chat_id)
    if not rows:
        send_message(chat_id, "You have no reminders.", reply_markup=MAIN_KEYBOARD)
        return
    lines = ["📋 Your reminders:\n"]
    for rid, t, msg, days in rows:
        lines.append(f"#{rid}  {t}  [{days}]\n    {msg}")
    send_message(chat_id, "\n\n".join(lines), reply_markup=MAIN_KEYBOARD)


def handle_cancel(chat_id: int) -> None:
    set_conv(chat_id)
    send_message(chat_id, "Cancelled.", reply_markup=MAIN_KEYBOARD)


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
            f"✅ Reminder set!\n\nTime: {conv['temp_time']}\nMessage: {conv['temp_message']}\nRepeat: {normalized}",
            reply_markup=MAIN_KEYBOARD,
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
        send_message(chat_id, f"✅ Logged: {units} {substance} today. Good work!", reply_markup=MAIN_KEYBOARD)
        config = get_user_config(chat_id)
        warning = check_tolerance(chat_id, info["phase"], config)
        if warning:
            send_message(chat_id, warning)

    elif state == ASKING_MORNING_TIME:
        try:
            parts = text.strip().split(":")
            h, m = int(parts[0]), int(parts[1])
            assert 0 <= h < 24 and 0 <= m < 60
        except Exception:
            send_message(chat_id, "Please use HH:MM format, e.g. 07:00")
            return
        time_str = f"{h:02d}:{m:02d}"
        set_conv(chat_id, state=ASKING_EVENING_TIME, temp_time=time_str)
        send_message(
            chat_id,
            f"Got it! Morning set to {time_str}.\n\n"
            f"What time should the evening check-in be sent? (HH:MM, 24h format)",
        )

    elif state == ASKING_EVENING_TIME:
        try:
            parts = text.strip().split(":")
            h, m = int(parts[0]), int(parts[1])
            assert 0 <= h < 24 and 0 <= m < 60
        except Exception:
            send_message(chat_id, "Please use HH:MM format, e.g. 21:00")
            return
        time_str = f"{h:02d}:{m:02d}"
        morning_time = conv["temp_time"]
        update_user_config(chat_id, morning_time=morning_time, evening_time=time_str)
        set_conv(chat_id)
        send_message(
            chat_id,
            f"✅ Notification times updated!\n\nMorning: {morning_time}\nEvening: {time_str}",
            reply_markup=MAIN_KEYBOARD,
        )

    elif state == ASKING_COFFEE_DOSE:
        try:
            units = int(text.strip())
            assert 1 <= units <= 10
        except Exception:
            send_message(chat_id, "Please enter a number between 1 and 10.")
            return
        set_conv(chat_id, state=ASKING_NICOTINE_DOSE, temp_time=str(units))
        send_message(
            chat_id,
            f"Got it! Coffee target: {units} cup{'s' if units != 1 else ''} "
            f"(warning at {units + 1}+).\n\n"
            f"What is your daily nicotine target? (number of pieces of gum, e.g. 3)",
        )

    elif state == ASKING_NICOTINE_DOSE:
        try:
            units = int(text.strip())
            assert 1 <= units <= 20
        except Exception:
            send_message(chat_id, "Please enter a number between 1 and 20.")
            return
        coffee_target = int(conv["temp_time"])
        update_user_config(chat_id, coffee_target=coffee_target, nicotine_target=units)
        set_conv(chat_id)
        send_message(
            chat_id,
            f"✅ Dose targets updated!\n\n"
            f"☕ Coffee: {coffee_target} cups (warning at {coffee_target + 1}+)\n"
            f"◽ Nicotine: {units} pieces (warning at {units + 2}+)",
            reply_markup=MAIN_KEYBOARD,
        )

    else:
        send_message(chat_id, "Tap a button below or use /status to see your current day.", reply_markup=MAIN_KEYBOARD)


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

    # Check pause state first — only Resume gets through
    config = get_user_config(chat_id)
    if config["paused"]:
        if text == "▶️ Resume":
            handle_resume(chat_id)
        else:
            send_message(chat_id, "⏸ Bot is paused. Press ▶️ Resume to continue.", reply_markup=PAUSE_KEYBOARD)
        return jsonify({"ok": True})

    # Reply keyboard button map
    button_map = {
        "📊 Status":      lambda: handle_status(chat_id),
        "📝 Log":         lambda: handle_log_command(chat_id, ""),
        "📈 History":     lambda: handle_history(chat_id),
        "🔄 Cycle":       lambda: handle_cycle(chat_id),
        "⏭ Skip":        lambda: handle_skip(chat_id),
        "🔁 Reset Cycle": lambda: handle_reset(chat_id),
        "⏰ Set Time":    lambda: handle_set_time(chat_id),
        "💊 Set Dose":    lambda: handle_set_dose(chat_id),
        "⏸ Pause":       lambda: handle_pause(chat_id),
    }

    if text in button_map:
        button_map[text]()
    elif text.startswith("/start"):
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
    elif text.startswith("/reset"):
        handle_reset(chat_id)
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
        cfg = get_user_config(AUTHORIZED_USER)

        if not cfg["paused"]:
            morning_time = cfg["morning_time"]
            evening_time = cfg["evening_time"]
            ev_h, ev_m = map(int, evening_time.split(":"))
            nudge_time = f"{(ev_h + 1) % 24:02d}:{ev_m:02d}"

            # Morning message
            if cycle_start and current_time == morning_time:
                info = get_cycle_info(cycle_start, today)
                send_message(AUTHORIZED_USER, format_morning_message(info, cfg))
                sent += 1

            # Evening check-in (only if not already logged)
            if cycle_start and current_time == evening_time:
                today_log = get_today_log(AUTHORIZED_USER, today)
                if not today_log or today_log["consumed_units"] is None:
                    info = get_cycle_info(cycle_start, today)
                    send_message(AUTHORIZED_USER, format_checkin_prompt(info))
                    set_conv(AUTHORIZED_USER, state=AWAITING_CHECKIN)
                    sent += 1

            # Nudge 1 hour after evening check-in if still not logged
            if cycle_start and current_time == nudge_time:
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
