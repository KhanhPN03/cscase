"""
monitors/youtube_monitor.py
---------------------------
Monitors YouTube channels for CSGOCASES promo codes.

Uses YouTube's public Atom RSS feed (no API key required):
    https://www.youtube.com/feeds/videos.xml?channel_id=<CHANNEL_ID>

To find a channel ID:
  1. Open the channel page in a browser
  2. Right-click → View Page Source
  3. Ctrl+F for "channel_id" or "UC..."

Known CSGOCASES-related channels are pre-configured below.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

from monitors.base_monitor import BaseMonitor, FindingResult
from utils.code_extractor import contains_promo_keyword, extract_codes
from utils.logger import get_logger
from utils.state import is_new

log = get_logger("youtube_monitor")

_YT_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

# channel_id → display_name mapping
# Add more channels via YOUTUBE_CHANNELS env var: "CHANNEL_ID:Name,CHANNEL_ID2:Name2"
_DEFAULT_CHANNELS: dict[str, str] = {
    "UCvNgMEarKwLTTf0f8Te2z3Q": "CSGOCASES Official",  # update if incorrect
}

_NS = {
    "atom":   "http://www.w3.org/2005/Atom",
    "media":  "http://search.yahoo.com/mrss/",
    "yt":     "http://www.youtube.com/xml/schemas/2015",
}


def _load_channels() -> dict[str, str]:
    channels = dict(_DEFAULT_CHANNELS)
    env_val = os.getenv("YOUTUBE_CHANNELS", "").strip()
    if env_val:
        for entry in env_val.split(","):
            entry = entry.strip()
            if ":" in entry:
                cid, name = entry.split(":", 1)
                channels[cid.strip()] = name.strip()
    return channels


def _parse_yt_feed(xml_text: str) -> list[dict]:
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("[YouTube] XML parse error: %s", exc)
        return items

    for entry in root.findall("atom:entry", _NS):
        def _t(tag: str, ns: str = "atom") -> str:
            el = entry.find(f"{ns}:{tag}", _NS)
            return (el.text or "").strip() if el is not None else ""

        video_id = _t("videoId", "yt")
        title    = _t("title")
        author_el = entry.find("atom:author/atom:name", _NS)
        author   = (author_el.text or "").strip() if author_el is not None else ""
        link_el  = entry.find("atom:link", _NS)
        link     = link_el.attrib.get("href", "") if link_el is not None else ""

        # Description lives in media:group/media:description
        desc_el  = entry.find("media:group/media:description", _NS)
        desc     = (desc_el.text or "").strip() if desc_el is not None else ""

        items.append({
            "video_id": video_id,
            "title":    title,
            "author":   author,
            "link":     link or f"https://www.youtube.com/watch?v={video_id}",
            "desc":     desc,
        })
    return items


class YouTubeMonitor(BaseMonitor):
    SOURCE_NAME = "YouTube"

    def fetch_new_items(self, state: dict) -> list[FindingResult]:
        findings: list[FindingResult] = []
        channels = _load_channels()

        for channel_id, display_name in channels.items():
            log.info("[YouTube] Checking channel: %s (%s) …", display_name, channel_id)
            url  = _YT_FEED_URL.format(channel_id=channel_id)
            resp = self._get(url)
            if resp is None:
                continue

            items = _parse_yt_feed(resp.text)
            for item in items:
                full_text = f"{item['title']} {item['desc']}"

                # For non-CSGOCASES channels, require keyword presence
                if "csgocases" not in display_name.lower():
                    if not contains_promo_keyword(full_text):
                        continue

                if not is_new(state, f"youtube_{channel_id}", item["video_id"]):
                    continue

                codes = extract_codes(full_text)
                log.info("[YouTube] %s new video | codes=%s", display_name, codes)

                findings.append(
                    FindingResult(
                        source=f"YouTube {display_name}",
                        item_id=item["video_id"],
                        url=item["link"],
                        author=item["author"] or display_name,
                        title=self._truncate(item["title"], 120),
                        body=self._truncate(full_text, 600),
                        codes=codes,
                    )
                )

        log.info("[YouTube] Done — %d new finding(s).", len(findings))
        return findings
