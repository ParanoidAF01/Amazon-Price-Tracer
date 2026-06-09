"""
Price Checker Worker  v5
------------------------
Fixes:
  - ScraperAPI structured endpoint for reliable amazon.in price + image fetch
  - Smart alerting: only ping when price CHANGES (drop or rise), not every cycle
  - Tracks last_alerted_price in Supabase to avoid duplicate alerts
  - Saves product image URL for dashboard display
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
SUPABASE_URL       = os.environ["SUPABASE_URL"]
SUPABASE_KEY       = os.environ["SUPABASE_KEY"]
TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN  = os.environ["TWILIO_AUTH_TOKEN"]
FROM_NUMBER        = os.environ["TWILIO_FROM"]
TO_NUMBER          = os.environ["ALERT_TO"]
SCRAPER_API_KEY    = os.environ.get("SCRAPER_API_KEY", "")
CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL_MINUTES", "60"))
MAX_RETRIES        = 3

supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("worker")


# ── ScraperAPI structured product endpoint ────────────────────────────────────
def fetch_via_structured_api(asin: str) -> dict | None:
    """
    Uses ScraperAPI's Amazon-specific structured data endpoint.
    Returns a dict with price, title, image or None on failure.
    Much more reliable than raw HTML scraping.
    Docs: https://docs.scraperapi.com/structured-data-collection/amazon
    """
    if not SCRAPER_API_KEY or not asin:
        return None

    url = f"https://api.scraperapi.com/structured/amazon/product"
    params = {
        "api_key":       SCRAPER_API_KEY,
        "asin":          asin,
        "country":       "in",
        "output_format": "json",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        wait = random.uniform(2, 4) * attempt
        log.info("  Structured API attempt %d/%d (%.1fs)…", attempt, MAX_RETRIES, wait)
        time.sleep(wait)
        try:
            resp = requests.get(url, params=params, timeout=60)
            if resp.status_code == 404:
                log.warning("  Structured API: ASIN not found")
                return None
            if resp.status_code in (403, 429):
                log.warning("  Structured API: quota/auth issue (%d)", resp.status_code)
                return None
            if resp.status_code == 500:
                log.warning("  Structured API 500 — retrying…")
                continue
            resp.raise_for_status()
            data = resp.json()

            # Extract fields from ScraperAPI structured response
            price_raw = (
                data.get("pricing")
                or data.get("price")
                or data.get("buybox_price")
            )
            title = (
                data.get("name")
                or data.get("title")
                or data.get("product_title")
            )
            image = (
                data.get("main_image")
                or data.get("image")
                or (data.get("images") or [None])[0]
            )

            # price_raw might be a string like "₹1,299" or a number
            if price_raw is None:
                log.warning("  Structured API returned no price field. Keys: %s", list(data.keys()))
                continue

            price = clean_price(str(price_raw))
            if price and price > 1:
                log.info("  ✓ Structured API → ₹%.2f | %s", price, (title or "")[:50])
                return {"price": price, "title": title, "image": image}

            log.warning("  Structured API price unparseable: %s", price_raw)

        except Exception as exc:
            log.warning("  Structured API error (attempt %d): %s", attempt, exc)

    return None


# ── Raw HTML scrape fallback ──────────────────────────────────────────────────
def fetch_via_raw_html(url: str) -> dict | None:
    """Fallback: route full page through ScraperAPI and parse HTML."""
    if not SCRAPER_API_KEY:
        return None

    params = {
        "api_key":      SCRAPER_API_KEY,
        "url":          url,
        "country_code": "in",
        "device_type":  "desktop",
        "render":       "false",
    }

    for attempt in range(1, MAX_RETRIES + 1):
        wait = random.uniform(3, 6) * attempt
        log.info("  HTML fallback attempt %d/%d (%.1fs)…", attempt, MAX_RETRIES, wait)
        time.sleep(wait)
        try:
            resp = requests.get("https://api.scraperapi.com/", params=params, timeout=60)
            if resp.status_code in (403, 500):
                log.warning("  Raw HTML ScraperAPI %d", resp.status_code)
                continue
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            # CAPTCHA check
            if soup.find("form", {"action": "/errors/validateCaptcha"}) or \
               "Type the characters" in soup.get_text():
                log.warning("  CAPTCHA detected on HTML fallback")
                continue

            title_el = soup.find("span", {"id": "productTitle"})
            title = title_el.get_text(strip=True) if title_el else None

            # Image
            img_el = (
                soup.find("img", {"id": "landingImage"})
                or soup.find("img", {"id": "imgBlkFront"})
                or soup.find("img", {"class": "a-dynamic-image"})
            )
            image = img_el.get("src") if img_el else None

            # Price
            price = _parse_price_from_soup(soup)
            if price:
                log.info("  ✓ HTML fallback → ₹%.2f", price)
                return {"price": price, "title": title, "image": image}

        except Exception as exc:
            log.warning("  HTML fallback error (attempt %d): %s", attempt, exc)

    return None


def _parse_price_from_soup(soup: BeautifulSoup) -> float | None:
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
                return p
    return None

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

def clean_price(raw: str) -> float | None:
    numeric = re.sub(r"[^\d.]", "", raw.replace(",", ""))
    try:
        return float(numeric) if numeric else None
    except ValueError:
        return None

def extract_asin(url: str) -> str | None:
    for pat in [r"/dp/([A-Z0-9]{10})", r"/gp/product/([A-Z0-9]{10})"]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


# ── Unified fetch ─────────────────────────────────────────────────────────────
def fetch_product_data(url: str) -> dict | None:
    """Try structured API first, fall back to raw HTML."""
    asin = extract_asin(url)
    log.info("  ASIN: %s", asin)

    if asin:
        result = fetch_via_structured_api(asin)
        if result:
            return result
        log.info("  Structured API failed — trying HTML fallback…")

    return fetch_via_raw_html(url)


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
        pid                = product["id"]
        url                = product["url"]
        name               = product.get("name", "Unknown")
        baseline           = product.get("baseline_price")
        last_alerted_price = product.get("last_alerted_price")

        log.info("▶ %s", name[:60])

        data = fetch_product_data(url)
        if data is None:
            log.warning("  Could not read product data — skipping.")
            continue

        current_price = data["price"]
        fetched_name  = data.get("title")
        image_url     = data.get("image")

        log.info("  Price : ₹%.2f", current_price)

        # Build DB update payload
        update_fields: dict = {}

        # Auto-update name if it's a placeholder
        if fetched_name and (not name or name in ("Unknown Product", "New product")):
            update_fields["name"] = fetched_name[:120]
            name = fetched_name[:60]

        # Save image if we have one and it's not stored yet
        if image_url and not product.get("image_url"):
            update_fields["image_url"] = image_url

        if update_fields:
            supabase.table("products").update(update_fields).eq("id", pid).execute()

        # Save price history
        supabase.table("price_history").insert({
            "product_id": pid,
            "price":      current_price,
        }).execute()

        # ── First time: set baseline ──────────────────────────────────────────
        if baseline is None:
            supabase.table("products").update({
                "baseline_price":    current_price,
                "last_alerted_price": current_price,
            }).eq("id", pid).execute()

            send_alert(
                f"🛒 Tracker Started!\n"
                f"Product : {name[:60]}\n"
                f"Baseline: ₹{current_price:,.2f}\n"
                f"Checking every {CHECK_INTERVAL} min.\n"
                f"You'll be alerted when the price changes."
            )
            log.info("  Baseline set → ₹%.2f", current_price)
            continue

        # ── Smart alert logic ─────────────────────────────────────────────────
        # last_alerted_price = price we sent the LAST alert about
        # We only alert when current price differs from last alerted price
        # This means: same price as last check → silent, even if still below baseline

        prev = last_alerted_price if last_alerted_price is not None else baseline
        price_changed = abs(current_price - prev) >= 0.01   # ignore sub-paisa float noise

        if not price_changed:
            log.info("  Price unchanged since last alert (₹%.2f) — silent.", prev)
            continue

        # Price has changed — determine direction and alert
        drop_from_baseline = baseline - current_price
        drop_pct           = (drop_from_baseline / baseline * 100) if baseline else 0

        if current_price < baseline:
            # Still a deal or dropped further
            if current_price < prev:
                emoji   = "📉"
                headline = "Price Dropped Further!"
            else:
                emoji   = "📈"
                headline = "Price Ticked Up (still below baseline)"

            msg = (
                f"{emoji} {headline}\n"
                f"Product : {name[:60]}\n"
                f"Baseline: ₹{baseline:,.2f}\n"
                f"Before  : ₹{prev:,.2f}\n"
                f"Now     : ₹{current_price:,.2f}\n"
                f"vs Base : ₹{drop_from_baseline:,.2f} ({drop_pct:.1f}% off)\n"
                f"Buy     : {url}\n"
                f"Time    : {datetime.now().strftime('%d %b %Y %H:%M')}"
            )

        elif current_price >= baseline and prev < baseline:
            # Price recovered back to or above baseline
            msg = (
                f"✅ Price Back to Normal\n"
                f"Product : {name[:60]}\n"
                f"Baseline: ₹{baseline:,.2f}\n"
                f"Now     : ₹{current_price:,.2f}\n"
                f"Deal has ended — alerts paused until next drop."
            )

        elif current_price > baseline:
            # Price rose above baseline (price hike)
            rise     = current_price - baseline
            rise_pct = (rise / baseline * 100)
            msg = (
                f"⚠️ Price Hike!\n"
                f"Product : {name[:60]}\n"
                f"Baseline: ₹{baseline:,.2f}\n"
                f"Before  : ₹{prev:,.2f}\n"
                f"Now     : ₹{current_price:,.2f}\n"
                f"Up by   : ₹{rise:,.2f} ({rise_pct:.1f}% above baseline)\n"
                f"Time    : {datetime.now().strftime('%d %b %Y %H:%M')}"
            )

        else:
            log.info("  No alert condition met.")
            continue

        send_alert(msg)

        # Update last_alerted_price so we don't re-alert for same price next cycle
        supabase.table("products").update({
            "last_alerted_price": current_price
        }).eq("id", pid).execute()

        log.info("  Alert sent. last_alerted_price updated → ₹%.2f", current_price)

        # Small delay between products
        time.sleep(random.uniform(3, 7))


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    log.info("Amazon Price Tracker Worker  v5")
    log.info("Interval  : %d min", CHECK_INTERVAL)
    log.info("Alerting  : %s → %s", FROM_NUMBER, TO_NUMBER)
    log.info("ScraperAPI: %s", "configured ✓" if SCRAPER_API_KEY else "MISSING ✗")

    check_all_products()
    schedule.every(CHECK_INTERVAL).minutes.do(check_all_products)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()