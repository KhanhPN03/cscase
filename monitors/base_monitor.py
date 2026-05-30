"""
monitors/base_monitor.py
------------------------
Abstract base class for all platform monitors.

Every monitor must implement `fetch_new_items()` which returns a list of
FindingResult dicts and a `SOURCE_NAME` class attribute.
"""

from __future__ import annotations

import abc
import os
import time
from typing import TypedDict

import requests

from utils.logger import get_logger

log = get_logger("base_monitor")

# How long to wait before retrying a failed HTTP request
_RETRY_DELAYS = (2, 5, 10)  # seconds

# Shared session-level headers that look like a real browser
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class FindingResult(TypedDict):
    """Standardised result returned by every monitor."""
    source: str       # e.g. "Reddit r/CSGOcases"
    item_id: str      # unique identifier used for dedup
    url: str          # direct link to the post/tweet/video
    author: str       # username / channel name
    title: str        # post title or first line
    body: str         # full text (used for code extraction)
    codes: list[str]  # extracted promo codes (may be empty)


class BaseMonitor(abc.ABC):
    """All platform monitors extend this class."""

    SOURCE_NAME: str = "unknown"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(_BROWSER_HEADERS)
        timeout_env = os.getenv("REQUEST_TIMEOUT", "20")
        self.timeout = int(timeout_env)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def fetch_new_items(self, state: dict) -> list[FindingResult]:
        """
        Poll the platform and return a list of new items not yet seen.
        Implementations should:
          1. Call the platform API / scrape the page.
          2. Use `utils.state.is_new()` to filter duplicates.
          3. Use `utils.code_extractor.extract_codes()` to fill `codes`.
          4. Return only genuinely new, potentially code-bearing items.
        """
        ...

    # ------------------------------------------------------------------
    # Helpers available to subclasses
    # ------------------------------------------------------------------

    def _get(self, url: str, **kwargs) -> requests.Response | None:
        """GET with automatic retry. Returns None on permanent failure."""
        for attempt, delay in enumerate([0] + list(_RETRY_DELAYS), start=1):
            if delay:
                log.debug("Retry %d/%d in %ds -> %s", attempt, len(_RETRY_DELAYS) + 1, delay, url)
                time.sleep(delay)
            try:
                resp = self.session.get(url, timeout=self.timeout, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.exceptions.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                if status in (403, 404, 410, 451):
                    log.warning("[%s] HTTP %s for %s — skipping.", self.SOURCE_NAME, status, url)
                    return None
                log.warning("[%s] HTTP %s -> will retry. URL: %s", self.SOURCE_NAME, status, url)
            except requests.exceptions.ConnectionError:
                log.warning("[%s] Connection error -> will retry. URL: %s", self.SOURCE_NAME, url)
            except requests.exceptions.Timeout:
                log.warning("[%s] Timeout -> will retry. URL: %s", self.SOURCE_NAME, url)
            except requests.exceptions.RequestException as exc:
                log.warning("[%s] Request error: %s", self.SOURCE_NAME, exc)
                return None
        log.error("[%s] All retries exhausted for %s", self.SOURCE_NAME, url)
        return None

    def _truncate(self, text: str, max_chars: int = 500) -> str:
        """Trim text and add ellipsis if needed."""
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "…"
