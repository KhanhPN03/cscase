"""
utils/notifier.py
-----------------
Gmail notification utility for the CSGOCASES promo code monitor.

Sends rich HTML + plain-text fallback emails via Gmail SMTP (TLS port 587)
using a Google App Password — no OAuth2 flow, no external dependencies.

Relationship to utils/email_sender.py
---------------------------------------
Both modules send Gmail notifications; they coexist and serve different roles:

  email_sender.py   Minimal original sender wired into main.py.
                    Uses GMAIL_APP_PASS env var.

  notifier.py       Full-featured standalone utility (this file).
  (this file)       - Uses GMAIL_APP_PASSWORD env var (as specified).
                    - Rich HTML template with animated header, code boxes,
                      multi-code layout, content-change vs. code-found modes.
                    - Retry logic (up to 3 attempts with back-off).
                    - Digest mode: batch multiple findings into one email.
                    - Dry-run mode (NOTIFY_DRY_RUN=true) for testing.
                    - Configurable via env vars only — no code changes needed.
                    - Embeds a complete standalone test suite at the bottom.

Environment variables
----------------------
  GMAIL_USER           Sender address        e.g. yourbot@gmail.com
  GMAIL_APP_PASSWORD   16-char App Password  (Google Account > Security > App Passwords)
  NOTIFY_EMAIL         Recipient address     (defaults to GMAIL_USER if not set)
  NOTIFY_DRY_RUN       Set to "true" to log but NOT send (useful for CI testing)
  NOTIFY_MAX_RETRIES   SMTP retry attempts, default 3
  NOTIFY_TIMEOUT       SMTP connection timeout seconds, default 15

How to get an App Password
---------------------------
  1. Go to https://myaccount.google.com/security
  2. Enable 2-Step Verification (required)
  3. Search "App Passwords" → Create → select "Mail" + "Other (custom name)"
  4. Copy the 16-character password into GMAIL_APP_PASSWORD

Public API
-----------
  send_notification(result)          -> bool
      Send one email for a single FindingResult or WebpageResult dict.

  send_digest(results)               -> bool
      Batch multiple findings into a single digest email.

  notify_content_change(url, content, codes) -> bool
      Send a "page changed" alert (no specific post author / platform).

  format_email(result)               -> tuple[str, str, str]
      Return (subject, plain_text, html) without sending — useful for testing.

Usage in main.py
-----------------
  from utils.notifier import send_notification
  for finding in new_findings:
      send_notification(finding)

Quick test (no email sent)
---------------------------
  NOTIFY_DRY_RUN=true python utils/notifier.py
"""

from __future__ import annotations

import html as html_lib
import os
import smtplib
import textwrap
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional, Union

from utils.logger import get_logger

log = get_logger("notifier")

# ---------------------------------------------------------------------------
# Configuration (all via env vars)
# ---------------------------------------------------------------------------

GMAIL_USER:         str = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD: str = os.getenv("GMAIL_APP_PASSWORD", "").strip()
NOTIFY_EMAIL:       str = os.getenv("NOTIFY_EMAIL", GMAIL_USER).strip()
DRY_RUN:            bool = os.getenv("NOTIFY_DRY_RUN", "false").lower() in ("true", "1", "yes")
MAX_RETRIES:        int  = int(os.getenv("NOTIFY_MAX_RETRIES", "3"))
SMTP_TIMEOUT:       int  = int(os.getenv("NOTIFY_TIMEOUT", "15"))

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587   # STARTTLS

MONITOR_SITE_URL = "https://csgocases.com"

# ---------------------------------------------------------------------------
# Result type alias (accepts FindingResult or WebpageResult dicts)
# ---------------------------------------------------------------------------

ResultDict = dict  # {"source", "url", "codes", "content"/"body", ...}

# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_STYLE = """\
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap');
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Inter', 'Segoe UI', Arial, sans-serif;
    background: #080810;
    color: #dde1f0;
    padding: 24px 12px;
  }
  .wrapper {
    max-width: 600px;
    margin: 0 auto;
  }
  .card {
    background: #10101e;
    border: 1px solid #1e1e3a;
    border-radius: 16px;
    overflow: hidden;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6);
  }

  /* ── Header ── */
  .header {
    background: linear-gradient(135deg, #f7971e 0%, #ffd200 50%, #f7971e 100%);
    background-size: 200% auto;
    padding: 28px 32px 24px;
    position: relative;
  }
  .header-badge {
    display: inline-block;
    background: rgba(0,0,0,.2);
    border-radius: 20px;
    padding: 4px 14px;
    font-size: 11px;
    font-weight: 700;
    color: #0f0f1a;
    letter-spacing: 1.5px;
    text-transform: uppercase;
    margin-bottom: 10px;
  }
  .header h1 {
    font-size: 22px;
    font-weight: 800;
    color: #0f0f1a;
    line-height: 1.2;
    margin-bottom: 6px;
  }
  .header-sub {
    font-size: 12px;
    color: rgba(0,0,0,.55);
    font-weight: 500;
  }

  /* ── Body ── */
  .body { padding: 28px 32px; }

  /* ── Code boxes ── */
  .codes-section { margin: 4px 0 20px; }
  .codes-label {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.5px;
    color: #f7971e;
    text-transform: uppercase;
    margin-bottom: 10px;
  }
  .code-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }
  .code-pill {
    background: linear-gradient(135deg, #1a1a2e, #0f0f1a);
    border: 2px solid #f7971e;
    border-radius: 10px;
    padding: 14px 22px;
    text-align: center;
    flex: 1;
    min-width: 120px;
  }
  .code-pill span {
    display: block;
    font-family: 'Courier New', 'Consolas', monospace;
    font-size: 22px;
    font-weight: 900;
    letter-spacing: 3px;
    color: #ffd200;
  }
  .code-pill small {
    display: block;
    font-size: 10px;
    color: #666;
    margin-top: 4px;
  }
  .no-codes {
    background: #0d0d1c;
    border: 1px dashed #2a2a45;
    border-radius: 10px;
    padding: 16px;
    font-size: 13px;
    color: #888;
    text-align: center;
  }

  /* ── Meta table ── */
  .meta-table {
    background: #0d0d1c;
    border-radius: 10px;
    padding: 16px 20px;
    margin: 20px 0;
    font-size: 13px;
    line-height: 2;
    border: 1px solid #1a1a30;
  }
  .meta-row { display: flex; gap: 12px; }
  .meta-key  { color: #f7971e; font-weight: 600; white-space: nowrap; min-width: 90px; }
  .meta-val  { color: #ccd0e0; word-break: break-all; }
  .meta-val a { color: #f7971e; text-decoration: none; }
  .meta-val a:hover { text-decoration: underline; }
  .divider { border: none; border-top: 1px solid #1a1a30; margin: 4px 0; }

  /* ── Snippet ── */
  .snippet-label {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.5px;
    color: #888;
    text-transform: uppercase;
    margin-bottom: 8px;
  }
  .snippet-box {
    background: #0a0a18;
    border-left: 3px solid #f7971e;
    border-radius: 0 8px 8px 0;
    padding: 14px 16px;
    font-size: 13px;
    color: #9da3b8;
    line-height: 1.6;
    word-break: break-word;
    white-space: pre-wrap;
  }

  /* ── CTA button ── */
  .cta { text-align: center; margin-top: 24px; }
  .btn {
    display: inline-block;
    background: linear-gradient(135deg, #f7971e, #ffd200);
    color: #0f0f1a !important;
    font-weight: 800;
    font-size: 14px;
    padding: 14px 36px;
    border-radius: 10px;
    text-decoration: none !important;
    letter-spacing: 0.5px;
  }
  .btn-secondary {
    display: inline-block;
    margin-top: 10px;
    font-size: 12px;
    color: #555;
    text-decoration: none;
  }
  .btn-secondary:hover { color: #f7971e; }

  /* ── Footer ── */
  .footer {
    background: #080810;
    border-top: 1px solid #1a1a30;
    padding: 16px 32px;
    text-align: center;
    font-size: 11px;
    color: #444;
    line-height: 1.8;
  }
  .footer a { color: #555; text-decoration: none; }
  .footer a:hover { color: #f7971e; }
</style>"""

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{subject}</title>
  {style}
</head>
<body>
<div class="wrapper">
<div class="card">

  <div class="header">
    <div class="header-badge">&#127918; CSGOCASES Monitor</div>
    <h1>{header_title}</h1>
    <div class="header-sub">Detected at {timestamp} UTC &nbsp;&#183;&nbsp; Automated Alert</div>
  </div>

  <div class="body">

    {codes_section}

    <div class="meta-table">
      <div class="meta-row">
        <span class="meta-key">&#128225; Source</span>
        <span class="meta-val">{source}</span>
      </div>
      <hr class="divider">
      <div class="meta-row">
        <span class="meta-key">&#128279; Link</span>
        <span class="meta-val"><a href="{url}">{url_display}</a></span>
      </div>
      {author_row}
      <hr class="divider">
      <div class="meta-row">
        <span class="meta-key">&#128336; Time</span>
        <span class="meta-val">{timestamp} UTC</span>
      </div>
    </div>

    <div class="snippet-label">Content Snippet</div>
    <div class="snippet-box">{snippet}</div>

    <div class="cta">
      <a class="btn" href="{url}">Open Source &rarr;</a><br>
      <a class="btn-secondary" href="{site_url}">Go to CSGOCASES.com</a>
    </div>

  </div>

  <div class="footer">
    Sent by <strong>CSGOCASES Promo Monitor</strong>
    &nbsp;&#183;&nbsp; Powered by GitHub Actions
    &nbsp;&#183;&nbsp; <a href="{site_url}">csgocases.com</a>
    <br>To stop alerts, remove NOTIFY_EMAIL from your GitHub secrets.
  </div>

</div>
</div>
</body>
</html>"""

_DIGEST_ITEM_HTML = """\
<div style="border:1px solid #1e1e3a; border-radius:10px; padding:16px 20px; margin-bottom:14px; background:#0d0d1c;">
  <div style="font-size:11px; color:#f7971e; font-weight:700; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px;">{source}</div>
  {codes_inline}
  <div style="font-size:12px; color:#888; margin-top:8px;">{snippet_short}</div>
  <a href="{url}" style="font-size:12px; color:#f7971e; text-decoration:none; display:inline-block; margin-top:6px;">View &rarr;</a>
</div>"""


# ---------------------------------------------------------------------------
# Code section builders
# ---------------------------------------------------------------------------

def _build_codes_section_html(codes: list[str]) -> str:
    """Render the coloured code pill(s) or a 'no codes' placeholder."""
    if not codes:
        return (
            '<div class="codes-section">'
            '<div class="no-codes">'
            '&#9888;&#65039; No promo code extracted &mdash; '
            'check the source link for context.'
            '</div></div>'
        )
    pills = "\n".join(
        f'<div class="code-pill">'
        f'<span>{html_lib.escape(c)}</span>'
        f'<small>Copy &amp; paste on csgocases.com</small>'
        f'</div>'
        for c in codes
    )
    return (
        '<div class="codes-section">'
        '<div class="codes-label">&#127381; Promo Code(s) Detected</div>'
        f'<div class="code-grid">{pills}</div>'
        '</div>'
    )


def _build_codes_inline_html(codes: list[str]) -> str:
    """Inline badge style for digest items."""
    if not codes:
        return '<span style="font-size:11px;color:#666;">No code extracted</span>'
    badges = " ".join(
        f'<span style="background:#1a1a2e;border:1px solid #f7971e;border-radius:6px;'
        f'padding:3px 10px;font-family:monospace;font-size:13px;font-weight:700;'
        f'color:#ffd200;letter-spacing:2px;">{html_lib.escape(c)}</span>'
        for c in codes
    )
    return badges


# ---------------------------------------------------------------------------
# Email building
# ---------------------------------------------------------------------------

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _safe(text: str, max_len: int = 800) -> str:
    """HTML-escape and truncate text for safe embedding."""
    escaped = html_lib.escape(str(text))
    if len(escaped) > max_len:
        escaped = escaped[:max_len] + "&#8230;"
    return escaped


def _get_field(result: ResultDict, *keys: str, default: str = "") -> str:
    """Try multiple key names and return the first non-empty value found."""
    for key in keys:
        val = result.get(key, "")
        if val:
            return str(val)
    return default


def format_email(result: ResultDict) -> tuple[str, str, str]:
    """
    Build (subject, plain_text, html) for a single finding result.

    Accepts both FindingResult (from monitors) and WebpageResult (from
    webpage_monitor) dicts — handles both field naming conventions.

    Parameters
    ----------
    result : dict
        A FindingResult or WebpageResult dictionary.

    Returns
    -------
    tuple[str, str, str]
        (subject, plain_text, html_body)
    """
    source   = _get_field(result, "source",  default="Unknown source")
    url      = _get_field(result, "url",      default=MONITOR_SITE_URL)
    author   = _get_field(result, "author",   default="")
    codes    = result.get("codes", [])
    # FindingResult uses "body"; WebpageResult uses "content"
    snippet  = _get_field(result, "body", "content", default="(no content)")
    title    = _get_field(result, "title", default="")

    timestamp = _now_utc()
    code_label = ", ".join(codes) if codes else "content change"

    # Subject
    if codes:
        subject = f"[CSGOCASES] Promo code detected: {code_label} | {source}"
    else:
        subject = f"[CSGOCASES] Content changed: {source}"

    # Plain-text fallback
    plain = textwrap.dedent(f"""\
        CSGOCASES Promo Monitor Alert
        ==============================
        {'Codes    : ' + code_label if codes else 'No codes extracted — check link'}
        Source   : {source}
        {'Title    : ' + title if title else ''}
        URL      : {url}
        {'Author   : ' + author if author else ''}
        Time     : {timestamp} UTC

        --- Content Snippet ---
        {snippet[:600]}

        Redeem codes at: {MONITOR_SITE_URL}
        ==============================
        Sent by CSGOCASES Promo Monitor (GitHub Actions)
    """).strip()

    # Author row HTML (omit if no author)
    author_row_html = ""
    if author:
        author_row_html = (
            '<hr class="divider">'
            '<div class="meta-row">'
            f'<span class="meta-key">&#9997;&#65039; Author</span>'
            f'<span class="meta-val">{_safe(author)}</span>'
            '</div>'
        )

    # URL display: truncate long URLs in the visible text
    url_display = url if len(url) <= 60 else url[:57] + "..."

    # Header title
    if codes:
        header_title = f"Promo Code Detected on {source}"
    elif title:
        header_title = title
    else:
        header_title = f"Content Changed: {source}"

    html_body = _HTML_TEMPLATE.format(
        style        = _STYLE,
        subject      = html_lib.escape(subject),
        header_title = _safe(header_title, max_len=200),
        timestamp    = html_lib.escape(timestamp),
        source       = _safe(source, max_len=200),
        url          = html_lib.escape(url),
        url_display  = html_lib.escape(url_display),
        author_row   = author_row_html,
        codes_section= _build_codes_section_html(codes),
        snippet      = _safe(snippet, max_len=800),
        site_url     = MONITOR_SITE_URL,
    )

    return subject, plain, html_body


def format_digest(results: list[ResultDict]) -> tuple[str, str, str]:
    """
    Build a single digest email for multiple findings.

    Returns (subject, plain_text, html_body).
    """
    n = len(results)
    all_codes = []
    for r in results:
        all_codes.extend(r.get("codes", []))
    # Deduplicate codes preserving order
    seen: set[str] = set()
    unique_codes = [c for c in all_codes if not (c in seen or seen.add(c))]  # type: ignore[func-returns-value]

    timestamp = _now_utc()

    if unique_codes:
        subject = f"[CSGOCASES] {n} alert(s) — codes: {', '.join(unique_codes[:4])}"
    else:
        subject = f"[CSGOCASES] {n} content change(s) detected"

    # Plain text
    plain_items = []
    for i, r in enumerate(results, 1):
        src     = _get_field(r, "source", default="?")
        url     = _get_field(r, "url",    default=MONITOR_SITE_URL)
        codes   = r.get("codes", [])
        snippet = _get_field(r, "body", "content", default="")[:200]
        plain_items.append(
            f"{i}. [{src}]\n"
            f"   Codes: {', '.join(codes) if codes else 'none'}\n"
            f"   URL:   {url}\n"
            f"   Snippet: {snippet}\n"
        )
    plain = (
        f"CSGOCASES Promo Monitor — {n} Alert(s)\n"
        f"{'=' * 44}\n"
        f"Time: {timestamp} UTC\n\n"
        + "\n".join(plain_items)
        + f"\nRedeem at: {MONITOR_SITE_URL}\n"
    )

    # HTML digest items
    html_items = "\n".join(
        _DIGEST_ITEM_HTML.format(
            source        = _safe(_get_field(r, "source", default="?"), max_len=100),
            codes_inline  = _build_codes_inline_html(r.get("codes", [])),
            snippet_short = _safe(_get_field(r, "body", "content", default=""), max_len=200),
            url           = html_lib.escape(_get_field(r, "url", default=MONITOR_SITE_URL)),
        )
        for r in results
    )

    html_body = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_lib.escape(subject)}</title>
  {_STYLE}
</head>
<body>
<div class="wrapper">
<div class="card">

  <div class="header">
    <div class="header-badge">&#127918; CSGOCASES Monitor</div>
    <h1>&#128680; {n} New Alert{'s' if n != 1 else ''}</h1>
    <div class="header-sub">{timestamp} UTC &nbsp;&#183;&nbsp; Digest summary</div>
  </div>

  <div class="body">
    {html_items}
    <div class="cta">
      <a class="btn" href="{MONITOR_SITE_URL}">Open CSGOCASES &rarr;</a>
    </div>
  </div>

  <div class="footer">
    Sent by <strong>CSGOCASES Promo Monitor</strong>
    &nbsp;&#183;&nbsp; GitHub Actions
    &nbsp;&#183;&nbsp; <a href="{MONITOR_SITE_URL}">csgocases.com</a>
  </div>

</div>
</div>
</body>
</html>"""

    return subject, plain, html_body


# ---------------------------------------------------------------------------
# SMTP send with retry
# ---------------------------------------------------------------------------

def _send_via_smtp(subject: str, plain: str, html_body: str) -> bool:
    """
    Deliver the email via Gmail SMTP with STARTTLS.

    Retries up to MAX_RETRIES times on transient failures.
    Returns True on success, False if all attempts fail.
    """
    gmail_user = GMAIL_USER or os.getenv("GMAIL_USER", "").strip()
    gmail_pass = GMAIL_APP_PASSWORD or os.getenv("GMAIL_APP_PASSWORD", "").strip()
    notify_to  = NOTIFY_EMAIL or os.getenv("NOTIFY_EMAIL", gmail_user).strip()

    if not gmail_user:
        log.error("[Notifier] GMAIL_USER is not set — cannot send email.")
        return False
    if not gmail_pass:
        log.error("[Notifier] GMAIL_APP_PASSWORD is not set — cannot send email.")
        return False
    if not notify_to:
        log.error("[Notifier] NOTIFY_EMAIL is not set — cannot send email.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"CSGOCASES Monitor <{gmail_user}>"
    msg["To"]      = notify_to
    msg["X-Mailer"] = "CSGOCASES-Monitor/1.0"
    msg.attach(MIMEText(plain,      "plain", "utf-8"))
    msg.attach(MIMEText(html_body,  "html",  "utf-8"))

    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.debug(
                "[Notifier] SMTP attempt %d/%d -> %s:%d",
                attempt, MAX_RETRIES, SMTP_HOST, SMTP_PORT,
            )
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(gmail_user, gmail_pass)
                smtp.sendmail(gmail_user, [notify_to], msg.as_bytes())

            log.info(
                "[Notifier] Email sent -> %s | subject: %s",
                notify_to, subject,
            )
            return True

        except smtplib.SMTPAuthenticationError as exc:
            log.error(
                "[Notifier] Gmail auth failed (attempt %d) — "
                "verify GMAIL_USER and GMAIL_APP_PASSWORD: %s",
                attempt, exc,
            )
            return False   # Auth errors are permanent — don't retry

        except smtplib.SMTPRecipientsRefused as exc:
            log.error("[Notifier] Recipient refused: %s", exc)
            return False   # Permanent

        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            last_exc = exc
            log.warning(
                "[Notifier] SMTP error attempt %d/%d: %s",
                attempt, MAX_RETRIES, exc,
            )
            if attempt < MAX_RETRIES:
                backoff = attempt * 5   # 5s, 10s between retries
                log.debug("[Notifier] Retrying in %ds...", backoff)
                time.sleep(backoff)

    log.error(
        "[Notifier] All %d SMTP attempts failed. Last error: %s",
        MAX_RETRIES, last_exc,
    )
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_notification(result: ResultDict) -> bool:
    """
    Send an email notification for a single finding.

    Accepts a FindingResult or WebpageResult dict (both supported).

    Parameters
    ----------
    result : dict
        Must contain at least "source" and "url".
        Optional: "codes", "body" / "content", "author", "title".

    Returns
    -------
    bool
        True if sent (or dry-run), False on failure.

    Examples
    --------
    >>> send_notification({
    ...     "source": "Discord",
    ...     "url": "https://discord.com/...",
    ...     "codes": ["BLAST50"],
    ...     "body": "Use promo code BLAST50 now!",
    ...     "author": "CSGOCases",
    ... })
    True
    """
    subject, plain, html_body = format_email(result)
    codes = result.get("codes", [])

    log.info(
        "[Notifier] Preparing notification | source=%s | codes=%s",
        result.get("source", "?"), codes,
    )

    if DRY_RUN:
        log.info("[Notifier] DRY RUN — email not sent. Subject: %s", subject)
        log.debug("[Notifier] DRY RUN plain text:\n%s", plain)
        return True   # Report success so the caller continues normally

    return _send_via_smtp(subject, plain, html_body)


def send_digest(results: list[ResultDict]) -> bool:
    """
    Send a single digest email summarising multiple findings.

    Useful when multiple monitors fire simultaneously — avoids inbox spam.

    Parameters
    ----------
    results : list[dict]
        List of FindingResult / WebpageResult dicts.

    Returns
    -------
    bool
        True if sent (or dry-run), False on failure.
    """
    if not results:
        log.debug("[Notifier] send_digest called with empty list — nothing to send.")
        return True

    subject, plain, html_body = format_digest(results)
    all_codes = [c for r in results for c in r.get("codes", [])]

    log.info(
        "[Notifier] Preparing digest | %d findings | codes=%s",
        len(results), list(dict.fromkeys(all_codes)),  # deduplicated
    )

    if DRY_RUN:
        log.info("[Notifier] DRY RUN — digest not sent. Subject: %s", subject)
        log.debug("[Notifier] DRY RUN plain text:\n%s", plain)
        return True

    return _send_via_smtp(subject, plain, html_body)


def notify_content_change(url: str, content: str, codes: list[str]) -> bool:
    """
    Convenience wrapper for webpage content-change alerts.

    Parameters
    ----------
    url : str
        The page URL that changed.
    content : str
        Extracted content snippet.
    codes : list[str]
        Any promo codes detected in the content.

    Returns
    -------
    bool
        True if sent (or dry-run), False on failure.
    """
    result: ResultDict = {
        "source":  f"Website ({url})",
        "url":     url,
        "codes":   codes,
        "content": content,
        "author":  "",
        "title":   f"Page content changed: {url}",
        "new":     True,
    }
    return send_notification(result)


# ---------------------------------------------------------------------------
# Standalone tests (run with: python utils/notifier.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    _P = "\033[32mPASS\033[0m"
    _F = "\033[31mFAIL\033[0m"
    _all_ok = True

    def _chk(label: str, got, expected) -> bool:
        global _all_ok
        ok = got == expected
        if not ok:
            _all_ok = False
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            print(f"         Got:      {got!r}")
            print(f"         Expected: {expected!r}")
        return ok

    def _chk_in(label: str, needle: str, haystack: str) -> bool:
        global _all_ok
        ok = needle in haystack
        if not ok:
            _all_ok = False
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            print(f"         '{needle}' not found in output")
        return ok

    print("=" * 62)
    print("  utils/notifier.py -- self-test (NOTIFY_DRY_RUN=true implied)")
    print("=" * 62)

    # Force dry-run so no actual email is sent during testing
    import utils.notifier as _notifier_mod
    _notifier_mod.DRY_RUN = True

    # ── Sample data ───────────────────────────────────────────────────────
    finding_with_codes = {
        "source":  "Discord #announcements",
        "url":     "https://discord.com/channels/123/456/789",
        "author":  "CSGOCases Official",
        "codes":   ["BLAST50", "SUMMER99"],
        "body":    "Use promo code BLAST50 or SUMMER99 for free spins! Limited time only.",
        "title":   "New promo codes live",
        "new":     True,
        "item_id": "abc123",
    }
    finding_no_codes = {
        "source":  "CSGOCASES Website (csgocases.com/case/instagram-1)",
        "url":     "https://csgocases.com/case/instagram-1",
        "author":  "",
        "codes":   [],
        "body":    "Page content changed — new items added to the Instagram case.",
        "title":   "Page content changed",
        "new":     True,
        "item_id": "def456",
    }
    webpage_result = {
        "source":  "website",
        "url":     "https://csgocases.com/case/instagram-1",
        "codes":   ["INSTA2024"],
        "content": "New drop alert! Open the Instagram case with code INSTA2024.",
        "new":     True,
    }
    finding_long_url = {
        "source": "Twitter @csgocasescom",
        "url":    "https://twitter.com/csgocasescom/status/12345678901234567890/extra/path/segment",
        "codes":  ["TWEET50"],
        "body":   "Tweet content with code TWEET50",
    }

    # ── 1. format_email — with codes ─────────────────────────────────────
    print("\n[1] format_email -- with codes")
    subj, plain, html = format_email(finding_with_codes)

    _chk_in("Subject contains code label", "BLAST50",          subj)
    _chk_in("Subject contains source",     "Discord",          subj)
    _chk_in("Subject prefix",              "[CSGOCASES]",      subj)
    _chk_in("Plain: Codes line present",   "BLAST50, SUMMER99", plain)
    _chk_in("Plain: Source present",       "Discord #announcements", plain)
    _chk_in("Plain: URL present",          "discord.com",      plain)
    _chk_in("Plain: Author present",       "CSGOCases Official", plain)
    _chk_in("Plain: Timestamp present",    "UTC",              plain)
    _chk_in("Plain: Site URL present",     "csgocases.com",    plain)
    _chk_in("HTML: Both codes rendered",   "BLAST50",          html)
    _chk_in("HTML: Second code rendered",  "SUMMER99",         html)
    _chk_in("HTML: Source rendered",       "Discord",          html)
    _chk_in("HTML: URL link present",      "discord.com",      html)
    _chk_in("HTML: Author row rendered",   "CSGOCases Official", html)
    _chk_in("HTML: Snippet rendered",      "free spins",       html)
    _chk_in("HTML: DOCTYPE present",       "<!DOCTYPE html>",  html)
    _chk_in("HTML: charset UTF-8",         "UTF-8",            html)
    _chk_in("HTML: CTA button present",    "Open Source",      html)
    _chk_in("HTML: Footer present",        "GitHub Actions",   html)

    # ── 2. format_email — no codes ────────────────────────────────────────
    print("\n[2] format_email -- no codes (content change)")
    subj2, plain2, html2 = format_email(finding_no_codes)

    _chk_in("Subject: content change label", "Content changed", subj2)
    _chk_in("HTML: no-codes placeholder",  "No promo code extracted", html2)
    _chk("Author row absent when no author", "Author" in html2, False)

    # ── 3. format_email — WebpageResult (uses 'content' not 'body') ───────
    print("\n[3] format_email -- WebpageResult dict")
    subj3, plain3, html3 = format_email(webpage_result)

    _chk_in("Subject: code from webpage", "INSTA2024", subj3)
    _chk_in("HTML: code rendered",        "INSTA2024", html3)
    _chk_in("HTML: snippet from content", "Instagram", html3)

    # ── 4. format_email — long URL truncated in display ──────────────────
    print("\n[4] format_email -- long URL truncation")
    subj4, plain4, html4 = format_email(finding_long_url)
    _chk_in("HTML: URL href intact (full)", "12345678901234567890", html4)
    _chk_in("HTML: URL display truncated", "...", html4)

    # ── 5. format_email — HTML injection safety ───────────────────────────
    print("\n[5] format_email -- HTML injection safety")
    malicious = {
        "source": "<script>alert('xss')</script>",
        "url":    "https://csgocases.com",
        "codes":  ["<IMG SRC=x>"],
        "body":   "Normal content & <injection>",
    }
    _, _, html5 = format_email(malicious)
    _chk("Script tag escaped",  "<script>" not in html5, True)
    _chk("IMG tag escaped",     "<IMG" not in html5,     True)
    _chk("Ampersand escaped",   "&amp;" in html5,        True)

    # ── 6. format_digest ─────────────────────────────────────────────────
    print("\n[6] format_digest -- multiple findings")
    subj_d, plain_d, html_d = format_digest([finding_with_codes, finding_no_codes, webpage_result])

    _chk_in("Digest subject: count",    "3",       subj_d)
    _chk_in("Digest subject: code",     "BLAST50", subj_d)
    _chk_in("Digest plain: item 1",     "Discord", plain_d)
    _chk_in("Digest plain: item 2",     "CSGOCASES Website", plain_d)
    _chk_in("Digest plain: item 3",     "INSTA2024", plain_d)
    _chk_in("Digest HTML: all 3 sources appear", "Discord", html_d)
    _chk_in("Digest HTML: INSTA2024 badge",      "INSTA2024", html_d)

    # ── 7. format_digest — empty list ────────────────────────────────────
    print("\n[7] format_digest -- empty list")
    _chk("Empty digest returns True", _notifier_mod.send_digest([]), True)

    # ── 8. format_digest — single item ───────────────────────────────────
    print("\n[8] format_digest -- single item")
    subj_s, plain_s, _ = format_digest([finding_with_codes])
    _chk_in("Single-item subject: '1 alert'", "1 alert", subj_s)
    _chk("No stray 's' on singular '1 alert'", "1 alerts" not in subj_s, True)

    # ── 9. send_notification — dry run ───────────────────────────────────
    print("\n[9] send_notification -- dry run")
    result = _notifier_mod.send_notification(finding_with_codes)
    _chk("Dry run returns True",  result, True)

    # ── 10. send_digest — dry run ─────────────────────────────────────────
    print("\n[10] send_digest -- dry run")
    result_d = _notifier_mod.send_digest([finding_with_codes, webpage_result])
    _chk("Digest dry run returns True", result_d, True)

    # ── 11. notify_content_change — dry run ──────────────────────────────
    print("\n[11] notify_content_change -- dry run")
    result_w = _notifier_mod.notify_content_change(
        url     = "https://csgocases.com/case/instagram-1",
        content = "New items added to Instagram case.",
        codes   = ["INSTA25"],
    )
    _chk("Content change dry run returns True", result_w, True)

    # ── 12. _send_via_smtp — missing credentials ──────────────────────────
    print("\n[12] _send_via_smtp -- missing credentials")
    orig_dry = _notifier_mod.DRY_RUN
    _notifier_mod.DRY_RUN = False  # Force real path to test credential check
    old_user = _notifier_mod.GMAIL_USER
    old_pass = _notifier_mod.GMAIL_APP_PASSWORD
    _notifier_mod.GMAIL_USER         = ""
    _notifier_mod.GMAIL_APP_PASSWORD = ""
    no_cred_result = _notifier_mod._send_via_smtp("test", "test", "<p>test</p>")
    _chk("Missing user returns False", no_cred_result, False)
    _notifier_mod.GMAIL_USER         = "test@gmail.com"
    _notifier_mod.GMAIL_APP_PASSWORD = ""
    no_pass_result = _notifier_mod._send_via_smtp("test", "test", "<p>test</p>")
    _chk("Missing password returns False", no_pass_result, False)
    _notifier_mod.GMAIL_USER         = old_user
    _notifier_mod.GMAIL_APP_PASSWORD = old_pass
    _notifier_mod.DRY_RUN = orig_dry

    # ── 13. Plain text is always 7-bit safe ──────────────────────────────
    print("\n[13] Plain text encoding")
    _, plain_enc, _ = format_email(finding_with_codes)
    try:
        plain_enc.encode("ascii")
        _chk("Plain text is ASCII-safe", True, True)
    except UnicodeEncodeError:
        _chk("Plain text is ASCII-safe", False, True)

    print()
    print("=" * 62)
    print(f"  Result: {'ALL TESTS PASSED' if _all_ok else 'SOME TESTS FAILED'}")
    print("=" * 62)

    if not _all_ok:
        sys.exit(1)
