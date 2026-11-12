"""
Cognetra 3.0 — Edge Client Logger Setup
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logger(logs_dir: str, level: int = logging.INFO) -> logging.Logger:
    """
    Set up rotating file logger + console handler for the edge client.
    Max 5MB per file, keep 3 backups.
    """
    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    log_path = Path(logs_dir) / "edge_client.log"

    logger = logging.getLogger("cognetra")
    logger.setLevel(level)

    if logger.handlers:
        return logger  # already configured

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file
    fh = RotatingFileHandler(
        str(log_path), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger
