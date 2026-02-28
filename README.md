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

### SIMPLY
This is actually a pretty elegant little system. Let me walk you through what's happening and why each piece exists.

**The goal:** You want a bot that messages you automatically every day on Telegram. The challenge is that *something* needs to be running 24/7 to send those messages — your laptop can't do that, so you need the internet to do it for you, for free.

---

**The services, in order of why they exist:**

**Telegram + BotFather** — Telegram lets anyone create a bot account. BotFather is Telegram's official bot that creates other bots. You talk to it, it gives you a secret token (like a password) that proves you own your bot.

**Supabase** — This is your bot's memory. Every time you log "2 cups today," that needs to be saved somewhere permanent. Supabase gives you a free online database (think of it as a spreadsheet in the cloud that your bot can read and write to).

**Render** — This is where your bot's brain actually lives and runs. It's a free server in the cloud that runs your Python code. The problem with Render's free tier is that it "falls asleep" if no one pokes it for a while — which would mean your bot misses its scheduled messages.

**cron-job.org** — This exists *specifically* to solve the Render sleep problem. It's a free service that pings your bot every single minute, which keeps Render awake. As a bonus, each ping also triggers your bot to check "is it 7am? Is it 9pm? Should I send a message right now?"

---

**The flow in plain English:**

1. You set up the bot identity on Telegram
2. You set up a database to remember things
3. You put your code on Render so it runs in the cloud
4. You tell Telegram where to find your bot (the "register webhook" step — basically giving Telegram Render's address)
5. You set up cron-job.org to poke Render every minute, keeping it alive and triggering timed messages

Each service is free, which is why there are five of them instead of one paid service that does everything.


## Files

```
boy_cycle.py      # all bot logic: DB, cycle engine, Flask routes
requirements.txt  # dependencies
Procfile          # gunicorn start command for Render
.python-version   # pins Python 3.11 for Render
.env.example      # environment variable template
```
