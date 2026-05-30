"""
utils/state.py
--------------
JSON-based state persistence.

State file layout:
{
    "seen_ids": {
        "reddit":        ["post_id_1", ...],
        "twitter":       ["tweet_id_1", ...],
        "csgocases_site": ["code_1", ...]
    },
    "twitter": {
        "csgocasescom": {
            "latest_id":   "1234567890123456789",
            "latest_time": "2024-01-01T00:00:00Z"
        }
    },
    "known_codes": ["CODE1", "CODE2", ...],
    "last_run":    "2024-01-01T00:00:00Z"
}

The file is committed back to the repository by the GitHub Actions workflow
so state persists across cron runs without any external database.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from utils.logger import get_logger

log = get_logger("state")

STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

_DEFAULT: dict[str, Any] = {
    "seen_ids": {},
    "twitter": {},      # keyed by account name, holds latest_id + latest_time
    "known_codes": [],
    "last_run": None,
}


def load() -> dict[str, Any]:
    """Load state from disk. Returns defaults if file is missing or corrupt."""
    if not STATE_FILE.exists():
        log.info("state.json not found — starting with empty state.")
        return _DEFAULT.copy()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        # Ensure all top-level keys exist (forward-compat)
        for k, v in _DEFAULT.items():
            data.setdefault(k, v)
        log.debug("State loaded: %d known codes, %d source buckets.",
                  len(data["known_codes"]), len(data["seen_ids"]))
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not parse state.json (%s) — resetting.", exc)
        return _DEFAULT.copy()


def save(state: dict[str, Any]) -> None:
    """Persist state to disk atomically (write-then-replace)."""
    state["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATE_FILE)
        log.debug("State saved → %s", STATE_FILE)
    except OSError as exc:
        log.error("Failed to save state: %s", exc)


def is_new(state: dict[str, Any], source: str, item_id: str) -> bool:
    """Return True if *item_id* has not been seen before for *source*."""
    bucket = state["seen_ids"].setdefault(source, [])
    if item_id in bucket:
        return False
    bucket.append(item_id)
    # Keep the list bounded to avoid unbounded growth (keep last 500 per source)
    state["seen_ids"][source] = bucket[-500:]
    return True


def register_code(state: dict[str, Any], code: str) -> bool:
    """
    Register a promo code as known.
    Returns True if the code is genuinely new, False if already known.
    """
    normalized = code.strip().upper()
    if normalized in [c.upper() for c in state["known_codes"]]:
        return False
    state["known_codes"].append(normalized)
    return True


# ---------------------------------------------------------------------------
# Twitter / snscrape convenience helpers
# ---------------------------------------------------------------------------

def get_twitter_latest_id(state: dict[str, Any], account: str) -> int:
    """
    Return the most-recently-processed tweet ID for *account* as an int.
    Returns 0 if no tweet has been processed yet (first run).
    """
    try:
        raw = state.get("twitter", {}).get(account, {}).get("latest_id", "0")
        return int(raw)
    except (ValueError, TypeError):
        return 0


def set_twitter_latest_id(
    state: dict[str, Any],
    account: str,
    tweet_id: str | int,
    tweet_time: str | None = None,
) -> None:
    """
    Persist *tweet_id* as the latest processed ID for *account*.

    Parameters
    ----------
    state      : mutable state dict (will be modified in place)
    account    : Twitter handle without @, e.g. "csgocasescom"
    tweet_id   : Twitter snowflake ID (str or int)
    tweet_time : optional ISO-8601 timestamp string for human reference
    """
    from datetime import datetime, timezone as _tz

    state.setdefault("twitter", {}).setdefault(account, {})
    state["twitter"][account]["latest_id"] = str(tweet_id)
    state["twitter"][account]["latest_time"] = (
        tweet_time
        if tweet_time
        else datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    log.debug(
        "[state] twitter.%s.latest_id = %s", account, tweet_id
    )


# ---------------------------------------------------------------------------
# Instagram / Playwright convenience helpers
# ---------------------------------------------------------------------------

_IG_MAX_HASHES = 50   # keep last N content hashes per account


def get_instagram_seen_hashes(state: dict[str, Any], account: str) -> set[str]:
    """
    Return the set of content hashes already seen for *account*.
    Empty set on first run.
    """
    return set(
        state.get("instagram", {})
             .get(account, {})
             .get("seen_hashes", [])
    )


def mark_instagram_post_seen(
    state: dict[str, Any],
    account: str,
    content_hash: str,
    post_url: str,
) -> None:
    """
    Record *content_hash* as seen for *account* and update latest metadata.

    Parameters
    ----------
    state        : mutable state dict (modified in place)
    account      : Instagram handle without @, e.g. "csgocasescom"
    content_hash : SHA-256 hex digest of the normalised caption
    post_url     : direct URL to the Instagram post
    """
    bucket = (
        state
        .setdefault("instagram", {})
        .setdefault(account, {})
    )
    seen: list[str] = bucket.get("seen_hashes", [])
    if content_hash not in seen:
        seen.append(content_hash)
    # Bound the list to avoid unbounded growth
    bucket["seen_hashes"]  = seen[-_IG_MAX_HASHES:]
    bucket["latest_hash"]  = content_hash
    bucket["latest_url"]   = post_url
    bucket["last_checked"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.debug(
        "[state] instagram.%s.latest_hash = %s...", account, content_hash[:12]
    )


# ---------------------------------------------------------------------------
# Facebook / Playwright convenience helpers
# ---------------------------------------------------------------------------

_FB_MAX_HASHES = 50


def get_facebook_seen_hashes(state: dict[str, Any], page: str) -> set[str]:
    """
    Return the set of content hashes already seen for the Facebook *page*.
    Empty set on first run.
    """
    return set(
        state.get("facebook", {})
             .get(page, {})
             .get("seen_hashes", [])
    )


def mark_facebook_post_seen(
    state: dict[str, Any],
    page: str,
    content_hash: str,
    post_url: str,
) -> None:
    """
    Record *content_hash* as seen for Facebook *page* and update metadata.

    Parameters
    ----------
    state        : mutable state dict (modified in place)
    page         : Facebook page slug, e.g. "csgocasescom"
    content_hash : SHA-256 hex digest of the normalised post text
    post_url     : direct link to the Facebook post
    """
    bucket = (
        state
        .setdefault("facebook", {})
        .setdefault(page, {})
    )
    seen: list[str] = bucket.get("seen_hashes", [])
    if content_hash not in seen:
        seen.append(content_hash)
    bucket["seen_hashes"]  = seen[-_FB_MAX_HASHES:]
    bucket["latest_hash"]  = content_hash
    bucket["latest_url"]   = post_url
    bucket["last_checked"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.debug(
        "[state] facebook.%s.latest_hash = %s...", page, content_hash[:12]
    )


# ---------------------------------------------------------------------------
# Discord convenience helpers
# ---------------------------------------------------------------------------

def get_discord_latest_message_id(state: dict[str, Any], channel_id: str) -> str:
    """
    Return the latest processed message snowflake ID for *channel_id*.
    Returns "0" if not yet set (first run).
    """
    return (
        state.get("discord", {})
             .get(channel_id, {})
             .get("latest_message_id", "0")
    )


def set_discord_latest_message_id(
    state: dict[str, Any],
    channel_id: str,
    message_id: str,
    timestamp: str = "",
) -> None:
    """
    Persist the newest processed message ID for *channel_id* into state.

    Parameters
    ----------
    state      : mutable state dict (modified in place)
    channel_id : Discord channel snowflake ID string
    message_id : Discord message snowflake ID string
    timestamp  : ISO-8601 timestamp of the message (optional)
    """
    bucket = (
        state
        .setdefault("discord", {})
        .setdefault(channel_id, {})
    )
    bucket["latest_message_id"] = message_id
    bucket["latest_timestamp"]  = (
        timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    bucket["last_checked"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log.debug(
        "[state] discord.%s.latest_message_id = %s", channel_id, message_id
    )
