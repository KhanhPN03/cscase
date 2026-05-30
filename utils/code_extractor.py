"""
utils/code_extractor.py
-----------------------
Regex-based promo code extractor.

CSGOCASES codes are typically:
  - 4–20 uppercase alphanumeric characters (sometimes with hyphens/underscores)
  - Preceded by keywords: "code", "promo", "bonus", "use", "redeem", "coupon", etc.

Strategy:
  1. Context-aware scan  → find keyword + nearby ALL-CAPS token
  2. Broad scan fallback → catch standalone CAPS codes that look promotional
"""

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# Keywords that commonly precede a promo code
_KW = (
    r"(?:promo[\s_-]*code|bonus[\s_-]*code|redeem|coupon|use\s+code"
    r"|code\s*[:\-–=]?|referral[\s_-]*code|discount[\s_-]*code"
    r"|free\s+code|gift\s+code|promocode)"
)

# A code token: 4-20 chars, uppercase letters + digits, optional hyphens/underscores
_CODE_TOKEN = r"[A-Z][A-Z0-9_\-]{3,19}"

# Pattern 1 — keyword immediately followed (within 0-30 chars) by a code
_CONTEXT_RE = re.compile(
    rf"{_KW}"                   # keyword
    r"[\s:\"\'`\-–=]{{0,30}}"  # separator (up to 30 chars)
    rf"({_CODE_TOKEN})",        # captured code
    re.IGNORECASE,
)

# Pattern 2 — broad: ALL-CAPS word of 4-16 chars that looks standalone
_BROAD_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9_\-]{2,14})\b")

# Blocklist — common false positives (English ALL-CAPS words, website names, etc.)
_BLOCKLIST: set[str] = {
    "CSGOCASES", "CSGO", "FREE", "CODE", "BONUS", "PROMO", "STEAM",
    "HTTP", "HTTPS", "HTML", "JSON", "NULL", "TRUE", "FALSE", "NONE",
    "INFO", "USER", "POST", "LINK", "VIEW", "MORE", "SITE", "PAGE",
    "OPEN", "LIKE", "JUST", "ALSO", "ONLY", "EVEN", "BACK", "NEXT",
    "BEST", "MANY", "MOST", "THAN", "THAT", "THIS", "WITH", "YOUR",
    "WILL", "FROM", "HAVE", "BEEN", "WERE", "THEY", "THEM", "THEN",
    "WHEN", "WHAT", "SOME", "SAME", "OVER", "MAKE", "MUCH", "VERY",
    "COME", "INTO", "GIVE", "KNOW", "TAKE", "WELL", "EACH", "SUCH",
    "BOTH", "LONG", "DOWN", "SIDE", "GAME", "CASE", "SKIN", "RARE",
    "ITEM", "KEYS", "GUNS", "REAL", "FAST", "EASY", "HIGH", "GOOD",
    "NICE", "COOL", "HUGE", "LAST", "DAYS", "WEEK", "YEAR", "TIME",
    "TODAY", "DAILY", "EVERY", "AGAIN", "RIGHT", "STILL", "FIRST",
    "ABOUT", "ABOVE", "AFTER", "ALONG", "AMONG", "BEING", "BELOW",
    "COULD", "DOING", "FOUND", "GOING", "GREAT", "HELLO", "HOURS",
    "HTTPS", "IMAGE", "LATER", "LUCKY", "MIGHT", "NEVER", "NIGHT",
    "OFTEN", "OTHER", "PRIOR", "QUITE", "REACH", "REPLY", "SHALL",
    "SINCE", "SMALL", "SORRY", "THEIR", "THERE", "THESE", "THINK",
    "THOSE", "THREE", "UNTIL", "USING", "VALID", "VISIT", "WATCH",
    "WHERE", "WHILE", "WHOSE", "WORLD", "WOULD", "WRITE", "WRONG",
    "YEARS",
    # Platform names
    "REDDIT", "TWITTER", "YOUTUBE", "DISCORD", "TWITCH", "INSTAGRAM",
    "TELEGRAM", "TIKTOK", "FACEBOOK", "GOOGLE", "GITHUB", "STEAM",
}


def extract_codes(text: str) -> list[str]:
    """
    Extract all candidate promo codes from *text*.
    Returns a deduplicated list, longest codes first.
    """
    found: set[str] = set()

    # Pass 1 — context-aware (high confidence)
    for match in _CONTEXT_RE.finditer(text):
        code = match.group(1).upper()
        if _valid(code):
            found.add(code)

    # Pass 2 — broad scan (lower confidence — only used if pass 1 found nothing)
    if not found:
        for match in _BROAD_RE.finditer(text):
            code = match.group(1).upper()
            if _valid(code):
                found.add(code)

    # Sort: longer codes first (more specific), then alphabetically
    return sorted(found, key=lambda c: (-len(c), c))


def _valid(code: str) -> bool:
    """Return True if *code* passes basic validity checks."""
    if code in _BLOCKLIST:
        return False
    if len(code) < 4 or len(code) > 20:
        return False
    # Must contain at least one digit OR be mixed-length to reduce false positives
    # (pure dictionary words like "FREE" are already blocklisted above)
    has_digit = any(c.isdigit() for c in code)
    mixed_case_origin = code != code.replace("-", "").replace("_", "")
    if not has_digit and not mixed_case_origin and len(code) < 6:
        return False
    return True


def contains_promo_keyword(text: str) -> bool:
    """Quick check — does *text* mention a promo code at all?"""
    return bool(re.search(_KW, text, re.IGNORECASE))
