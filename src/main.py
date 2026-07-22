"""
No-Website Business Leads — main entry point.

Strategy: delegate scraping to apify/google-maps-scraper (battle-tested,
fast, Chromium-based), then filter its output for no-website businesses
and calculate lead scores.  Our actor runs no browser at all — it is a
pure filter + scoring layer, which is cheap and fast.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from apify import Actor
from apify_client import ApifyClientAsync

from .utils import calculate_lead_score, classify_website

# The upstream scraper that does the heavy lifting.
# apify/google-maps-scraper is the official, maintained Apify actor.
UPSTREAM_ACTOR = "compass/crawler-google-places"


def _extract_city_country(location: str) -> tuple[str, str]:
    parts = [p.strip() for p in location.replace(",", " ").split() if p.strip()]
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]
    return location.strip(), ""


def _build_record(item: dict, website_status: str, city: str, country: str) -> dict:
    phone = item.get("phone") or item.get("phoneUnformatted") or None
    record = {
        "businessName": item.get("title") or item.get("name") or "",
        "phone": phone,
        "category": item.get("categoryName") or (item.get("categories") or [None])[0],
        "address": item.get("address") or item.get("street"),
        "city": item.get("city") or city,
        "country": item.get("countryCode") or country,
        "rating": item.get("totalScore") or item.get("rating"),
        "reviewCount": item.get("reviewsCount") or item.get("reviewCount"),
        "googleMapsUrl": item.get("url") or item.get("googleMapsUrl") or "",
        "hasPhone": bool(phone),
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

        city, country = _extract_city_country(location)

        # ── Call upstream scraper ──────────────────────────────────────────────
        # Pass website="EMPTY" to the upstream actor so it only returns businesses
        # with no website — we fetch exactly what we need, no over-fetching.
        fetch_count = max_results

        Actor.log.info(
            "Calling %s for '%s in %s' (fetching up to %d listings)…",
            UPSTREAM_ACTOR, search_query, location, fetch_count,
        )

        # APIFY_TOKEN is always set by the platform when an actor runs
        token = os.environ.get("APIFY_TOKEN") or os.environ.get("APIFY_API_TOKEN")
        if not token:
            raise RuntimeError("APIFY_TOKEN environment variable not set.")

        client = ApifyClientAsync(token)

        run = await client.actor(UPSTREAM_ACTOR).call(
            run_input={
                "searchStringsArray": [f"{search_query} in {location}"],
                "maxCrawledPlacesPerSearch": fetch_count,
                "language": "en",
                "website": "withoutWebsite",  # only businesses with NO website
                "scrapeContacts": False,
                "additionalInfo": False,
            },
            build="latest",
        )

        if not run:
            raise RuntimeError("Upstream actor run failed to start.")

        Actor.log.info(
            "Upstream run %s finished (status: %s). Fetching results…",
            run["id"], run["status"],
        )

        dataset_id = run.get("defaultDatasetId")
        if not dataset_id:
            raise RuntimeError("No dataset returned by upstream actor.")

        # ── Stream and filter dataset items ───────────────────────────────────
        results: list[dict] = []
        count_has_website = 0
        count_weak_skipped = 0
        total_seen = 0

        async for item in client.dataset(dataset_id).iterate_items():
            if len(results) >= max_results:
                break

            total_seen += 1
            raw_website = (
                item.get("website")
                or item.get("domain")
                or None
            )
            status = classify_website(raw_website)

            if status == "real":
                count_has_website += 1
                Actor.log.info("Skip (has website): %s", item.get("title", "?"))
                continue

            if status in ("social_only", "free_builder", "weak") and not include_weak:
                count_weak_skipped += 1
                continue

            name = item.get("title") or item.get("name") or ""
            if not name:
                continue

            record = _build_record(item, status, city, country)
            results.append(record)
            Actor.log.info(
                "[%d/%d] LEAD: %s | score=%d | phone=%s | website=%s",
                len(results), max_results,
                record["businessName"],
                record["leadScore"],
                record["phone"] or "none",
                status,
            )

        # ── Sort and push ──────────────────────────────────────────────────────
        results.sort(key=lambda r: r["leadScore"], reverse=True)

        Actor.log.info(
            "Done: %d leads returned | %d had real websites | %d weak-skipped | %d total examined",
            len(results), count_has_website, count_weak_skipped, total_seen,
        )

        await Actor.push_data(results)
