"""
monitors/discord_monitor.py
----------------------------
Discord announcement channel monitor for CSGOCASES.

Honest assessment of Discord monitoring without a paid API
----------------------------------------------------------
Discord does NOT provide public/unauthenticated access to channel messages.
Every working approach requires some form of credentials. Here is the full
menu of options, ordered by legitimacy and simplicity:

  ┌──────────────────────────────────────────────────────────────────────┐
  │  Method          │ Token needed │ ToS-safe │ Reliable │ Setup cost  │
  │──────────────────│──────────────│──────────│──────────│─────────────│
  │ 1. Bot token     │ Bot token    │ ✅ Yes    │ ✅ Best   │ ~5 minutes  │
  │ 2. User token    │ User token   │ ⚠️  No*   │ ✅ Good   │ ~2 minutes  │
  │ 3. RSSHub bridge │ None         │ ✅ Yes    │ ⚠️  Maybe │ 0 minutes   │
  └──────────────────────────────────────────────────────────────────────┘

  * User tokens violate Discord's ToS (Section 5.9) for automated access.
    Use at your own risk — for personal monitoring only.

How to get credentials
-----------------------
  Bot token (recommended):
    1. Go to https://discord.com/developers/applications
    2. Create a new application → Bot → "Reset Token" → copy token
    3. Bot permissions needed: Read Messages / View Channels
    4. Invite URL: https://discord.com/oauth2/authorize?client_id=APP_ID&scope=bot&permissions=66560
    5. Set env var:  DISCORD_BOT_TOKEN=your_token_here

  User token (simple, unofficial):
    1. Open Discord in browser, press F12 → Network tab
    2. Reload, find any request to discord.com/api, copy the "Authorization" header value
    3. Set env var:  DISCORD_USER_TOKEN=your_token_here

  RSSHub (zero-config, best-effort):
    Uses public RSSHub instances to bridge the channel to RSS.
    May not work if the instance doesn't have Discord credentials configured.
    No env var needed — tried automatically if no token is set.

API used (Strategy 1 & 2)
--------------------------
    GET https://discord.com/api/v10/channels/{channel_id}/messages
    Params: after={latest_message_id}&limit=10
    Headers: Authorization: Bot {token}   ← for bot tokens
             Authorization: {token}        ← for user tokens

    Response: JSON array of message objects, newest-first without `after`,
              oldest-first when `after` is set.

Strategy 3 — RSSHub bridge
--------------------------
    GET https://rsshub.app/discord/channel/{guild_id}/{channel_id}
    Tries multiple public instances as fallback.

Return format (per message)
----------------------------
{
    "source":     "discord",
    "new":        True,
    "message_id": "1234567890123456789",
    "content":    "Use promo code WELCOME100 for free cases!",
    "codes":      ["WELCOME100"],
    "url":        "https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
}

State layout (merged into state.json)
--------------------------------------
{
    "discord": {
        "1279122419994595361": {          ← channel ID as key
            "latest_message_id": "0",     ← snowflake ID, "0" = never seen
            "latest_timestamp":  "...",
            "last_checked":      "..."
        }
    }
}

No extra dependencies — uses requests (already in requirements.txt).

GitHub Actions — add to repo secrets
--------------------------------------
    DISCORD_BOT_TOKEN   ← preferred
    DISCORD_USER_TOKEN  ← fallback
"""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import TypedDict

import requests

from utils.logger import get_logger

log = get_logger("discord_monitor")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Guild (server) and channel IDs from the target URL
GUILD_ID:   str = os.getenv("DISCORD_GUILD_ID",   "1263461920791466055")
CHANNEL_ID: str = os.getenv("DISCORD_CHANNEL_ID", "1279122419994595361")

# Tokens — bot token preferred, user token fallback
DISCORD_BOT_TOKEN:  str = os.getenv("DISCORD_BOT_TOKEN",  "")
DISCORD_USER_TOKEN: str = os.getenv("DISCORD_USER_TOKEN", "")

# How many messages to fetch per run
FETCH_LIMIT: int = int(os.getenv("DISCORD_FETCH_LIMIT", "10"))

# HTTP timeout (seconds)
HTTP_TIMEOUT: int = int(os.getenv("DISCORD_TIMEOUT", "20"))

# Max retry attempts on transient errors
MAX_RETRIES: int = int(os.getenv("DISCORD_MAX_RETRIES", "3"))

# Public RSSHub instances to try (in order) when no token is available
RSSHUB_INSTANCES: list[str] = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
    "https://hub.slarker.me",
]

# Discord REST API base
_API_BASE = "https://discord.com/api/v10"

# Promo-code regex
_CODE_RE = re.compile(r"\b([A-Z0-9]{4,15})\b")

# Promo-keyword context detector
_KW_RE = re.compile(
    r"(?:promo|bonus|code|coupon|redeem|discount|voucher|offer"
    r"|use\s+code|gift\s+code|free\s+code|enter\s+code)",
    re.IGNORECASE,
)

# Blocklist — tokens that are never promo codes
_BLACKLIST: set[str] = {
    "FREE", "CSGO", "CASE", "DAILY",
    "HTTP", "HTTPS", "HTML", "CSGOCASES", "STEAM", "REDDIT",
    "TWITTER", "YOUTUBE", "TWITCH", "DISCORD", "INSTAGRAM", "TELEGRAM",
    "FACEBOOK", "MESSENGER", "WHATSAPP", "SNAPCHAT", "TIKTOK",
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
    "DROPS", "ITEMS", "SKINS", "KNIVES", "GLOVES", "TRADE",
    "STORE", "SHOP", "PLAY", "LIMITED", "OFFERS", "DEALS",
    "WINNER", "EXTRA", "ADDED", "TOTAL", "HAPPY", "YEAR",
    "MONTH", "WEEK", "HOUR", "TIME", "AVAILABLE", "SPECIAL",
    "NOTHING", "SOMETHING", "EVERYONE", "GIVEAWAY", "GIVEAWAYS",
    "ANYTHING", "FEATURED", "RELEASED", "PASSWORD",
    "REWARD", "REWARDS", "POINT", "POINTS", "COINS", "YOURS",
    "EARNED", "COLLECT", "REDEEMED", "GIFTED", "CONTEST",
    "CHECKOUT", "PURCHASE", "PAYMENT", "CART", "BASKET",
    "SHIPPING", "DELIVERY", "INVOICE", "RECEIPT", "ORDER",
    "HALF", "PRICE", "PERCENT", "SALE", "SAVE", "SAVING",
    # Discord-specific noise
    "EVERYONE", "HERE", "CHANNEL", "SERVER", "GUILD", "MEMBER",
    "MEMBERS", "ONLINE", "ROLE", "ROLES", "ANNOUNCEMENT", "ANNOUNCE",
    "ANNOUNCEMENTS", "PINNED", "THREAD", "THREADS", "INVITE", "JOIN",
    "JOINED", "BOOST", "BOOSTS", "NITRO", "EMOJI", "STICKER", "REACT",
    "MESSAGE", "MESSAGES", "REPLY", "MENTION", "MENTIONS",
    # Calendar / time words
    "HOLIDAY", "HOLIDAYS", "WEEKEND", "SEASONAL", "WINTER", "SUMMER",
    "SPRING", "AUTUMN", "MONDAY", "FRIDAY", "SUNDAY",
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

class DiscordResult(TypedDict):
    source:     str
    new:        bool
    message_id: str
    content:    str
    codes:      list[str]
    url:        str


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

_STATE_KEY = "discord"


def _get_latest_id(state: dict) -> int:
    """Return the saved latest message snowflake ID as int (0 if not set)."""
    try:
        raw = (
            state.get(_STATE_KEY, {})
                 .get(CHANNEL_ID, {})
                 .get("latest_message_id", "0")
        )
        return int(raw)
    except (ValueError, TypeError):
        return 0


def _set_latest_id(state: dict, message_id: str, timestamp: str = "") -> None:
    """Persist the newest message ID into the state dict."""
    bucket = state.setdefault(_STATE_KEY, {}).setdefault(CHANNEL_ID, {})
    bucket["latest_message_id"] = message_id
    bucket["latest_timestamp"]  = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    bucket["last_checked"]      = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.debug(
        "[state] discord.%s.latest_message_id = %s",
        CHANNEL_ID, message_id,
    )


# ---------------------------------------------------------------------------
# Code extraction
# ---------------------------------------------------------------------------

def _extract_codes(text: str) -> list[str]:
    """
    Extract promo codes from message content using r'\\b[A-Z0-9]{4,15}\\b'.
    3-layer filter:
      1. Blacklist rejection
      2. Pure-alpha tokens < 9 chars require promo-keyword context
      3. Digit-containing or >= 9-char alpha tokens accepted freely
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


def _message_url(message_id: str) -> str:
    return f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/{message_id}"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _auth_headers() -> dict[str, str] | None:
    """
    Return the Authorization header dict for the Discord REST API.
    Returns None if no token is configured.

    Bot tokens:  "Authorization: Bot <token>"
    User tokens: "Authorization: <token>"   (no prefix)
    """
    if DISCORD_BOT_TOKEN:
        log.debug("[Discord] Using bot token authentication.")
        return {
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
            "User-Agent":    "DiscordBot (https://github.com/your/repo, 1.0)",
            "Content-Type":  "application/json",
        }
    if DISCORD_USER_TOKEN:
        log.warning(
            "[Discord] Using user token (self-bot). "
            "This violates Discord ToS — use for personal monitoring only."
        )
        return {
            "Authorization": DISCORD_USER_TOKEN,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/json",
        }
    return None


# ---------------------------------------------------------------------------
# Strategy 1 & 2: Discord REST API
# ---------------------------------------------------------------------------

def _api_get_messages(after_id: int) -> list[dict]:
    """
    Fetch up to FETCH_LIMIT messages from the channel via Discord REST API.

    Parameters
    ----------
    after_id : int
        Snowflake ID — only return messages AFTER this ID (0 = recent history).

    Returns
    -------
    list[dict]
        Raw Discord message objects, or empty list on error.
        Messages are returned oldest-first when after_id > 0.
    """
    headers = _auth_headers()
    if headers is None:
        return []

    params: dict = {"limit": FETCH_LIMIT}
    if after_id > 0:
        params["after"] = str(after_id)

    url = f"{_API_BASE}/channels/{CHANNEL_ID}/messages"
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=HTTP_TIMEOUT,
            )

            # Handle rate limiting (HTTP 429)
            if resp.status_code == 429:
                retry_after = float(resp.json().get("retry_after", 5))
                log.warning(
                    "[Discord] Rate limited — waiting %.1fs (attempt %d/%d).",
                    retry_after, attempt, MAX_RETRIES,
                )
                time.sleep(min(retry_after + 0.5, 30))
                continue

            # 401 / 403 — bad token
            if resp.status_code in (401, 403):
                log.error(
                    "[Discord] API returned HTTP %d — check your token. "
                    "Bot must be in the server and have Read Messages permission.",
                    resp.status_code,
                )
                return []

            # 404 — wrong channel ID
            if resp.status_code == 404:
                log.error(
                    "[Discord] Channel %s not found (HTTP 404). "
                    "Check DISCORD_CHANNEL_ID env var.",
                    CHANNEL_ID,
                )
                return []

            resp.raise_for_status()

            messages = resp.json()
            if not isinstance(messages, list):
                log.warning("[Discord] Unexpected API response type: %s", type(messages))
                return []

            log.info("[Discord] API returned %d message(s).", len(messages))
            return messages

        except requests.Timeout:
            last_exc = TimeoutError(f"Request timed out after {HTTP_TIMEOUT}s")
            log.warning("[Discord] Timeout on attempt %d/%d.", attempt, MAX_RETRIES)
        except requests.RequestException as exc:
            last_exc = exc
            log.warning("[Discord] Request error attempt %d/%d: %s", attempt, MAX_RETRIES, exc)

        if attempt < MAX_RETRIES:
            time.sleep(attempt * 2)

    log.error("[Discord] All %d API attempts failed. Last: %s", MAX_RETRIES, last_exc)
    return []


def _process_api_messages(
    raw_messages: list[dict],
    after_id: int,
    state: dict,
) -> list[DiscordResult]:
    """
    Convert raw Discord API message dicts to DiscordResult objects.
    Filters to only genuinely new messages (ID > after_id).
    Updates state with the highest message ID seen.
    """
    if not raw_messages:
        return []

    # API returns newest-first when no `after` param; oldest-first when `after` is set
    # We want to process oldest-to-newest so state is updated correctly
    if after_id == 0:
        # Without `after`, messages come newest-first — reverse for processing
        raw_messages = list(reversed(raw_messages))

    results: list[DiscordResult] = []
    highest_id = after_id

    for msg in raw_messages:
        msg_id      = msg.get("id", "")
        content     = (msg.get("content") or "").strip()
        author      = msg.get("author", {}).get("username", "unknown")
        timestamp   = msg.get("timestamp", "")
        msg_type    = msg.get("type", 0)

        if not msg_id:
            continue

        # Skip system messages (joins, pins, etc.) — type 0 = regular message
        # type 0 = DEFAULT, type 19 = REPLY — both are normal content
        if msg_type not in (0, 19):
            log.debug("[Discord] Skipping system message type=%d id=%s", msg_type, msg_id)
            continue

        # Skip empty messages (image/embed-only posts with no text)
        # But still process if there are embeds with description
        if not content:
            embeds = msg.get("embeds", [])
            for embed in embeds:
                desc = embed.get("description", "")
                if desc:
                    content = desc
                    break
            if not content:
                log.debug("[Discord] Skipping message with no text content: %s", msg_id)
                continue

        try:
            msg_id_int = int(msg_id)
        except ValueError:
            continue

        if msg_id_int <= after_id:
            log.debug("[Discord] Skipping already-seen message ID %s", msg_id)
            continue

        codes = _extract_codes(content)
        url   = _message_url(msg_id)

        log.info(
            "[Discord] New message %s | author=%s | codes=%s | %.80s",
            msg_id, author, codes, content,
        )

        results.append(
            DiscordResult(
                source     = "discord",
                new        = True,
                message_id = msg_id,
                content    = content,
                codes      = codes,
                url        = url,
            )
        )

        if msg_id_int > highest_id:
            highest_id = msg_id_int

    if highest_id > after_id:
        _set_latest_id(
            state,
            str(highest_id),
            raw_messages[-1].get("timestamp", "") if raw_messages else "",
        )

    return results


# ---------------------------------------------------------------------------
# Strategy 3: RSSHub bridge (no-auth fallback)
# ---------------------------------------------------------------------------

# XML namespaces used in Atom feeds
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _fetch_rsshub() -> list[dict]:
    """
    Try each public RSSHub instance to get the Discord channel as an RSS/Atom feed.

    RSSHub Discord route: /discord/channel/{guild_id}/{channel_id}
    Returns a list of {message_id, content, timestamp} dicts.
    """
    for base in RSSHUB_INSTANCES:
        url = f"{base}/discord/channel/{GUILD_ID}/{CHANNEL_ID}"
        try:
            log.debug("[Discord/RSSHub] Trying %s", url)
            resp = requests.get(
                url,
                timeout=HTTP_TIMEOUT,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; CSGOCasesMonitor/1.0)",
                    "Accept":     "application/rss+xml, application/atom+xml, text/xml, */*",
                },
            )
            if resp.status_code != 200:
                log.debug(
                    "[Discord/RSSHub] %s returned HTTP %d", base, resp.status_code
                )
                continue

            items = _parse_feed(resp.content)
            if items:
                log.info(
                    "[Discord/RSSHub] Got %d item(s) from %s", len(items), base
                )
                return items

        except requests.RequestException as exc:
            log.debug("[Discord/RSSHub] Error from %s: %s", base, exc)
            continue

    log.warning("[Discord/RSSHub] All RSSHub instances failed or returned no items.")
    return []


def _parse_feed(content: bytes) -> list[dict]:
    """
    Parse RSS 2.0 or Atom 1.0 feed bytes into a list of
    {message_id, content, timestamp} dicts.
    """
    items: list[dict] = []

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        log.debug("[Discord/RSSHub] XML parse error: %s", exc)
        return items

    tag = root.tag.lower()

    # ── Atom feed ─────────────────────────────────────────────────────────
    if "atom" in tag or "feed" in tag:
        # Detect namespace prefix
        ns_prefix = ""
        if root.tag.startswith("{"):
            ns_prefix = root.tag.split("}")[0] + "}"

        # Collect entries — try namespaced and non-namespaced tags
        entries = root.findall(f"{ns_prefix}entry")
        if not entries:
            entries = root.findall("entry")   # plain (no namespace)

        for entry in entries:
            # IMPORTANT: ElementTree elements with no child elements evaluate as
            # falsy even when they have text content. Use explicit `is not None`.
            _p = ns_prefix

            def _ef(tag):
                """Find child by namespaced tag, fallback to plain tag."""
                el = entry.find(f"{_p}{tag}")
                return el if el is not None else entry.find(tag)

            content_el = _ef("content")
            if content_el is None:
                content_el = _ef("summary")
            id_el   = _ef("id")
            date_el = _ef("updated")
            if date_el is None:
                date_el = _ef("published")

            text   = (content_el.text or "").strip() if content_el is not None else ""
            raw_id = (id_el.text or "").strip()      if id_el is not None else ""
            ts     = (date_el.text or "").strip()    if date_el is not None else ""

            msg_id = _extract_discord_id_from_url(raw_id)

            if text:
                items.append({
                    "message_id": msg_id,
                    "content":    _strip_html(text),
                    "timestamp":  ts,
                })

    # ── RSS 2.0 feed ──────────────────────────────────────────────────────
    else:
        channel = root.find("channel")
        if channel is None:
            channel = root
        for item in channel.findall("item"):
            desc_el  = item.find("description")
            guid_el  = item.find("guid")
            date_el  = item.find("pubDate")

            text   = (desc_el.text or "").strip() if desc_el is not None else ""
            raw_id = (guid_el.text or "").strip() if guid_el is not None else ""
            ts     = (date_el.text or "").strip() if date_el is not None else ""

            msg_id = _extract_discord_id_from_url(raw_id)

            if text:
                items.append({
                    "message_id": msg_id,
                    "content":    _strip_html(text),
                    "timestamp":  ts,
                })

    return items


def _extract_discord_id_from_url(s: str) -> str:
    """
    Extract a Discord snowflake message ID from a URL or GUID string.
    e.g. 'https://discord.com/channels/guild_id/channel_id/message_id'
    Discord channel URLs have the form /channels/{guild}/{channel}/{message}
    so the MESSAGE snowflake is always the LAST numeric segment.

    Falls back to the whole string if it's already a bare snowflake.
    """
    # Find ALL long numeric segments and take the LAST one (message ID)
    segments = re.findall(r"/(\d{15,20})(?:/|$)", s)
    if segments:
        return segments[-1]   # last segment = message ID in a Discord URL
    # Already a bare snowflake?
    if re.fullmatch(r"\d{15,20}", s):
        return s
    return s  # Unknown format — use as-is


_HTML_TAG_RE    = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|quot|apos|nbsp);")
_HTML_ENTITIES  = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'", "&nbsp;": " "}


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode common HTML entities."""
    text = _HTML_TAG_RE.sub(" ", text)
    text = _HTML_ENTITY_RE.sub(lambda m: _HTML_ENTITIES.get(m.group(0), m.group(0)), text)
    return " ".join(text.split()).strip()


def _process_rsshub_items(
    items: list[dict],
    after_id: int,
    state: dict,
) -> list[DiscordResult]:
    """Convert RSSHub feed items to DiscordResult objects, deduping by ID."""
    results: list[DiscordResult] = []
    highest_id = after_id

    for item in items:
        content  = item.get("content", "").strip()
        msg_id   = item.get("message_id", "")
        ts       = item.get("timestamp", "")

        if not content:
            continue

        # Dedup: if we have a numeric ID, compare against saved
        try:
            msg_id_int = int(msg_id)
            if msg_id_int <= after_id:
                log.debug("[Discord/RSSHub] Skipping seen message %s", msg_id)
                continue
            if msg_id_int > highest_id:
                highest_id = msg_id_int
        except (ValueError, TypeError):
            # Non-numeric ID — use it as-is, no snowflake comparison
            pass

        codes = _extract_codes(content)
        url   = _message_url(msg_id) if re.fullmatch(r"\d+", msg_id) else \
                f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}"

        log.info(
            "[Discord/RSSHub] New message %s | codes=%s | %.80s",
            msg_id, codes, content,
        )

        results.append(
            DiscordResult(
                source     = "discord",
                new        = True,
                message_id = msg_id,
                content    = content,
                codes      = codes,
                url        = url,
            )
        )

    if highest_id > after_id:
        _set_latest_id(state, str(highest_id))

    return results


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def fetch_new_messages(state: dict) -> list[DiscordResult]:
    """
    Check the Discord announcement channel for new messages and return
    only genuinely new ones (i.e., message_id > saved latest_message_id).

    Tries strategies in order:
      1. Discord REST API with bot token (most reliable)
      2. Discord REST API with user token (ToS violation)
      3. RSSHub public bridge (no-auth fallback, may not work)

    Updates state["discord"][CHANNEL_ID] in place.
    Caller is responsible for persisting state.json to disk.
    """
    after_id = _get_latest_id(state)
    log.info(
        "[Discord] Checking channel=%s guild=%s (after_id=%s, limit=%d) ...",
        CHANNEL_ID, GUILD_ID, after_id or "none", FETCH_LIMIT,
    )

    # ── Strategy 1 & 2: REST API (bot or user token) ─────────────────────
    headers = _auth_headers()
    if headers is not None:
        raw = _api_get_messages(after_id)
        if raw is not None:  # None = fatal auth error, [] = no new messages
            results = _process_api_messages(raw, after_id, state)
            log.info("[Discord] REST API: %d new message(s).", len(results))
            return results
    else:
        log.warning(
            "[Discord] No token configured (DISCORD_BOT_TOKEN or DISCORD_USER_TOKEN). "
            "Falling back to RSSHub bridge. Set a token for reliable results."
        )

    # ── Strategy 3: RSSHub fallback ───────────────────────────────────────
    log.info("[Discord] Attempting RSSHub bridge fallback ...")
    items = _fetch_rsshub()
    results = _process_rsshub_items(items, after_id, state)
    log.info("[Discord] RSSHub: %d new message(s).", len(results))
    return results


# ---------------------------------------------------------------------------
# BaseMonitor-compatible adapter
# ---------------------------------------------------------------------------

class DiscordMonitor:
    """
    Sync adapter so this module slots directly into the main.py MONITORS list.
    Uses only the stdlib `requests` library — no async/Playwright needed.
    """
    SOURCE_NAME = "Discord"

    def fetch_new_items(self, state: dict) -> list[dict]:
        """
        Delegates to fetch_new_messages() and maps DiscordResult ->
        the FindingResult shape expected by the email notifier.
        """
        from monitors.base_monitor import FindingResult

        try:
            raw = fetch_new_messages(state)
        except Exception as exc:
            log.error("[Discord] Unhandled error: %s", exc, exc_info=True)
            return []

        findings: list[FindingResult] = []
        for r in raw:
            findings.append(
                FindingResult(
                    source  = f"Discord #{CHANNEL_ID}",
                    item_id = r["message_id"],
                    url     = r["url"],
                    author  = "Discord Announcement",
                    title   = r["content"][:120],
                    body    = r["content"],
                    codes   = r["codes"],
                )
            )

        return findings
