"""
Price Checker Worker  v4
------------------------
Reads active products from Supabase, checks each price via ScraperAPI,
writes history back, and fires Twilio WhatsApp/SMS alerts on drops.
Runs on its own Railway service — completely decoupled from the web server.
"""

import os
import re
import time
import random
import logging
import schedule
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from twilio.rest import Client
from supabase import create_client, Client as SupabaseClient

# ── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL        = os.environ["SUPABASE_URL"]
SUPABASE_KEY        = os.environ["SUPABASE_KEY"]
TWILIO_ACCOUNT_SID  = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
FROM_NUMBER         = os.environ["TWILIO_FROM"]
TO_NUMBER           = os.environ["ALERT_TO"]
SCRAPER_API_KEY     = os.environ.get("SCRAPER_API_KEY", "")
CHECK_INTERVAL      = int(os.environ.get("CHECK_INTERVAL_MINUTES", "60"))
MAX_RETRIES         = 3

supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("worker")

# ── Price scraping ────────────────────────────────────────────────────────────
def extract_asin(url: str):
    for pat in [r"/dp/([A-Z0-9]{10})", r"/gp/product/([A-Z0-9]{10})"]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None

def clean_price(raw: str):
    numeric = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    try:
        return float(numeric) if numeric else None
    except ValueError:
        return None

def parse_soup(soup: BeautifulSoup):
    if soup.find("form", {"action": "/errors/validateCaptcha"}) or \
       "Type the characters" in soup.get_text():
        return None, None

    title_el = soup.find("span", {"id": "productTitle"}) or soup.find("h1", {"id": "title"})
    title = title_el.get_text(strip=True) if title_el else "Unknown Product"

    strategies = [
        lambda s: _from_div(s, "corePriceDisplay_desktop_feature_div"),
        lambda s: _from_div(s, "apex_desktop"),
        lambda s: _span_text(s, {"id": "priceblock_ourprice"}),
        lambda s: _span_text(s, {"id": "priceblock_dealprice"}),
        lambda s: _span_text(s, {"id": "price_inside_buybox"}),
        lambda s: _span_text(s, {"id": "newBuyBoxPrice"}),
        lambda s: _a_price(s),
        lambda s: _span_text(s, {"class": "offer-price"}),
    ]
    for fn in strategies:
        raw = fn(soup)
        if raw:
            p = clean_price(raw)
            if p and p > 1:
                return p, title
    return None, title

def _span_text(soup, attrs):
    el = soup.find("span", attrs)
    return el.get_text(strip=True) if el else None

def _from_div(soup, div_id):
    div = soup.find("div", {"id": div_id})
    if not div:
        return None
    el = div.find("span", {"class": "a-price-whole"})
    return el.get_text(strip=True) if el else None

def _a_price(soup):
    w = soup.find("span", {"class": "a-price-whole"})
    f = soup.find("span", {"class": "a-price-fraction"})
    if w:
        whole = w.get_text(strip=True).rstrip(".")
        frac  = f.get_text(strip=True) if f else "00"
        return f"{whole}.{frac}"
    return None

def fetch_price(url: str):
    if not SCRAPER_API_KEY:
        log.warning("No SCRAPER_API_KEY set — skipping.")
        return None, None

    params = {
        "api_key":      SCRAPER_API_KEY,
        "url":          url,
        "country_code": "in",
        "device_type":  "desktop",
        "render":       "false",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        wait = random.uniform(2, 5) * attempt
        log.info("  Attempt %d/%d (%.1fs wait)…", attempt, MAX_RETRIES, wait)
        time.sleep(wait)
        try:
            resp = requests.get("https://api.scraperapi.com/", params=params, timeout=60)
            if resp.status_code in (403, 500):
                log.warning("  ScraperAPI %d", resp.status_code)
                continue
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            price, title = parse_soup(soup)
            if price:
                return price, title
        except Exception as exc:
            log.warning("  Request error: %s", exc)

    return None, None

# ── Twilio alert ──────────────────────────────────────────────────────────────
def send_alert(body: str):
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(body=body, from_=FROM_NUMBER, to=TO_NUMBER)
        log.info("  Alert sent ✓  SID=%s", msg.sid)
    except Exception as exc:
        log.error("  Twilio error: %s", exc)

# ── Core check cycle ──────────────────────────────────────────────────────────
def check_all_products():
    log.info("═" * 56)
    log.info("Check cycle  %s", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    log.info("═" * 56)

    products = supabase.table("products").select("*").eq("active", True).execute().data or []

    if not products:
        log.info("No active products to check.")
        return

    for product in products:
        pid      = product["id"]
        url      = product["url"]
        name     = product.get("name", "Unknown")
        baseline = product.get("baseline_price")

        log.info("▶ %s", name[:60])
        current_price, fetched_name = fetch_price(url)

        if current_price is None:
            log.warning("  Could not read price — skipping.")
            continue

        log.info("  Price: ₹%.2f", current_price)

        # Auto-update name from page if not set
        if fetched_name and (not name or name in ("Unknown Product", "New product")):
            supabase.table("products").update({"name": fetched_name[:120]}).eq("id", pid).execute()
            name = fetched_name[:60]

        # Save price history
        supabase.table("price_history").insert({
            "product_id": pid,
            "price":      current_price,
        }).execute()

        # First time: set baseline
        if baseline is None:
            supabase.table("products").update({"baseline_price": current_price}).eq("id", pid).execute()
            send_alert(
                f"🛒 Tracker Started!\n"
                f"Product : {name[:60]}\n"
                f"Baseline: ₹{current_price:,.2f}\n"
                f"Checking every {CHECK_INTERVAL} min."
            )
            log.info("  Baseline set to ₹%.2f", current_price)
            continue

        drop     = baseline - current_price
        drop_pct = (drop / baseline) * 100 if baseline else 0

        if current_price < baseline:
            log.info("  PRICE DROP ₹%.2f → ₹%.2f (↓%.1f%%)", baseline, current_price, drop_pct)
            send_alert(
                f"🚨 Price Drop!\n"
                f"Product : {name[:60]}\n"
                f"Was     : ₹{baseline:,.2f}\n"
                f"Now     : ₹{current_price:,.2f}\n"
                f"Save    : ₹{drop:,.2f} ({drop_pct:.1f}% off)\n"
                f"Buy     : {url}\n"
                f"Time    : {datetime.now().strftime('%d %b %Y %H:%M')}"
            )
        else:
            log.info("  No drop. Baseline ₹%.2f | Current ₹%.2f", baseline, current_price)

        # Small delay between products to avoid hammering ScraperAPI
        time.sleep(random.uniform(3, 7))

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    log.info("Amazon Price Tracker Worker  v4")
    log.info("Interval : %d min", CHECK_INTERVAL)
    log.info("Alerting : %s → %s", FROM_NUMBER, TO_NUMBER)
    log.info("ScraperAPI: %s", "configured ✓" if SCRAPER_API_KEY else "MISSING ✗")

    check_all_products()
    schedule.every(CHECK_INTERVAL).minutes.do(check_all_products)

    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    main()
