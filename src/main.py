"""
Main entry point for the No-Website Business Leads actor.

Flow:
1. Read actor input.
2. Open Google Maps search page and scroll to collect listing URLs.
3. Visit listings CONCURRENTLY (up to CONCURRENCY pages at once).
4. Filter out businesses with real websites.
5. Calculate lead score, sort, push to dataset.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from datetime import datetime, timezone

from apify import Actor
from camoufox.async_api import AsyncCamoufox

from .parser import extract_business_details, get_listing_urls
from .utils import (
    _parse_proxy,
    calculate_lead_score,
    classify_website,
)

CONCURRENCY = 5          # parallel listing pages open at once
MAX_RETRIES = 2
PROGRESS_LOG_EVERY = 10


async def _open_browser(proxy_url: str | None):
    """Start Camoufox browser and return (context_manager, browser)."""
    proxy = _parse_proxy(proxy_url)
    browser_cm = AsyncCamoufox(
        headless=True,
        proxy=proxy,
        geoip=True,
        firefox_user_prefs={"security.sandbox.content.level": 0},
    )
    browser = await browser_cm.__aenter__()
    return browser_cm, browser


async def _scroll_search_page(browser, search_url: str, target: int) -> list[str]:
    """Open search page on a dedicated page, scroll, return listing URLs."""
    page = await browser.new_page()
    try:
        await page.goto(search_url, wait_until="load", timeout=60_000)
        await page.wait_for_timeout(2_000)
        html = await page.content()
        Actor.log.info("Search page loaded (%d bytes).", len(html))
        urls = await get_listing_urls(page, max_scroll_attempts=40, target=target)
    finally:
        await page.close()
    return urls


async def _fetch_listing(browser, url: str, semaphore: asyncio.Semaphore) -> dict:
    """Fetch one listing under the concurrency semaphore, with retries."""
    async with semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                details = await extract_business_details(browser, url)
                return details
            except Exception as exc:
                Actor.log.warning("Listing attempt %d/%d failed %s: %s", attempt, MAX_RETRIES, url, exc)
                await asyncio.sleep(1)
        return {}


def _extract_city_country(location: str) -> tuple[str, str]:
    parts = [p.strip() for p in location.replace(",", " ").split() if p.strip()]
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]
    return location.strip(), ""


def _build_record(details: dict, website_status: str, city: str, country: str) -> dict:
    record = {
        "businessName": details["businessName"],
        "phone": details.get("phone"),
        "category": details.get("category"),
        "address": details.get("address"),
        "city": city,
        "country": country,
        "rating": details.get("rating"),
        "reviewCount": details.get("reviewCount"),
        "googleMapsUrl": details.get("googleMapsUrl"),
        "hasPhone": details.get("hasPhone", False),
        "websiteStatus": website_status,
        "leadScore": 0,
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
    }
    record["leadScore"] = calculate_lead_score(record)
    return record


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}

        search_query: str | None = actor_input.get("searchQuery")
        location: str | None = actor_input.get("location")
        max_results: int = int(actor_input.get("maxResults") or 100)
        include_weak: bool = bool(actor_input.get("includeWeakWebsite", False))

        if not search_query or not location:
            raise ValueError("Both 'searchQuery' and 'location' are required inputs.")

        # ── Proxy ─────────────────────────────────────────────────────────────
        proxy_cfg = await Actor.create_proxy_configuration(
            actor_proxy_input=actor_input.get("proxyConfiguration"),
        )
        proxy_url: str | None = await proxy_cfg.new_url() if proxy_cfg else None
        if proxy_url:
            Actor.log.info("Using proxy: %s", proxy_url.split("@")[-1])
        else:
            Actor.log.info("No proxy configured.")

        query_str = urllib.parse.quote_plus(f"{search_query} in {location}")
        search_url = f"https://www.google.com/maps/search/{query_str}"
        Actor.log.info("Search URL: %s", search_url)

        city, country = _extract_city_country(location)

        # ── Open browser ───────────────────────────────────────────────────────
        browser_cm, browser = await _open_browser(proxy_url)

        try:
            # ── Phase 1: collect listing URLs (one scrolling page) ─────────────
            Actor.log.info("Collecting listing URLs…")
            listing_urls = await _scroll_search_page(
                browser, search_url, target=max_results * 3
            )
            Actor.log.info("Collected %d listing URLs.", len(listing_urls))

            if not listing_urls:
                Actor.log.warning("No listings found. Check searchQuery and location.")
                return

            # ── Phase 2: visit listings concurrently ───────────────────────────
            Actor.log.info(
                "Visiting listings with concurrency=%d…", CONCURRENCY
            )
            semaphore = asyncio.Semaphore(CONCURRENCY)
            tasks = [
                _fetch_listing(browser, url, semaphore)
                for url in listing_urls
            ]
            all_details = await asyncio.gather(*tasks)

            # ── Phase 3: filter and score ──────────────────────────────────────
            results: list[dict] = []
            total_filtered = 0

            for details in all_details:
                if len(results) >= max_results:
                    break
                if not details or not details.get("businessName"):
                    continue

                raw_website = details.pop("websiteUrl", None)
                website_status = classify_website(raw_website)

                if website_status == "real":
                    total_filtered += 1
                    continue

                if website_status in ("social_only", "free_builder", "weak") and not include_weak:
                    total_filtered += 1
                    continue

                results.append(_build_record(details, website_status, city, country))

                if len(results) % PROGRESS_LOG_EVERY == 0:
                    Actor.log.info(
                        "Filtered: %d kept / %d skipped so far.", len(results), total_filtered
                    )

        finally:
            try:
                await browser_cm.__aexit__(None, None, None)
            except Exception:
                pass

        results.sort(key=lambda r: r["leadScore"], reverse=True)

        Actor.log.info(
            "Done. %d leads returned (%d filtered out as having real/weak websites).",
            len(results), total_filtered,
        )

        await Actor.push_data(results)
