"""
monitors/webpage_monitor.py
----------------------------
Generic webpage content-change monitor using requests + BeautifulSoup.

Primary target: https://csgocases.com/case/instagram-1

How it works
------------
1. Fetch the target URL with realistic browser headers + retry logic.
2. Extract meaningful signals from the HTML even for JavaScript-rendered SPAs:
   a. Inline JSON data blobs (window.__NUXT__, __vue__, JSON-LD scripts)
   b. <meta> tag content (og:description, keywords, etc.)
   c. Visible text from any non-empty elements (fallback)
   d. Full-page SHA-256 hash for change detection (always computed)
3. Compare the full-page hash against the saved hash in state.json.
4. If changed (or new), extract promo codes from both JSON data and visible text.
5. Persist the new hash + content snapshot to state.json.

Why the full-page hash works for SPAs
--------------------------------------
csgocases.com/case/instagram-1 is a Vue.js SPA — the body is mostly empty
custom elements populated at runtime. However, the server still injects:
  • Inline <script>window.__NUXT__={...}</script> with case data
  • <script type="application/ld+json">{...}</script> (structured data)
  • <meta property="og:description"> with case description
  • <meta name="keywords"> with tags

Any server-side change to the case (new items, limited-time promo, price
update) will change at least one of these — and therefore the full-page hash.
We extract meaningful text from these signals for the notification body and
code detection.

Return format (per change detected)
-------------------------------------
{
    "source":  "website",
    "new":     True,
    "content": "extracted meaningful text …",
    "codes":   ["CODE1"],
    "url":     "https://csgocases.com/case/instagram-1"
}

State layout (merged into state.json)
--------------------------------------
{
    "webpage": {
        "csgocases.com/case/instagram-1": {
            "content_hash":    "sha256hexdigest",  ← full page hash
            "content_snippet": "first 500 chars …",
            "last_changed":    "2024-01-01T00:00:00Z",
            "last_checked":    "2024-01-01T00:00:00Z",
            "check_count":     42
        }
    }
}

Configuration (env vars)
-------------------------
    WEBPAGE_URLS          Comma-separated list of URLs to monitor.
                          Default: https://csgocases.com/case/instagram-1
    WEBPAGE_TIMEOUT       HTTP timeout in seconds. Default: 20
    WEBPAGE_MAX_RETRIES   Max retry attempts. Default: 3
    WEBPAGE_SNIPPET_LEN   Length of content snapshot to save. Default: 500

Dependencies
------------
    requests>=2.32.3  (already in requirements.txt)
    beautifulsoup4    (already in requirements.txt)
    lxml              (already in requirements.txt)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import TypedDict

import requests
from bs4 import BeautifulSoup

from monitors.base_monitor import FindingResult
from utils.code_extractor import extract_codes
from utils.logger import get_logger

log = get_logger("webpage_monitor")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_URLS = "https://csgocases.com/case/instagram-1"

WEBPAGE_URLS: list[str] = [
    u.strip()
    for u in os.getenv("WEBPAGE_URLS", _DEFAULT_URLS).split(",")
    if u.strip()
]
HTTP_TIMEOUT: int = int(os.getenv("WEBPAGE_TIMEOUT",     "20"))
MAX_RETRIES:  int = int(os.getenv("WEBPAGE_MAX_RETRIES", "3"))
SNIPPET_LEN:  int = int(os.getenv("WEBPAGE_SNIPPET_LEN", "500"))

# ---------------------------------------------------------------------------
# HTTP headers — look like a real Chrome browser.
# NOTE: Brotli (br) is excluded from Accept-Encoding because requests can only
# decompress gzip/deflate natively. Sending 'br' causes the server to return
# compressed bytes that requests cannot decode without the optional 'brotli' pkg.
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",   # no brotli — see note above
    "Sec-Fetch-Dest":  "document",
    "Sec-Fetch-Mode":  "navigate",
    "Sec-Fetch-Site":  "none",
    "Sec-Fetch-User":  "?1",
    "Upgrade-Insecure-Requests": "1",
}

# ---------------------------------------------------------------------------
# Noise tags to strip before extracting visible text
# ---------------------------------------------------------------------------

_NOISE_TAGS = [
    "style", "noscript", "iframe", "link",
    "header", "footer", "nav", "aside",
]

# CSS selectors for overlays / banners to remove
_NOISE_SELECTORS = [
    "[id*='cookie']", "[class*='cookie']",
    "[id*='gdpr']",   "[class*='gdpr']",
    "[class*='popup']", "[class*='modal']",
    ".ad", ".ads", "#ad", "#ads",
]

# Content selectors tried in order (most specific → most general)
_CONTENT_SELECTORS = [
    ".case-detail", ".case-info", ".case-description", "[class*='case']",
    ".product-detail", ".product-info",
    "main", "[role='main']", "#main", "#content", ".content", ".container",
    "body",
]

_WHITESPACE_RE = re.compile(r"\s{2,}")

# Regex to find JSON-like data embedded in <script> tags
# Matches:  window.__NUXT__ = {...}  /  __vue_store__ = {...}  etc.
_JS_DATA_RE = re.compile(
    r"(?:window\.__(?:NUXT|vue|data|store|STATE|state)__\s*=\s*|"
    r"__(?:NUXT|STATE|data)__\s*=\s*)"
    r"(\{.+?\});?\s*$",
    re.MULTILINE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# Result type (output schema as specified)
# ---------------------------------------------------------------------------

class WebpageResult(TypedDict):
    source:  str
    new:     bool
    content: str
    codes:   list[str]
    url:     str


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

_STATE_KEY = "webpage"


def _url_key(url: str) -> str:
    """Derive a short, stable state key from a URL (strip scheme)."""
    return re.sub(r"^https?://", "", url).rstrip("/")


def _get_saved_hash(state: dict, url: str) -> str:
    """Return the last known content hash for *url*, or '' if never seen."""
    return (
        state.get(_STATE_KEY, {})
             .get(_url_key(url), {})
             .get("content_hash", "")
    )


def _save_state(state: dict, url: str, content_hash: str, content: str) -> None:
    """Persist the content hash and a snippet for *url* into state."""
    key    = _url_key(url)
    bucket = state.setdefault(_STATE_KEY, {}).setdefault(key, {})
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    prev_hash = bucket.get("content_hash", "")
    is_new    = prev_hash != content_hash

    bucket["content_hash"]    = content_hash
    bucket["content_snippet"] = content[:SNIPPET_LEN]
    bucket["last_checked"]    = now
    bucket["check_count"]     = bucket.get("check_count", 0) + 1
    if is_new:
        bucket["last_changed"] = now

    log.debug(
        "[Webpage] state updated for %s | hash=%s... | changed=%s",
        key, content_hash[:12], is_new,
    )


# ---------------------------------------------------------------------------
# Content hash  (always on full raw HTML — most stable signal for SPAs)
# ---------------------------------------------------------------------------

def _content_hash(text: str) -> str:
    """SHA-256 of *text* (full HTML or normalised content)."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# HTTP fetch with retry
# ---------------------------------------------------------------------------

def _fetch(url: str) -> str | None:
    """
    Fetch *url* and return the response body as a decoded string.
    Retries up to MAX_RETRIES times on transient errors.
    Returns None on permanent failure (4xx except 429, network error).
    """
    session = requests.Session()
    session.headers.update(_HEADERS)
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)

            if resp.status_code in (403, 404, 410, 451):
                log.warning(
                    "[Webpage] HTTP %d for %s — skipping permanently.",
                    resp.status_code, url,
                )
                return None

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 30))
                log.warning(
                    "[Webpage] HTTP 429 rate-limited, waiting %ds (attempt %d/%d).",
                    retry_after, attempt, MAX_RETRIES,
                )
                time.sleep(min(retry_after, 60))
                continue

            resp.raise_for_status()

            # Normalise encoding: default to utf-8 for modern sites
            if resp.encoding is None or resp.encoding.lower() in ("utf-8", "utf8"):
                resp.encoding = "utf-8"

            log.debug(
                "[Webpage] Fetched %s — %d bytes, HTTP %d (encoding=%s).",
                url, len(resp.content), resp.status_code, resp.encoding,
            )
            return resp.text

        except requests.Timeout:
            last_exc = TimeoutError(f"Timed out after {HTTP_TIMEOUT}s")
            log.warning("[Webpage] Timeout attempt %d/%d: %s", attempt, MAX_RETRIES, url)
        except requests.ConnectionError as exc:
            last_exc = exc
            log.warning("[Webpage] Connection error attempt %d/%d: %s", attempt, MAX_RETRIES, exc)
        except requests.RequestException as exc:
            last_exc = exc
            log.warning("[Webpage] Request error: %s", exc)
            return None

        if attempt < MAX_RETRIES:
            time.sleep(attempt * 3)

    log.error(
        "[Webpage] All %d attempts failed for %s. Last: %s",
        MAX_RETRIES, url, last_exc,
    )
    return None


# ---------------------------------------------------------------------------
# HTML content extraction  (SPA-aware)
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """Collapse whitespace and strip."""
    return _WHITESPACE_RE.sub(" ", text).strip()


def _extract_meta_content(soup: BeautifulSoup) -> str:
    """
    Extract meaningful text from <meta> tags that survive JS rendering:
    og:title, og:description, description, keywords.
    Returns a single joined string.
    """
    parts: list[str] = []
    for prop in ("og:title", "og:description", "og:site_name", "twitter:title",
                 "twitter:description", "description", "keywords"):
        el = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        if el:
            val = el.get("content", "").strip()
            if val:
                parts.append(val)
    return " | ".join(parts)


def _extract_json_ld(soup: BeautifulSoup) -> str:
    """
    Extract text from <script type="application/ld+json"> structured data.
    Returns all string values joined together.
    """
    parts: list[str] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = (script.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            parts.append(_flatten_json_strings(data))
        except json.JSONDecodeError:
            parts.append(raw[:200])
    return " ".join(parts)


def _flatten_json_strings(obj, depth: int = 0) -> str:
    """Recursively extract all string values from a JSON structure."""
    if depth > 10:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return " ".join(_flatten_json_strings(v, depth + 1) for v in obj.values())
    if isinstance(obj, list):
        return " ".join(_flatten_json_strings(item, depth + 1) for item in obj)
    return str(obj) if isinstance(obj, (int, float)) else ""


def _extract_nuxt_data(soup: BeautifulSoup) -> str:
    """
    Extract text from inline window.__NUXT__ or similar JS data blobs
    that are populated server-side before hydration.
    """
    parts: list[str] = []
    for script in soup.find_all("script"):
        text = script.string or ""
        if not text.strip():
            continue
        # Try to find JSON-like data in the script body
        m = _JS_DATA_RE.search(text)
        if m:
            try:
                data = json.loads(m.group(1))
                parts.append(_flatten_json_strings(data))
            except (json.JSONDecodeError, IndexError):
                pass
    return " ".join(parts)


def _extract_visible_text(soup: BeautifulSoup) -> str:
    """
    Extract any visible text from the page — works for server-rendered pages.
    Falls back gracefully to empty string for pure SPAs with no visible text.
    """
    # Try content selectors from most specific to most general
    for sel in _CONTENT_SELECTORS:
        try:
            el = soup.select_one(sel)
            if el:
                text = _normalise(el.get_text(" ", strip=True))
                if len(text) >= 20:
                    return text
        except Exception:
            continue
    return ""


def _extract_content(html: str) -> tuple[str, str]:
    """
    Parse *html* and extract meaningful content text + page title.

    Strategy (SPA-aware):
    1. Meta tags (always populated server-side)
    2. JSON-LD structured data (always populated server-side)
    3. Inline window.__NUXT__ / similar JS data (populated server-side)
    4. Visible text from DOM (works for SSR pages; empty for pure CSR SPAs)

    Returns (content_text, page_title).
    """
    soup = BeautifulSoup(html, "lxml")

    # Page title
    title_el   = soup.find("title")
    page_title = title_el.get_text(strip=True) if title_el else ""

    # Strip noise before extracting visible text
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    for sel in _NOISE_SELECTORS:
        try:
            for el in soup.select(sel):
                el.decompose()
        except Exception:
            pass

    # Collect signals in priority order
    signals: list[str] = []

    meta_text = _extract_meta_content(soup)
    if meta_text:
        signals.append(meta_text)
        log.debug("[Webpage] Meta signal: %d chars", len(meta_text))

    ld_text = _extract_json_ld(soup)
    if ld_text:
        signals.append(ld_text)
        log.debug("[Webpage] JSON-LD signal: %d chars", len(ld_text))

    nuxt_text = _extract_nuxt_data(soup)
    if nuxt_text:
        signals.append(nuxt_text)
        log.debug("[Webpage] Nuxt/JS data signal: %d chars", len(nuxt_text))

    visible = _extract_visible_text(soup)
    if visible:
        signals.append(visible)
        log.debug("[Webpage] Visible text signal: %d chars", len(visible))

    content = _normalise(" ".join(signals)) if signals else ""

    if not content and page_title:
        content = page_title  # absolute fallback

    return content, page_title


# ---------------------------------------------------------------------------
# Core check function
# ---------------------------------------------------------------------------

def check_url(url: str, state: dict) -> WebpageResult | None:
    """
    Fetch *url*, compare the FULL PAGE HASH against saved state, and return
    a WebpageResult if the page has changed (or is seen for the first time).

    The full-page hash catches all server-side changes including:
    - Changes to window.__NUXT__ data (new items, prices, promotions)
    - Changes to <meta> tags (og:description, keywords)
    - Changes to JSON-LD structured data
    - Any HTML structural change

    Returns None if:
    - The URL is unreachable.
    - The page content is identical to the previously saved hash.
    - It is the very first run (baseline recorded, no notification sent).

    Updates state["webpage"][url_key] in place.
    """
    log.info("[Webpage] Checking: %s", url)

    html = _fetch(url)
    if html is None:
        log.warning("[Webpage] Could not fetch %s — skipping.", url)
        return None

    # ── Full-page hash (primary change detector) ──────────────────────────
    page_hash  = _content_hash(html)
    saved_hash = _get_saved_hash(state, url)

    is_first_run = saved_hash == ""
    has_changed  = page_hash != saved_hash

    if not has_changed:
        # Still save to update last_checked and check_count
        _save_state(state, url, page_hash, "")
        log.info("[Webpage] No change at %s (hash=%s...).", url, page_hash[:12])
        return None

    # ── Extract meaningful content for notification / code detection ──────
    content, page_title = _extract_content(html)
    if not content:
        content = page_title or url

    codes = extract_codes(content)

    # Always save the new hash + content snippet
    _save_state(state, url, page_hash, content)

    if is_first_run:
        log.info(
            "[Webpage] First run baseline recorded for %s | title=%r | codes=%s",
            url, page_title, codes,
        )
        # On the very first run, record the baseline but DON'T notify —
        # there's nothing to compare against (any page would fire an alert).
        return None

    log.info(
        "[Webpage] Content changed at %s | title=%r | codes=%s | %.80s",
        url, page_title, codes, content,
    )

    return WebpageResult(
        source  = "website",
        new     = True,
        content = content[:SNIPPET_LEN],
        codes   = codes,
        url     = url,
    )


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def fetch_new_items(state: dict) -> list[WebpageResult]:
    """
    Check all configured URLs (WEBPAGE_URLS env var, comma-separated).
    Returns a list of WebpageResult for any pages that have changed.
    """
    results: list[WebpageResult] = []

    for url in WEBPAGE_URLS:
        try:
            result = check_url(url, state)
            if result is not None:
                results.append(result)
        except Exception as exc:
            log.error("[Webpage] Unhandled error checking %s: %s", url, exc, exc_info=True)

    log.info("[Webpage] Done — %d changed page(s).", len(results))
    return results


# ---------------------------------------------------------------------------
# BaseMonitor-compatible adapter
# ---------------------------------------------------------------------------

class WebpageMonitor:
    """
    Sync adapter that plugs into the main.py MONITORS list.
    Wraps fetch_new_items() and maps WebpageResult → FindingResult.
    """
    SOURCE_NAME = "Webpage"

    def fetch_new_items(self, state: dict) -> list[FindingResult]:
        """Check all configured URLs and return FindingResult for each change."""
        try:
            raw = fetch_new_items(state)
        except Exception as exc:
            log.error("[Webpage] Adapter error: %s", exc, exc_info=True)
            return []

        findings: list[FindingResult] = []
        for r in raw:
            url_key = _url_key(r["url"])
            findings.append(
                FindingResult(
                    source  = f"CSGOCASES Website ({url_key})",
                    item_id = _content_hash(r["content"]),
                    url     = r["url"],
                    author  = "CSGOCASES Official",
                    title   = f"Page content changed: {url_key}",
                    body    = r["content"],
                    codes   = r["codes"],
                )
            )

        return findings
