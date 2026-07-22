"""
Google Maps parser — click-based extraction.

Instead of navigating to each listing URL (page.goto = 3-5s each),
we click cards in the left search panel.  Google Maps is a SPA:
clicking a card updates the right detail panel in ~500ms without a
full page reload.  This is 6-10x faster than the page.goto approach.
"""

from __future__ import annotations

import re
from typing import Any

from apify import Actor
from playwright.async_api import Page

from .utils import clean_phone

# ── Selectors ────────────────────────────────────────────────────────────────

# Result cards in the left sidebar
CARD_SELECTOR = 'a.hfpxzc'          # primary (modern Maps)
CARD_FALLBACK = 'a[href*="/maps/place/"]'

# Feed container (left sidebar)
FEED_SELECTORS = ['div[role="feed"]', 'div.m6QErb', 'div[jsaction*="pane"]']

# Right-panel detail fields
DETAIL = {
    "name": ['h1.DUwDvf', 'h1[class*="fontHeadlineLarge"]', 'div.lMbq3e h1', 'h1'],
    "category": ['button[jsaction*="category"]', 'span.DkEaL', 'button.DkEaL', 'div.skqShb button'],
    "address": ['button[data-item-id="address"]', 'div[data-item-id="address"] div.rogA2c', 'button[aria-label*="Address"]'],
    "phone": ['button[data-item-id^="phone"]', 'div[data-item-id^="phone"] div.rogA2c', 'button[aria-label*="Phone"]', 'a[href^="tel:"]'],
    "website": ['a[data-item-id="authority"]', 'div[data-item-id="authority"] a', 'a[aria-label*="website" i]', 'a[href*="http"][class*="CsEnBe"]'],
    "rating": ['div.F7nice span[aria-hidden="true"]', 'span.ceNzKf'],
    "review_count": ['div.F7nice span[aria-label*="review"]', 'span[aria-label*="review"]'],
}


async def _text(page: Page, selectors: list[str]) -> str | None:
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                t = await el.inner_text()
                if t and t.strip():
                    return t.strip()
        except Exception:
            continue
    return None


async def _attr(page: Page, selectors: list[str], attr: str) -> str | None:
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                v = await el.get_attribute(attr)
                if v and v.strip():
                    return v.strip()
        except Exception:
            continue
    return None


async def wait_for_feed(page: Page) -> bool:
    """Wait for the search results feed to be JS-rendered. Returns True if found."""
    for sel in FEED_SELECTORS:
        try:
            await page.wait_for_selector(sel, timeout=20_000)
            await page.wait_for_timeout(1_000)
            Actor.log.info("Results feed ready.")
            return True
        except Exception:
            continue
    Actor.log.warning("Feed not detected — proceeding after 4s fallback wait.")
    await page.wait_for_timeout(4_000)
    return False


async def get_card_hrefs(page: Page) -> dict[str, Any]:
    """
    Return {href: element} for all currently visible, unprocessed cards.
    Primary selector first, fallback second.
    """
    cards: dict[str, Any] = {}
    for sel in (CARD_SELECTOR, CARD_FALLBACK):
        els = await page.query_selector_all(sel)
        for el in els:
            href = await el.get_attribute("href") or ""
            if "/maps/place/" in href and href not in cards:
                cards[href] = el
        if cards:
            break
    return cards


async def scroll_feed(page: Page) -> None:
    """Scroll the left-panel feed to load more results."""
    try:
        panel = await page.query_selector('div[role="feed"]')
        if panel:
            await panel.evaluate("el => el.scrollBy(0, 2000)")
        else:
            await page.keyboard.press("End")
    except Exception:
        await page.evaluate("window.scrollBy(0, 2000)")
    await page.wait_for_timeout(1_200)


async def click_and_extract(page: Page, card_el, maps_url: str) -> dict[str, Any]:
    """
    Click a search result card and extract details from the right detail panel.
    Falls back to page.goto if the click causes a full navigation.
    Returns {} on failure.
    """
    try:
        await card_el.scroll_into_view_if_needed()
        await card_el.click()
        # Wait for the business name heading to appear in the detail panel
        await page.wait_for_selector("h1", timeout=6_000)
        await page.wait_for_timeout(400)
    except Exception as exc:
        Actor.log.debug("Card click failed for %s: %s", maps_url, exc)
        # Fall back to direct navigation
        try:
            await page.goto(maps_url, wait_until="domcontentloaded", timeout=25_000)
            await page.wait_for_selector("h1", timeout=5_000)
        except Exception:
            return {}

    return await _extract_panel(page, maps_url)


async def _extract_panel(page: Page, maps_url: str) -> dict[str, Any]:
    """Extract all detail fields from whichever panel is currently showing."""
    name = await _text(page, DETAIL["name"])
    if not name:
        return {}

    category = await _text(page, DETAIL["category"])

    address_raw = await _text(page, DETAIL["address"]) or await _attr(page, DETAIL["address"], "aria-label")
    address = re.sub(r"^Address:\s*", "", address_raw, flags=re.IGNORECASE).strip() if address_raw else None

    phone_raw = await _text(page, DETAIL["phone"])
    if not phone_raw:
        phone_raw = await _attr(page, DETAIL["phone"], "aria-label")
        if phone_raw:
            phone_raw = re.sub(r"^Phone:\s*", "", phone_raw, flags=re.IGNORECASE)
    if not phone_raw:
        tel = await _attr(page, ['a[href^="tel:"]'], "href")
        if tel:
            phone_raw = tel.replace("tel:", "")
    phone = clean_phone(phone_raw)

    website_url = await _attr(page, DETAIL["website"], "href") or await _text(page, DETAIL["website"])

    rating: float | None = None
    rt = await _text(page, DETAIL["rating"])
    if rt:
        m = re.search(r"(\d+[.,]\d+)", rt)
        if m:
            try:
                rating = float(m.group(1).replace(",", "."))
            except ValueError:
                pass

    review_count: int | None = None
    rct = await _text(page, DETAIL["review_count"]) or await _attr(page, DETAIL["review_count"], "aria-label")
    if rct:
        m = re.search(r"([\d,]+)\s+review", rct, re.IGNORECASE)
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
