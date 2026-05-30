"""
monitors/instagram_monitor.py
------------------------------
Instagram monitor for @csgocasescom using async Playwright.

Strategy
--------
1. Launch Chromium in headless mode with a realistic browser profile.
2. Navigate to the public Instagram profile page.
3. Intercept the Instagram GraphQL/API responses to extract post data
   WITHOUT needing a login (public profile, guest session).
4. Fallback: parse the page DOM for og:description meta tags.
5. Hash each post's content — compare against state to detect new posts.
6. Extract promo codes with the shared regex pattern.
7. Return structured results and update state["instagram"][ACCOUNT].

Return format (per post)
------------------------
{
    "source":  "instagram",
    "new":     True,
    "content": "caption text …",
    "codes":   ["CODE1", "CODE2"],
    "url":     "https://www.instagram.com/p/<shortcode>/"
}

State layout (merged into state.json)
--------------------------------------
{
    "instagram": {
        "csgocasescom": {
            "latest_hash":    "sha256hexdigest",
            "latest_url":     "https://www.instagram.com/p/.../",
            "last_checked":   "2024-01-01T00:00:00Z",
            "seen_hashes":    ["hash1", "hash2", ...]   # last 50
        }
    }
}

GitHub Actions requirements
----------------------------
Add these steps BEFORE "Run monitor" in monitor.yml:
    - name: Install Playwright browsers
      run: playwright install chromium --with-deps

Dependencies
------------
    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import TypedDict

from utils.logger import get_logger

log = get_logger("instagram_monitor")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_ACCOUNT: str = os.getenv("INSTAGRAM_ACCOUNT", "csgocasescom")
PROFILE_URL:    str = f"https://www.instagram.com/{TARGET_ACCOUNT}/"

# How many posts to inspect (latest N from the profile grid)
MAX_POSTS: int = int(os.getenv("INSTAGRAM_MAX_POSTS", "6"))

# Per-page navigation timeout (ms)
PAGE_TIMEOUT: int = int(os.getenv("INSTAGRAM_TIMEOUT_MS", "30000"))

# Seconds to wait for posts to render after page load
RENDER_WAIT:  float = float(os.getenv("INSTAGRAM_RENDER_WAIT", "4"))

# Max retries for the full scrape cycle
MAX_RETRIES: int = int(os.getenv("INSTAGRAM_MAX_RETRIES", "3"))

# Promo-code regex — exact spec from requirements
_CODE_RE = re.compile(r"\b([A-Z0-9]{4,15})\b")

# Promo-keyword detector — used for context-aware filtering
_KW_RE = re.compile(
    r"(?:promo|bonus|code|coupon|redeem|discount|voucher|offer"
    r"|use\s+code|gift\s+code|free\s+code|enter\s+code)",
    re.IGNORECASE,
)

# Blocklist — never a promo code (shared with twitter_snscrape_monitor)
_BLACKLIST: set[str] = {
    "FREE", "CSGO", "CASE", "DAILY",
    "HTTP", "HTTPS", "HTML", "CSGOCASES", "STEAM", "REDDIT",
    "TWITTER", "YOUTUBE", "TWITCH", "DISCORD", "INSTAGRAM", "TELEGRAM",
    "CODE", "CODES", "PROMO", "BONUS", "REDEEM", "COUPON",
    "DISCOUNT", "OFFER", "DEAL", "LINK", "CLICK", "HERE",
    "USE", "ENTER", "SPIN", "SPINS", "OPEN", "CASES",
    "NULL", "TRUE", "FALSE", "NONE",
    "THIS", "THAT", "WITH", "FROM", "HAVE", "WILL", "BEEN",
    "THEY", "WHEN", "YOUR", "WHAT", "SOME", "MORE", "JUST",
    "POST", "VIEW", "USER", "INFO", "SITE", "ALSO", "EVEN",
    "BACK", "NEXT", "VERY", "ONLY", "THEN", "GAME", "SKIN",
    "ITEM", "KEYS", "RARE", "REAL", "BEST", "GOOD", "NICE",
    "COOL", "HUGE", "LAST", "FAST", "EASY", "HIGH", "MANY",
    "MOST", "LONG", "DOWN", "SIDE", "OVER", "MAKE", "MUCH",
    "COME", "INTO", "GIVE", "KNOW", "TAKE", "WELL", "EACH",
    "SUCH", "BOTH", "ONCE", "LIKE", "TODAY", "EVERY", "AGAIN",
    "RIGHT", "STILL", "FIRST", "ABOUT", "BEING", "COULD",
    "GOING", "GREAT", "HELLO", "HOURS", "IMAGE", "LATER",
    "LUCKY", "MIGHT", "NEVER", "NIGHT", "OFTEN", "OTHER",
    "QUITE", "REACH", "REPLY", "SHALL", "SINCE", "SMALL",
    "SORRY", "THEIR", "THERE", "THESE", "THINK", "THOSE",
    "THREE", "UNTIL", "USING", "VALID", "VISIT", "WATCH",
    "WHERE", "WHILE", "WORLD", "WOULD", "WRITE", "WRONG",
    "YEARS", "PRIZE", "CLAIM", "CHECK", "SHARE", "FOLLOW",
    "TWEET", "RETWEET", "QUOTE", "THREAD", "DROPS", "ITEMS",
    "SKINS", "KNIVES", "GLOVES", "TRADE", "STORE", "SHOP",
    "PLAY", "LIMITED", "OFFERS", "DEALS", "PROMOS", "WINNERS",
    "WINNER", "EXTRA", "ADDED", "TOTAL", "HAPPY", "YEAR",
    "YEARS", "MONTH", "WEEK", "HOUR", "TIME", "AVAILABLE",
    "SPECIAL", "NOTHING", "SOMETHING", "EVERYONE", "GIVEAWAY",
    "ANYTHING", "FEATURED", "RELEASED", "GENERATE", "INCLUDES",
    "PASSWORD", "REWARD", "REWARDS", "POINT", "POINTS", "COINS",
    "YOURS", "EARNED", "COLLECT", "REDEEMED", "GIFTED",
    # Instagram-specific noise
    "PHOTO", "VIDEO", "REEL", "STORY", "FOLLOW", "LIKE",
    "COMMENT", "SHARE", "SAVE", "TAGGED", "MENTION", "HASHTAG",
    "PROFILE", "ACCOUNT", "PAGE", "FEED", "EXPLORE", "DIRECT",
    "INSTAGRAM", "IGTV", "COLLAB", "BRAND", "PARTNER",
    "GIVEAWAY", "GIVEAWAYS", "CONTEST", "WINNERS", "WINNER",
    # Purchase / checkout flow words
    "CHECKOUT", "PURCHASE", "PAYMENT", "CART", "BASKET",
    "SHIPPING", "DELIVERY", "INVOICE", "RECEIPT", "ORDER",
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

class InstagramResult(TypedDict):
    source:  str
    new:     bool
    content: str
    codes:   list[str]
    url:     str


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

_STATE_KEY = "instagram"
_MAX_SEEN_HASHES = 50  # keep last N hashes per account to bound growth


def _content_hash(text: str) -> str:
    """SHA-256 of the normalised (whitespace-collapsed) caption text."""
    normalised = " ".join(text.split()).strip().lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _get_seen_hashes(state: dict) -> set[str]:
    return set(
        state.get(_STATE_KEY, {})
             .get(TARGET_ACCOUNT, {})
             .get("seen_hashes", [])
    )


def _is_new_post(state: dict, content_hash: str) -> bool:
    """Return True if this hash hasn't been seen before."""
    return content_hash not in _get_seen_hashes(state)


def _mark_seen(state: dict, content_hash: str, url: str) -> None:
    """Add the hash to the seen set and update latest metadata."""
    bucket = state.setdefault(_STATE_KEY, {}).setdefault(TARGET_ACCOUNT, {})
    seen: list[str] = bucket.get("seen_hashes", [])
    if content_hash not in seen:
        seen.append(content_hash)
    # Keep bounded
    bucket["seen_hashes"]  = seen[-_MAX_SEEN_HASHES:]
    bucket["latest_hash"]  = content_hash
    bucket["latest_url"]   = url
    bucket["last_checked"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Code extraction
# ---------------------------------------------------------------------------

def _extract_codes(text: str) -> list[str]:
    """
    Extract promo codes from caption text.
    Uses the required regex r'\\b[A-Z0-9]{4,15}\\b' with a 3-layer filter:
      1. Blacklist rejection
      2. Short pure-alpha tokens (< 9 chars) require promo keyword context
      3. Digit-containing or long tokens accepted freely
    """
    upper = text.upper()
    has_promo_context = bool(_KW_RE.search(text))
    found: set[str] = set()

    for match in _CODE_RE.finditer(upper):
        token = match.group(1)
        if token in _BLACKLIST:
            continue
        if token.isalpha() and len(token) < 9 and not has_promo_context:
            continue
        found.add(token)

    return sorted(found, key=lambda c: (-len(c), c))


# ---------------------------------------------------------------------------
# Browser fingerprint helpers
# ---------------------------------------------------------------------------

# Realistic desktop viewport + user-agent strings
_VIEWPORTS = [
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 800},
]
_USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
]


def _random_choice(items: list, seed: int | None = None):
    import random
    rng = random.Random(seed or int(time.time()))
    return rng.choice(items)


# ---------------------------------------------------------------------------
# Core async scraper
# ---------------------------------------------------------------------------

async def _dismiss_overlays(page) -> None:
    """
    Dismiss common Instagram overlays: cookie banners, login modals,
    notification prompts. Each dismissal is a best-effort — silently ignored
    if the element isn't present.
    """
    # Selector → text to match (case-insensitive)
    _DISMISS_SELECTORS = [
        # Cookie consent (EU)
        "button[data-cookiebanner='accept_button']",
        "button:has-text('Accept All')",
        "button:has-text('Allow all cookies')",
        "button:has-text('Allow essential and optional cookies')",
        # Login / sign-up modals
        "div[role='dialog'] button:has-text('Not Now')",
        "div[role='dialog'] button:has-text('Not now')",
        "div[role='dialog'] button:has-text('Cancel')",
        "div[role='dialog'] svg[aria-label='Close']",
        # Notification prompt
        "button:has-text('Not Now')",
    ]
    for sel in _DISMISS_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=800):
                await btn.click(timeout=800)
                log.debug("[Instagram] Dismissed overlay: %s", sel)
                await page.wait_for_timeout(400)
        except Exception:
            pass  # overlay not present — continue


async def _extract_posts_from_api_response(page, timeout_ms: int) -> list[dict]:
    """
    Intercept Instagram's internal API calls for the profile page
    (web_profile_info endpoint) and extract post data directly from JSON.
    This is more reliable than DOM parsing.

    Returns a list of {shortcode, caption, url} dicts.
    """
    posts: list[dict] = []
    collected_response: list[dict] = []

    async def _on_response(response):
        try:
            url = response.url
            # Instagram profile info endpoint (no auth required for public profiles)
            if (
                "web_profile_info" in url
                or "graphql/query" in url
                and "profile" in url.lower()
            ):
                if response.status == 200:
                    try:
                        body = await response.json()
                        collected_response.append(body)
                    except Exception:
                        pass
        except Exception:
            pass

    page.on("response", _on_response)

    # Navigate and wait for network to settle
    try:
        await page.goto(
            PROFILE_URL,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        # Wait for API calls to fire
        await page.wait_for_timeout(int(RENDER_WAIT * 1000))
    except Exception as exc:
        log.warning("[Instagram] Navigation error: %s", exc)

    # Parse intercepted API responses
    for body in collected_response:
        try:
            # Path varies by Instagram API version
            edges = (
                body.get("data", {})
                    .get("user", {})
                    .get("edge_owner_to_timeline_media", {})
                    .get("edges", [])
            )
            for edge in edges:
                node = edge.get("node", {})
                shortcode = node.get("shortcode", "")
                if not shortcode:
                    continue
                # Caption text
                caption_edges = (
                    node.get("edge_media_to_caption", {})
                        .get("edges", [])
                )
                caption = " ".join(
                    e.get("node", {}).get("text", "")
                    for e in caption_edges
                ).strip()
                post_url = f"https://www.instagram.com/p/{shortcode}/"
                posts.append({
                    "shortcode": shortcode,
                    "caption":   caption,
                    "url":       post_url,
                })
        except Exception as exc:
            log.debug("[Instagram] API parse error: %s", exc)

    return posts


async def _extract_posts_from_dom(page) -> list[dict]:
    """
    Fallback DOM-based extraction.

    Strategy:
    1. Find all post links (href="/p/...") in the profile grid.
    2. For the first MAX_POSTS links, navigate to each post page.
    3. Extract the og:description meta tag (server-rendered, reliable).
    """
    posts: list[dict] = []

    try:
        # Wait for the post grid to appear
        await page.wait_for_selector("a[href*='/p/']", timeout=10000)
    except Exception:
        log.warning("[Instagram] Post grid not found in DOM.")
        return posts

    # Collect unique post links from the grid
    hrefs: list[str] = await page.eval_on_selector_all(
        "a[href*='/p/']",
        "els => [...new Set(els.map(e => e.getAttribute('href')))].slice(0, 10)",
    )
    log.debug("[Instagram] Found %d post links in DOM.", len(hrefs))

    seen_hrefs: set[str] = set()
    for href in hrefs:
        if len(posts) >= MAX_POSTS:
            break
        if href in seen_hrefs:
            continue
        seen_hrefs.add(href)

        post_url = f"https://www.instagram.com{href}"
        try:
            await page.goto(post_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            await _dismiss_overlays(page)

            # Primary: og:description (server-rendered, most reliable)
            og_desc = await page.eval_on_selector_all(
                "meta[property='og:description']",
                "els => els.map(e => e.getAttribute('content'))",
            )
            caption = og_desc[0].strip() if og_desc else ""

            # Secondary: article > div > span caption text
            if not caption:
                try:
                    caption_el = page.locator(
                        "article h1, "
                        "div._a9zs > h1, "
                        "div[class*='caption'] span, "
                        "article div > span"
                    ).first
                    if await caption_el.is_visible(timeout=3000):
                        caption = (await caption_el.inner_text()).strip()
                except Exception:
                    pass

            shortcode = href.rstrip("/").split("/")[-1]
            if caption or shortcode:
                posts.append({
                    "shortcode": shortcode,
                    "caption":   caption,
                    "url":       post_url,
                })
                log.debug("[Instagram] DOM post %s | caption=%d chars", shortcode, len(caption))

            # Back to profile for next post
            await page.go_back(timeout=PAGE_TIMEOUT)
            await page.wait_for_timeout(1200)

        except Exception as exc:
            log.warning("[Instagram] Could not fetch post %s: %s", href, exc)

    return posts


async def _scrape_once(attempt: int) -> list[dict]:
    """
    One full scrape attempt. Returns a list of post dicts or raises.
    """
    from playwright.async_api import async_playwright

    viewport   = _random_choice(_VIEWPORTS,    seed=attempt)
    user_agent = _random_choice(_USER_AGENTS,   seed=attempt + 100)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions",
                "--disable-gpu",
                "--window-size={},{}".format(viewport["width"], viewport["height"]),
            ],
        )

        context = await browser.new_context(
            viewport=viewport,
            user_agent=user_agent,
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;"
                    "q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "Sec-Fetch-Dest":  "document",
                "Sec-Fetch-Mode":  "navigate",
                "Sec-Fetch-Site":  "none",
                "Sec-Fetch-User":  "?1",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        # Spoof navigator.webdriver to avoid bot detection
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });
            window.chrome = { runtime: {} };
        """)

        page = await context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        try:
            # ── Strategy 1: intercept API responses (fastest, most data) ──────
            log.debug("[Instagram] Attempt %d — trying API intercept strategy.", attempt)
            posts = await _extract_posts_from_api_response(page, PAGE_TIMEOUT)

            if posts:
                log.info("[Instagram] API intercept: found %d post(s).", len(posts))
                return posts[:MAX_POSTS]

            # ── Strategy 2: DOM scraping fallback ─────────────────────────────
            log.info("[Instagram] No API data — falling back to DOM scraping.")
            await _dismiss_overlays(page)
            posts = await _extract_posts_from_dom(page)

            log.info("[Instagram] DOM scraping: found %d post(s).", len(posts))
            return posts[:MAX_POSTS]

        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Public async interface
# ---------------------------------------------------------------------------

async def fetch_new_posts(state: dict) -> list[InstagramResult]:
    """
    Scrape @{TARGET_ACCOUNT}, compare against saved hashes,
    and return only genuinely new posts.

    Updates state["instagram"][TARGET_ACCOUNT] in place.
    Caller is responsible for persisting state to disk.
    """
    seen_hashes = _get_seen_hashes(state)
    log.info(
        "[Instagram] Checking @%s (seen=%d hashes, max_posts=%d) ...",
        TARGET_ACCOUNT, len(seen_hashes), MAX_POSTS,
    )

    # Retry loop
    posts: list[dict] = []
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            posts = await _scrape_once(attempt)
            break
        except Exception as exc:
            last_exc = exc
            wait = attempt * 3
            log.warning(
                "[Instagram] Attempt %d/%d failed: %s — retrying in %ds.",
                attempt, MAX_RETRIES, exc, wait,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(wait)

    if not posts and last_exc:
        log.error("[Instagram] All %d attempts failed. Last error: %s", MAX_RETRIES, last_exc)
        return []

    if not posts:
        log.info("[Instagram] No posts found for @%s.", TARGET_ACCOUNT)
        return []

    # Process posts — filter to new ones only
    results: list[InstagramResult] = []

    for post in posts:
        caption  = post.get("caption", "").strip()
        post_url = post.get("url", PROFILE_URL)

        if not caption:
            log.debug("[Instagram] Skipping post with empty caption: %s", post_url)
            continue

        chash = _content_hash(caption)

        if not _is_new_post(state, chash):
            log.debug("[Instagram] Post already seen (hash=%s...): %s", chash[:12], post_url)
            continue

        # New post found!
        codes = _extract_codes(caption)
        _mark_seen(state, chash, post_url)

        log.info(
            "[Instagram] New post detected | url=%s | codes=%s | %.80s",
            post_url, codes, caption,
        )

        results.append(
            InstagramResult(
                source  = "instagram",
                new     = True,
                content = caption,
                codes   = codes,
                url     = post_url,
            )
        )

    log.info("[Instagram] Done — %d new post(s).", len(results))
    return results


# ---------------------------------------------------------------------------
# Sync wrapper (used by main.py run loop)
# ---------------------------------------------------------------------------

async def _run_async(state: dict) -> list[InstagramResult]:
    """Thin coroutine wrapper for asyncio.run()."""
    return await fetch_new_posts(state)


# ---------------------------------------------------------------------------
# BaseMonitor-compatible adapter (plug into main.py MONITORS list)
# ---------------------------------------------------------------------------

class InstagramMonitor:
    """
    Sync adapter around the async Instagram scraper so it slots into the
    main.py MONITORS list alongside the other synchronous monitors.

    asyncio.run() is used to bridge sync -> async cleanly.
    """
    SOURCE_NAME = "Instagram"

    def fetch_new_items(self, state: dict) -> list[dict]:
        """
        Runs the async scraper in a new event loop and maps InstagramResult
        dicts to the FindingResult shape expected by the email notifier.
        """
        from monitors.base_monitor import FindingResult

        try:
            raw: list[InstagramResult] = asyncio.run(_run_async(state))
        except Exception as exc:
            log.error("[Instagram] Unhandled error in async runner: %s", exc, exc_info=True)
            return []

        findings: list[FindingResult] = []
        for r in raw:
            findings.append(
                FindingResult(
                    source  = f"Instagram @{TARGET_ACCOUNT}",
                    item_id = _content_hash(r["content"]),
                    url     = r["url"],
                    author  = f"@{TARGET_ACCOUNT}",
                    title   = r["content"][:120],
                    body    = r["content"],
                    codes   = r["codes"],
                )
            )

        return findings
