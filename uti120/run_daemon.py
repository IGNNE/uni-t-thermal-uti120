"""Main thermal daemon application."""

from __future__ import annotations
from PyQt6.QtCore import QCoreApplication

import logging

from .daemon import Daemon
from .config import argparse_config, DaemonConfig

def main() -> None:
    """Entry point for the thermal daemon."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    config = argparse_config()
    daemon = Daemon(config=config)
    app = QCoreApplication([])
    daemon.start()
    app.exec()