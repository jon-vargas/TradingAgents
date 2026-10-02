"""
Structured Logging Configuration (Feature 19)

Provides a consistent logging setup across all TradingAgents modules.
Replaces ad-hoc print() statements with hierarchical, level-aware logging.

Usage:
    from tradingagents.utils.logging_config import setup_logging
    setup_logging()  # Call once at application startup

    # In any module:
    import logging
    logger = logging.getLogger("tradingagents.screening")
    logger.info("Scanning %d tickers", count)
"""

import logging
import logging.handlers
import os
import sys


def setup_logging(
    level: str = "INFO",
    log_dir: str = "logs",
    log_file: str = "tradingagents.log",
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
    console: bool = True,
):
    """Configure structured logging for the TradingAgents application.

    Sets up:
    - Console handler (colored, concise format)
    - Rotating file handler (detailed format, 10 MB max, 5 backups)
    - Hierarchical loggers for each subsystem

    Args:
        level: Root log level (DEBUG, INFO, WARNING, ERROR)
        log_dir: Directory for log files (created if not exists)
        log_file: Log file name
        max_bytes: Max size per log file before rotation
        backup_count: Number of rotated log files to keep
        console: Whether to output to console (in addition to file)
    """
    root = logging.getLogger("tradingagents")
    # Avoid duplicate handlers if called multiple times
    if root.handlers:
        return
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # --- Console handler (concise, colored) ---
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_fmt = logging.Formatter(
            "[%(levelname).1s] %(name)s: %(message)s"
        )
        console_handler.setFormatter(console_fmt)
        root.addHandler(console_handler)

    # --- File handler (detailed, rotating) ---
    try:
        os.makedirs(log_dir, exist_ok=True)
        file_path = os.path.join(log_dir, log_file)
        file_handler = logging.handlers.RotatingFileHandler(
            file_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d): %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_fmt)
        root.addHandler(file_handler)
    except OSError:
        # Can't create log dir (e.g., read-only filesystem) — file logging disabled
        pass

    # --- Quiet down noisy third-party loggers ---
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("peewee").setLevel(logging.WARNING)
