"""
Main entry point for the No-Website Business Leads actor.

Flow:
1. Read actor input.
2. Open Google Maps search page and scroll to collect listing URLs.
3. Visit listings with CONCURRENCY=2 (safe for container memory).
4. Restart browser automatically on crash.
5. Filter, score, sort, push to dataset.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

from apify import Actor
from camoufox.async_api import AsyncCamoufox

from .parser import extract_business_details, get_listing_urls
from .utils import (
    _parse_proxy,
    calculate_lead_score,
    classify_website,
)

CONCURRENCY = 2   # 2 concurrent pages is safe on Apify containers
MAX_RETRIES = 2
PROGRESS_LOG_EVERY = 5


def _clean_maps_url(url: str) -> str:
    """Strip tracking/auth query params from a Maps place URL.

    Removes ?authuser=, hl=, rclk= etc. that can trigger bot detection
    or return non-English UI that breaks our CSS selectors.
    """
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


class BrowserManager:
    """Holds a Camoufox browser and restarts it on crash."""

    def __init__(self, proxy_url: str | None):
        self._proxy_url = proxy_url
        self._cm = None
        self.browser = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        proxy = _parse_proxy(self._proxy_url)
        self._cm = AsyncCamoufox(
            headless=True,
            proxy=proxy,
            geoip=True,
            firefox_user_prefs={"security.sandbox.content.level": 0},
        )
        self.browser = await self._cm.__aenter__()

    async def stop(self) -> None:
        if self._cm:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._cm = None
            self.browser = None

    async def restart(self) -> None:
        async with self._lock:
            Actor.log.warning("Restarting browser after crash…")
            await self.stop()
            await self.start()


async def _fetch_one(mgr: BrowserManager, url: str, semaphore: asyncio.Semaphore) -> dict:
    """Fetch one listing under the semaphore; restarts browser on crash."""
    clean_url = _clean_maps_url(url)
    async with semaphore:
        for attempt in range(1, MAX_RETRIES + 1):
            page = None
            try:
                page = await mgr.browser.new_page()
                details = await extract_business_details(page, clean_url)
                return details
            except Exception as exc:
                msg = str(exc).lower()
                Actor.log.warning("Attempt %d/%d failed %s: %s", attempt, MAX_RETRIES, clean_url, exc)
                if any(kw in msg for kw in ("crashed", "closed", "disconnected", "browser")):
                    await mgr.restart()
                await asyncio.sleep(1)
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
        return {}


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

        # ── Phase 1: scroll search results for listing URLs ────────────────────
        mgr = BrowserManager(proxy_url)
        await mgr.start()

        try:
            scroll_page = await mgr.browser.new_page()
            try:
                await scroll_page.goto(search_url, wait_until="load", timeout=60_000)
                await scroll_page.wait_for_timeout(2_000)
                html = await scroll_page.content()
                Actor.log.info("Search page loaded (%d bytes).", len(html))

                Actor.log.info("Collecting listing URLs…")
                listing_urls = await get_listing_urls(
                    scroll_page,
                    max_scroll_attempts=40,
                    target=max_results * 3,
                )
            finally:
                await scroll_page.close()

            Actor.log.info("Collected %d listing URLs.", len(listing_urls))
            if not listing_urls:
                Actor.log.warning("No listings found. Check searchQuery and location.")
                return

            # ── Phase 2: concurrent listing visits ─────────────────────────────
            Actor.log.info("Visiting listings (concurrency=%d)…", CONCURRENCY)
            semaphore = asyncio.Semaphore(CONCURRENCY)
            tasks = [_fetch_one(mgr, url, semaphore) for url in listing_urls]
            all_details = await asyncio.gather(*tasks)

            # ── Phase 3: filter and score ──────────────────────────────────────
            results: list[dict] = []
            count_failed = 0
            count_has_website = 0
            count_weak_skipped = 0

            for details in all_details:
                if len(results) >= max_results:
                    break

                if not details or not details.get("businessName"):
                    count_failed += 1
                    continue

                raw_website = details.pop("websiteUrl", None)
                website_status = classify_website(raw_website)

                if website_status == "real":
                    count_has_website += 1
                    Actor.log.debug("Skipped (real website): %s", details["businessName"])
                    continue

                if website_status in ("social_only", "free_builder", "weak") and not include_weak:
                    count_weak_skipped += 1
                    continue

                results.append(_build_record(details, website_status, city, country))

                if len(results) % PROGRESS_LOG_EVERY == 0:
                    Actor.log.info("Found %d/%d leads so far.", len(results), max_results)

        finally:
            await mgr.stop()

        results.sort(key=lambda r: r["leadScore"], reverse=True)

        Actor.log.info(
            "Done: %d leads returned | %d had real websites | %d weak-skipped | %d extraction failed.",
            len(results), count_has_website, count_weak_skipped, count_failed,
        )

        await Actor.push_data(results)
