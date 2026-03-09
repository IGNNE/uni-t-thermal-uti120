"""UNI-T UTi120 Mobile Thermal Camera - Linux USB Viewer."""

from __future__ import annotations

import logging

__all__ = ["__version__"]

__version__ = "1.0.0"

logging.getLogger(__name__).addHandler(logging.NullHandler())
