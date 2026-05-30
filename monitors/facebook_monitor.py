"""
monitors/facebook_monitor.py
-----------------------------
Facebook page monitor for @csgocasescom using async Playwright.

Why Facebook is Different
--------------------------
Facebook aggressively redirects unauthenticated users to login and uses
generated CSS class names that change daily. This module uses a layered
strategy to maximise the chance of extracting post text without a login:

  Strategy 1 — Mobile site (m.facebook.com)
    The mobile Facebook site renders simpler HTML and is more lenient
    with unauthenticated requests. Posts appear in <article> or <div>
    elements with stable data attributes.

  Strategy 2 — Desktop site with og:description fallback
    Navigate the regular page and extract the og:description meta tag,
    which Facebook populates server-side with the page/latest post
    description — often contains the most recent post text.

  Strategy 3 — GraphQL API intercept
    Intercept Facebook's internal __api/__graphql__ responses that fire
    when the timeline loads and parse story text from JSON blobs.

Return format (per post)
------------------------
{
    "source":  "facebook",
    "new":     True,
    "content": "post text …",
    "codes":   ["CODE1", "CODE2"],
    "url":     "https://www.facebook.com/csgocasescom/posts/<id>"
}

State layout (merged into state.json)
--------------------------------------
{
    "facebook": {
        "csgocasescom": {
            "seen_hashes":  ["sha256...", ...],   # last 50
            "latest_hash":  "sha256hexdigest",
            "latest_url":   "https://www.facebook.com/...",
            "last_checked": "2024-01-01T00:00:00Z"
        }
    }
}

Dependencies (already in requirements.txt)
------------------------------------------
    playwright >= 1.44.0
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

log = get_logger("facebook_monitor")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_PAGE: str    = os.getenv("FACEBOOK_PAGE", "csgocasescom")
DESKTOP_URL: str    = f"https://www.facebook.com/{TARGET_PAGE}/"
MOBILE_URL:  str    = f"https://m.facebook.com/{TARGET_PAGE}/"

# Max posts to return per run
MAX_POSTS:   int    = int(os.getenv("FACEBOOK_MAX_POSTS",   "5"))

# Page navigation timeout (ms)
PAGE_TIMEOUT: int   = int(os.getenv("FACEBOOK_TIMEOUT_MS", "30000"))

# Seconds to wait for the feed to render after DOM load
RENDER_WAIT: float  = float(os.getenv("FACEBOOK_RENDER_WAIT", "4"))

# Max retry attempts per run
MAX_RETRIES: int    = int(os.getenv("FACEBOOK_MAX_RETRIES", "3"))

# Promo-code regex — `r'\b[A-Z0-9]{4,15}\b'`
_CODE_RE = re.compile(r"\b([A-Z0-9]{4,15})\b")

# Promo-keyword context detector
_KW_RE = re.compile(
    r"(?:promo|bonus|code|coupon|redeem|discount|voucher|offer"
    r"|use\s+code|gift\s+code|free\s+code|enter\s+code)",
    re.IGNORECASE,
)

# Blocklist — tokens that are never promo codes
_BLACKLIST: set[str] = {
    # User-specified
    "FREE", "CSGO", "CASE", "DAILY",
    # Platform / brand names
    "HTTP", "HTTPS", "HTML", "CSGOCASES", "STEAM", "REDDIT",
    "TWITTER", "YOUTUBE", "TWITCH", "DISCORD", "INSTAGRAM", "TELEGRAM",
    "FACEBOOK", "MESSENGER", "WHATSAPP", "SNAPCHAT", "TIKTOK",
    # Common promo-context words (not actual codes)
    "CODE", "CODES", "PROMO", "BONUS", "REDEEM", "COUPON",
    "DISCOUNT", "OFFER", "DEAL", "LINK", "CLICK", "HERE",
    "USE", "ENTER", "SPIN", "SPINS", "OPEN", "CASES",
    # Generic English words that match the regex
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
    "PLAY", "LIMITED", "OFFERS", "DEALS", "PROMOS", "WINNER",
    "EXTRA", "ADDED", "TOTAL", "HAPPY", "YEAR", "MONTH",
    "WEEK", "HOUR", "TIME", "AVAILABLE", "SPECIAL", "NOTHING",
    "SOMETHING", "EVERYONE", "GIVEAWAY", "GIVEAWAYS", "ANYTHING",
    "FEATURED", "RELEASED", "GENERATE", "INCLUDES", "PASSWORD",
    "REWARD", "REWARDS", "POINT", "POINTS", "COINS", "YOURS",
    "EARNED", "COLLECT", "REDEEMED", "GIFTED", "CONTEST",
    "CHECKOUT", "PURCHASE", "PAYMENT", "CART", "BASKET",
    "SHIPPING", "DELIVERY", "INVOICE", "RECEIPT", "ORDER",
    # Discount descriptor words — often appear near promo codes
    "HALF", "PRICE", "PERCENT", "SALE", "SAVE", "SAVING",
    "SAVINGS", "REDUCE", "SLASH", "LOWER", "MARK",
    # Facebook-specific noise
    "LIKE", "COMMENT", "SHARE", "SAVE", "REACT", "REACTIONS",
    "PROFILE", "PAGE", "GROUP", "EVENT", "STORY", "REEL",
    "WATCH", "MARKETPLACE", "FRIENDS", "PEOPLE", "MEMORY",
    "PHOTOS", "VIDEOS", "TAGGED", "MENTION", "TIMELINE",
    "FACEBOOK", "MESSENGER", "NOTIFICATION", "SUGGESTED",
    "SPONSORED", "MANAGE", "ABOUT", "COMMUNITY", "REVIEWS",
}

# Minimum content length — ignore near-empty posts (UI text noise)
_MIN_CONTENT_LEN = 15


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

class FacebookResult(TypedDict):
    source:  str
    new:     bool
    content: str
    codes:   list[str]
    url:     str


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

_STATE_KEY       = "facebook"
_MAX_SEEN_HASHES = 50


def _content_hash(text: str) -> str:
    """SHA-256 of whitespace-normalised, lowercased post text."""
    normalised = " ".join(text.split()).strip().lower()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _get_seen_hashes(state: dict) -> set[str]:
    return set(
        state.get(_STATE_KEY, {})
             .get(TARGET_PAGE, {})
             .get("seen_hashes", [])
    )


def _is_new_post(state: dict, content_hash: str) -> bool:
    return content_hash not in _get_seen_hashes(state)


def _mark_seen(state: dict, content_hash: str, url: str) -> None:
    bucket = state.setdefault(_STATE_KEY, {}).setdefault(TARGET_PAGE, {})
    seen: list[str] = bucket.get("seen_hashes", [])
    if content_hash not in seen:
        seen.append(content_hash)
    bucket["seen_hashes"]  = seen[-_MAX_SEEN_HASHES:]
    bucket["latest_hash"]  = content_hash
    bucket["latest_url"]   = url
    bucket["last_checked"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Code extraction
# ---------------------------------------------------------------------------

def _extract_codes(text: str) -> list[str]:
    """
    Extract promo codes using r'\\b[A-Z0-9]{4,15}\\b' with a 3-layer filter:
      1. Blacklist rejection
      2. Pure-alpha tokens < 9 chars require promo-keyword context
      3. Digit-containing or >= 9-char alpha tokens accepted freely
    Returns deduplicated list sorted longest-first.
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
# Browser fingerprinting
# ---------------------------------------------------------------------------

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

_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)

_STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
    window.chrome = { runtime: {} };
"""


def _pick(items: list, seed: int):
    import random
    return random.Random(seed).choice(items)


# ---------------------------------------------------------------------------
# Overlay / modal dismissal
# ---------------------------------------------------------------------------

async def _dismiss_overlays(page) -> None:
    """
    Best-effort dismissal of Facebook cookie banners, login walls,
    notification prompts, and age gates. Silently swallows all errors.
    """
    _SELECTORS = [
        # Cookie consent — EU / global
        "button[data-cookiebanner='accept_button']",
        "button[title='Accept All']",
        "button[title='Allow all cookies']",
        "[data-testid='cookie-policy-manage-dialog-accept-button']",
        "div[aria-label='Allow all cookies'] > div > div",
        # Playwright has-text pseudo
        "button:has-text('Accept All')",
        "button:has-text('Allow all cookies')",
        "button:has-text('Only allow essential cookies')",  # secondary option
        "button:has-text('Accept cookies')",
        # Login / registration walls
        "div[role='dialog'] div[aria-label='Close']",
        "div[role='dialog'] button:has-text('Not Now')",
        "div[role='dialog'] button:has-text('Not now')",
        "div[role='dialog'] button:has-text('Cancel')",
        # Notification prompt
        "button:has-text('Not Now')",
        # Age verification
        "button:has-text('Confirm')",
    ]
    for sel in _SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=700):
                await btn.click(timeout=700)
                log.debug("[Facebook] Dismissed overlay: %s", sel[:60])
                await page.wait_for_timeout(350)
        except Exception:
            pass


def _is_login_page(url: str) -> bool:
    """Return True if Facebook redirected us to a login/checkpoint page."""
    blocked = ("login", "checkpoint", "recover", "disabled", "suspended")
    return any(kw in url.lower() for kw in blocked)


# ---------------------------------------------------------------------------
# Strategy 1 — Mobile site (m.facebook.com)
# ---------------------------------------------------------------------------

async def _scrape_mobile(page) -> list[dict]:
    """
    Scrape the mobile Facebook site.
    Mobile Facebook renders simpler, more stable HTML suitable for extraction.

    Post selectors tried (in order of reliability):
      - <article> elements (most stable semantic tag)
      - <div data-ft=...> elements (Facebook tracking attr, contains post data)
      - <div class="story_body_container"> (classic mobile layout)
    """
    posts: list[dict] = []

    try:
        await page.goto(MOBILE_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    except Exception as exc:
        log.warning("[Facebook/mobile] Navigation error: %s", exc)
        return posts

    # Dismiss cookie/login overlays
    await _dismiss_overlays(page)

    # If redirected to login, bail out
    if _is_login_page(page.url):
        log.warning("[Facebook/mobile] Redirected to login: %s", page.url)
        return posts

    # Wait for content to settle
    await page.wait_for_timeout(int(RENDER_WAIT * 1000))

    # --- Attempt A: <article> elements ---
    try:
        articles = await page.query_selector_all("article")
        if articles:
            log.debug("[Facebook/mobile] Found %d <article> element(s).", len(articles))
            for art in articles[:MAX_POSTS]:
                try:
                    text = (await art.inner_text()).strip()
                    # Get the closest post link
                    link_el = await art.query_selector("a[href*='/story.php'], a[href*='/posts/'], a[href*='?story_fbid=']")
                    href = await link_el.get_attribute("href") if link_el else ""
                    url  = _normalise_fb_url(href) or MOBILE_URL
                    if len(text) >= _MIN_CONTENT_LEN:
                        posts.append({"content": _clean_fb_text(text), "url": url})
                except Exception:
                    pass
    except Exception as exc:
        log.debug("[Facebook/mobile] Article scan error: %s", exc)

    if posts:
        return posts

    # --- Attempt B: data-ft attribute containers ---
    try:
        ft_divs = await page.query_selector_all("div[data-ft]")
        log.debug("[Facebook/mobile] Found %d data-ft div(s).", len(ft_divs))
        for div in ft_divs[:MAX_POSTS * 2]:
            try:
                text = (await div.inner_text()).strip()
                if len(text) < _MIN_CONTENT_LEN:
                    continue
                # Only include if it looks like post content (not nav/sidebar)
                if not _looks_like_post(text):
                    continue
                posts.append({"content": _clean_fb_text(text), "url": MOBILE_URL})
                if len(posts) >= MAX_POSTS:
                    break
            except Exception:
                pass
    except Exception as exc:
        log.debug("[Facebook/mobile] data-ft scan error: %s", exc)

    if posts:
        return posts

    # --- Attempt C: classic story_body_container ---
    try:
        containers = await page.query_selector_all(
            "div.story_body_container, div[class*='_5rgt'], div[class*='userContent']"
        )
        log.debug("[Facebook/mobile] Found %d story containers.", len(containers))
        for c in containers[:MAX_POSTS]:
            try:
                text = (await c.inner_text()).strip()
                if len(text) >= _MIN_CONTENT_LEN and _looks_like_post(text):
                    posts.append({"content": _clean_fb_text(text), "url": MOBILE_URL})
            except Exception:
                pass
    except Exception as exc:
        log.debug("[Facebook/mobile] story_body scan error: %s", exc)

    return posts


# ---------------------------------------------------------------------------
# Strategy 2 — Desktop site + GraphQL intercept
# ---------------------------------------------------------------------------

async def _scrape_desktop(page) -> list[dict]:
    """
    Scrape the desktop Facebook site.

    Sub-strategies:
      A. Intercept __api / graphql responses → parse story text from JSON
      B. DOM: [role='article'] → div[data-ad-preview] or deepest text spans
      C. og:description meta tag (server-rendered page description)
    """
    posts: list[dict] = []
    graphql_texts: list[str] = []

    # ── GraphQL interception ──────────────────────────────────────────────
    async def _on_response(response):
        try:
            url = response.url
            if (
                "graphql" in url
                or "__api" in url
                or "api/graphql" in url
            ) and response.status == 200:
                content_type = response.headers.get("content-type", "")
                if "json" in content_type or "javascript" in content_type:
                    try:
                        text = await response.text()
                        _parse_graphql_texts(text, graphql_texts)
                    except Exception:
                        pass
        except Exception:
            pass

    page.on("response", _on_response)

    # Navigate
    try:
        await page.goto(DESKTOP_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    except Exception as exc:
        log.warning("[Facebook/desktop] Navigation error: %s", exc)

    await _dismiss_overlays(page)

    if _is_login_page(page.url):
        log.warning("[Facebook/desktop] Redirected to login: %s", page.url)
        return posts

    await page.wait_for_timeout(int(RENDER_WAIT * 1000))
    await _dismiss_overlays(page)  # second pass after render

    # ── Sub-strategy A: use intercepted GraphQL texts ─────────────────────
    if graphql_texts:
        log.debug("[Facebook/desktop] Got %d text fragment(s) from GraphQL.", len(graphql_texts))
        for text in graphql_texts[:MAX_POSTS]:
            if len(text) >= _MIN_CONTENT_LEN and _looks_like_post(text):
                posts.append({"content": _clean_fb_text(text), "url": DESKTOP_URL})
        if posts:
            log.info("[Facebook/desktop] GraphQL intercept: %d post(s).", len(posts))
            return posts

    # ── Sub-strategy B: DOM [role='article'] ─────────────────────────────
    try:
        articles = await page.query_selector_all("[role='article']")
        log.debug("[Facebook/desktop] Found %d [role=article] element(s).", len(articles))

        for art in articles[:MAX_POSTS * 2]:
            try:
                # Try the data-ad-preview div first (FB's own post preview attr)
                msg_el = await art.query_selector(
                    "div[data-ad-preview='message'], "
                    "div[data-testid='post_message'], "
                    "div[data-testid='story-subtitle'], "
                    "div[class*='ecm0bbzt']"  # common in FB's obfuscated classes
                )
                if msg_el:
                    text = (await msg_el.inner_text()).strip()
                else:
                    # Fallback: full article text minus UI chrome
                    text = (await art.inner_text()).strip()

                # Post link
                link_el = await art.query_selector(
                    "a[href*='/posts/'], a[href*='story_fbid='], "
                    "a[href*='/permalink/'], a[aria-label*='comment']"
                )
                href = await link_el.get_attribute("href") if link_el else ""
                url  = _normalise_fb_url(href) or DESKTOP_URL

                if len(text) >= _MIN_CONTENT_LEN and _looks_like_post(text):
                    posts.append({"content": _clean_fb_text(text), "url": url})
                    if len(posts) >= MAX_POSTS:
                        break
            except Exception:
                pass
    except Exception as exc:
        log.debug("[Facebook/desktop] article DOM scan error: %s", exc)

    if posts:
        return posts

    # ── Sub-strategy C: og:description meta fallback ──────────────────────
    try:
        og = await page.eval_on_selector_all(
            "meta[property='og:description']",
            "els => els.map(e => e.getAttribute('content'))",
        )
        if og and og[0]:
            text = og[0].strip()
            if len(text) >= _MIN_CONTENT_LEN:
                log.info("[Facebook/desktop] og:description fallback: %d chars.", len(text))
                posts.append({"content": _clean_fb_text(text), "url": DESKTOP_URL})
    except Exception as exc:
        log.debug("[Facebook/desktop] og:description error: %s", exc)

    return posts


# ---------------------------------------------------------------------------
# GraphQL JSON text parser
# ---------------------------------------------------------------------------

# Patterns that indicate a JSON string value is actual post text
_TEXT_PAYLOAD_RE = re.compile(
    r'"(?:message|text|body|story_text|primary_text|description)"'
    r'\s*:\s*\{\s*"text"\s*:\s*"([^"]{15,})"',
    re.DOTALL,
)
_TEXT_INLINE_RE = re.compile(
    r'"(?:message|text|story|body)"\s*:\s*"([^"\\]{15,}(?:\\.[^"\\]*)*)"'
)


def _parse_graphql_texts(raw: str, out: list[str]) -> None:
    """
    Extract candidate post text strings from a Facebook GraphQL JSON response.
    Appends results to *out* in-place.
    """
    for pattern in (_TEXT_PAYLOAD_RE, _TEXT_INLINE_RE):
        for match in pattern.finditer(raw):
            text = match.group(1)
            # Unescape JSON string escapes
            try:
                text = bytes(text, "utf-8").decode("unicode_escape")
            except Exception:
                pass
            text = text.strip()
            if text and text not in out:
                out.append(text)


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

# Noise phrases injected by Facebook's UI that should be stripped
_FB_UI_NOISE = re.compile(
    r"(?:"
    r"Like\s*·\s*Comment\s*·\s*Share"
    r"|View\s+\d+\s+comment"
    r"|Write\s+a\s+comment"
    r"|\d+\s+(?:likes?|reactions?|comments?|shares?)"
    r"|See\s+(?:More|All|Translation)"
    r"|Translated\s+by\s+Facebook"
    r"|Manage\s+Page"
    r"|Follow\s+Page"
    r"|Send\s+Message"
    r"|This\s+Page\s+is\s+unresponsive"
    r")",
    re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"\s{2,}")


def _clean_fb_text(text: str) -> str:
    """Strip Facebook UI noise and normalise whitespace."""
    text = _FB_UI_NOISE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _looks_like_post(text: str) -> bool:
    """
    Heuristic to decide if a block of text looks like a genuine post
    vs. navigation chrome / sidebar / UI labels.
    """
    # Must be long enough
    if len(text) < _MIN_CONTENT_LEN:
        return False
    # Reject pure UI noise (all noise words)
    upper = text.upper()
    words = set(re.findall(r"[A-Z]+", upper))
    if words and words.issubset(_BLACKLIST):
        return False
    return True


def _normalise_fb_url(href: str) -> str:
    """
    Turn a relative or mangled Facebook URL into an absolute desktop URL.
    Strips tracking parameters.
    """
    if not href:
        return ""
    # Strip query tracking
    href = re.sub(r"\?(?:__cft__|__tn__|ref|refid|_rdc|_rdr)[^#]*", "", href)
    if href.startswith("https://"):
        return href
    if href.startswith("/"):
        return "https://www.facebook.com" + href
    return href


# ---------------------------------------------------------------------------
# Main scrape attempt (tries mobile then desktop)
# ---------------------------------------------------------------------------

async def _scrape_once(attempt: int) -> list[dict]:
    """
    One full scrape attempt. Tries mobile first, falls back to desktop.
    Returns a list of {content, url} dicts or raises.
    """
    from playwright.async_api import async_playwright

    viewport   = _pick(_VIEWPORTS,    seed=attempt)
    desktop_ua = _pick(_USER_AGENTS,  seed=attempt + 42)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-gpu",
                "--mute-audio",
                "--window-size={},{}".format(viewport["width"], viewport["height"]),
            ],
        )

        try:
            # ── Mobile attempt ────────────────────────────────────────────────
            log.debug("[Facebook] Attempt %d — trying mobile site.", attempt)
            mobile_ctx = await browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent=_MOBILE_UA,
                locale="en-US",
                timezone_id="America/New_York",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                },
            )
            await mobile_ctx.add_init_script(_STEALTH_SCRIPT)
            mobile_page = await mobile_ctx.new_page()
            mobile_page.set_default_timeout(PAGE_TIMEOUT)

            try:
                posts = await _scrape_mobile(mobile_page)
                if posts:
                    log.info("[Facebook] Mobile strategy: %d post(s) found.", len(posts))
                    return posts[:MAX_POSTS]
            finally:
                await mobile_ctx.close()

            # ── Desktop attempt ───────────────────────────────────────────────
            log.info("[Facebook] Mobile returned nothing — trying desktop site.")
            desktop_ctx = await browser.new_context(
                viewport=viewport,
                user_agent=desktop_ua,
                locale="en-US",
                timezone_id="America/New_York",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,image/avif,image/webp,*/*;q=0.8"
                    ),
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            await desktop_ctx.add_init_script(_STEALTH_SCRIPT)
            desktop_page = await desktop_ctx.new_page()
            desktop_page.set_default_timeout(PAGE_TIMEOUT)

            try:
                posts = await _scrape_desktop(desktop_page)
                log.info("[Facebook] Desktop strategy: %d post(s) found.", len(posts))
                return posts[:MAX_POSTS]
            finally:
                await desktop_ctx.close()

        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Public async interface
# ---------------------------------------------------------------------------

async def fetch_new_posts(state: dict) -> list[FacebookResult]:
    """
    Scrape the CSGOCASES Facebook page, compare against saved content hashes,
    and return only genuinely new posts.

    Updates state["facebook"][TARGET_PAGE] in-place.
    Caller is responsible for persisting state.json.
    """
    seen_hashes = _get_seen_hashes(state)
    log.info(
        "[Facebook] Checking page=%s (seen=%d hashes, max=%d) ...",
        TARGET_PAGE, len(seen_hashes), MAX_POSTS,
    )

    posts: list[dict] = []
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            posts = await _scrape_once(attempt)
            break
        except Exception as exc:
            last_exc = exc
            wait = attempt * 4
            log.warning(
                "[Facebook] Attempt %d/%d failed: %s — retrying in %ds.",
                attempt, MAX_RETRIES, exc, wait,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(wait)

    if not posts and last_exc:
        log.error("[Facebook] All %d attempts failed. Last: %s", MAX_RETRIES, last_exc)
        return []

    if not posts:
        log.info("[Facebook] No posts found for page=%s.", TARGET_PAGE)
        return []

    # ── Dedup and build results ───────────────────────────────────────────
    results: list[FacebookResult] = []

    for post in posts:
        content = post.get("content", "").strip()
        url     = post.get("url", DESKTOP_URL)

        if not content or len(content) < _MIN_CONTENT_LEN:
            continue

        chash = _content_hash(content)

        if not _is_new_post(state, chash):
            log.debug("[Facebook] Already seen (hash=%s...): %.60s", chash[:12], content)
            continue

        codes = _extract_codes(content)
        _mark_seen(state, chash, url)

        log.info(
            "[Facebook] New post | codes=%s | url=%s | %.80s",
            codes, url, content,
        )

        results.append(
            FacebookResult(
                source  = "facebook",
                new     = True,
                content = content,
                codes   = codes,
                url     = url,
            )
        )

    log.info("[Facebook] Done — %d new post(s).", len(results))
    return results


# ---------------------------------------------------------------------------
# Sync wrapper
# ---------------------------------------------------------------------------

async def _run_async(state: dict) -> list[FacebookResult]:
    return await fetch_new_posts(state)


# ---------------------------------------------------------------------------
# BaseMonitor-compatible adapter
# ---------------------------------------------------------------------------

class FacebookMonitor:
    """
    Sync adapter so this module slots directly into the main.py MONITORS list.
    asyncio.run() bridges the sync caller into the async scraper.
    """
    SOURCE_NAME = "Facebook"

    def fetch_new_items(self, state: dict) -> list[dict]:
        """
        Runs the async scraper and maps FacebookResult -> FindingResult
        for the email notifier.
        """
        from monitors.base_monitor import FindingResult

        try:
            raw: list[FacebookResult] = asyncio.run(_run_async(state))
        except Exception as exc:
            log.error("[Facebook] Async runner error: %s", exc, exc_info=True)
            return []

        findings: list[FindingResult] = []
        for r in raw:
            findings.append(
                FindingResult(
                    source  = f"Facebook @{TARGET_PAGE}",
                    item_id = _content_hash(r["content"]),
                    url     = r["url"],
                    author  = f"@{TARGET_PAGE}",
                    title   = r["content"][:120],
                    body    = r["content"],
                    codes   = r["codes"],
                )
            )

        return findings
