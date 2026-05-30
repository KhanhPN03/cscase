"""
monitors/twitter_monitor.py
---------------------------
Monitors Twitter/X for CSGOCASES promo codes via Nitter RSS feeds.

Nitter is an open-source Twitter front-end that exposes RSS feeds without
requiring a Twitter API key. Multiple public Nitter instances are tried in
sequence so the monitor degrades gracefully if one is down.

Targets: @csgocases official account + keyword search feeds
"""

from __future__ import annotations

import hashlib
import os
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

from monitors.base_monitor import BaseMonitor, FindingResult
from utils.code_extractor import contains_promo_keyword, extract_codes
from utils.logger import get_logger
from utils.state import is_new

log = get_logger("twitter_monitor")

# Public Nitter instances — tried in order, first success wins
_NITTER_INSTANCES: list[str] = [
    inst.strip()
    for inst in os.getenv(
        "NITTER_INSTANCES",
        "nitter.privacydev.net,nitter.poast.org,nitter.cz",
    ).split(",")
    if inst.strip()
]

# Twitter accounts to monitor
_ACCOUNTS: list[str] = [
    acc.strip()
    for acc in os.getenv(
        "TWITTER_ACCOUNTS",
        "csgocases,CSGOCasescom",
    ).split(",")
    if acc.strip()
]

# Search terms to watch via Nitter search RSS
_SEARCH_TERMS: list[str] = [
    t.strip()
    for t in os.getenv(
        "TWITTER_SEARCH_TERMS",
        "csgocases promo code,csgocases bonus code",
    ).split(",")
    if t.strip()
]

_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _parse_rss(xml_text: str) -> list[dict]:
    """Parse an RSS/Atom feed and return a flat list of item dicts."""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("[Twitter] XML parse error: %s", exc)
        return items

    # RSS 2.0 items
    for item in root.findall(".//item"):
        def _text(tag: str) -> str:
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""
        items.append({
            "title": _text("title"),
            "link":  _text("link"),
            "desc":  _text("description"),
            "guid":  _text("guid") or _text("link"),
            "author": _text("dc:creator") or _text("author"),
        })

    # Atom entries (some Nitter instances use Atom)
    for entry in root.findall("atom:entry", _NS):
        def _atext(tag: str) -> str:
            el = entry.find(f"atom:{tag}", _NS)
            return (el.text or "").strip() if el is not None else ""
        link_el = entry.find("atom:link", _NS)
        link = link_el.attrib.get("href", "") if link_el is not None else ""
        author_el = entry.find("atom:author/atom:name", _NS)
        author = (author_el.text or "").strip() if author_el is not None else ""
        items.append({
            "title":  _atext("title"),
            "link":   link,
            "desc":   _atext("content") or _atext("summary"),
            "guid":   _atext("id") or link,
            "author": author,
        })

    return items


class TwitterMonitor(BaseMonitor):
    SOURCE_NAME = "Twitter/X"

    def _rss_url(self, instance: str, path: str) -> str:
        return f"https://{instance}{path}"

    def _fetch_feed(self, path: str) -> list[dict]:
        """Try each Nitter instance and return parsed items from first success."""
        for instance in _NITTER_INSTANCES:
            url = self._rss_url(instance, path)
            log.debug("[Twitter] Trying %s …", url)
            resp = self._get(url, headers={"Accept": "application/rss+xml, application/xml, text/xml"})
            if resp is None:
                continue
            content_type = resp.headers.get("Content-Type", "")
            if "xml" not in content_type and "rss" not in content_type and "atom" not in content_type:
                # Some instances return HTML error pages
                log.warning("[Twitter] Non-XML response from %s (CT: %s)", instance, content_type)
                continue
            items = _parse_rss(resp.text)
            if items:
                log.debug("[Twitter] Got %d items from %s", len(items), instance)
                return items
            log.warning("[Twitter] Empty feed from %s", instance)
        return []

    def fetch_new_items(self, state: dict) -> list[FindingResult]:
        findings: list[FindingResult] = []

        # --- Account feeds ---
        for account in _ACCOUNTS:
            log.info("[Twitter] Checking @%s …", account)
            items = self._fetch_feed(f"/{account}/rss")
            for item in items:
                full_text = f"{item['title']} {item['desc']}"
                if not contains_promo_keyword(full_text):
                    continue
                # Stable ID: hash the guid
                item_id = hashlib.md5(item["guid"].encode()).hexdigest()
                if not is_new(state, f"twitter_account_{account}", item_id):
                    continue
                codes = extract_codes(full_text)
                author = item["author"] or f"@{account}"
                log.info("[Twitter] @%s new tweet | codes=%s", account, codes)
                findings.append(
                    FindingResult(
                        source=f"Twitter @{account}",
                        item_id=item_id,
                        url=item["link"],
                        author=author,
                        title=self._truncate(item["title"], 120),
                        body=self._truncate(full_text, 600),
                        codes=codes,
                    )
                )

        # --- Search feeds ---
        for term in _SEARCH_TERMS:
            log.info("[Twitter] Search: %s", term)
            path = f"/search/rss?q={quote_plus(term)}&f=tweets"
            items = self._fetch_feed(path)
            for item in items:
                full_text = f"{item['title']} {item['desc']}"
                item_id = hashlib.md5(item["guid"].encode()).hexdigest()
                term_key = term.replace(" ", "_")[:30]
                if not is_new(state, f"twitter_search_{term_key}", item_id):
                    continue
                codes = extract_codes(full_text)
                author = item["author"] or "unknown"
                log.info("[Twitter] Search '%s' new result | codes=%s", term, codes)
                findings.append(
                    FindingResult(
                        source=f"Twitter search: {term}",
                        item_id=item_id,
                        url=item["link"],
                        author=author,
                        title=self._truncate(item["title"], 120),
                        body=self._truncate(full_text, 600),
                        codes=codes,
                    )
                )

        log.info("[Twitter] Done — %d new finding(s).", len(findings))
        return findings
