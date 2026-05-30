"""
monitors/csgocases_site_monitor.py
----------------------------------
Directly scrapes the CSGOCASES website for active promo codes.

Targets:
  1. /promocodes  or /bonus  page (if it exists publicly)
  2. The site's homepage for any visible promo banner
  3. A lightweight HTML parse looking for code-like strings

No API key required — plain HTTP GET + BeautifulSoup.
"""

from __future__ import annotations

import hashlib
import re

from bs4 import BeautifulSoup

from monitors.base_monitor import BaseMonitor, FindingResult
from utils.code_extractor import extract_codes
from utils.logger import get_logger
from utils.state import is_new

log = get_logger("site_monitor")

_BASE_URL = "https://csgocases.com"

# Pages to check on the site
_PAGES: list[tuple[str, str]] = [
    ("/",          "Homepage"),
    ("/promocodes","Promo Codes Page"),
    ("/bonus",     "Bonus Page"),
    ("/free",      "Free Page"),
]

# CSS selectors that commonly wrap promo code displays
_CODE_SELECTORS = [
    "[class*='promo']",
    "[class*='code']",
    "[class*='bonus']",
    "[class*='coupon']",
    "[class*='discount']",
    "[data-code]",
    "[data-promo]",
    "input[value]",       # copy-paste input boxes
    ".alert",
    ".banner",
    ".notification",
]

# Regex for inline code-like tokens
_INLINE_CODE_RE = re.compile(r'\b([A-Z][A-Z0-9_\-]{3,19})\b')


def _extract_from_soup(soup: BeautifulSoup) -> list[str]:
    """Pull candidate codes from a parsed BeautifulSoup page."""
    candidates: set[str] = set()

    # From targeted selectors
    for selector in _CODE_SELECTORS:
        try:
            for el in soup.select(selector):
                text = el.get_text(" ", strip=True)
                val  = el.get("value", "") or el.get("data-code", "") or el.get("data-promo", "")
                for raw in (text, val):
                    if raw:
                        candidates.update(extract_codes(raw))
        except Exception:
            pass

    # From all visible text
    page_text = soup.get_text(" ", strip=True)
    candidates.update(extract_codes(page_text))

    return list(candidates)


class CSGOCasesSiteMonitor(BaseMonitor):
    SOURCE_NAME = "CSGOCASES Website"

    def fetch_new_items(self, state: dict) -> list[FindingResult]:
        findings: list[FindingResult] = []

        for path, label in _PAGES:
            url = _BASE_URL + path
            log.info("[Site] Checking %s (%s) …", label, url)

            resp = self._get(url)
            if resp is None:
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            # Remove script / style noise
            for tag in soup(["script", "style", "noscript", "iframe"]):
                tag.decompose()

            codes = _extract_from_soup(soup)
            if not codes:
                log.debug("[Site] No codes found on %s", label)
                continue

            # Create a stable fingerprint of the found code set for dedup
            codes_sorted = sorted(set(codes))
            page_fingerprint = hashlib.md5("|".join(codes_sorted).encode()).hexdigest()

            if not is_new(state, f"site_{path.strip('/') or 'home'}", page_fingerprint):
                log.debug("[Site] %s — codes already known: %s", label, codes_sorted)
                continue

            # Also check if any code is individually new
            any_new_code = any(
                code not in [c.upper() for c in state.get("known_codes", [])]
                for code in codes_sorted
            )

            if not any_new_code:
                log.debug("[Site] %s — all codes already in state.", label)
                continue

            page_title_el = soup.find("title")
            page_title = page_title_el.get_text(strip=True) if page_title_el else label

            snippet = " | ".join(codes_sorted)

            log.info("[Site] %s new codes detected: %s", label, codes_sorted)

            findings.append(
                FindingResult(
                    source=f"CSGOCASES {label}",
                    item_id=page_fingerprint,
                    url=url,
                    author="CSGOCASES Official",
                    title=f"Active promo codes on {label}",
                    body=snippet,
                    codes=codes_sorted,
                )
            )

        log.info("[Site] Done — %d new finding(s).", len(findings))
        return findings
