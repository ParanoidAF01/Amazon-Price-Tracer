# Amazon Price Tracker — WhatsApp / SMS Alerts

Tracks any Amazon product price every **30 minutes** and pings you on
**WhatsApp or SMS** the moment the price drops, then keeps pinging every
30 minutes until the price recovers.

---

## Quick Start

### 1 — Install Python dependencies
```bash
pip install requests beautifulsoup4 lxml twilio schedule
```

### 2 — Get free Twilio credentials
1. Sign up at <https://www.twilio.com/try-twilio> (free trial gives $15 credit).
2. Grab your **Account SID** and **Auth Token** from the dashboard.
3. For **WhatsApp** (easiest): use the free sandbox at  
   <https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn>  
   — send the join code from your WhatsApp to `+14155238886` once.
4. For **plain SMS**: buy a Twilio number (≈$1/month).

### 3 — Set environment variables
```bash
cp .env.example .env
# edit .env with your actual values
source .env
```

Or just edit the `CONFIG` dict directly in `amazon_price_tracker.py`.

### 4 — Run
```bash
python amazon_price_tracker.py
```

The script:
- Fetches the price immediately on start and sets it as the **baseline**.
- Sends you a "tracker started" WhatsApp confirmation.
- Checks every 30 minutes via `schedule`.
- Sends a **price-drop alert** with the saving amount + buy link whenever
  price < baseline.
- Keeps sending the alert every 30 minutes as long as price stays low.
- Sends a **"price recovered"** message when price climbs back to baseline.

---

## Running 24/7 (background / server)

### Linux / Mac — `nohup`
```bash
nohup python amazon_price_tracker.py > /dev/null 2>&1 &
```

### Linux — `systemd` service
Create `/etc/systemd/system/price_tracker.service`:
```ini
[Unit]
Description=Amazon Price Tracker

[Service]
ExecStart=/usr/bin/python3 /path/to/amazon_price_tracker.py
EnvironmentFile=/path/to/.env
Restart=always

[Install]
WantedBy=multi-user.target
```
Then:
```bash
sudo systemctl enable --now price_tracker
```

### Windows — Task Scheduler
Set a trigger "At startup" → action `python amazon_price_tracker.py`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Price always `None` | Amazon is rate-limiting. Add a `time.sleep(5)` before the request, or use a proxy. |
| Twilio error 21211 | Phone number format wrong — must include country code e.g. `+919876543210` |
| WhatsApp not receiving | Make sure you joined the sandbox (send the join keyword first) |
| `lxml` not found | `pip install lxml` or change `"lxml"` to `"html.parser"` in `fetch_price()` |

---

## Customising

| What | Where in config |
|---|---|
| Check interval | `CHECK_INTERVAL_MINUTES` (default 30) |
| Notify on any drop | Current behaviour — baseline is set at first run |
| Notify only if drop > 5% | Add `if drop_pct < 5: return` before `send_alert` in `check_price()` |
| Track multiple products | Run one instance of the script per product URL |

