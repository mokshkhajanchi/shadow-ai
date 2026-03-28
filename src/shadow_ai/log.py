"""
Logging setup — rotating file handler + console handler.
Extracted from bot.py lines 341-350.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler


def setup_logging(log_file: str) -> logging.Logger:
    """
    Configure and return the application logger.

    Creates:
    - A RotatingFileHandler (10MB max, 5 backups)
    - A StreamHandler for stdout
    Both use the same timestamp/level/name/message format.
    """
    fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    datefmt = "%H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)

    # Console handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    # Rotating file handler
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    # Configure root logging
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt=datefmt,
        handlers=[console_handler, file_handler],
    )

    logger = logging.getLogger("slack-claude-code")
    return logger
