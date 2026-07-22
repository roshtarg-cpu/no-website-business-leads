"""
Google Maps HTML / DOM parser.

Extracts business listing cards from a search-results page and detailed
business information from an individual listing page.
"""

from __future__ import annotations

import re
from typing import Any

from apify import Actor
from playwright.async_api import Page

from .utils import clean_phone


# ── Selectors ────────────────────────────────────────────────────────────────
# Google Maps frequently changes its HTML structure.  We target multiple
# candidate selectors so the actor degrades gracefully if one disappears.

# Container for each result card on the search-results panel
RESULT_CARD_SELECTORS = [
    'div[role="feed"] > div[jsaction]',
    'div.Nv2PK',
    'a[href*="/maps/place/"]',
]

# Individual detail panel selectors (opened after clicking a card)
DETAIL_SELECTORS = {
    "name": [
        'h1.DUwDvf',
        'h1[class*="fontHeadlineLarge"]',
        'div[class*="tAiQdd"] h1',
        'div.lMbq3e h1',
    ],
    "category": [
        'button[jsaction*="category"]',
        'div.skqShb button',
        'span.DkEaL',
        'button.DkEaL',
    ],
    "address": [
        'button[data-item-id="address"]',
        'div[data-item-id="address"] div.rogA2c',
        'button[aria-label*="Address"]',
    ],
    "phone": [
        'button[data-item-id^="phone"]',
        'div[data-item-id^="phone"] div.rogA2c',
        'button[aria-label*="Phone"]',
        'a[href^="tel:"]',
    ],
    "website": [
        'a[data-item-id="authority"]',
        'div[data-item-id="authority"] a',
        'a[aria-label*="website" i]',
        'a[href*="http"][class*="CsEnBe"]',
    ],
    "rating": [
        'div.F7nice span[aria-hidden="true"]',
        'span.ceNzKf',
        'div.fontDisplayLarge',
    ],
    "review_count": [
        'div.F7nice span[aria-label*="review"]',
        'span[aria-label*="review"]',
    ],
}


async def _try_text(page: Page, selectors: list[str]) -> str | None:
    """Return inner text of the first matching selector, or None."""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                text = await el.inner_text()
                if text and text.strip():
                    return text.strip()
        except Exception:
            continue
    return None


async def _try_attr(page: Page, selectors: list[str], attr: str) -> str | None:
    """Return an attribute value of the first matching selector, or None."""
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                val = await el.get_attribute(attr)
                if val and val.strip():
                    return val.strip()
        except Exception:
            continue
    return None


async def get_listing_urls(page: Page, max_scroll_attempts: int = 30, target: int = 0) -> list[str]:
    """
    Scroll the left-hand search-results panel to load all cards and
    return a de-duplicated list of /maps/place/ URLs.
    Stops early once `target` URLs are collected (0 = no limit).
    """
    urls: list[str] = []
    seen: set[str] = set()
    no_new_count = 0

    for attempt in range(max_scroll_attempts):
        prev_len = len(urls)

        # Collect all place links currently visible
        anchors = await page.query_selector_all('a[href*="/maps/place/"]')
        for anchor in anchors:
            href = await anchor.get_attribute("href")
            if href and "/maps/place/" in href and href not in seen:
                seen.add(href)
                urls.append(href)

        # Early-exit: hit target count
        if target and len(urls) >= target:
            Actor.log.info("Collected target of %d listing URLs after %d scrolls.", target, attempt + 1)
            break

        # Early-exit: no new URLs found for 3 consecutive scrolls
        if len(urls) == prev_len:
            no_new_count += 1
            if no_new_count >= 3:
                Actor.log.info("No new listings found for 3 scrolls — stopping at %d URLs.", len(urls))
                break
        else:
            no_new_count = 0

        # Scroll the results panel (not the map)
        try:
            panel = await page.query_selector('div[role="feed"]')
            if panel:
                await panel.evaluate("el => el.scrollBy(0, 2000)")
            else:
                await page.evaluate("window.scrollBy(0, 2000)")
        except Exception:
            await page.evaluate("window.scrollBy(0, 2000)")

        # 700 ms is enough for lazy-loaded cards to appear
        await page.wait_for_timeout(700)

        # Detect end-of-results sentinel (multiple known class names)
        for sentinel in ('span.HlvSq', 'span.wmrBd', 'div.PbZDve p.fontBodyMedium'):
            end_marker = await page.query_selector(sentinel)
            if end_marker:
                Actor.log.info("Reached end of search results after %d scrolls.", attempt + 1)
                return urls

    Actor.log.info("Found %d listing URLs in search results.", len(urls))
    return urls


async def extract_business_details(page: Page, maps_url: str) -> dict[str, Any]:
    """
    Navigate an already-open page to a Maps listing URL and extract all fields.
    Caller is responsible for creating and closing the page.
    """
    try:
        # domcontentloaded fires as soon as the DOM is parsed — much faster than
        # "load" (waits for images/fonts) or "networkidle" (never fires on Maps).
        await page.goto(maps_url, wait_until="domcontentloaded", timeout=25_000)
        # Wait for the business name heading to appear (JS-rendered)
        try:
            await page.wait_for_selector("h1", timeout=6_000)
        except Exception:
            pass  # proceed anyway; extraction will return {} if name is missing
    except Exception as exc:
        Actor.log.warning("Could not load listing %s: %s", maps_url, exc)
        return {}

    # ── Name ────────────────────────────────────────────────────────────────
    name = await _try_text(page, DETAIL_SELECTORS["name"])
    if not name:
        Actor.log.debug("No name found for %s — skipping.", maps_url)
        return {}

    # ── Category ────────────────────────────────────────────────────────────
    category = await _try_text(page, DETAIL_SELECTORS["category"])

    # ── Address ─────────────────────────────────────────────────────────────
    address_raw = await _try_text(page, DETAIL_SELECTORS["address"])
    # Also try aria-label attribute (often contains the full address)
    if not address_raw:
        address_raw = await _try_attr(page, DETAIL_SELECTORS["address"], "aria-label")
    address = address_raw.replace("Address: ", "").strip() if address_raw else None

    # ── Phone ────────────────────────────────────────────────────────────────
    phone_raw = await _try_text(page, DETAIL_SELECTORS["phone"])
    if not phone_raw:
        # Try aria-label which sometimes contains the number
        phone_raw = await _try_attr(page, DETAIL_SELECTORS["phone"], "aria-label")
        if phone_raw:
            # Strip prefix like "Phone: "
            phone_raw = re.sub(r"^Phone:\s*", "", phone_raw, flags=re.IGNORECASE)
    # Try tel: href
    if not phone_raw:
        tel_href = await _try_attr(page, ['a[href^="tel:"]'], "href")
        if tel_href:
            phone_raw = tel_href.replace("tel:", "")
    phone = clean_phone(phone_raw)

    # ── Website ──────────────────────────────────────────────────────────────
    website_url = await _try_attr(page, DETAIL_SELECTORS["website"], "href")
    if not website_url:
        website_url = await _try_text(page, DETAIL_SELECTORS["website"])

    # ── Rating ───────────────────────────────────────────────────────────────
    rating: float | None = None
    rating_text = await _try_text(page, DETAIL_SELECTORS["rating"])
    if rating_text:
        m = re.search(r"(\d+[.,]\d+)", rating_text)
        if m:
            try:
                rating = float(m.group(1).replace(",", "."))
            except ValueError:
                pass

    # ── Review count ─────────────────────────────────────────────────────────
    review_count: int | None = None
    review_text = await _try_text(page, DETAIL_SELECTORS["review_count"])
    if not review_text:
        review_text = await _try_attr(
            page, DETAIL_SELECTORS["review_count"], "aria-label"
        )
    if review_text:
        m = re.search(r"([\d,]+)\s+review", review_text, re.IGNORECASE)
        if m:
            try:
                review_count = int(m.group(1).replace(",", ""))
            except ValueError:
                pass

    return {
        "businessName": name,
        "phone": phone,
        "category": category,
        "address": address,
        "websiteUrl": website_url,
        "rating": rating,
        "reviewCount": review_count,
        "googleMapsUrl": maps_url,
        "hasPhone": phone is not None,
    }
