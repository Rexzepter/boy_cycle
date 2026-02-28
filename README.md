# boy_cycle

A personal Telegram bot for managing a 7-day caffeine/nicotine sensitivity cycle. Sends daily morning briefings, evening check-ins, and tracks consumption over time.

---

## What it does

The bot runs a perpetual 7-day cycle:

| Days | Phase | Target |
|------|-------|--------|
| 1–4 | ☕ Coffee | 2 cups/day, no nicotine |
| 5–7 | ◽ Nicotine | 3–4 pieces of 2mg gum, no coffee |
| Day 8 | → resets to Day 1 | |

**Automated daily messages:**
- **7:00 AM** — morning briefing with phase, day, and recommended consumption
- **9:00 PM** — check-in prompt asking how many cups/pieces today
- **10:00 PM** — nudge if no reply
- **11:55 PM** — auto-logs "no data" if still no reply

**Commands / keyboard buttons:**
| Button | Function |
|--------|----------|
| 📊 Status | Current phase, day, days remaining, today's log |
| 📝 Log | Log today's consumption (prompts for number) |
| 📈 History | Last 14 days of logs + averages, trend, streak |
| 🔄 Cycle | Full 7-day schedule with today marked |
| ⏭ Skip | Jump to the next phase immediately |
| 🔁 Reset | Reset to Day 1 of the current phase |

**Tolerance warnings** — fires after logging if you've been at 4+ cups (coffee) or 5+ pieces (nicotine) for 3 consecutive days.

---

## Architecture

```
Telegram ──► POST /webhook  ──► Flask app (Render)
                                      │
                                      └──► Supabase (PostgreSQL)

cron-job.org ──► GET /cron (every minute) ──► check time, fire messages
```

**Stack:**
- Python 3.11 + Flask — webhook handler and cron endpoint
- psycopg2 — PostgreSQL client
- Supabase — free hosted PostgreSQL database
- Render — free web service hosting (kept alive by cron pings)
- cron-job.org — free external cron, hits `/cron` every minute

---

## Database tables

| Table | Purpose |
|-------|---------|
| `cycle_config` | Stores `cycle_start_date` per user |
| `daily_log` | Date, phase, consumed units, notes |
| `conversations` | Tracks multi-step conversation state |
| `reminders` | Generic timed reminders (infrastructure, unused in UI) |

---

## Setup

### 1. Telegram bot
1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the token

### 2. Supabase (database)
1. Create a project at [supabase.com](https://supabase.com)
2. Go to **Settings → Database → Connection pooling → URI**
3. Copy the pooler connection string (port 6543)

### 3. Environment variables
Copy `.env.example` to `.env` and fill in:
```
BOT_TOKEN=...
DATABASE_URL=postgresql://postgres.[ref]:[password]@...pooler.supabase.com:6543/postgres
TIMEZONE=Europe/Belgrade
TELEGRAM_USER_ID=...          # your Telegram numeric ID (get it from @userinfobot)
```

### 4. Render (hosting)
1. Push this repo to GitHub
2. New Web Service on [render.com](https://render.com) → connect repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn boy_cycle:app`
5. Add the four environment variables above
6. Deploy

### 5. Register webhook (once after deploy)
Visit in browser:
```
https://YOUR-APP.onrender.com/setup?url=https://YOUR-APP.onrender.com
```
Expected response: `{"ok": true, "description": "Webhook was set"}`

### 6. cron-job.org
1. Create a free account at [cron-job.org](https://cron-job.org)
2. New cron job → URL: `https://YOUR-APP.onrender.com/cron`
3. Schedule: every 1 minute
4. Save and enable

### 7. Start the cycle
Send `/start` to your bot on Telegram.

---

## Local development
```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your values
python boy_cycle.py    # runs on localhost:5000
```
For local testing use polling or expose via [ngrok](https://ngrok.com) to receive webhooks.

---

## Files

```
boy_cycle.py      # all bot logic: DB, cycle engine, Flask routes
requirements.txt  # dependencies
Procfile          # gunicorn start command for Render
.python-version   # pins Python 3.11 for Render
.env.example      # environment variable template
```
