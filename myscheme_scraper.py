import asyncio
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, quote_plus

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from supabase_client import get_supabase_client
from youtube_search import find_youtube_video
from playwright.async_api import async_playwright

DOTENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=DOTENV_PATH)

SERPAPI_URL = "https://serpapi.com/search.json"
SERPAPI_ENGINE = "google"
SEARCH_QUERY = "site:myscheme.gov.in/schemes myscheme"
SERPAPI_PAGE_SIZE = 10
SERPAPI_COUNTRY = "in"
SERPAPI_LANGUAGE = "en"
MIN_SCHEMES = 50
REQUEST_DELAY_SECONDS = 1.5
SCHEME_PATH_PREFIX = "/schemes/"
MAX_SERPAPI_PAGES_WITHOUT_PROGRESS = 6

# MyScheme.gov.in is a Next.js SPA — its content is rendered via JavaScript.
# We attempt multiple strategies to extract data:
#   1. SerpAPI rich snippets and search result metadata (most reliable)
#   2. __NEXT_DATA__ JSON embedded in the page shell (sometimes available)
#   3. Open Graph / meta tags from the HTML head (always available)
#   4. Headings and nearby text extraction (last resort)

MYSCHEME_API_BASE = "https://api.myscheme.gov.in/search"


class SerpApiAuthenticationError(RuntimeError):
    """Raised when SerpAPI rejects the configured API key."""


class SerpApiNetworkError(RuntimeError):
    """Raised when SerpAPI cannot be reached reliably."""


def _clean_text(value: Optional[str]) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _parse_income(text: str) -> Optional[int]:
    match = re.search(r"(\d{2,9})", text.replace(",", ""))
    return int(match.group(1)) if match else None


def _parse_age_range(text: str) -> Optional[Tuple[int, int]]:
    match = re.search(r"(\d{1,3})\s*[-to]+\s*(\d{1,3})", text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def parse_eligibility_text(raw_text: str) -> List[Dict[str, str]]:
    text = _clean_text(raw_text).lower()
    criteria: List[Dict[str, str]] = []

    if not text:
        return criteria

    income_value = _parse_income(text) if "income" in text else None
    if income_value:
        criteria.append(
            {
                "field": "annual_income",
                "operator": "less_than",
                "value": str(income_value),
            }
        )

    if re.search(r"\b(sc|st|obc|general)\b", text):
        categories = []
        if "sc" in text:
            categories.append("SC")
        if "st" in text:
            categories.append("ST")
        if "obc" in text:
            categories.append("OBC")
        if "general" in text:
            categories.append("General")
        criteria.append(
            {
                "field": "caste_category",
                "operator": "in",
                "value": "/".join(categories),
            }
        )

    if "female" in text or "women" in text:
        criteria.append(
            {
                "field": "gender",
                "operator": "equals",
                "value": "female",
            }
        )

    age_range = _parse_age_range(text)
    if age_range:
        min_age, max_age = age_range
        criteria.extend(
            [
                {
                    "field": "age",
                    "operator": "greater_than",
                    "value": str(min_age),
                },
                {
                    "field": "age",
                    "operator": "less_than",
                    "value": str(max_age),
                },
            ]
        )

    if not criteria:
        criteria.append(
            {
                "field": "other",
                "operator": "text",
                "value": _clean_text(raw_text),
            }
        )

    return criteria


# ---------------------------------------------------------------------------
# HTML extraction helpers
# ---------------------------------------------------------------------------

def _extract_text_near_heading(soup: BeautifulSoup, heading_pattern: str) -> str:
    """Find a heading matching the pattern and return nearby paragraph/list text."""
    heading = soup.find(string=re.compile(heading_pattern, re.IGNORECASE))
    if not heading:
        return ""
    parent = heading.parent
    # Walk up to find a container with siblings
    for _ in range(4):
        if not parent:
            break
        siblings = list(parent.find_next_siblings(["p", "li", "div", "span", "ul"], limit=5))
        if siblings:
            return _clean_text(" ".join(s.get_text(" ") for s in siblings))
        parent = parent.parent
    return ""


def _extract_json_ld(soup: BeautifulSoup) -> Dict[str, str]:
    """Try to extract description/benefits from JSON-LD structured data."""
    result: Dict[str, str] = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0] if data else {}
            desc = data.get("description") or data.get("serviceDescription") or ""
            if desc:
                result["description"] = _clean_text(str(desc))
            provider = data.get("provider") or data.get("serviceProvider") or {}
            if isinstance(provider, dict) and provider.get("name"):
                result["ministry"] = _clean_text(str(provider["name"]))
        except Exception:
            pass
    return result


def _extract_next_data(soup: BeautifulSoup) -> Dict[str, Any]:
    """Extract data from Next.js __NEXT_DATA__ script tag (SSR/SSG props)."""
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return {}
    try:
        data = json.loads(script.string)
        page_props = data.get("props", {}).get("pageProps", {})
        return page_props
    except (json.JSONDecodeError, AttributeError):
        return {}


def _extract_meta_tags(soup: BeautifulSoup) -> Dict[str, str]:
    """Extract metadata from Open Graph and standard meta tags."""
    result: Dict[str, str] = {}

    # og:title
    og_title = soup.select_one("meta[property='og:title']")
    if og_title and og_title.get("content"):
        result["name"] = _clean_text(og_title["content"])

    # og:description or meta description
    for selector in ["meta[property='og:description']", "meta[name='description']"]:
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            content = _clean_text(tag["content"])
            if len(content) > 20:  # skip trivial placeholders
                result["description"] = content
                break

    # title tag
    if not result.get("name"):
        title_tag = soup.select_one("title")
        if title_tag:
            name = _clean_text(title_tag.get_text())
            name = re.sub(r"\s*\|\s*myscheme.*", "", name, flags=re.IGNORECASE)
            if name:
                result["name"] = name

    return result


def _extract_from_next_data(page_props: Dict[str, Any]) -> Dict[str, str]:
    """Pull structured scheme data from Next.js pageProps."""
    result: Dict[str, str] = {}

    # The data structure varies but typically looks like:
    # pageProps.schemeData or pageProps.data or pageProps.scheme
    scheme_data = (
        page_props.get("schemeData")
        or page_props.get("data")
        or page_props.get("scheme")
        or page_props.get("schemeDetails")
        or {}
    )

    if isinstance(scheme_data, list) and scheme_data:
        scheme_data = scheme_data[0]
    if not isinstance(scheme_data, dict):
        scheme_data = {}

    # Try common field names
    for name_key in ["schemeName", "scheme_name", "name", "title", "schemeTitle"]:
        val = scheme_data.get(name_key)
        if val and isinstance(val, str):
            result["name"] = _clean_text(val)
            break

    for desc_key in ["schemeDescription", "description", "briefDescription", "about",
                      "scheme_description", "longDescription", "detailedDescription"]:
        val = scheme_data.get(desc_key)
        if val and isinstance(val, str) and len(val) > 20:
            # Strip HTML tags if present
            result["description"] = _clean_text(re.sub(r"<[^>]+>", " ", val))
            break

    for ben_key in ["benefits", "schemeBenefits", "benefitDescription", "benefit",
                     "scheme_benefits"]:
        val = scheme_data.get(ben_key)
        if val and isinstance(val, str) and len(val) > 10:
            result["benefits"] = _clean_text(re.sub(r"<[^>]+>", " ", val))
            break
        # Sometimes benefits is a list of objects
        if isinstance(val, list):
            parts = []
            for item in val:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(item.get("description") or item.get("text") or "")
            text = _clean_text(" ".join(parts))
            if text:
                result["benefits"] = text
                break

    for elig_key in ["eligibility", "schemeEligibility", "eligibilityCriteria",
                      "eligibility_criteria"]:
        val = scheme_data.get(elig_key)
        if val and isinstance(val, str):
            result["eligibility_text"] = _clean_text(re.sub(r"<[^>]+>", " ", val))
            break
        if isinstance(val, list):
            parts = []
            for item in val:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(item.get("description") or item.get("text") or "")
            text = _clean_text(" ".join(parts))
            if text:
                result["eligibility_text"] = text
                break

    for min_key in ["ministry", "ministryName", "department", "nodal_ministry",
                     "scheme_ministry"]:
        val = scheme_data.get(min_key)
        if val and isinstance(val, str):
            result["ministry"] = _clean_text(val)
            break

    for state_key in ["state", "stateName", "level"]:
        val = scheme_data.get(state_key)
        if val and isinstance(val, str):
            result["state"] = _clean_text(val)
            break

    for url_key in ["applicationUrl", "applyUrl", "application_url", "howToApply",
                     "applyLink"]:
        val = scheme_data.get(url_key)
        if val and isinstance(val, str) and val.startswith("http"):
            result["application_url"] = val
            break

    return result


def _extract_scheme_details(page_html: str, base_url: str) -> Dict[str, str]:
    """Extract scheme details from the page HTML using multiple strategies."""
    soup = BeautifulSoup(page_html, "html.parser")

    # --- Strategy 1: __NEXT_DATA__ (most reliable for Next.js SSR/SSG) ---
    page_props = _extract_next_data(soup)
    next_data = _extract_from_next_data(page_props) if page_props else {}

    # --- Strategy 2: Meta tags (always available even in SPA shell) ---
    meta_data = _extract_meta_tags(soup)

    # --- Strategy 3: JSON-LD structured data ---
    jld = _extract_json_ld(soup)

    # --- Strategy 4: Direct HTML parsing (may work if page has SSR content) ---
    html_data: Dict[str, str] = {}

    # Name from h1
    h1 = soup.select_one("h1")
    if h1:
        h1_text = _clean_text(h1.get_text())
        if h1_text and len(h1_text) > 3 and "myscheme" not in h1_text.lower():
            html_data["name"] = h1_text

    # Ministry
    ministry_el = soup.select_one(".scheme-header__ministry")
    if ministry_el:
        html_data["ministry"] = _clean_text(ministry_el.get_text())
    if not html_data.get("ministry"):
        ministry_text = _extract_text_near_heading(soup, r"ministry|department|nodal")
        if ministry_text:
            html_data["ministry"] = ministry_text

    # Description from CSS classes
    for selector in [".scheme-description", ".scheme-about", "[class*='description']"]:
        desc_el = soup.select_one(selector)
        if desc_el:
            text = _clean_text(desc_el.get_text(" "))
            if len(text) > 30:
                html_data["description"] = text
                break

    if not html_data.get("description"):
        desc_text = _extract_text_near_heading(soup, r"^(about|description|overview)$")
        if desc_text:
            html_data["description"] = desc_text

    # Benefits
    for selector in [".scheme-benefits", "[class*='benefit']"]:
        ben_el = soup.select_one(selector)
        if ben_el:
            text = _clean_text(ben_el.get_text(" "))
            if len(text) > 10:
                html_data["benefits"] = text
                break

    if not html_data.get("benefits"):
        ben_text = _extract_text_near_heading(soup, r"benefit|what.*get|entitlement")
        if ben_text:
            html_data["benefits"] = ben_text

    # Eligibility
    for selector in [".scheme-eligibility", "[class*='eligibility']"]:
        elig_el = soup.select_one(selector)
        if elig_el:
            text = _clean_text(elig_el.get_text(" "))
            if text:
                html_data["eligibility_text"] = text
                break

    if not html_data.get("eligibility_text"):
        elig_text = _extract_text_near_heading(soup, r"^eligibility$")
        if elig_text:
            html_data["eligibility_text"] = elig_text

    # Application URL
    apply_pattern = re.compile(r"apply|how to apply|apply now|apply online", re.IGNORECASE)
    href_pattern = re.compile(r"apply", re.IGNORECASE)
    for a in soup.find_all("a", href=True):
        link_text = _clean_text(a.get_text(" "))
        href = a["href"]
        if apply_pattern.search(link_text) or href_pattern.search(href):
            html_data["application_url"] = urljoin(base_url, href)
            break

    # State
    if soup.find(string=re.compile(r"Central Scheme", re.IGNORECASE)):
        html_data["state"] = ""
    state_tag = soup.find(string=re.compile(r"\bState\b", re.IGNORECASE))
    if state_tag and state_tag.parent:
        html_data["state"] = _clean_text(state_tag.parent.get_text(" "))

    # --- Merge all sources: next_data > meta_data > jld > html_data ---
    # Priority: Next.js data > JSON-LD > Meta tags > HTML parsing
    merged: Dict[str, str] = {}
    for source in [html_data, meta_data, jld, next_data]:
        for key, value in source.items():
            if value and (not merged.get(key) or len(str(value)) > len(str(merged.get(key, "")))):
                merged[key] = value

    return merged


# ---------------------------------------------------------------------------
# SerpAPI
# ---------------------------------------------------------------------------

def _is_scheme_link(href: str) -> bool:
    if not href:
        return False
    parsed = urlparse(href)
    path = parsed.path or ""
    if not path.startswith(SCHEME_PATH_PREFIX):
        return False
    slug = path[len(SCHEME_PATH_PREFIX) :].strip("/")
    return bool(slug)


def _get_serpapi_key() -> str:
    key = os.environ.get("SERPAPI_KEY", "").strip()
    if not key:
        raise ValueError(f"SERPAPI_KEY must be set in {DOTENV_PATH} to use SerpAPI")
    return key


async def _fetch_serpapi_page(client: httpx.AsyncClient, start: int) -> Dict[str, Any]:
    _log(f"Fetching SerpAPI results (start={start})...")
    params = {
        "engine": SERPAPI_ENGINE,
        "q": SEARCH_QUERY,
        "api_key": _get_serpapi_key(),
        "start": start,
        "num": SERPAPI_PAGE_SIZE,
        "gl": SERPAPI_COUNTRY,
        "hl": SERPAPI_LANGUAGE,
    }
    for attempt in range(1, 4):
        try:
            response = await client.get(SERPAPI_URL, params=params)
            break
        except httpx.TimeoutException as exc:
            if attempt == 3:
                raise SerpApiNetworkError(
                    "SerpAPI timed out three times while fetching search results. "
                    "Check your internet connection, VPN/firewall, or SerpAPI availability, "
                    "then retry."
                ) from exc
            _log(f"SerpAPI request timed out; retrying ({attempt}/3)...")
            await asyncio.sleep(float(attempt))
        except httpx.RequestError as exc:
            if attempt == 3:
                raise SerpApiNetworkError(
                    "Could not connect to SerpAPI after three attempts. "
                    "Check your internet connection or firewall, then retry."
                ) from exc
            _log(f"SerpAPI network error; retrying ({attempt}/3)...")
            await asyncio.sleep(float(attempt))

    if response.status_code in {401, 403}:
        raise SerpApiAuthenticationError(
            "SerpAPI rejected SERPAPI_KEY. Generate a new active key in the SerpAPI dashboard, "
            f"then update {DOTENV_PATH} and restart the command."
        )
    if response.status_code == 429:
        raise RuntimeError(
            "SerpAPI rate limit reached. Check your SerpAPI plan or wait before retrying."
        )
    response.raise_for_status()
    return response.json()


def _extract_serpapi_results(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract scheme links + enriched data from SerpAPI organic results."""
    results = data.get("organic_results") or []
    items: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    for entry in results:
        link = entry.get("link") or entry.get("url")
        if not link or not _is_scheme_link(link):
            continue
        if link in seen:
            continue
        seen.add(link)

        # Extract as much data as possible from the SerpAPI result
        title = _clean_text(entry.get("title") or "")
        snippet = _clean_text(entry.get("snippet") or "")

        # Rich snippets can contain structured description
        rich_snippet = entry.get("rich_snippet") or {}
        rich_top = rich_snippet.get("top") or {}
        rich_bottom = rich_snippet.get("bottom") or {}

        # Combine snippet sources for a richer description
        rich_description = ""
        for snippet_src in [rich_top, rich_bottom]:
            detected = snippet_src.get("detected_extensions") or {}
            extensions = snippet_src.get("extensions") or []
            snippets_list = snippet_src.get("snippets") or []
            if snippets_list:
                rich_description = _clean_text(" ".join(str(s) for s in snippets_list))
                break
            if extensions:
                rich_description = _clean_text(" ".join(str(e) for e in extensions))
                break

        # Highlighted words from snippet can hint at key eligibility terms
        highlighted_words = entry.get("snippet_highlighted_words") or []

        # Sitelinks may contain "How to Apply" links
        sitelinks = entry.get("sitelinks") or {}
        inline_links = sitelinks.get("inline") or sitelinks.get("expanded") or []
        apply_url = ""
        for sl in inline_links:
            sl_title = _clean_text(sl.get("title") or "")
            sl_link = sl.get("link") or ""
            if re.search(r"apply|how to apply", sl_title, re.IGNORECASE):
                apply_url = sl_link
                break

        # Source info
        source_info = entry.get("source") or ""
        displayed_link = entry.get("displayed_link") or ""

        items.append(
            {
                "link": link,
                "title": title,
                "snippet": snippet,
                "rich_description": rich_description,
                "highlighted_words": highlighted_words,
                "apply_url": apply_url,
                "source_info": _clean_text(str(source_info)) if isinstance(source_info, str) else "",
            }
        )

    return items


# ---------------------------------------------------------------------------
# HTML fetching
# ---------------------------------------------------------------------------

async def _fetch_html(client: httpx.AsyncClient, url: str) -> Optional[str]:
    for attempt in range(3):
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                )
                page = await context.new_page()
                await page.goto(url, wait_until="networkidle", timeout=30000)
                # Give the SPA an extra moment to populate the DOM
                await asyncio.sleep(2)
                content = await page.content()
                await browser.close()
                return content
        except Exception as e:
            _log(f"Playwright error fetching {url}: {e}")
            await asyncio.sleep(2.0)
            continue
    return None


# ---------------------------------------------------------------------------
# Supabase persistence
# ---------------------------------------------------------------------------

def _upsert_scheme(scheme: Dict[str, Any]) -> Optional[str]:
    """Insert or update a scheme. Only overwrites fields that have non-empty new values."""
    supabase = get_supabase_client()
    name = scheme.get("name")
    if not name:
        return None

    payload = {
        "name": name,
        "ministry": scheme.get("ministry"),
        "state": scheme.get("state"),
        "description": scheme.get("description"),
        "benefits": scheme.get("benefits"),
        "application_url": scheme.get("application_url"),
        "source_url": scheme.get("source_url"),
        "youtube_url": scheme.get("youtube_url") or find_youtube_video(str(name)),
    }

    existing = _execute_supabase(
        lambda: supabase.table("schemes").select("id,description,benefits,application_url,ministry,state,youtube_url").eq("name", name).limit(1).execute(),
        "looking up a scheme",
    )
    rows = existing.data or []
    if rows:
        scheme_id = rows[0]["id"]
        existing_row = rows[0]

        # Only update fields where the new value is non-empty.
        # This prevents overwriting good data with empty strings from failed scrapes.
        update_payload: Dict[str, Any] = {}
        for key, new_value in payload.items():
            if key == "name":
                continue  # Don't update the name, it's the lookup key
            if new_value:
                # Only overwrite if new value is non-empty
                update_payload[key] = new_value
            # If new value is empty but existing has data, keep existing (don't add to update)

        if update_payload:
            _execute_supabase(
                lambda: supabase.table("schemes").update(update_payload).eq("id", scheme_id).execute(),
                "updating a scheme",
            )
        return scheme_id

    # New scheme — insert, filtering out None values
    insert_payload = {k: v for k, v in payload.items() if v is not None}
    response = _execute_supabase(
        lambda: supabase.table("schemes").insert(insert_payload).execute(),
        "inserting a scheme",
    )
    data = response.data or []
    return data[0]["id"] if data else None


def _upsert_criteria(scheme_id: str, criteria: List[Dict[str, str]]) -> None:
    """Insert eligibility criteria, deduplicating by deleting old criteria first."""
    if not criteria:
        return
    supabase = get_supabase_client()

    # Delete existing criteria for this scheme to prevent duplicates on re-scrape
    _execute_supabase(
        lambda: supabase.table("eligibility_criteria").delete().eq("scheme_id", scheme_id).execute(),
        "clearing old eligibility criteria",
    )

    rows = [
        {
            "scheme_id": scheme_id,
            "field": row["field"],
            "operator": row["operator"],
            "value": row["value"],
        }
        for row in criteria
    ]
    _execute_supabase(
        lambda: supabase.table("eligibility_criteria").insert(rows).execute(),
        "inserting eligibility criteria",
    )


def _execute_supabase(operation: Callable[[], Any], description: str) -> Any:
    for attempt in range(1, 4):
        try:
            return operation()
        except httpx.RequestError as exc:
            if attempt == 3:
                raise RuntimeError(
                    f"Supabase connection failed while {description}. "
                    "Check the Supabase URL, network connection, and project status, then retry."
                ) from exc
            _log(f"Supabase connection dropped; retrying {description} ({attempt}/3)...")
            time.sleep(float(attempt))
    raise RuntimeError(f"Supabase operation failed while {description}.")


def _log(message: str) -> None:
    print(message, flush=True)


def _log_fields(label: str, data: Dict[str, Any]) -> None:
    """Log which fields have data for debugging."""
    filled = [k for k, v in data.items() if v and str(v).strip()]
    empty = [k for k, v in data.items() if not v or not str(v).strip()]
    _log(f"  {label}: filled=[{', '.join(filled)}] empty=[{', '.join(empty)}]")


# ---------------------------------------------------------------------------
# Main scrape loop
# ---------------------------------------------------------------------------

async def scrape_all() -> None:
    scraped = 0
    seen_links: Set[str] = set()
    start = 0
    pages_without_progress = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        _log("Starting SerpAPI scraper...")
        while scraped < MIN_SCHEMES:
            scraped_before = scraped
            data = await _fetch_serpapi_page(client, start)
            scheme_results = _extract_serpapi_results(data)
            _log(f"Found {len(scheme_results)} SerpAPI results.")

            if not scheme_results:
                _log("No scheme links found from SerpAPI, stopping.")
                break

            new_results = [entry for entry in scheme_results if entry["link"] not in seen_links]
            _log(f"Processing {len(new_results)} new links (seen: {len(seen_links)}).")
            if not new_results:
                _log("No new scheme links found, stopping.")
                break

            for entry in new_results:
                if scraped >= MIN_SCHEMES:
                    break
                link = entry["link"]
                seen_links.add(link)

                # --- Fetch the scheme page HTML ---
                page_html = await _fetch_html(client, link)

                # Extract details from page HTML (may be mostly empty for SPA)
                html_details: Dict[str, str] = {}
                if page_html:
                    html_details = _extract_scheme_details(page_html, link)

                # --- Merge: SerpAPI data + HTML data ---
                # SerpAPI data as fallback for fields not found in HTML
                serp_name = entry.get("title") or ""
                serp_snippet = entry.get("snippet") or ""
                serp_rich_desc = entry.get("rich_description") or ""
                serp_apply_url = entry.get("apply_url") or ""

                # Build final scheme data, preferring richer/longer values
                final_name = html_details.get("name") or serp_name
                if not final_name:
                    _log(f"Skipping {link} — no name found.")
                    continue

                # For description: prefer HTML-extracted > rich_description > snippet
                final_description = html_details.get("description") or ""
                if not final_description or len(final_description) < 30:
                    if serp_rich_desc and len(serp_rich_desc) > len(final_description):
                        final_description = serp_rich_desc
                if not final_description or len(final_description) < 20:
                    if serp_snippet and len(serp_snippet) > len(final_description):
                        final_description = serp_snippet

                final_benefits = html_details.get("benefits") or ""
                final_eligibility = html_details.get("eligibility_text") or ""
                if not final_eligibility:
                    # Try to extract eligibility hints from the snippet
                    if any(word in serp_snippet.lower() for word in ["eligib", "income", "age", "caste", "bpl"]):
                        final_eligibility = serp_snippet

                final_application_url = html_details.get("application_url") or serp_apply_url
                # If still no apply URL, construct one from the scheme page
                if not final_application_url:
                    final_application_url = link

                scheme_payload = {
                    "name": final_name,
                    "ministry": html_details.get("ministry") or "",
                    "state": html_details.get("state") or "",
                    "description": final_description,
                    "benefits": final_benefits,
                    "eligibility_text": final_eligibility,
                    "application_url": final_application_url,
                    "source_url": link,
                }

                _log_fields(final_name, scheme_payload)

                criteria = parse_eligibility_text(scheme_payload.get("eligibility_text", ""))
                scheme_id = _upsert_scheme(scheme_payload)
                if scheme_id:
                    _upsert_criteria(scheme_id, criteria)
                    scraped += 1
                    _log(f"Scraped {scraped}: {final_name}")

                await asyncio.sleep(REQUEST_DELAY_SECONDS)

            start += SERPAPI_PAGE_SIZE

            if scraped == scraped_before:
                pages_without_progress += 1
                if pages_without_progress >= MAX_SERPAPI_PAGES_WITHOUT_PROGRESS:
                    _log("No new schemes parsed after multiple pages, stopping.")
                    break
            else:
                pages_without_progress = 0


if __name__ == "__main__":
    try:
        asyncio.run(scrape_all())
    except SerpApiAuthenticationError as exc:
        _log(f"ERROR: {exc}")
        raise SystemExit(1) from exc
    except ValueError as exc:
        _log(f"ERROR: {exc}")
        raise SystemExit(1) from exc
    except SerpApiNetworkError as exc:
        _log(f"ERROR: {exc}")
        raise SystemExit(1) from exc
    except RuntimeError as exc:
        _log(f"ERROR: {exc}")
        raise SystemExit(1) from exc