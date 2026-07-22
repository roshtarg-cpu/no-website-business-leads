"""Utility helpers: proxy parsing, fetch via Camoufox, __NEXT_DATA__ extraction."""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from apify import Actor
from camoufox.async_api import AsyncCamoufox


# ── domains that count as "weak" or "social-only" web presence ──────────────
SOCIAL_DOMAINS: set[str] = {
    "facebook.com",
    "fb.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "youtube.com",
    "linkedin.com",
}

FREE_BUILDER_DOMAINS: set[str] = {
    "wix.com",
    "wixsite.com",
    "weebly.com",
    "squarespace.com",   # free subdomain only — paid plans have custom domains
    "wordpress.com",
    "blogspot.com",
    "blogger.com",
    "jimdo.com",
    "site123.me",
    "yolasite.com",
    "godaddysites.com",
    "mysite.com",
    "webnode.com",
}

LINK_IN_BIO_DOMAINS: set[str] = {
    "linktree.com",
    "linktr.ee",
    "bio.link",
    "beacons.ai",
    "bento.me",
    "lnk.bio",
    "tap.bio",
    "campsite.bio",
    "milkshake.app",
    "koji.com",
    "link.gallery",
}


def _parse_proxy(proxy_url: str | None) -> dict | None:
    """Convert a proxy URL string into a Playwright/Camoufox proxy dict."""
    if not proxy_url:
        return None
    p = urlparse(proxy_url)
    proxy: dict = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        proxy["username"] = p.username
    if p.password:
        proxy["password"] = p.password
    return proxy


async def fetch_html(url: str, proxy_url: str | None) -> str | None:
    """Fetch a URL with Camoufox (stealth Firefox) and return the HTML body."""
    proxy = _parse_proxy(proxy_url)
    try:
        async with AsyncCamoufox(
            headless=True,
            proxy=proxy,
            geoip=True,
            firefox_user_prefs={"security.sandbox.content.level": 0},
        ) as browser:
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=90_000)
            await page.wait_for_timeout(3_000)
            html = await page.content()
            if len(html) > 500:
                return html
            Actor.log.warning("Short response (%d bytes) for %s", len(html), url)
    except Exception as exc:
        Actor.log.warning("Fetch failed for %s: %s", url, exc)
    return None


async def fetch_html_with_page(url: str, proxy_url: str | None, timeout_ms: int = 90_000):
    """
    Return (browser, page, html) so the caller can keep interacting with the
    page after the initial load (e.g. scrolling, clicking).  The caller is
    responsible for closing the browser context.
    """
    proxy = _parse_proxy(proxy_url)
    browser_ctx = AsyncCamoufox(
        headless=True,
        proxy=proxy,
        geoip=True,
        firefox_user_prefs={"security.sandbox.content.level": 0},
    )
    browser = await browser_ctx.__aenter__()
    page = await browser.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        await page.wait_for_timeout(3_000)
    except Exception as exc:
        Actor.log.warning("Page load issue for %s: %s", url, exc)
    html = await page.content()
    return browser_ctx, browser, page, html


def extract_next_data(html: str) -> dict | None:
    """Pull __NEXT_DATA__ JSON from a Next.js page."""
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def classify_website(url: str | None) -> str:
    """
    Return one of: "none", "social_only", "free_builder", "weak", "real".

    "weak" covers link-in-bio tools that are not strictly social or a builder.
    """
    if not url or url.strip() == "":
        return "none"

    url_lower = url.lower().strip()

    # Strip protocol for domain matching
    try:
        hostname = urlparse(url_lower).hostname or url_lower
    except Exception:
        hostname = url_lower

    # Remove www. prefix
    if hostname.startswith("www."):
        hostname = hostname[4:]

    # Check domain membership
    def _matches(domain_set: set[str]) -> bool:
        for domain in domain_set:
            if hostname == domain or hostname.endswith("." + domain):
                return True
        return False

    if _matches(SOCIAL_DOMAINS):
        return "social_only"

    if _matches(FREE_BUILDER_DOMAINS):
        return "free_builder"

    if _matches(LINK_IN_BIO_DOMAINS):
        return "weak"

    return "real"


def calculate_lead_score(record: dict) -> int:
    """
    Score 1–100 based on data richness and business activity signals.

    +40  phone number present
    +30  rating exists AND reviewCount > 10
    +20  reviewCount > 50
    +10  address is complete (non-empty)
    """
    score = 0

    if record.get("hasPhone"):
        score += 40

    rating = record.get("rating")
    review_count = record.get("reviewCount") or 0

    if rating is not None and review_count > 10:
        score += 30

    if review_count > 50:
        score += 20

    if record.get("address") and record["address"].strip():
        score += 10

    return min(score, 100)


def clean_phone(raw: str | None) -> str | None:
    """Normalise a phone string; return None when nothing usable."""
    if not raw:
        return None
    cleaned = raw.strip()
    # Keep only digits, +, -, spaces, (, )
    cleaned = re.sub(r"[^\d\+\-\(\)\s]", "", cleaned)
    cleaned = cleaned.strip()
    return cleaned if cleaned else None
