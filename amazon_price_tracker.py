"""
Amazon Price Tracker with WhatsApp / SMS Alerts
================================================
Checks the price of an Amazon product every 30 minutes.
Sends WhatsApp/SMS via Twilio when price drops, keeps pinging
every 30 min until the price recovers to baseline.

Anti-bot strategy used (in order):
  1. Realistic browser headers + session cookies
  2. Random human-like delay before each request
  3. rainforestapi.com (free tier) as primary source — no scraping at all
  4. BeautifulSoup HTML scraping as fallback with 10+ CSS selectors
  5. Automatic retry with exponential back-off on failure

Prerequisites
-------------
  pip install requests beautifulsoup4 lxml twilio schedule

Optional (for the API route — most reliable):
  Sign up FREE at https://rainforestapi.com  → get your API key
  Set env var  RAINFOREST_API_KEY=your_key
  (If not set, falls back to HTML scraping)
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
    "TWILIO_ACCOUNT_SID": os.getenv("TWILIO_ACCOUNT_SID", "ACxxxxxxxxxxxxxxxx"),
    "TWILIO_AUTH_TOKEN":  os.getenv("TWILIO_AUTH_TOKEN",  "your_auth_token"),
    "FROM_NUMBER":        os.getenv("TWILIO_FROM",        "whatsapp:+14155238886"),
    "TO_NUMBER":          os.getenv("ALERT_TO",           "whatsapp:+919120294045"),
    "AMAZON_URL":         os.getenv("AMAZON_URL",         "https://www.amazon.in/dp/B00JAK1PMI"),
    "RAINFOREST_API_KEY": os.getenv("RAINFOREST_API_KEY", ""),   # optional but recommended
    "CHECK_INTERVAL_MINUTES": 30,
    "LOG_FILE": "price_tracker.log",
    "MAX_SCRAPE_RETRIES": 3,
    "RETRY_DELAY_SECONDS": 15,
}
# ─────────────────────────────────────────────────────────────────────────────

# Large pool of realistic User-Agents
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
]

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
baseline_price: float | None = None
alert_active: bool = False


# ─────────────────────── ASIN EXTRACTOR ──────────────────────────────────────

def extract_asin(url: str) -> str | None:
    """Pull the ASIN out of any Amazon URL format."""
    patterns = [
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r"/product/([A-Z0-9]{10})",
        r"([A-Z0-9]{10})(?:[/?]|$)",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def detect_domain(url: str) -> str:
    """Return amazon.in, amazon.com etc from URL."""
    m = re.search(r"(amazon\.[a-z.]+)", url)
    return m.group(1) if m else "amazon.in"


# ─────────────────── METHOD 1: RAINFOREST API ─────────────────────────────────

def fetch_via_rainforest(asin: str, domain: str) -> tuple[float | None, str | None]:
    """
    Use Rainforest API (https://rainforestapi.com) — free 100 req/month.
    Returns (price, title) or (None, None).
    """
    api_key = CONFIG["RAINFOREST_API_KEY"]
    if not api_key:
        return None, None

    # Map Amazon domain → Rainforest amazon_domain param
    domain_map = {
        "amazon.in": "amazon.in",
        "amazon.com": "amazon.com",
        "amazon.co.uk": "amazon.co.uk",
        "amazon.de": "amazon.de",
    }
    amazon_domain = domain_map.get(domain, "amazon.in")

    params = {
        "api_key": api_key,
        "type": "product",
        "asin": asin,
        "amazon_domain": amazon_domain,
    }

    try:
        resp = requests.get(
            "https://api.rainforestapi.com/request",
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        product = data.get("product", {})
        title = product.get("title", "Unknown Product")

        # Try buybox price first, then main price
        price = None
        for key in ["buybox_winner", "price"]:
            p = product.get(key, {})
            if isinstance(p, dict):
                val = p.get("value") or p.get("raw")
                if val:
                    price = float(re.sub(r"[^\d.]", "", str(val)))
                    break

        if price:
            log.info("✓ Price fetched via Rainforest API")
            return price, title
        else:
            log.warning("Rainforest API returned no price for ASIN %s", asin)
            return None, title

    except Exception as exc:
        log.warning("Rainforest API error: %s", exc)
        return None, None


# ─────────────────── METHOD 2: HTML SCRAPING ─────────────────────────────────

def _make_session() -> requests.Session:
    """Create a session that looks like a real browser."""
    session = requests.Session()
    ua = random.choice(USER_AGENTS)

    session.headers.update({
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "DNT": "1",
    })

    # Warm up cookies by hitting the homepage first (looks human)
    try:
        session.get("https://www.amazon.in", timeout=10)
        time.sleep(random.uniform(1.5, 3.5))
    except Exception:
        pass

    return session


def _extract_price_from_soup(soup: BeautifulSoup) -> str | None:
    """Try every known Amazon price selector. Returns raw price string or None."""

    # Check for CAPTCHA / bot detection page
    if soup.find("form", {"action": "/errors/validateCaptcha"}):
        log.warning("Amazon served a CAPTCHA page — bot detected.")
        return None
    if "Type the characters you see in this image" in soup.get_text():
        log.warning("Amazon served a CAPTCHA page — bot detected.")
        return None

    # Ordered list of selectors: most reliable first
    selectors = [
        # Core price display (most common 2024+)
        ("div",  {"id": "corePriceDisplay_desktop_feature_div"}),
        ("div",  {"id": "apex_desktop"}),
        # Standard price blocks
        ("span", {"id": "priceblock_ourprice"}),
        ("span", {"id": "priceblock_dealprice"}),
        ("span", {"id": "priceblock_saleprice"}),
        # Deal / sale price
        ("span", {"id": "sns-base-price"}),
        # Generic a-price spans
        ("span", {"class": "a-price-whole"}),
        # priceToPay (used for EMI/finance pages)
        ("span", {"class": "priceToPay"}),
        # Offer listing
        ("span", {"class": "offer-price"}),
        # Mobile layout
        ("span", {"id": "price_inside_buybox"}),
        ("span", {"id": "newBuyBoxPrice"}),
    ]

    for tag, attrs in selectors:
        el = soup.find(tag, attrs)
        if not el:
            continue

        # If it's a container div, look inside for the price span
        if tag == "div":
            inner = el.find("span", {"class": "a-price-whole"})
            if not inner:
                inner = el.find("span", {"class": re.compile(r"a-price")})
            if inner:
                el = inner

        text = el.get_text(strip=True)
        # Must contain a digit to be valid
        if re.search(r"\d", text):
            return text

    return None


def fetch_via_scraping(url: str) -> tuple[float | None, str | None]:
    """HTML scraping with retries and random delays."""
    for attempt in range(1, CONFIG["MAX_SCRAPE_RETRIES"] + 1):
        delay = random.uniform(3, 8) * attempt
        log.info("Scrape attempt %d/%d — waiting %.1fs before request…",
                 attempt, CONFIG["MAX_SCRAPE_RETRIES"], delay)
        time.sleep(delay)

        session = _make_session()
        try:
            resp = session.get(url, timeout=25, allow_redirects=True)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log.warning("HTTP error on attempt %d: %s", attempt, exc)
            continue

        soup = BeautifulSoup(resp.text, "lxml")

        # Title
        title_tag = (
            soup.find("span", {"id": "productTitle"})
            or soup.find("h1", {"id": "title"})
        )
        title = title_tag.get_text(strip=True) if title_tag else "Unknown Product"

        raw_price = _extract_price_from_soup(soup)

        if raw_price is None:
            log.warning("Could not find price selector on attempt %d.", attempt)
            # Log a snippet of the page to help debug
            snippet = soup.get_text()[:300].replace("\n", " ")
            log.debug("Page snippet: %s", snippet)
            continue

        # Clean and parse price
        numeric = re.sub(r"[^\d.]", "", raw_price.replace(",", ""))
        if not numeric:
            log.warning("Empty numeric string from raw_price='%s'", raw_price)
            continue

        try:
            price = float(numeric)
            log.info("✓ Price fetched via HTML scraping")
            return price, title
        except ValueError:
            log.warning("Could not convert '%s' to float", numeric)
            continue

    log.error("All %d scrape attempts failed.", CONFIG["MAX_SCRAPE_RETRIES"])
    return None, None


# ─────────────────── UNIFIED FETCH ───────────────────────────────────────────

def fetch_price(url: str) -> tuple[float | None, str | None]:
    """
    Try methods in order:
      1. Rainforest API  (if API key is set)
      2. HTML scraping   (with retries)
    """
    asin   = extract_asin(url)
    domain = detect_domain(url)
    log.info("ASIN detected: %s  |  Domain: %s", asin, domain)

    # Method 1: API (most reliable, no bot blocking)
    if asin and CONFIG["RAINFOREST_API_KEY"]:
        price, title = fetch_via_rainforest(asin, domain)
        if price is not None:
            return price, title
        log.info("Falling back to HTML scraping…")

    # Method 2: HTML scraping
    return fetch_via_scraping(url)


# ─────────────────── TWILIO ALERT ────────────────────────────────────────────

def send_alert(message: str) -> bool:
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


# ─────────────────── CORE LOGIC ──────────────────────────────────────────────

def check_price() -> None:
    global baseline_price, alert_active

    url = CONFIG["AMAZON_URL"]
    log.info("── Checking price… ──────────────────────────────────")

    current_price, title = fetch_price(url)

    if current_price is None:
        log.warning("Skipping this cycle — could not read price after all retries.")
        return

    log.info("Product : %s", (title or "")[:80])
    log.info("Price   : ₹%.2f", current_price)

    if baseline_price is None:
        # First successful read — set baseline
        globals()["baseline_price"] = current_price
        log.info("Baseline set to ₹%.2f", current_price)
        send_alert(
            f"🛒 Price Tracker Started!\n"
            f"Product: {(title or '')[:60]}\n"
            f"Baseline: ₹{current_price:,.2f}\n"
            f"Checking every {CONFIG['CHECK_INTERVAL_MINUTES']} min.\n"
            f"You'll be pinged when the price drops."
        )
        return

    drop     = baseline_price - current_price
    drop_pct = (drop / baseline_price) * 100

    if current_price < baseline_price:
        globals()["alert_active"] = True
        log.info("PRICE DROP  ₹%.2f → ₹%.2f  (↓ ₹%.2f / %.1f%%)",
                 baseline_price, current_price, drop, drop_pct)
        send_alert(
            f"🚨 Price Drop Alert!\n"
            f"Product: {(title or '')[:60]}\n"
            f"Was   : ₹{baseline_price:,.2f}\n"
            f"Now   : ₹{current_price:,.2f}\n"
            f"Save  : ₹{drop:,.2f}  ({drop_pct:.1f}% off)\n"
            f"Buy   : {url}\n"
            f"Time  : {datetime.now().strftime('%d %b %Y  %H:%M')}"
        )

    elif alert_active and current_price >= baseline_price:
        globals()["alert_active"] = False
        log.info("Price recovered. Alerts paused.")
        send_alert(
            f"✅ Price Back to Normal\n"
            f"Product: {(title or '')[:60]}\n"
            f"Current : ₹{current_price:,.2f}\n"
            f"Baseline: ₹{baseline_price:,.2f}\n"
            f"Alerts paused until next drop."
        )

    else:
        log.info("No drop. Baseline ₹%.2f | Current ₹%.2f", baseline_price, current_price)


# ─────────────────── ENTRY POINT ─────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("Amazon Price Tracker  v2  (anti-bot edition)")
    log.info("Interval : %d minutes", CONFIG["CHECK_INTERVAL_MINUTES"])
    log.info("URL      : %s", CONFIG["AMAZON_URL"])
    log.info("Alerting : %s → %s", CONFIG["FROM_NUMBER"], CONFIG["TO_NUMBER"])
    log.info("API mode : %s", "Rainforest API" if CONFIG["RAINFOREST_API_KEY"] else "HTML scraping")
    log.info("=" * 60)

    check_price()
    schedule.every(CONFIG["CHECK_INTERVAL_MINUTES"]).minutes.do(check_price)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
