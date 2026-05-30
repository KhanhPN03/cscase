"""
utils/logger.py
---------------
Structured, colored console logger for the CSGOCASES promo monitor.
Outputs ISO-8601 timestamps so GitHub Actions logs are easy to read.
"""

import io

import logging
import sys
from datetime import timezone


class _ColorFormatter(logging.Formatter):
    """Add ANSI color codes to log levels (stripped by GitHub Actions automatically)."""

    COLORS = {
        logging.DEBUG:    "\033[36m",   # Cyan
        logging.INFO:     "\033[32m",   # Green
        logging.WARNING:  "\033[33m",   # Yellow
        logging.ERROR:    "\033[31m",   # Red
        logging.CRITICAL: "\033[35m",   # Magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, self.RESET)
        record.levelname = f"{color}{record.levelname:<8}{self.RESET}"
        return super().format(record)


def get_logger(name: str = "cscase_monitor") -> logging.Logger:
    """Return a configured logger. Safe to call multiple times — same instance."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.DEBUG)

    # Force UTF-8 on Windows so arrows/emoji don't crash on CP1252 terminals
    stdout_utf8 = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace") \
        if hasattr(sys.stdout, "buffer") else sys.stdout
    handler = logging.StreamHandler(stdout_utf8)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        _ColorFormatter(
            fmt="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
    )
    # Force UTC timestamps
    handler.formatter.converter = lambda *_: __import__("time").gmtime()

    logger.addHandler(handler)
    return logger
