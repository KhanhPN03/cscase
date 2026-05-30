"""
utils/code_detector.py
-----------------------
Lightweight, single-regex promo code detector.

Relationship to utils/code_extractor.py
-----------------------------------------
This project contains two complementary code-finding utilities:

  ┌─────────────────────┬──────────────────────────────────────────────────┐
  │ Module              │ Strategy                                          │
  ├─────────────────────┼──────────────────────────────────────────────────┤
  │ code_detector.py    │ Broad single-pass regex scan.                    │
  │  (this file)        │ Catches any [A-Z0-9]{4,15} token not blacklisted.│
  │                     │ Fast, simple, high recall, lower precision.       │
  ├─────────────────────┼──────────────────────────────────────────────────┤
  │ code_extractor.py   │ Two-pass context-aware scan.                     │
  │                     │ Pass 1: keyword + nearby code (high confidence). │
  │                     │ Pass 2: broad fallback only if pass 1 finds none.│
  │                     │ Higher precision, more complex.                   │
  └─────────────────────┴──────────────────────────────────────────────────┘

When to use which
------------------
  code_detector.extract_codes()   → when you want maximum recall and the
                                    source is known to contain codes (e.g.
                                    a dedicated promo-code post or page).

  code_extractor.extract_codes()  → when the source is noisy (full social
                                    media feeds, web pages) and precision
                                    matters more than recall.

Both functions share the same name and signature so they are drop-in
interchangeable.

Public API
----------
    extract_codes(text: str) -> list[str]
        Extract promo code candidates from *text*.
        Returns a deduplicated, uppercased, sorted list.

    is_blacklisted(token: str) -> bool
        Check whether a token is on the built-in or custom blacklist.

    BLACKLIST: frozenset[str]
        The built-in blacklist (read-only view).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Core pattern
# ---------------------------------------------------------------------------

# Primary extraction regex (as specified):
#   \b          word boundary
#   [A-Z0-9]    uppercase letters OR digits
#   {4,15}      between 4 and 15 characters inclusive
#   \b          word boundary
#
# Applied to the UPPERCASED version of the input so mixed-case source text
# like "Welcome100" is correctly matched as "WELCOME100".
_CODE_RE = re.compile(r"\b([A-Z0-9]{4,15})\b")

# ---------------------------------------------------------------------------
# Blacklist
# ---------------------------------------------------------------------------
# Tokens that are NEVER promo codes — common English words, brand names,
# platform names, and UI noise that would otherwise match the regex.
#
# Minimum required by spec: FREE, CSGO, CASE, DAILY
# Extended set: common false-positives seen across social media + Discord.
# ---------------------------------------------------------------------------

BLACKLIST: frozenset[str] = frozenset({
    # ── Spec-required ───────────────────────────────────────────────────────
    "FREE", "CSGO", "CASE", "DAILY",

    # ── Brand / platform noise ───────────────────────────────────────────────
    "CSGOCASES", "STEAM", "VALVE",
    "REDDIT", "TWITTER", "YOUTUBE", "DISCORD", "TWITCH",
    "INSTAGRAM", "TELEGRAM", "TIKTOK", "FACEBOOK",
    "GOOGLE", "GITHUB", "PAYPAL",

    # ── Web / code literals ──────────────────────────────────────────────────
    "HTTP", "HTTPS", "HTML", "JSON", "NULL",
    "TRUE", "FALSE", "NONE", "UNDEFINED",
    "UUID", "CSRF", "CORS", "AUTH", "AJAX",

    # ── Common English 4-letter words ────────────────────────────────────────
    "ABLE", "ALSO", "BACK", "BEEN", "BEST", "BOTH", "CAME",
    "CASE", "CODE", "COME", "COOL", "DOWN", "EACH", "EASY",
    "EVEN", "EVER", "FAST", "FIND", "FROM", "GAME", "GIVE",
    "GOOD", "GUNS", "HAVE", "HERE", "HIGH", "HUGE", "INFO",
    "INTO", "ITEM", "JUST", "KEEP", "KEYS", "KNOW", "LAST",
    "LIKE", "LINK", "LIVE", "LONG", "LOOK", "MADE", "MAKE",
    "MANY", "MISS", "MOST", "MUCH", "NEED", "NEXT", "NICE",
    "ONLY", "OPEN", "OVER", "PAGE", "PLAY", "POST", "RARE",
    "REAL", "SITE", "SKIN", "SOME", "SPIN", "SUCH", "TAKE",
    "THAN", "THAT", "THEM", "THEN", "THEY", "THIS", "TIME",
    "VERY", "VIEW", "WANT", "WEEK", "WELL", "WERE", "WHAT",
    "WHEN", "WILL", "WITH", "YEAR", "YOUR", "ZERO",

    # ── Common English 5-letter words ────────────────────────────────────────
    "ABOUT", "ABOVE", "AFTER", "AGAIN", "ALERT", "ALONG", "AMONG",
    "BEING", "BELOW", "BONUS", "CHECK", "CLAIM", "CLICK",
    "CLOSE", "COULD", "DOING", "ENTER", "EVERY", "EXTRA",
    "FIRST", "FOUND", "GOING", "GREAT", "HAPPY", "HELLO",
    "HOURS", "IMAGE", "ITEMS", "LATER", "LUCKY", "MIGHT",
    "MONEY", "NEVER", "NIGHT", "OFFER", "OFTEN", "ORDER",
    "OTHER", "POINT", "PRICE", "PROMO", "QUITE", "REACH",
    "READY", "RIGHT", "ROUND", "SHALL", "SHARE", "SINCE",
    "SKINS", "SMALL", "SORRY", "SPINS", "STILL", "STORE",
    "THEIR", "THERE", "THESE", "THINK", "THREE", "TODAY",
    "THOSE", "TRADE", "UNDER", "UNTIL", "USING", "VALID",
    "VISIT", "WATCH", "WHERE", "WHILE", "WHOSE", "WORLD",
    "WOULD", "WRITE", "WRONG", "YEARS",

    # ── Common English 6+ letter words ───────────────────────────────────────
    "ACTIVE", "ALWAYS", "AMOUNT", "ANYONE", "BEFORE", "CANNOT",
    "DURING", "FOLLOW", "GIVING", "MEMBER", "MIDDLE", "MIDNIGHT",
    "PLEASE", "RECEIVE", "REDEEM", "REWARD", "SHOULD", "THANKS",
    "THROUGH", "UPDATE", "WINNER", "UPDATES", "CHECKOUT",
    "LIMITED", "OFFERS", "ONLINE", "OPENED", "PRIZES",
    "EVERYONE", "INCLUDED", "OPENING", "REWARDS", "UPDATED",
    "PROMOS", "DROP",

    # ── Social media / Discord UI noise ─────────────────────────────────────
    "BOOST", "BOOSTS", "NITRO", "GUILD", "SERVER", "ROLES",
    "EMOJI", "REACT", "THREAD", "INVITE", "JOINED",
    "LIKES", "SAVES", "SAVED", "VIEWS", "WATCH",

    # ── CSGOCASES-specific noise ─────────────────────────────────────────────
    "CASES", "DROPS", "KNIFE", "GLOVE", "CODES",
    "PRIZE", "WHEEL", "VAULT",
})

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_codes(text: str) -> list[str]:
    """
    Extract promo code candidates from *text* using the pattern
    ``r'\b[A-Z0-9]{4,15}\b'``.

    Steps
    -----
    1. Uppercase the input so mixed-case tokens like ``Welcome50`` are found.
    2. Run the regex to find all matching tokens.
    3. Reject anything on the blacklist.
    4. Deduplicate using a set.
    5. Return sorted: longest first, then alphabetically (deterministic output).

    Parameters
    ----------
    text : str
        Any text — tweet, Discord message, webpage snippet, etc.

    Returns
    -------
    list[str]
        Deduplicated, uppercased promo code candidates.
        Empty list if none found or *text* is empty/None.

    Examples
    --------
    >>> extract_codes("Use promo code SUMMER50 for free spins!")
    ['SUMMER50']

    >>> extract_codes("BLAST99 or SPIN2WIN — both active now")
    ['SPIN2WIN', 'BLAST99']

    >>> extract_codes("FREE CSGO CASE DAILY — no codes here")
    []

    >>> extract_codes("")
    []
    """
    if not text:
        return []

    upper    = text.upper()
    seen:    set[str] = set()
    results: list[str] = []

    for match in _CODE_RE.finditer(upper):
        token = match.group(1)           # already uppercased
        if token in BLACKLIST:
            continue
        if token not in seen:
            seen.add(token)
            results.append(token)

    # Sort: longest first (more specific codes first), then alpha for stability
    results.sort(key=lambda c: (-len(c), c))
    return results


def is_blacklisted(token: str) -> bool:
    """
    Return True if *token* (case-insensitive) is on the blacklist.

    Parameters
    ----------
    token : str
        A single word or token to check.

    Returns
    -------
    bool

    Examples
    --------
    >>> is_blacklisted("FREE")
    True
    >>> is_blacklisted("free")   # case-insensitive
    True
    >>> is_blacklisted("SUMMER50")
    False
    """
    return token.upper() in BLACKLIST


# ---------------------------------------------------------------------------
# Standalone tests / examples  (run with:  python utils/code_detector.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    _PASS = "\033[32mPASS\033[0m"
    _FAIL = "\033[31mFAIL\033[0m"

    all_ok = True

    def _check(label: str, got, expected) -> bool:
        global all_ok
        ok = got == expected
        if not ok:
            all_ok = False
        status = _PASS if ok else _FAIL
        print(f"  [{status}] {label}")
        if not ok:
            print(f"           Got:      {got!r}")
            print(f"           Expected: {expected!r}")
        return ok

    print("=" * 60)
    print("  utils/code_detector.py -- self-test")
    print("=" * 60)

    # ── 1. Happy path ─────────────────────────────────────────────────────
    print("\n[1] Basic extraction")
    _check(
        "Single code with context",
        extract_codes("Use promo code SUMMER50 for free spins!"),
        ["SUMMER50"],
    )
    _check(
        "Two codes in one message",
        extract_codes("BLAST99 or SPIN2WIN - both active now"),
        ["SPIN2WIN", "BLAST99"],   # longest first
    )
    _check(
        "Code mixed in normal sentence",
        extract_codes("Don't miss code WELCOME100 - limited offer!"),
        ["WELCOME100"],
    )
    _check(
        "Digit-only code (valid: all chars are [A-Z0-9])",
        extract_codes("Use code 1234 for 50% off"),
        ["1234"],
    )

    # ── 2. Normalisation ──────────────────────────────────────────────────
    print("\n[2] Case normalisation")
    _check(
        "Lowercase input uppercased",
        extract_codes("use code summer50 now"),
        ["SUMMER50"],
    )
    _check(
        "Mixed case token uppercased",
        extract_codes("Enter Welcome100 at checkout"),
        ["WELCOME100"],
    )
    _check(
        "Already uppercase unchanged",
        extract_codes("XMAS2024"),
        ["XMAS2024"],
    )

    # ── 3. Blacklist ──────────────────────────────────────────────────────
    print("\n[3] Blacklist filtering")
    _check(
        "FREE blocked",
        extract_codes("FREE CSGO CASE DAILY"),
        [],
    )
    _check(
        "All spec-required blacklist words blocked",
        extract_codes("FREE spin on this CSGO CASE every DAILY!"),
        [],
    )
    _check(
        "Mixed blacklisted + valid code",
        extract_codes("FREE case — use XMAS50 now!"),
        ["XMAS50"],
    )
    _check(
        "Platform names blocked",
        extract_codes("Check DISCORD REDDIT TWITTER for updates"),
        [],
    )
    _check(
        "is_blacklisted() uppercase",
        is_blacklisted("FREE"),
        True,
    )
    _check(
        "is_blacklisted() lowercase",
        is_blacklisted("free"),
        True,
    )
    _check(
        "is_blacklisted() valid code",
        is_blacklisted("SUMMER50"),
        False,
    )

    # ── 4. Deduplication ─────────────────────────────────────────────────
    print("\n[4] Deduplication")
    _check(
        "Same code repeated twice",
        extract_codes("BLAST50 is the code. Use BLAST50 before midnight!"),
        ["BLAST50"],
    )
    _check(
        "Same code in different case",
        extract_codes("blast50 and BLAST50 and Blast50"),
        ["BLAST50"],
    )
    _check(
        "Three distinct codes, no dupes",
        extract_codes("Codes: ALPHA1 BETA22 GAMMA333"),
        ["GAMMA333", "ALPHA1", "BETA22"],  # GAMMA333(8) > 6-char; ALPHA < BETA alpha
    )

    # -- 5. Length boundary conditions --
    print("\n[5] Length boundaries (4-15 chars)")
    _check(
        "3-char token rejected (too short)",
        extract_codes("ABC XYZ"),
        [],
    )
    _check(
        "4-char token accepted (minimum)",
        extract_codes("CODE ABCD"),   # ABCD passes; CODE is blacklisted
        ["ABCD"],
    )
    _check(
        "15-char token accepted (maximum)",
        extract_codes("ABCDEFGHIJKLMNO"),
        ["ABCDEFGHIJKLMNO"],
    )
    _check(
        "16-char token rejected (too long)",
        extract_codes("ABCDEFGHIJKLMNOP"),
        [],
    )

    # ── 6. Regex character set ────────────────────────────────────────────
    print("\n[6] Regex character set [A-Z0-9]")
    _check(
        "Letters only accepted",
        extract_codes("SUMMER"),
        ["SUMMER"],
    )
    _check(
        "Digits only accepted",
        extract_codes("12345"),
        ["12345"],
    )
    _check(
        "Mixed letters + digits accepted",
        extract_codes("BLAST99"),
        ["BLAST99"],
    )
    _check(
        "Hyphen breaks word boundary - each part evaluated separately",
        extract_codes("PROMO-CODE50"),   # splits at hyphen -> PROMO (blocked) + CODE50
        ["CODE50"],
    )
    _check(
        "Underscore: \\b treats underscore as word char in Python regex.\n"
        "  PROMO_BLAST50 contains underscore which is NOT in [A-Z0-9], so\n"
        "  the regex sees no valid 4-15 char [A-Z0-9]-only run -> no match.",
        extract_codes("PROMO_BLAST50"),
        [],  # underscore breaks the [A-Z0-9] run; neither PROMO nor BLAST50
             # forms a valid \b-bounded [A-Z0-9]{4,15} token here
    )
    _check(
        "Special chars ignored, adjacent tokens extracted",
        extract_codes("WELCOME100! #SPIN999."),
        ["WELCOME100", "SPIN999"],
    )

    # ── 7. Sort order ─────────────────────────────────────────────────────
    print("\n[7] Sort order (longest first, then alpha)")
    _check(
        "Longest code first",
        extract_codes("A123 ABCDE12345 AB12"),
        ["ABCDE12345", "A123", "AB12"],  # 10 > 4 = 4; among 4-char: 'A123' < 'AB12' (ord '1' < ord 'B')
    )
    _check(
        "Same length -> alphabetical",
        extract_codes("ZZZ1 AAA1 MMM1"),
        ["AAA1", "MMM1", "ZZZ1"],
    )

    # ── 8. Edge cases ─────────────────────────────────────────────────────
    print("\n[8] Edge cases")
    _check("Empty string", extract_codes(""), [])
    _check("None-like empty", extract_codes("   "), [])
    _check("Only punctuation", extract_codes("!!! ???"), [])
    _check("Only numbers in text", extract_codes("123 4567"), ["4567"])
    _check(
        "Real-world Discord message",
        extract_codes(
            "@everyone NEW promo code: BLAST50 - use it now! "
            "Valid for CSGO CASE opening. FREE spins included."
        ),
        ["BLAST50"],
    )
    _check(
        "Real-world Instagram caption",
        extract_codes(
            "New drop alert! Open the Instagram case for FREE "
            "with code INSTA2024 Use daily for bonus DAILY spins!"
        ),
        ["INSTA2024"],
    )
    _check(
        "Real-world tweet with multiple codes",
        extract_codes(
            "Two promos live: SUMMER50 and BONUS25! "
            "CSGO cases updated with new FREE daily rewards."
        ),
        ["SUMMER50", "BONUS25"],
    )

    print()
    print("=" * 60)
    print(f"  Result: {'ALL TESTS PASSED' if all_ok else 'SOME TESTS FAILED'}")
    print("=" * 60)
    sys.exit(0 if all_ok else 1)
