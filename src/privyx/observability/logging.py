"""Structured logging configuration."""

from __future__ import annotations

import logging
import sys

from privyx.config.schema import Settings


def configure_logging(settings: Settings) -> None:
    """Set up Python logging based on Privyx settings."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    if settings.logging == "json":
        # JSON structured logging — placeholder
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            '{"time": "%(asctime)s", "level": "%(levelname)s", '
            '"logger": "%(name)s", "message": "%(message)s"}'
        )
        handler.setFormatter(formatter)
        logging.basicConfig(handlers=[handler], level=level)
    else:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            stream=sys.stdout,
        )