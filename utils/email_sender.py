"""
utils/email_sender.py
---------------------
Sends Gmail notifications via SMTP (port 587 / STARTTLS).
Uses an App Password — no OAuth2 required.

Required environment variables:
    GMAIL_USER      your_account@gmail.com
    GMAIL_APP_PASS  16-char app password from Google Account → Security
    NOTIFY_EMAIL    recipient address (can be same as GMAIL_USER)
"""

import os
import smtplib
import textwrap
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from utils.logger import get_logger

log = get_logger("email_sender")

# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background:#0f0f1a; color:#e0e0e0; margin:0; padding:20px; }}
  .card {{ background:#1a1a2e; border:1px solid #2d2d5e; border-radius:12px; max-width:580px;
           margin:0 auto; overflow:hidden; box-shadow:0 8px 32px rgba(0,0,0,.5); }}
  .header {{ background:linear-gradient(135deg,#f7971e,#ffd200); padding:24px 28px; }}
  .header h1 {{ margin:0; font-size:22px; color:#0f0f1a; font-weight:800; letter-spacing:.5px; }}
  .header p  {{ margin:4px 0 0; font-size:13px; color:#333; }}
  .body {{ padding:24px 28px; }}
  .code-box {{ background:#0a0a18; border:2px solid #f7971e; border-radius:8px;
               padding:16px 20px; margin:16px 0; text-align:center; }}
  .code-box span {{ font-size:28px; font-weight:900; letter-spacing:4px;
                    color:#ffd200; font-family:'Courier New',monospace; }}
  .meta {{ background:#12122a; border-radius:8px; padding:14px 18px; margin:12px 0;
           font-size:13px; line-height:1.8; }}
  .meta strong {{ color:#f7971e; }}
  .snippet {{ background:#0d0d1f; border-left:3px solid #f7971e; padding:10px 14px;
              margin:12px 0; font-size:12px; color:#aaa; border-radius:0 6px 6px 0;
              word-break:break-word; }}
  .btn {{ display:inline-block; margin-top:16px; padding:12px 28px;
          background:linear-gradient(135deg,#f7971e,#ffd200); color:#0f0f1a;
          font-weight:800; text-decoration:none; border-radius:8px; font-size:14px; }}
  .footer {{ padding:14px 28px; background:#0f0f1a; font-size:11px; color:#555; text-align:center; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <h1>🎮 CSGOCASES Promo Code Detected!</h1>
    <p>Automated monitor — {timestamp}</p>
  </div>
  <div class="body">
    {code_section}
    <div class="meta">
      <strong>📡 Source:</strong> {source}<br>
      <strong>🔗 URL:</strong> <a href="{url}" style="color:#f7971e;">{url}</a><br>
      <strong>✍️ Author:</strong> {author}<br>
      <strong>🕐 Detected at:</strong> {timestamp}
    </div>
    <div class="snippet">
      <strong style="color:#f7971e;">📝 Context snippet:</strong><br><br>
      {snippet}
    </div>
    <a class="btn" href="https://csgocases.com">Open CSGOCASES →</a>
  </div>
  <div class="footer">
    Sent by CSGOCASES Promo Monitor · Running on GitHub Actions · <a href="https://github.com" style="color:#555;">View workflow</a>
  </div>
</div>
</body>
</html>
"""


def _build_code_section(codes: list[str]) -> str:
    if not codes:
        return "<p style='color:#aaa;font-size:13px;'>⚠️ No explicit code extracted — check the link above.</p>"
    parts = []
    for code in codes:
        parts.append(
            f'<div class="code-box"><span>{code}</span></div>'
            f'<p style="font-size:12px;color:#777;text-align:center;margin:-8px 0 0;">Copy &amp; paste on csgocases.com</p>'
        )
    return "\n".join(parts)


def send_notification(
    *,
    source: str,
    url: str,
    author: str,
    snippet: str,
    codes: list[str],
    subject_override: Optional[str] = None,
) -> bool:
    """
    Send a Gmail notification.

    Returns True on success, False on failure (non-fatal — monitor continues).
    """
    gmail_user = os.getenv("GMAIL_USER", "").strip()
    gmail_pass = os.getenv("GMAIL_APP_PASS", "").strip()
    notify_to  = os.getenv("NOTIFY_EMAIL", gmail_user).strip()

    if not gmail_user or not gmail_pass:
        log.error("GMAIL_USER / GMAIL_APP_PASS not set — skipping email.")
        return False

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    code_label = ", ".join(codes) if codes else "possible promo"
    subject = subject_override or f"🎮 CSGOCASES Promo Code: {code_label} [{source}]"

    # --- Plain text fallback ---
    plain = textwrap.dedent(f"""\
        CSGOCASES Promo Code Detected!
        ================================
        Codes  : {code_label}
        Source : {source}
        URL    : {url}
        Author : {author}
        Time   : {timestamp}

        Snippet:
        {snippet[:500]}

        Redeem at: https://csgocases.com
    """)

    # --- HTML body ---
    html = _HTML_TEMPLATE.format(
        timestamp=timestamp,
        source=source,
        url=url,
        author=author,
        snippet=snippet[:600].replace("<", "&lt;").replace(">", "&gt;"),
        code_section=_build_code_section(codes),
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"CSGOCASES Monitor <{gmail_user}>"
    msg["To"]      = notify_to
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(gmail_user, gmail_pass)
            smtp.sendmail(gmail_user, [notify_to], msg.as_string())
        log.info("✅ Email sent → %s | codes: %s", notify_to, code_label)
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("Gmail auth failed — check GMAIL_USER / GMAIL_APP_PASS.")
    except smtplib.SMTPException as exc:
        log.error("SMTP error: %s", exc)
    except OSError as exc:
        log.error("Network error sending email: %s", exc)
    return False
