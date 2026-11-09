"""Logging configuration for the CLI."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"


def setup_logging(verbose: bool = False, log_file: str | Path | None = None) -> None:
    """Configure root logging.

    Args:
        verbose: DEBUG level (and chatty third-party loggers) when True, else INFO.
        log_file: Optional path; the log is also written there (always DEBUG level).
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    root.addHandler(console)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(file_handler)

    noisy_level = logging.DEBUG if verbose else logging.WARNING
    for name in ("urllib3", "charset_normalizer", "asyncio"):
        logging.getLogger(name).setLevel(noisy_level)
