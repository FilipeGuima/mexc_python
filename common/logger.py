"""
Centralized logging configuration for all trading bots.

Usage:
    from common.logger import setup_logging

    # In bot startup (engine.run() or standalone script):
    logger = setup_logging("MEXC TP1 BOT")

    # In other modules (no setup needed, just get the logger):
    import logging
    logger = logging.getLogger("MEXC TP1 BOT")
"""

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOGS_DIR = _PROJECT_ROOT / "logs"


def _sanitize_filename(name: str) -> str:
    """Convert a bot name like 'MEXC TP1 BOT (Smart Entry)' to 'mexc_tp1_bot_smart_entry'."""
    sanitized = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')
    return sanitized


def setup_logging(
    bot_name: str,
    log_level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    """
    Configure centralized logging with both console and file output.

    Call this ONCE at bot startup. Returns the named logger for the bot.
    All other modules that use logging.getLogger() will automatically
    inherit the handlers configured here via the root logger.

    Args:
        bot_name: Human-readable bot identifier (e.g., strategy.name).
        log_level: Minimum log level (default: INFO).
        max_bytes: Max size per log file before rotation (default: 10 MB).
        backup_count: Number of rotated backup files to keep (default: 5).

    Returns:
        Configured logging.Logger instance for this bot.
    """
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)

    filename = _sanitize_filename(bot_name)
    log_file = _LOGS_DIR / f"{filename}.log"

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    logger = logging.getLogger(bot_name)
    logger.info(f"Logging initialized -> {log_file}")

    return logger
