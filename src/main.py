"""
Main entry point for the No-Website Business Leads actor.

Architecture: single-page, click-based extraction.

Instead of opening a new browser page for every listing (page.goto = 3-5s,
causes OOM with concurrency), we stay on the search-results page and
CLICK each card.  Google Maps is a SPA: clicking a card updates the
right detail panel in ~500 ms without a full page reload.

One browser.  One page.  No concurrency issues.  6-10x faster.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

from apify import Actor
from camoufox.async_api import AsyncCamoufox

from .parser import (
    click_and_extract,
    get_card_hrefs,
    scroll_feed,
    wait_for_feed,
)
from .utils import (
    _parse_proxy,
    calculate_lead_score,
    classify_website,
)

MAX_STALE_SCROLLS = 4   # stop scrolling after this many scrolls with no new cards
MAX_SCROLLS = 60        # absolute cap on scroll attempts


def _clean_maps_url(url: str) -> str:
    """Strip tracking/lang query params that can break selectors or trigger bans."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))


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

        # ── Open browser + single page ─────────────────────────────────────────
        proxy = _parse_proxy(proxy_url)
        async with AsyncCamoufox(
            headless=True,
            proxy=proxy,
            geoip=True,
            firefox_user_prefs={"security.sandbox.content.level": 0},
        ) as browser:
            page = await browser.new_page()

            # Load search results
            Actor.log.info("Loading Google Maps search…")
            await page.goto(search_url, wait_until="domcontentloaded", timeout=60_000)
            await wait_for_feed(page)

            results: list[dict] = []
            processed: set[str] = set()   # clean URLs already visited
            count_has_website = 0
            count_weak_skipped = 0
            count_failed = 0
            stale_scrolls = 0
            scroll_count = 0

            Actor.log.info("Starting card-click extraction…")

            while len(results) < max_results and stale_scrolls < MAX_STALE_SCROLLS and scroll_count <= MAX_SCROLLS:

                # Get all currently visible cards in the left sidebar
                cards = await get_card_hrefs(page)
                new_this_pass = 0

                for raw_href, card_el in cards.items():
                    if len(results) >= max_results:
                        break

                    clean_url = _clean_maps_url(raw_href)
                    if clean_url in processed:
                        continue
                    processed.add(clean_url)
                    new_this_pass += 1

                    # Click card → SPA navigation → right panel updates (~500ms)
                    details = await click_and_extract(page, card_el, clean_url)

                    if not details or not details.get("businessName"):
                        count_failed += 1
                        Actor.log.debug("Extraction failed: %s", clean_url)
                        continue

                    raw_website = details.pop("websiteUrl", None)
                    status = classify_website(raw_website)

                    if status == "real":
                        count_has_website += 1
                        Actor.log.info(
                            "Skip (has website): %s", details["businessName"]
                        )
                        continue

                    if status in ("social_only", "free_builder", "weak") and not include_weak:
                        count_weak_skipped += 1
                        Actor.log.info(
                            "Skip (weak presence): %s", details["businessName"]
                        )
                        continue

                    record = _build_record(details, status, city, country)
                    results.append(record)
                    Actor.log.info(
                        "[%d/%d] LEAD: %s | score=%d | phone=%s | website=%s",
                        len(results), max_results,
                        record["businessName"],
                        record["leadScore"],
                        record["phone"] or "none",
                        status,
                    )

                # Track whether scrolling is yielding new cards
                if new_this_pass == 0:
                    stale_scrolls += 1
                    Actor.log.debug("No new cards this pass (%d/%d stale).", stale_scrolls, MAX_STALE_SCROLLS)
                else:
                    stale_scrolls = 0

                if len(results) >= max_results:
                    break

                # Scroll the feed panel to load more cards
                await scroll_feed(page)
                scroll_count += 1
                Actor.log.debug(
                    "Scroll %d | processed=%d | leads=%d | with-website=%d",
                    scroll_count, len(processed), len(results), count_has_website,
                )

        # ── Sort and push ──────────────────────────────────────────────────────
        results.sort(key=lambda r: r["leadScore"], reverse=True)

        Actor.log.info(
            "Done: %d leads | %d had real websites | %d weak-skipped | %d failed extraction",
            len(results), count_has_website, count_weak_skipped, count_failed,
        )

        await Actor.push_data(results)
