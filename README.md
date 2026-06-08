# PriceWatch — Amazon Price Tracker Dashboard

A full-stack price tracker with:
- **Public dashboard** — dark, minimal UI showing current / baseline / lowest prices + sparklines
- **Admin panel** — password-protected, add/remove up to 10 products instantly without redeploying
- **Worker** — checks prices every 60 min via ScraperAPI, saves history to Supabase, sends WhatsApp alerts

---

## Architecture

```
Browser (you)
  │
  ├─ GET /          → Public dashboard (reads Supabase)
  └─ GET /admin     → Admin panel (add/remove products in Supabase)
       │
       ▼
  FastAPI (Railway web service)
       │
       ▼
  Supabase (products + price_history tables)
       ▲
       │
  Worker (Railway worker service)
       │ reads products, writes price history
       ├─ ScraperAPI → Amazon prices
       └─ Twilio → WhatsApp alerts
```

---

## Setup

### 1 — Supabase
1. Create a free project at https://supabase.com
2. Go to **SQL Editor** → **New query** → paste contents of `schema.sql` → Run
3. Go to **Settings → API** → copy:
   - `Project URL`  → `SUPABASE_URL`
   - `anon public` key → `SUPABASE_KEY`

### 2 — ScraperAPI
1. Sign up at https://scraperapi.com (free 1,000 calls/month)
2. Copy API key → `SCRAPER_API_KEY`

### 3 — Twilio
1. Sign up at https://twilio.com
2. For WhatsApp: join sandbox at https://console.twilio.com
3. Copy:
   - Account SID → `TWILIO_ACCOUNT_SID`
   - Auth Token  → `TWILIO_AUTH_TOKEN`

### 4 — Push to GitHub
```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/amazon-tracker.git
git push -u origin main
```

### 5 — Deploy to Railway (TWO services from same repo)

#### Service 1 — Web server
1. railway.app → New Project → Deploy from GitHub → select repo
2. Variables tab → add all env vars (see below)
3. Settings → Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Settings → Generate Domain (this is your public URL)

#### Service 2 — Worker
1. Same project → **New Service** → GitHub repo (same repo)
2. Variables tab → add same env vars
3. Settings → Start Command: `python worker.py`
4. No domain needed (it's a background worker)

---

## Environment Variables (set on BOTH services)

| Variable | Description |
|---|---|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Your Supabase anon public key |
| `TWILIO_ACCOUNT_SID` | Twilio Account SID |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token |
| `TWILIO_FROM` | `whatsapp:+14155238886` |
| `ALERT_TO` | `whatsapp:+91XXXXXXXXXX` |
| `SCRAPER_API_KEY` | Your ScraperAPI key |
| `ADMIN_PASSWORD` | Password for /admin (choose something strong) |
| `SESSION_SECRET` | Random string for cookie signing (e.g. run `openssl rand -hex 32`) |
| `CHECK_INTERVAL_MINUTES` | `60` (free ScraperAPI tier) or `30` (paid) |

---

## Usage

1. Visit `https://your-railway-domain.up.railway.app` → see the public dashboard
2. Visit `/admin` → log in with your `ADMIN_PASSWORD`
3. Paste any Amazon product URL → click **Add**
4. Worker picks it up on its next cycle (within the hour)
5. First check → WhatsApp confirmation + baseline set
6. Every subsequent check → silent unless price drops

---

## File structure

```
amazon-tracker/
├── main.py              FastAPI app (web server)
├── worker.py            Price checker (background worker)
├── templates/
│   ├── dashboard.html   Public dark UI
│   └── admin.html       Admin panel
├── requirements.txt
├── Procfile
├── railway.toml
├── schema.sql           Run once in Supabase SQL editor
└── README.md
```
