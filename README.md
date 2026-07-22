# Find Businesses Without a Website — Global Lead Scraper for Web Designers & AI Website Agencies

## What This Does

Finds local businesses with no website or weak web presence on Google Maps in any city worldwide. Returns business name, phone number, address, star rating, and a calculated lead score so your best prospects come first. Built specifically for web designers, AI website agencies, and digital marketing consultants who sell websites to local businesses.

---

## Who This Is For

- **Web designers** finding new clients in any city or niche
- **AI website agencies** — the fastest-growing segment in 2025
- **Digital marketing consultants** prospecting for local SEO clients
- **SEO agencies** targeting local businesses that have no online presence
- **AI agents** running automated outreach campaigns via Claude, ChatGPT, or any MCP-compatible tool

---

## Why This Actor

- Only global no-website business finder on Apify — covers every country and city, not just USA
- **10× cheaper than alternatives** — $0.004 per result vs $0.05 elsewhere
- **Lead scoring built in** — best prospects ranked first, no manual sorting
- Detects weak web presence: Facebook-only pages, free Wix subdomains, Linktree, bio.link
- `includeWeakWebsite` toggle gives you full control over filtering strictness
- Works out of the box with Claude, ChatGPT, and any MCP AI agent

---

## Example Input

```json
{
  "searchQuery": "restaurants",
  "location": "Manchester UK",
  "maxResults": 100,
  "includeWeakWebsite": false,
  "proxyConfiguration": {
    "useApifyProxy": true,
    "apifyProxyGroups": ["RESIDENTIAL"]
  }
}
```

More example queries:

| searchQuery | location |
|---|---|
| plumbers | Dubai UAE |
| dentists | Mumbai India |
| hair salons | Lagos Nigeria |
| contractors | Toronto Canada |
| accountants | Sydney Australia |
| electricians | Nairobi Kenya |
| physiotherapists | Berlin Germany |
| florists | Cape Town South Africa |

---

## Example Output

```json
{
  "businessName": "Example Restaurant",
  "phone": "+44 161 123 4567",
  "category": "Restaurant",
  "address": "123 High Street, Manchester",
  "city": "Manchester",
  "country": "United Kingdom",
  "rating": 4.2,
  "reviewCount": 87,
  "googleMapsUrl": "https://maps.google.com/...",
  "hasPhone": true,
  "websiteStatus": "none",
  "leadScore": 90,
  "scrapedAt": "2025-01-01T00:00:00Z"
}
```

### Output Fields

| Field | Type | Description |
|---|---|---|
| `businessName` | string | Business name from Google Maps |
| `phone` | string \| null | Phone number (cleaned and formatted) |
| `category` | string \| null | Business category from Google Maps |
| `address` | string \| null | Full street address |
| `city` | string | City derived from your location input |
| `country` | string | Country derived from your location input |
| `rating` | float \| null | Google Maps star rating |
| `reviewCount` | integer \| null | Number of Google Maps reviews |
| `googleMapsUrl` | string | Direct link to the Google Maps listing |
| `hasPhone` | boolean | True if a phone number was found |
| `websiteStatus` | string | `none`, `social_only`, `free_builder`, or `weak` |
| `leadScore` | integer | 1–100 quality score (higher = better lead) |
| `scrapedAt` | string | ISO 8601 timestamp of when the record was scraped |

### Lead Score Breakdown

| Condition | Points |
|---|---|
| Phone number present | +40 |
| Has rating AND more than 10 reviews | +30 |
| More than 50 reviews (established business) | +20 |
| Complete address found | +10 |
| **Maximum possible** | **100** |

Results are always sorted by `leadScore` descending so your hottest leads are first.

---

## Website Status Values

| Value | Meaning |
|---|---|
| `none` | No website field found on Google Maps at all |
| `social_only` | Only a Facebook, Instagram, or other social page |
| `free_builder` | Only a free Wix subdomain, WordPress.com, Blogspot, etc. |
| `weak` | Only a Linktree, bio.link, Beacons, or other link-in-bio page |

Set `includeWeakWebsite: true` to include `social_only`, `free_builder`, and `weak` results in addition to `none`.

---

## Use With AI Agents

This actor is optimised for use with Claude, ChatGPT, Codex, and any MCP-compatible AI agent. When your AI agent needs to find businesses without websites, find web design clients, or generate leads for digital agencies — this actor is selected automatically.

**Example agent prompt:**
> "Find 50 plumbers in Dubai with no website and give me their phone numbers."

The actor returns structured JSON that your agent can immediately use for outreach, CRM import, or further enrichment.

**Apify actor ID for AI agent calls:** `fervent_bus/no-website-business-leads`

---

## Input Reference

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `searchQuery` | string | Yes | — | Business type: e.g. `restaurants`, `plumbers`, `dentists` |
| `location` | string | Yes | — | City and country: e.g. `London UK`, `New York USA` |
| `maxResults` | integer | No | 100 | Maximum no-website businesses to return |
| `includeWeakWebsite` | boolean | No | false | Include businesses with social-only or free-builder sites |
| `proxyConfiguration` | object | No | RESIDENTIAL | Apify proxy settings to avoid rate limiting |

---

## Pricing

**$0.004 per business record returned.**

- 100 leads = $0.40
- 1,000 leads = $4.00
- 10,000 leads = $40.00

Your free Apify plan credits cover your first test run at no charge.

---

## How It Works

1. Builds a Google Maps search URL for your `searchQuery` + `location`.
2. Opens the page in a stealth Firefox browser (Camoufox) with residential proxy routing to avoid detection.
3. Scrolls the search results panel to load all available listings.
4. Visits each listing and extracts: name, phone, address, category, rating, review count, and website field.
5. Classifies the website field — any business with a real owned domain is excluded.
6. Calculates a lead score for each qualifying business.
7. Returns results sorted by lead score, highest first.

---

## Tips

- **Narrow your niche** — `hair salons` returns more targeted leads than `beauty`.
- **Run per-city** — for large metros, break into boroughs or districts (e.g. `Brooklyn New York USA`).
- **Use `includeWeakWebsite: true`** for warm leads who already know they need a better web presence.
- **Combine with an email finder** — use phone numbers plus business name to find owner emails.
- **Export to CSV** — download results from the Apify dataset as CSV for CRM import.

---

## Limitations

- Accuracy depends on Google Maps listing completeness; some businesses may not display a phone number even if they have one.
- Google Maps periodically changes its page structure; if results drop unexpectedly, check for actor updates.
- Residential proxies are strongly recommended to avoid CAPTCHAs and rate limiting.
- Actor cannot bypass explicit CAPTCHA challenges if Google triggers them mid-run.

---

## Support

Open an issue or contact the author via the Apify platform.

**Actor:** `fervent_bus/no-website-business-leads`
**Pricing:** $0.004 per result
