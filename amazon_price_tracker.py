"""
Amazon Price Tracker  v3  — WhatsApp / SMS Alerts
==================================================
Uses ScraperAPI (https://scraperapi.com) to bypass Amazon bot detection.
Free tier: 1,000 API calls/month — sufficient for testing.
At 30-min checks: ~1,440 calls/month → use their $49/mo plan OR
set CHECK_INTERVAL_MINUTES=60 to stay within the free 1,000 calls.

Setup (5 minutes):
  1. Sign up FREE at https://scraperapi.com → copy your API key
  2. Set env var: SCRAPER_API_KEY=your_key
  3. Set other env vars (Twilio + Amazon URL) as before
  4. pip install requests beautifulsoup4 lxml twilio schedule
"""

import os
import re
import time
import random
import logging
import schedule
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from twilio.rest import Client

# ─────────────────────────── CONFIG ──────────────────────────────────────────
CONFIG = {
    # ── Twilio ────────────────────────────────────────────
    "TWILIO_ACCOUNT_SID": os.getenv("TWILIO_ACCOUNT_SID", "ACxxxxxxxxxxxxxxxx"),
    "TWILIO_AUTH_TOKEN":  os.getenv("TWILIO_AUTH_TOKEN",  "your_auth_token"),
    "FROM_NUMBER":        os.getenv("TWILIO_FROM",        "whatsapp:+14155238886"),
    "TO_NUMBER":          os.getenv("ALERT_TO",           "whatsapp:+919120294045"),

    # ── ScraperAPI (https://scraperapi.com — free 1000 calls/mo) ──
    "SCRAPER_API_KEY":    os.getenv("SCRAPER_API_KEY",    ""),

    # ── Amazon product URL ─────────────────────────────────
    "AMAZON_URL":         os.getenv("AMAZON_URL",         "https://www.amazon.in/dp/B00JAK1PMI"),

    # ── Behaviour ──────────────────────────────────────────
    # Use 60 min interval to stay within ScraperAPI free tier (1000/mo)
    # Switch to 30 once you upgrade their plan
    "CHECK_INTERVAL_MINUTES": int(os.getenv("CHECK_INTERVAL_MINUTES", "60")),
    "MAX_RETRIES":            3,
    "LOG_FILE":               "price_tracker.log",
}
# ─────────────────────────────────────────────────────────────────────────────

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

# ── Global state ──────────────────────────────────────────────────────────────
baseline_price: float | None = None
alert_active:   bool         = False


# ─────────────────────────── UTILITIES ───────────────────────────────────────

def extract_asin(url: str) -> str | None:
    for pat in [r"/dp/([A-Z0-9]{10})", r"/gp/product/([A-Z0-9]{10})"]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def clean_price(raw: str) -> float | None:
    """Strip currency symbols/commas and return float, or None."""
    numeric = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    if not numeric:
        return None
    try:
        return float(numeric)
    except ValueError:
        return None


# ─────────────────── PRICE SELECTORS ─────────────────────────────────────────

def parse_price_from_soup(soup: BeautifulSoup) -> tuple[float | None, str | None]:
    """
    Extract price and title from a parsed Amazon page.
    Returns (price, title).
    """
    # ── CAPTCHA check ──
    if (soup.find("form", {"action": "/errors/validateCaptcha"})
            or "Type the characters" in soup.get_text()):
        log.warning("CAPTCHA page detected.")
        return None, None

    # ── Title ──
    title_el = (
        soup.find("span", {"id": "productTitle"})
        or soup.find("h1",  {"id": "title"})
    )
    title = title_el.get_text(strip=True) if title_el else "Unknown Product"

    # ── Price: ordered from most → least reliable ──
    strategies = [
        # 1. corePriceDisplay div (most common on amazon.in 2024+)
        lambda s: _from_core_price_div(s),
        # 2. apex_desktop div
        lambda s: _first_price_whole(s.find("div", {"id": "apex_desktop"})),
        # 3. Classic IDs
        lambda s: _text(s.find("span", {"id": "priceblock_ourprice"})),
        lambda s: _text(s.find("span", {"id": "priceblock_dealprice"})),
        lambda s: _text(s.find("span", {"id": "priceblock_saleprice"})),
        # 4. Buybox
        lambda s: _text(s.find("span", {"id": "price_inside_buybox"})),
        lambda s: _text(s.find("span", {"id": "newBuyBoxPrice"})),
        # 5. a-price span (whole + fraction)
        lambda s: _from_a_price(s),
        # 6. offer-price
        lambda s: _text(s.find("span", {"class": "offer-price"})),
    ]

    for strategy in strategies:
        raw = strategy(soup)
        if raw:
            price = clean_price(raw)
            if price and price > 1:          # sanity: price must be > ₹1
                return price, title

    log.warning("No price selector matched. Page title: '%s'",
                soup.title.string if soup.title else "N/A")
    return None, title


def _text(el) -> str | None:
    return el.get_text(strip=True) if el else None


def _from_core_price_div(soup: BeautifulSoup) -> str | None:
    div = soup.find("div", {"id": "corePriceDisplay_desktop_feature_div"})
    return _first_price_whole(div)


def _first_price_whole(container) -> str | None:
    if not container:
        return None
    el = container.find("span", {"class": "a-price-whole"})
    return el.get_text(strip=True) if el else None


def _from_a_price(soup: BeautifulSoup) -> str | None:
    """Combine a-price-whole + a-price-fraction for full price."""
    whole_el    = soup.find("span", {"class": "a-price-whole"})
    fraction_el = soup.find("span", {"class": "a-price-fraction"})
    if whole_el:
        whole    = whole_el.get_text(strip=True).rstrip(".")
        fraction = fraction_el.get_text(strip=True) if fraction_el else "00"
        return f"{whole}.{fraction}"
    return None


# ─────────────────── FETCH METHODS ───────────────────────────────────────────

def fetch_via_scraperapi(url: str) -> tuple[float | None, str | None]:
    """
    Route the request through ScraperAPI which handles:
      • Rotating residential proxies
      • JS rendering (premium)
      • CAPTCHA solving
    Returns (price, title) or (None, None).
    """
    api_key = CONFIG["SCRAPER_API_KEY"]
    if not api_key:
        return None, None

    endpoint = "https://api.scraperapi.com/"
    params   = {
        "api_key":          api_key,
        "url":              url,
        "country_code":     "in",        # use Indian IPs for amazon.in
        "device_type":      "desktop",
        "render":           "false",     # set "true" if price still missing (uses JS credits)
        "keep_headers":     "true",
    }

    for attempt in range(1, CONFIG["MAX_RETRIES"] + 1):
        wait = random.uniform(2, 5) * attempt
        log.info("ScraperAPI attempt %d/%d (wait %.1fs)…", attempt, CONFIG["MAX_RETRIES"], wait)
        time.sleep(wait)

        try:
            resp = requests.get(endpoint, params=params, timeout=60)

            if resp.status_code == 403:
                log.warning("ScraperAPI 403 — check your API key or quota.")
                return None, None
            if resp.status_code == 500:
                log.warning("ScraperAPI 500 — target blocked; retrying…")
                continue

            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")
            price, title = parse_price_from_soup(soup)

            if price:
                log.info("✓ ScraperAPI  →  ₹%.2f", price)
                return price, title

            log.warning("ScraperAPI returned page but no price found (attempt %d).", attempt)

        except requests.RequestException as exc:
            log.warning("ScraperAPI request error (attempt %d): %s", attempt, exc)

    return None, None


def fetch_via_direct(url: str) -> tuple[float | None, str | None]:
    """
    Last-resort direct scrape (will usually fail on cloud IPs for amazon.in,
    but worth one try in case Railway's exit IP happens to be clean).
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language":         "en-IN,en;q=0.9",
        "Accept":                  "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding":         "gzip, deflate, br",
        "Referer":                 "https://www.google.com/",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        time.sleep(random.uniform(2, 5))
        resp = requests.get(url, headers=headers, timeout=25)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        price, title = parse_price_from_soup(soup)
        if price:
            log.info("✓ Direct scrape  →  ₹%.2f", price)
        return price, title
    except Exception as exc:
        log.warning("Direct scrape failed: %s", exc)
        return None, None


def fetch_price(url: str) -> tuple[float | None, str | None]:
    """Unified fetch: ScraperAPI first, direct scrape as last resort."""
    asin = extract_asin(url)
    log.info("ASIN: %s", asin)

    if CONFIG["SCRAPER_API_KEY"]:
        price, title = fetch_via_scraperapi(url)
        if price:
            return price, title
        log.warning("ScraperAPI exhausted — trying direct scrape as last resort…")

    return fetch_via_direct(url)


# ─────────────────── TWILIO ALERT ────────────────────────────────────────────

def send_alert(body: str) -> bool:
    try:
        client = Client(CONFIG["TWILIO_ACCOUNT_SID"], CONFIG["TWILIO_AUTH_TOKEN"])
        msg = client.messages.create(
            body=body,
            from_=CONFIG["FROM_NUMBER"],
            to=CONFIG["TO_NUMBER"],
        )
        log.info("Alert sent ✓  SID=%s", msg.sid)
        return True
    except Exception as exc:
        log.error("Twilio error: %s", exc)
        return False


# ─────────────────── CORE CHECK ──────────────────────────────────────────────

def check_price() -> None:
    global baseline_price, alert_active

    url = CONFIG["AMAZON_URL"]
    log.info("── Checking price ───────────────────────────────────")

    current_price, title = fetch_price(url)

    if current_price is None:
        log.warning("Could not read price this cycle. Will retry next interval.")
        return

    log.info("Product : %.80s", title or "")
    log.info("Price   : ₹%.2f", current_price)

    # ── First successful read → set baseline ──
    if baseline_price is None:
        baseline_price = current_price
        log.info("Baseline set → ₹%.2f", baseline_price)
        send_alert(
            f"🛒 Price Tracker Started!\n"
            f"Product : {(title or '')[:60]}\n"
            f"Baseline: ₹{baseline_price:,.2f}\n"
            f"Interval: every {CONFIG['CHECK_INTERVAL_MINUTES']} min\n"
            f"You'll be pinged when the price drops."
        )
        return

    drop     = baseline_price - current_price
    drop_pct = (drop / baseline_price) * 100

    if current_price < baseline_price:
        alert_active = True
        log.info("PRICE DROP  ₹%.2f → ₹%.2f  (↓₹%.2f / %.1f%%)",
                 baseline_price, current_price, drop, drop_pct)
        send_alert(
            f"🚨 Price Drop Alert!\n"
            f"Product : {(title or '')[:60]}\n"
            f"Was     : ₹{baseline_price:,.2f}\n"
            f"Now     : ₹{current_price:,.2f}\n"
            f"You save: ₹{drop:,.2f}  ({drop_pct:.1f}% off)\n"
            f"Buy now : {url}\n"
            f"Time    : {datetime.now().strftime('%d %b %Y  %H:%M')}"
        )

    elif alert_active and current_price >= baseline_price:
        alert_active = False
        log.info("Price recovered. Alerts paused.")
        send_alert(
            f"✅ Price Back to Normal\n"
            f"Product : {(title or '')[:60]}\n"
            f"Current : ₹{current_price:,.2f}\n"
            f"Baseline: ₹{baseline_price:,.2f}\n"
            f"Alerts paused until next drop."
        )

    else:
        log.info("No change. Baseline ₹%.2f | Current ₹%.2f", baseline_price, current_price)


# ─────────────────── ENTRY POINT ─────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("Amazon Price Tracker  v3")
    log.info("Interval : %d min", CONFIG["CHECK_INTERVAL_MINUTES"])
    log.info("URL      : %s",     CONFIG["AMAZON_URL"])
    log.info("Alerting : %s → %s", CONFIG["FROM_NUMBER"], CONFIG["TO_NUMBER"])
    log.info("Fetch    : %s", "ScraperAPI" if CONFIG["SCRAPER_API_KEY"] else "Direct scrape (may fail)")
    log.info("=" * 60)

    check_price()
    schedule.every(CONFIG["CHECK_INTERVAL_MINUTES"]).minutes.do(check_price)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
