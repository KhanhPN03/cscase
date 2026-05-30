"""
monitors/telegram_monitor.py
----------------------------
Monitors public Telegram channels for CSGOCASES promo codes.

Uses Telegram's public web interface (t.me/s/<channel>) which renders
the last ~20 messages as HTML without any API key or bot token.

Note: Works only for PUBLIC channels.
"""

from __future__ import annotations

import hashlib
import os
import re

from bs4 import BeautifulSoup

from monitors.base_monitor import BaseMonitor, FindingResult
from utils.code_extractor import contains_promo_keyword, extract_codes
from utils.logger import get_logger
from utils.state import is_new

log = get_logger("telegram_monitor")

# Public Telegram channels to watch
_CHANNELS: list[str] = [
    ch.strip()
    for ch in os.getenv(
        "TELEGRAM_CHANNELS",
        "csgocases,csgo_promo_codes",
    ).split(",")
    if ch.strip()
]

_TG_URL = "https://t.me/s/{channel}"


def _parse_telegram_page(html: str, channel: str) -> list[dict]:
    """Extract message dicts from the t.me/s/<channel> HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    messages: list[dict] = []

    for msg_div in soup.find_all("div", class_="tgme_widget_message"):
        # Message ID
        msg_url = ""
        link_el = msg_div.find("a", class_="tgme_widget_message_date")
        if link_el:
            msg_url = link_el.get("href", "")

        msg_id = re.search(r"/(\d+)$", msg_url)
        msg_id = msg_id.group(1) if msg_id else hashlib.md5(msg_url.encode()).hexdigest()

        # Message text
        text_el = msg_div.find("div", class_="tgme_widget_message_text")
        text = text_el.get_text(" ", strip=True) if text_el else ""

        # Author (for channels this is usually the channel name)
        author_el = msg_div.find("a", class_="tgme_widget_message_owner_name")
        author = author_el.get_text(strip=True) if author_el else f"@{channel}"

        if text:
            messages.append({
                "id":     msg_id,
                "text":   text,
                "url":    msg_url or f"https://t.me/{channel}",
                "author": author,
            })

    return messages


class TelegramMonitor(BaseMonitor):
    SOURCE_NAME = "Telegram"

    def fetch_new_items(self, state: dict) -> list[FindingResult]:
        findings: list[FindingResult] = []

        for channel in _CHANNELS:
            url = _TG_URL.format(channel=channel)
            log.info("[Telegram] Checking @%s …", channel)

            resp = self._get(url)
            if resp is None:
                continue

            messages = _parse_telegram_page(resp.text, channel)
            if not messages:
                log.debug("[Telegram] No messages parsed from @%s", channel)
                continue

            for msg in messages:
                if not contains_promo_keyword(msg["text"]):
                    continue

                if not is_new(state, f"telegram_{channel}", str(msg["id"])):
                    continue

                codes = extract_codes(msg["text"])
                log.info("[Telegram] @%s new message | codes=%s", channel, codes)

                findings.append(
                    FindingResult(
                        source=f"Telegram @{channel}",
                        item_id=str(msg["id"]),
                        url=msg["url"],
                        author=msg["author"],
                        title=self._truncate(msg["text"], 100),
                        body=self._truncate(msg["text"], 600),
                        codes=codes,
                    )
                )

        log.info("[Telegram] Done — %d new finding(s).", len(findings))
        return findings
