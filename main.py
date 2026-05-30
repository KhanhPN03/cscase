"""
main.py
-------
Entry point for the CSGOCASES Promo Code Monitor.

Orchestrates all platform monitors, deduplicates findings, sends email
notifications, and persists state back to disk.

Usage:
    python main.py                  # normal run
    python main.py --dry-run        # run without sending emails
    python main.py --test-email     # send a test email and exit
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Bootstrap — load .env file if present (local development)
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — rely on real env vars (CI/CD)

from monitors.csgocases_site_monitor import CSGOCasesSiteMonitor
from monitors.discord_monitor import DiscordMonitor
from monitors.facebook_monitor import FacebookMonitor
from monitors.instagram_monitor import InstagramMonitor
from monitors.reddit_monitor import RedditMonitor
from monitors.telegram_monitor import TelegramMonitor
from monitors.twitter_snscrape_monitor import TwitterSnscrapeMonitor  # snscrape-based
from monitors.webpage_monitor import WebpageMonitor
from monitors.youtube_monitor import YouTubeMonitor
from utils import state as state_store
from utils.email_sender import send_notification
from utils.logger import get_logger

log = get_logger("main")

# ---------------------------------------------------------------------------
# All active monitors — add / remove / reorder here
# ---------------------------------------------------------------------------
MONITORS = [
    CSGOCasesSiteMonitor,      # generic site code scan (homepage, /promocodes…)
    WebpageMonitor,            # content-change monitor for specific URLs
    InstagramMonitor,          # @csgocasescom via async Playwright
    FacebookMonitor,           # fb.com/csgocasescom via async Playwright
    DiscordMonitor,            # Discord channel via REST API or RSSHub bridge
    RedditMonitor,
    TwitterSnscrapeMonitor,    # @csgocasescom via snscrape (latest-ID dedup)
    YouTubeMonitor,
    TelegramMonitor,
]


def _test_email() -> None:
    """Send a test notification and exit."""
    log.info("Sending test email …")
    ok = send_notification(
        source="Test Run",
        url="https://csgocases.com",
        author="Monitor Bot",
        snippet="This is a test notification to verify your Gmail configuration.",
        codes=["TESTCODE123", "FREESPIN2024"],
        subject_override="✅ CSGOCASES Monitor — Email Test Successful",
    )
    sys.exit(0 if ok else 1)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CSGOCASES Promo Code Monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run all monitors but do NOT send emails.")
    parser.add_argument("--test-email", action="store_true",
                        help="Send a test email and exit.")
    return parser.parse_args()


def run(dry_run: bool = False) -> int:
    """
    Main monitoring loop.
    Returns the number of new findings detected.
    """
    start = time.monotonic()
    log.info("=" * 60)
    log.info("CSGOCASES Promo Monitor — %s",
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    log.info("Mode: %s", "DRY RUN" if dry_run else "LIVE")
    log.info("=" * 60)

    # Load persisted state
    state = state_store.load()
    total_findings = 0
    email_results: list[dict] = []

    for MonitorClass in MONITORS:
        monitor = MonitorClass()
        log.info("▶ Running: %s", monitor.SOURCE_NAME)
        try:
            findings = monitor.fetch_new_items(state)
        except Exception as exc:
            log.error("Unhandled error in %s: %s", monitor.SOURCE_NAME, exc, exc_info=True)
            findings = []

        for finding in findings:
            total_findings += 1

            # Register new codes in state
            truly_new_codes = []
            for code in finding["codes"]:
                if state_store.register_code(state, code):
                    truly_new_codes.append(code)

            # Always alert even if code extraction failed —
            # the post itself might be worth checking manually
            if not dry_run:
                ok = send_notification(
                    source=finding["source"],
                    url=finding["url"],
                    author=finding["author"],
                    snippet=finding["body"],
                    codes=finding["codes"],
                )
                email_results.append({"finding": finding, "sent": ok})
            else:
                log.info(
                    "[DRY RUN] Would notify: source=%s codes=%s url=%s",
                    finding["source"], finding["codes"], finding["url"],
                )

    # Save updated state
    state_store.save(state)

    elapsed = time.monotonic() - start
    log.info("=" * 60)
    log.info(
        "Run complete in %.1fs — %d new finding(s) | %d email(s) sent.",
        elapsed,
        total_findings,
        sum(1 for r in email_results if r["sent"]),
    )
    log.info("Known codes so far: %s", state.get("known_codes", []))
    log.info("=" * 60)

    return total_findings


def main() -> None:
    args = _parse_args()

    if args.test_email:
        _test_email()

    try:
        run(dry_run=args.dry_run)
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
        sys.exit(0)
    except Exception as exc:
        log.critical("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
