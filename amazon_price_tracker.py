"""
Amazon Price Tracker with WhatsApp / SMS Alerts
================================================
Checks the price of an Amazon product every 30 minutes.
• Sends a WhatsApp (or SMS) alert the moment the price drops below the
  baseline price you recorded when you first ran the script.
• Keeps pinging you every 30 minutes until the price recovers back to
  (or above) the original baseline.

Prerequisites
-------------
1. Install dependencies:
       pip install requests beautifulsoup4 lxml twilio schedule

2. Set environment variables (or fill the CONFIG block below):
       TWILIO_ACCOUNT_SID   – from https://console.twilio.com
       TWILIO_AUTH_TOKEN    – from https://console.twilio.com
       TWILIO_FROM          – your Twilio number  e.g. +14155238886
                              For WhatsApp: whatsapp:+14155238886
       ALERT_TO             – your number          e.g. +919876543210
                              For WhatsApp: whatsapp:+919876543210
       AMAZON_URL           – full Amazon product URL

   Or simply edit the CONFIG dict below.

WhatsApp sandbox note
---------------------
To use the free WhatsApp sandbox:
  • Twilio FROM  →  whatsapp:+14155238886
  • Your number  →  whatsapp:+91XXXXXXXXXX
  • Join sandbox first: https://console.twilio.com/us1/develop/sms/try-it-out/whatsapp-learn
"""

import os
import re
import time
import logging
import schedule
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from twilio.rest import Client

# ─────────────────────────── CONFIG ──────────────────────────────────────────
CONFIG = {
    # ── Twilio credentials (env vars take precedence) ──
    "TWILIO_ACCOUNT_SID": os.getenv("TWILIO_ACCOUNT_SID", "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"),
    "TWILIO_AUTH_TOKEN":  os.getenv("TWILIO_AUTH_TOKEN",  "your_auth_token_here"),

    # ── Phone numbers ──────────────────────────────────
    # For WhatsApp prefix with  whatsapp:
    # For plain SMS keep just   +91XXXXXXXXXX
    "FROM_NUMBER": os.getenv("TWILIO_FROM", "whatsapp:+14155238886"),
    "TO_NUMBER":   os.getenv("ALERT_TO",    "whatsapp:+919876543210"),  # ← YOUR number

    # ── Amazon product URL ─────────────────────────────
    "AMAZON_URL": os.getenv(
        "AMAZON_URL",
        "https://www.amazon.in/dp/XXXXXXXXXX"          # ← paste your URL here
    ),

    # ── Tracker behaviour ──────────────────────────────
    "CHECK_INTERVAL_MINUTES": 30,   # how often to check
    "LOG_FILE": "price_tracker.log",
}
# ─────────────────────────────────────────────────────────────────────────────

# Rotate User-Agent strings to reduce bot-blocking
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
]
_ua_index = 0

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(CONFIG["LOG_FILE"]),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("price_tracker")

# ── State ─────────────────────────────────────────────────────────────────────
baseline_price: float | None = None   # price captured on first successful fetch
alert_active: bool = False            # True while price is below baseline


# ─────────────────────────── HELPERS ─────────────────────────────────────────

def _next_user_agent() -> str:
    global _ua_index
    ua = USER_AGENTS[_ua_index % len(USER_AGENTS)]
    _ua_index += 1
    return ua


def fetch_price(url: str) -> tuple[float | None, str | None]:
    """
    Scrape the current price and product title from an Amazon product page.
    Returns (price_as_float, product_title) or (None, None) on failure.
    """
    headers = {
        "User-Agent": _next_user_agent(),
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.amazon.in/",
        "DNT": "1",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("HTTP request failed: %s", exc)
        return None, None

    soup = BeautifulSoup(resp.text, "lxml")

    # ── Title ──
    title_tag = (
        soup.find("span", {"id": "productTitle"})
        or soup.find("h1", {"id": "title"})
    )
    title = title_tag.get_text(strip=True) if title_tag else "Unknown Product"

    # ── Price  (try several selectors Amazon uses) ──
    price_selectors = [
        {"id": "priceblock_ourprice"},
        {"id": "priceblock_dealprice"},
        {"id": "priceblock_saleprice"},
        {"class": "a-price-whole"},
        {"class": "priceToPay"},
    ]
    raw_price: str | None = None

    for sel in price_selectors:
        tag = soup.find("span", sel)
        if tag:
            raw_price = tag.get_text(strip=True)
            break

    # Also try the corePriceDisplay block (used on many .in pages)
    if not raw_price:
        core = soup.find("div", {"id": "corePriceDisplay_desktop_feature_div"})
        if core:
            whole = core.find("span", {"class": "a-price-whole"})
            if whole:
                raw_price = whole.get_text(strip=True)

    if not raw_price:
        log.warning("Could not find price on page. Amazon may be blocking scraping.")
        return None, title

    # Strip currency symbols, commas, dots-used-as-thousands-separators
    numeric = re.sub(r"[^\d.]", "", raw_price.replace(",", ""))
    # Handle Indian formatting: "1.299" really means 1299
    if numeric.count(".") > 1:
        numeric = numeric.replace(".", "", numeric.count(".") - 1)

    try:
        price = float(numeric)
    except ValueError:
        log.error("Could not convert '%s' to float", numeric)
        return None, title

    return price, title


def send_alert(message: str) -> bool:
    """Send WhatsApp / SMS via Twilio. Returns True on success."""
    try:
        client = Client(CONFIG["TWILIO_ACCOUNT_SID"], CONFIG["TWILIO_AUTH_TOKEN"])
        msg = client.messages.create(
            body=message,
            from_=CONFIG["FROM_NUMBER"],
            to=CONFIG["TO_NUMBER"],
        )
        log.info("Alert sent ✓  SID=%s", msg.sid)
        return True
    except Exception as exc:
        log.error("Failed to send alert: %s", exc)
        return False


# ─────────────────────────── CORE LOGIC ──────────────────────────────────────

def check_price() -> None:
    global baseline_price, alert_active

    url = CONFIG["AMAZON_URL"]
    log.info("Checking price…  %s", url)

    current_price, title = fetch_price(url)

    if current_price is None:
        log.warning("Skipping this cycle — could not read price.")
        return

    log.info("Product : %s", title[:80])
    log.info("Price   : ₹%.2f", current_price)

    # ── First run: establish baseline ──
    if baseline_price is None:
        baseline_price = current_price
        log.info("Baseline set to ₹%.2f", baseline_price)
        send_alert(
            f"🛒 Price Tracker Started!\n"
            f"Product: {title[:60]}\n"
            f"Baseline price: ₹{baseline_price:,.2f}\n"
            f"Checking every {CONFIG['CHECK_INTERVAL_MINUTES']} min.\n"
            f"You'll be pinged whenever the price drops."
        )
        return

    drop = baseline_price - current_price
    drop_pct = (drop / baseline_price) * 100

    if current_price < baseline_price:
        # ── Price has dropped ──
        alert_active = True
        log.info(
            "PRICE DROP  ₹%.2f → ₹%.2f  (↓ ₹%.2f / %.1f%%)",
            baseline_price, current_price, drop, drop_pct,
        )
        send_alert(
            f"🚨 Price Drop Alert!\n"
            f"Product: {title[:60]}\n"
            f"Was  : ₹{baseline_price:,.2f}\n"
            f"Now  : ₹{current_price:,.2f}\n"
            f"Save : ₹{drop:,.2f}  ({drop_pct:.1f}% off)\n"
            f"Buy  : {url}\n"
            f"Time : {datetime.now().strftime('%d %b %Y  %H:%M')}"
        )

    elif alert_active and current_price >= baseline_price:
        # ── Price recovered — stop alerting ──
        alert_active = False
        log.info("Price recovered to ₹%.2f (baseline ₹%.2f). Alerts paused.", current_price, baseline_price)
        send_alert(
            f"✅ Price Back to Normal\n"
            f"Product: {title[:60]}\n"
            f"Current: ₹{current_price:,.2f}\n"
            f"Baseline was ₹{baseline_price:,.2f}\n"
            f"Alerts paused until next drop."
        )

    else:
        log.info("No drop detected (baseline ₹%.2f, current ₹%.2f).", baseline_price, current_price)


# ─────────────────────────── ENTRY POINT ─────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("Amazon Price Tracker starting up")
    log.info("Interval : %d minutes", CONFIG["CHECK_INTERVAL_MINUTES"])
    log.info("URL      : %s", CONFIG["AMAZON_URL"])
    log.info("Alerting : %s → %s", CONFIG["FROM_NUMBER"], CONFIG["TO_NUMBER"])
    log.info("=" * 60)

    # Run immediately, then on schedule
    check_price()
    schedule.every(CONFIG["CHECK_INTERVAL_MINUTES"]).minutes.do(check_price)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
