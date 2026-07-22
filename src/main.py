"""
Main entry point for the No-Website Business Leads actor.

Flow:
1. Read actor input (searchQuery, location, maxResults, includeWeakWebsite, proxy).
2. Open Google Maps search page for "[searchQuery] in [location]".
3. Scroll search results panel to collect listing URLs.
4. Visit each listing, extract business details.
5. Filter out businesses that have a real website.
6. Calculate lead score and accumulate until maxResults reached.
7. Sort by lead score descending and push to Apify dataset.
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

MAX_RETRIES = 3
BETWEEN_REQUESTS_DELAY_MS = 1_500   # 1.5 s between listing visits
PROGRESS_LOG_EVERY = 10              # log progress every N results


async def _open_search_page(proxy_url: str | None, search_url: str):
    """
    Open Camoufox browser, navigate to Google Maps search URL and return
    (browser_cm, browser, page) so the caller can reuse the same session.
    """
    proxy = _parse_proxy(proxy_url)
    browser_cm = AsyncCamoufox(
        headless=True,
        proxy=proxy,
        geoip=True,
        firefox_user_prefs={"security.sandbox.content.level": 0},
    )
    browser = await browser_cm.__aenter__()
    page = await browser.new_page()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            await page.goto(search_url, wait_until="networkidle", timeout=90_000)
            await page.wait_for_timeout(3_000)
            html = await page.content()
            if len(html) > 500:
                Actor.log.info("Search page loaded (%d bytes).", len(html))
                return browser_cm, browser, page
            Actor.log.warning(
                "Short response on attempt %d/%d (%d bytes).",
                attempt, MAX_RETRIES, len(html),
            )
        except Exception as exc:
            Actor.log.warning("Attempt %d/%d failed: %s", attempt, MAX_RETRIES, exc)
        await asyncio.sleep(2)

    raise RuntimeError(f"Could not load Google Maps search after {MAX_RETRIES} attempts.")


def _extract_city_country(location: str) -> tuple[str, str]:
    """Best-effort split of 'City, Country' or 'City Country'."""
    parts = [p.strip() for p in location.replace(",", " ").split() if p.strip()]
    if len(parts) >= 2:
        # Last word → country hint, everything else → city
        return " ".join(parts[:-1]), parts[-1]
    return location.strip(), ""


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
            Actor.log.info("Using proxy: %s", proxy_url.split("@")[-1])  # hide credentials
        else:
            Actor.log.info("No proxy configured — running without proxy.")

        # ── Build search URL ──────────────────────────────────────────────────
        query_str = urllib.parse.quote_plus(f"{search_query} in {location}")
        search_url = f"https://www.google.com/maps/search/{query_str}"
        Actor.log.info("Search URL: %s", search_url)

        city, country = _extract_city_country(location)

        # ── Open search page ──────────────────────────────────────────────────
        browser_cm, browser, page = await _open_search_page(proxy_url, search_url)

        results: list[dict] = []
        total_examined = 0
        total_filtered_out = 0

        try:
            # ── Collect all listing URLs ──────────────────────────────────────
            Actor.log.info("Scrolling search results to collect listing URLs…")
            listing_urls = await get_listing_urls(page, max_scroll_attempts=50)
            Actor.log.info("Collected %d listing URLs to examine.", len(listing_urls))

            if not listing_urls:
                Actor.log.warning(
                    "No listings found. Check that searchQuery and location are valid."
                )
                return

            # ── Process each listing ──────────────────────────────────────────
            for url in listing_urls:
                if len(results) >= max_results:
                    break

                total_examined += 1
                Actor.log.debug("Examining listing %d: %s", total_examined, url)

                # Retry loop for individual listings
                details: dict = {}
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        details = await extract_business_details(page, url)
                        break
                    except Exception as exc:
                        Actor.log.warning(
                            "Listing %s attempt %d/%d failed: %s",
                            url, attempt, MAX_RETRIES, exc,
                        )
                        await asyncio.sleep(2)

                if not details or not details.get("businessName"):
                    total_filtered_out += 1
                    continue

                # ── Website classification ────────────────────────────────────
                raw_website = details.pop("websiteUrl", None)
                website_status = classify_website(raw_website)

                # Determine eligibility
                if website_status == "real":
                    total_filtered_out += 1
                    Actor.log.debug(
                        "Skipped (real website): %s — %s",
                        details["businessName"], raw_website,
                    )
                    continue

                if website_status == "none":
                    pass  # Always included
                elif website_status in ("social_only", "free_builder", "weak"):
                    if not include_weak:
                        total_filtered_out += 1
                        Actor.log.debug(
                            "Skipped (weak presence, includeWeakWebsite=false): %s",
                            details["businessName"],
                        )
                        continue

                # ── Build output record ───────────────────────────────────────
                record = {
                    "businessName": details["businessName"],
                    "phone": details.get("phone"),
                    "category": details.get("category"),
                    "address": details.get("address"),
                    "city": city,
                    "country": country,
                    "rating": details.get("rating"),
                    "reviewCount": details.get("reviewCount"),
                    "googleMapsUrl": url,
                    "hasPhone": details.get("hasPhone", False),
                    "websiteStatus": website_status,
                    "leadScore": 0,
                    "scrapedAt": datetime.now(timezone.utc).isoformat(),
                }
                record["leadScore"] = calculate_lead_score(record)
                results.append(record)

                # Log progress
                if len(results) % PROGRESS_LOG_EVERY == 0:
                    Actor.log.info(
                        "Progress: %d/%d no-website businesses found "
                        "(%d examined, %d filtered out).",
                        len(results), max_results,
                        total_examined, total_filtered_out,
                    )

                # Polite delay between requests
                await asyncio.sleep(BETWEEN_REQUESTS_DELAY_MS / 1_000)

        finally:
            try:
                await browser_cm.__aexit__(None, None, None)
            except Exception:
                pass

        # ── Sort and push ─────────────────────────────────────────────────────
        results.sort(key=lambda r: r["leadScore"], reverse=True)

        Actor.log.info(
            "Done. %d no-website businesses returned out of %d examined "
            "(%d filtered out as having real websites or weak presence).",
            len(results), total_examined, total_filtered_out,
        )

        await Actor.push_data(results)
