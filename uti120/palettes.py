"""Color palettes for thermal image visualization."""

from __future__ import annotations

import numpy as np
import cv2

__all__ = ["PALETTES", "apply_palette"]


def make_iron_palette() -> np.ndarray:
    lut = np.zeros((256, 1, 3), dtype=np.uint8)
    for i in range(256):
        r = int(min(255, max(0, (i - 64) * 4)))
        g = int(min(255, max(0, (i - 128) * 4)))
        b = int(min(255, max(0, i * 2 if i < 128 else 255 - (i - 128) * 2)))
        lut[i, 0] = [b, g, r]
    return lut


def make_rainbow_palette() -> np.ndarray:
    lut = np.zeros((256, 1, 3), dtype=np.uint8)
    for i in range(256):
        lut[i, 0] = [int(i * 0.7), 255, 255]
    return cv2.cvtColor(lut, cv2.COLOR_HSV2BGR)


def make_whitehot_palette() -> np.ndarray:
    lut = np.zeros((256, 1, 3), dtype=np.uint8)
    for i in range(256):
        lut[i, 0] = [i, i, i]
    return lut


def make_blackhot_palette() -> np.ndarray:
    lut = np.zeros((256, 1, 3), dtype=np.uint8)
    for i in range(256):
        v = 255 - i
        lut[i, 0] = [v, v, v]
    return lut


PALETTES = [
    ("Iron", make_iron_palette()),
    ("Rainbow", make_rainbow_palette()),
    ("White Hot", make_whitehot_palette()),
    ("Black Hot", make_blackhot_palette()),
    ("Jet", None),
    ("Inferno", None),
]


def apply_palette(normalized_u8: np.ndarray, palette_idx: int) -> np.ndarray:
    """Apply a color palette to a normalized uint8 grayscale image."""
    name, lut = PALETTES[palette_idx % len(PALETTES)]
    if name == "Jet":
        return cv2.applyColorMap(normalized_u8, cv2.COLORMAP_JET)
    elif name == "Inferno":
        return cv2.applyColorMap(normalized_u8, cv2.COLORMAP_INFERNO)
    else:
        return cv2.LUT(cv2.merge([normalized_u8, normalized_u8, normalized_u8]), lut)
