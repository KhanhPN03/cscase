"""
monitors/reddit_monitor.py
--------------------------
Monitors Reddit for CSGOCASES promo codes.

Uses Reddit's free, unauthenticated JSON API:
    https://www.reddit.com/r/<subreddit>/new.json

No API keys required — works with a browser User-Agent.
Monitors: r/CSGOcases, r/GlobalOffensive (filtered by keyword)
"""

from __future__ import annotations

import os

from monitors.base_monitor import BaseMonitor, FindingResult
from utils.code_extractor import contains_promo_keyword, extract_codes
from utils.logger import get_logger
from utils.state import is_new

log = get_logger("reddit_monitor")

# Subreddits to watch
_SUBREDDITS: list[str] = [
    sub.strip()
    for sub in os.getenv(
        "REDDIT_SUBREDDITS",
        "CSGOcases,GlobalOffensive,csgo,csgobetting",
    ).split(",")
    if sub.strip()
]

# How many posts to check per subreddit (max 100)
_LIMIT = int(os.getenv("REDDIT_LIMIT", "25"))

# Keywords to filter non-targeted subreddits with
_CSGO_KEYWORDS = {"csgocases", "csgo cases", "csgocase", "promo code", "bonus code"}


class RedditMonitor(BaseMonitor):
    SOURCE_NAME = "Reddit"

    def fetch_new_items(self, state: dict) -> list[FindingResult]:
        findings: list[FindingResult] = []

        for subreddit in _SUBREDDITS:
            url = f"https://www.reddit.com/r/{subreddit}/new.json?limit={_LIMIT}&sort=new"
            log.info("[Reddit] Checking r/%s …", subreddit)

            resp = self._get(url, headers={"Accept": "application/json"})
            if resp is None:
                continue

            try:
                data = resp.json()
                posts = data.get("data", {}).get("children", [])
            except Exception as exc:
                log.warning("[Reddit] Failed to parse r/%s JSON: %s", subreddit, exc)
                continue

            for post in posts:
                p = post.get("data", {})
                post_id   = p.get("id", "")
                title     = p.get("title", "")
                body      = p.get("selftext", "")
                author    = p.get("author", "[deleted]")
                permalink = "https://www.reddit.com" + p.get("permalink", "")
                flair     = (p.get("link_flair_text") or "").lower()

                full_text = f"{title} {body} {flair}"

                # For broad subreddits, only process CSGOCASES-related posts
                if subreddit.lower() not in ("csgocases", "csgocases"):
                    if not any(kw in full_text.lower() for kw in _CSGO_KEYWORDS):
                        continue

                # Only process posts that mention promos
                if not contains_promo_keyword(full_text):
                    continue

                source_key = f"reddit_{subreddit}"
                if not is_new(state, source_key, post_id):
                    continue

                codes = extract_codes(full_text)
                log.info("[Reddit] New post in r/%s by u/%s | codes=%s", subreddit, author, codes)

                findings.append(
                    FindingResult(
                        source=f"Reddit r/{subreddit}",
                        item_id=post_id,
                        url=permalink,
                        author=f"u/{author}",
                        title=self._truncate(title, 120),
                        body=self._truncate(full_text, 600),
                        codes=codes,
                    )
                )

        log.info("[Reddit] Done — %d new finding(s).", len(findings))
        return findings
