"""
monitors/twitter_snscrape_monitor.py
-------------------------------------
Twitter/X monitor for @csgocasescom using snscrape.

Strategy
--------
1. Scrape the latest N tweets from @csgocasescom via snscrape (no API key).
2. Load the last processed tweet ID from state.json
   (state["twitter"]["csgocasescom"]["latest_id"]).
3. Yield only tweets whose ID is strictly greater than the saved ID
   (Twitter snowflake IDs are chronologically ordered integers).
4. Extract promo codes with the user-specified regex:  r'\\b[A-Z0-9]{4,15}\\b'
5. Filter out blacklisted tokens.
6. Return a list of structured result dicts and update state.

Return format (per tweet)
-------------------------
{
    "source":   "twitter",
    "new":      True,
    "post_id":  "1234567890123456789",
    "content":  "full tweet text …",
    "codes":    ["CODE1", "CODE2"],
    "url":      "https://x.com/csgocasescom/status/1234567890123456789"
}

State layout added by this module (merged into state.json)
----------------------------------------------------------
{
    "twitter": {
        "csgocasescom": {
            "latest_id":    "1234567890123456789",
            "latest_time":  "2024-01-01T00:00:00Z"
        }
    }
}

Dependencies
------------
    pip install snscrape

Note on reliability
-------------------
snscrape scrapes Twitter's front-end HTML / guest API. Twitter occasionally
tightens its anti-bot rules, which can cause temporary failures. The module
handles these gracefully: if scraping fails it logs a warning and returns an
empty list so the rest of the monitor pipeline continues unaffected.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import TypedDict

from utils.logger import get_logger

log = get_logger("twitter_snscrape")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Account to watch (without @)
TARGET_ACCOUNT: str = os.getenv("TWITTER_SNSCRAPE_ACCOUNT", "csgocasescom")

# How many of the most-recent tweets to fetch per run (keep low to stay fast)
FETCH_LIMIT: int = int(os.getenv("TWITTER_SNSCRAPE_LIMIT", "20"))

# Promo-code extraction — user-specified pattern
_CODE_RE = re.compile(r"\b([A-Z0-9]{4,15})\b")

# Tokens that are never promo codes
_BLACKLIST: set[str] = {
    # ── User-specified ──────────────────────────────────────────────────────
    "FREE", "CSGO", "CASE", "DAILY",
    # ── Platform & brand names ───────────────────────────────────────────────
    "HTTP", "HTTPS", "HTML", "CSGOCASES", "STEAM", "REDDIT",
    "TWITTER", "YOUTUBE", "TWITCH", "DISCORD", "INSTAGRAM", "TELEGRAM",
    # ── Common promo-context words ───────────────────────────────────────────
    "CODE", "CODES", "PROMO", "BONUS", "REDEEM", "COUPON",
    "DISCOUNT", "OFFER", "DEAL", "LINK", "CLICK", "HERE",
    "USE", "ENTER", "SPIN", "SPINS", "OPEN", "CASES",
    # ── Generic English 4-15 char words ─────────────────────────────────────
    "NULL", "TRUE", "FALSE", "NONE",
    "THIS", "THAT", "WITH", "FROM", "HAVE", "WILL", "BEEN",
    "THEY", "WHEN", "YOUR", "WHAT", "SOME", "MORE", "JUST",
    "POST", "VIEW", "USER", "INFO", "SITE", "ALSO", "EVEN",
    "BACK", "NEXT", "VERY", "ONLY", "THEN", "GAME", "SKIN",
    "ITEM", "KEYS", "RARE", "REAL", "BEST", "GOOD", "NICE",
    "COOL", "HUGE", "LAST", "FAST", "EASY", "HIGH", "MANY",
    "MOST", "LONG", "DOWN", "SIDE", "OVER", "MAKE", "MUCH",
    "COME", "INTO", "GIVE", "KNOW", "TAKE", "WELL", "EACH",
    "SUCH", "BOTH", "ONCE", "LIKE", "TODAY", "DAILY", "EVERY",
    "AGAIN", "RIGHT", "STILL", "FIRST", "ABOUT", "BEING",
    "COULD", "DOING", "FOUND", "GOING", "GREAT", "HELLO",
    "HOURS", "IMAGE", "LATER", "LUCKY", "MIGHT", "NEVER",
    "NIGHT", "OFTEN", "OTHER", "QUITE", "REACH", "REPLY",
    "SHALL", "SINCE", "SMALL", "SORRY", "THEIR", "THERE",
    "THESE", "THINK", "THOSE", "THREE", "UNTIL", "USING",
    "VALID", "VISIT", "WATCH", "WHERE", "WHILE", "WORLD",
    "WOULD", "WRITE", "WRONG", "YEARS", "PRIZE", "ENTER",
    "PROMO", "BONUS", "CLAIM", "CHECK", "SHARE", "FOLLOW",
    "LIKE", "TWEET", "RETWEET", "REPLY", "QUOTE", "THREAD",
    "DROPS", "ITEMS", "SKINS", "CASES", "KNIVES", "GLOVES",
    "TRADE", "TRADE", "STORE", "SHOP", "SITE", "PLAY", "OPEN",
    "LIMITED", "OFFERS", "DEALS", "CODES", "PROMOS", "WINNERS",
    "WINNER", "LUCKY", "BONUS", "EXTRA", "ADDED", "TOTAL",
    "HAPPY", "YEAR", "YEARS", "MONTH", "WEEK", "HOUR", "TIME",
    "AVAILABLE", "DISCOUNT", "SPECIAL", "NOTHING", "SOMETHING",
    "EVERYONE", "GIVEAWAY", "FOLLOWED", "ACCOUNTS", "ANYTHING",
    "FEATURED", "RELEASED", "GENERATE", "INCLUDES", "PASSWORD",
    "REWARD", "REWARDS", "POINT", "POINTS", "COINS", "YOURS",
    "CLAIM", "EARNED", "COLLECT", "REDEEMED", "GIFTED",
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

class TwitterResult(TypedDict):
    source:  str
    new:     bool
    post_id: str
    content: str
    codes:   list[str]
    url:     str


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

_STATE_KEY = "twitter"


def _get_latest_id(state: dict) -> int:
    """Return the saved latest tweet ID as int (0 if not set)."""
    try:
        raw = state.get(_STATE_KEY, {}).get(TARGET_ACCOUNT, {}).get("latest_id", "0")
        return int(raw)
    except (ValueError, TypeError):
        return 0


def _set_latest_id(state: dict, tweet_id: str, tweet_time: datetime) -> None:
    """Persist the newest tweet ID and timestamp into the state dict."""
    state.setdefault(_STATE_KEY, {}).setdefault(TARGET_ACCOUNT, {})
    state[_STATE_KEY][TARGET_ACCOUNT]["latest_id"] = tweet_id
    state[_STATE_KEY][TARGET_ACCOUNT]["latest_time"] = (
        tweet_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        if tweet_time else
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


# ---------------------------------------------------------------------------
# Code extraction
# ---------------------------------------------------------------------------

def _extract_codes(text: str) -> list[str]:
    """
    Apply the regex  r'\\b[A-Z0-9]{4,15}\\b'  to *text* (uppercased),
    then apply these filters in order:

    1. Token must not be in _BLACKLIST.
    2. For purely alphabetic tokens shorter than 8 characters,
       require the surrounding context to contain a promo keyword
       (otherwise too many normal English words pass through).
    3. For longer tokens or tokens with at least one digit, accept freely.

    Returns a deduplicated list sorted by length descending (most specific first).
    """
    upper = text.upper()
    found: set[str] = set()

    # Check if the tweet contains any promo-context keyword
    _KW_RE = re.compile(
        r"(?:promo|bonus|code|coupon|redeem|discount|voucher|offer|use\s+code"
        r"|gift\s+code|free\s+code|enter\s+code)",
        re.IGNORECASE,
    )
    has_promo_context = bool(_KW_RE.search(text))

    for match in _CODE_RE.finditer(upper):
        token = match.group(1)

        # Filter 1 — blacklist
        if token in _BLACKLIST:
            continue

        # Filter 2 — pure-alpha short tokens need promo context
        is_pure_alpha = token.isalpha()
        is_short      = len(token) < 9   # covers 4-8 char English words
        if is_pure_alpha and is_short and not has_promo_context:
            continue

        # Filter 3 — pure-alpha medium tokens (8-15 chars) are fine
        found.add(token)

    return sorted(found, key=lambda c: (-len(c), c))


# ---------------------------------------------------------------------------
# snscrape import (deferred so the module loads even if snscrape is missing)
# ---------------------------------------------------------------------------

def _import_snscrape():
    """
    Import snscrape lazily.  Raises ImportError with a helpful message if not
    installed so the caller can surface a clear error rather than an obscure one.
    """
    try:
        import snscrape.modules.twitter as sntwitter  # type: ignore[import]
        return sntwitter
    except ImportError as exc:
        raise ImportError(
            "snscrape is not installed. Run:  pip install snscrape\n"
            "If that fails try:  pip install git+https://github.com/JustAnotherArchivist/snscrape.git"
        ) from exc


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def fetch_new_tweets(state: dict) -> list[TwitterResult]:
    """
    Scrape @{TARGET_ACCOUNT}, compare with the saved latest tweet ID,
    and return only genuinely new tweets with extracted promo codes.

    Parameters
    ----------
    state : dict
        The mutable state dict loaded from state.json.
        This function updates state["twitter"][TARGET_ACCOUNT] in place
        if new tweets are found — the caller is responsible for saving.

    Returns
    -------
    list[TwitterResult]
        Newest-first list of new tweet results (may be empty).
    """
    try:
        sntwitter = _import_snscrape()
    except ImportError as exc:
        log.error("[Twitter/snscrape] %s", exc)
        return []

    saved_latest_id = _get_latest_id(state)
    log.info(
        "[Twitter/snscrape] Checking @%s (saved latest_id=%s, limit=%d) ...",
        TARGET_ACCOUNT, saved_latest_id or "none", FETCH_LIMIT,
    )

    # ---- Scrape --------------------------------------------------------
    raw_tweets: list = []
    try:
        scraper = sntwitter.TwitterUserScraper(TARGET_ACCOUNT)
        for i, tweet in enumerate(scraper.get_items()):
            if i >= FETCH_LIMIT:
                break
            raw_tweets.append(tweet)
    except Exception as exc:
        log.warning(
            "[Twitter/snscrape] Scraping @%s failed: %s — returning empty.",
            TARGET_ACCOUNT, exc,
        )
        return []

    if not raw_tweets:
        log.info("[Twitter/snscrape] No tweets returned for @%s.", TARGET_ACCOUNT)
        return []

    log.debug("[Twitter/snscrape] Fetched %d tweet(s) from @%s.", len(raw_tweets), TARGET_ACCOUNT)

    # ---- Filter: only tweets newer than saved_latest_id ---------------
    new_tweets = [t for t in raw_tweets if int(t.id) > saved_latest_id]

    if not new_tweets:
        log.info("[Twitter/snscrape] No new tweets since ID %s.", saved_latest_id)
        return []

    log.info("[Twitter/snscrape] %d new tweet(s) detected.", len(new_tweets))

    # ---- Build results ------------------------------------------------
    results: list[TwitterResult] = []
    newest_tweet = None

    for tweet in new_tweets:
        content = tweet.content or tweet.rawContent or ""
        codes   = _extract_codes(content)
        post_id = str(tweet.id)
        url     = f"https://x.com/{TARGET_ACCOUNT}/status/{post_id}"

        log.info(
            "[Twitter/snscrape] New tweet %s | codes=%s | %.80s",
            post_id, codes, content,
        )

        results.append(
            TwitterResult(
                source  = "twitter",
                new     = True,
                post_id = post_id,
                content = content,
                codes   = codes,
                url     = url,
            )
        )

        # Track the very newest tweet to update state
        if newest_tweet is None or int(tweet.id) > int(newest_tweet.id):
            newest_tweet = tweet

    # ---- Update state with newest processed tweet ID ------------------
    if newest_tweet is not None:
        _set_latest_id(state, str(newest_tweet.id), newest_tweet.date)
        log.debug(
            "[Twitter/snscrape] State updated: latest_id=%s", newest_tweet.id
        )

    return results


# ---------------------------------------------------------------------------
# BaseMonitor-compatible adapter (plug into main.py MONITORS list)
# ---------------------------------------------------------------------------

class TwitterSnscrapeMonitor:
    """
    Thin adapter so this module slots into the main.py MONITORS list
    alongside the other BaseMonitor subclasses.
    """
    SOURCE_NAME = "Twitter/X (snscrape)"

    def fetch_new_items(self, state: dict) -> list[dict]:
        """
        Delegates to fetch_new_tweets() and maps TwitterResult ->
        the FindingResult dict shape used by the email notifier.
        """
        from monitors.base_monitor import FindingResult

        raw = fetch_new_tweets(state)
        findings: list[FindingResult] = []

        for r in raw:
            findings.append(
                FindingResult(
                    source  = f"Twitter @{TARGET_ACCOUNT}",
                    item_id = r["post_id"],
                    url     = r["url"],
                    author  = f"@{TARGET_ACCOUNT}",
                    title   = r["content"][:120],
                    body    = r["content"],
                    codes   = r["codes"],
                )
            )

        return findings
