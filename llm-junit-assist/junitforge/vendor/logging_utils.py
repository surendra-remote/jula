"""Logging helpers for junitforge."""

from __future__ import annotations

import logging
import sys


def configure(level: str = "info") -> None:
    """Configure concise junitforge logging and suppress noisy third-party DEBUG logs."""
    target_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )

    # Only junitforge becomes verbose. Libraries such as urllib3 stay quiet.
    logging.getLogger("junitforge").setLevel(target_level)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)