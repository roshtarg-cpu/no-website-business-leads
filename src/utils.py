"""Utility helpers: website classification and lead scoring."""

from __future__ import annotations

import re
from urllib.parse import urlparse


SOCIAL_DOMAINS: set[str] = {
    "facebook.com", "fb.com", "instagram.com", "twitter.com",
    "x.com", "tiktok.com", "youtube.com", "linkedin.com",
}

FREE_BUILDER_DOMAINS: set[str] = {
    "wix.com", "wixsite.com", "weebly.com", "squarespace.com",
    "wordpress.com", "blogspot.com", "blogger.com", "jimdo.com",
    "site123.me", "yolasite.com", "godaddysites.com", "mysite.com", "webnode.com",
}

LINK_IN_BIO_DOMAINS: set[str] = {
    "linktree.com", "linktr.ee", "bio.link", "beacons.ai",
    "bento.me", "lnk.bio", "tap.bio", "campsite.bio",
    "milkshake.app", "koji.com", "link.gallery",
}


def classify_website(url: str | None) -> str:
    """Return 'none', 'social_only', 'free_builder', 'weak', or 'real'."""
    if not url or not url.strip():
        return "none"

    try:
        hostname = urlparse(url.lower().strip()).hostname or url.lower()
    except Exception:
        hostname = url.lower()

    if hostname.startswith("www."):
        hostname = hostname[4:]

    def _matches(domain_set: set[str]) -> bool:
        return any(hostname == d or hostname.endswith("." + d) for d in domain_set)

    if _matches(SOCIAL_DOMAINS):
        return "social_only"
    if _matches(FREE_BUILDER_DOMAINS):
        return "free_builder"
    if _matches(LINK_IN_BIO_DOMAINS):
        return "weak"
    return "real"


def calculate_lead_score(record: dict) -> int:
    """Score 1-100: +40 phone, +30 active (rating+reviews>10), +20 established (>50 reviews), +10 address."""
    score = 0
    if record.get("hasPhone"):
        score += 40
    review_count = record.get("reviewCount") or 0
    if record.get("rating") is not None and review_count > 10:
        score += 30
    if review_count > 50:
        score += 20
    if record.get("address") and str(record["address"]).strip():
        score += 10
    return min(score, 100)


def clean_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^\d\+\-\(\)\s]", "", raw.strip()).strip()
    return cleaned if cleaned else None
