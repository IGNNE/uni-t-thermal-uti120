"""Main thermal daemon application."""

from __future__ import annotations

import logging

from .daemon import Daemon
from .config import argparse_config


def main() -> None:
    """Entry point for the thermal daemon."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(module)s %(message)s"
    )
    config = argparse_config()
    daemon = Daemon(config=config)
    daemon.start()
