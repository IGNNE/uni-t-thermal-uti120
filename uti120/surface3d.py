"""3D surface plot of thermal data using pyqtgraph OpenGL."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel

import pyqtgraph.opengl as gl

from .constants import FRAME_WIDTH, FRAME_HEIGHT, DISPLAY_WIDTH, DISPLAY_HEIGHT
from .palettes import apply_palette

__all__ = ["ThermalSurface3D"]

if TYPE_CHECKING:
    from .processor import FrameProcessor


class ThermalSurface3D(QWidget):
    """Interactive 3D surface plot of the thermal temperature map."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frame_count = 0
        self._recording: bool = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._glw = gl.GLViewWidget()
        self._glw.setCameraPosition(distance=150, elevation=30, azimuth=-45)
        self._glw.pan(FRAME_WIDTH / 2, FRAME_HEIGHT / 2, 0)
        self._glw.setBackgroundColor((45, 45, 45, 255))
        layout.addWidget(self._glw)

        # Surface plot item — start with flat plane
        # GLSurfacePlotItem expects z as (x, y) i.e. (width, height)
        z = np.zeros((FRAME_WIDTH, FRAME_HEIGHT), dtype=np.float32)
        self._surface = gl.GLSurfacePlotItem(
            z=z, smooth=False, computeNormals=False,
        )
        self._surface.setGLOptions('opaque')
        self._glw.addItem(self._surface)

        # Reference grid — positioned to match the surface's X/Y range
        self._grid = gl.GLGridItem()
        self._grid.setSize(FRAME_WIDTH, FRAME_HEIGHT, 0)
        self._grid.setSpacing(10, 10, 0)
        # Surface vertices span X=0..119, Y=0..89; grid is centered at origin
        # so translate grid center to match surface center
        self._grid.translate(FRAME_WIDTH / 2, FRAME_HEIGHT / 2, 0)
        self._glw.addItem(self._grid)

        # On-screen controls hint (overlaid on the GL widget, fades after 5s)
        self._hint = QLabel(
            "Left-drag: Rotate  |  Ctrl+drag / Middle-drag: Pan  |  Scroll: Zoom",
            self._glw,
        )
        self._hint.setStyleSheet(
            "background: rgba(0,0,0,160); color: #ccc; padding: 4px 8px;"
            "border-radius: 4px; font-size: 11px;"
        )
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.adjustSize()
        self._hint.move(8, 8)

        # Recording indicator (overlaid on GL widget, top-right)
        self._rec_label = QLabel("\u25cf REC", self._glw)
        self._rec_label.setStyleSheet(
            "background: rgba(0,0,0,160); color: #ff3c3c; padding: 4px 8px;"
            "border-radius: 4px; font-size: 11px; font-weight: bold;"
        )
        self._rec_label.adjustSize()
        self._rec_label.hide()

        self.setMinimumSize(0, 0)

    def update_frame(self, display_bgr: np.ndarray, processor: FrameProcessor) -> None:
        """Update the 3D surface with new thermal data."""
        # Update every 2nd frame to reduce GPU load
        self._frame_count += 1
        if self._frame_count % 2 != 0:
            return

        temp_map = processor._temp_map
        if temp_map is None:
            return

        z = temp_map.copy()
        if processor.flip:
            z = np.flip(z, axis=1)
        if processor.rotation == 90:
            z = np.rot90(z, k=-1)
        elif processor.rotation == 180:
            z = np.rot90(z, k=2)
        elif processor.rotation == 270:
            z = np.rot90(z, k=1)

        # GLSurfacePlotItem expects (width, height) layout — transpose from (H,W) to (W,H)
        z = z.T
        colors = self._make_vertex_colors(z, processor)
        # MeshData.setVertexColors expects (N_vertices, 4), not (W, H, 4)
        colors = colors.reshape(-1, 4)

        # Scale Z so temperature relief is visible relative to the X/Y grid
        z_min = z.min()
        z_range = z.max() - z_min
        if z_range < 0.1:
            z_range = 0.1
        z_scaled = (z - z_min) / z_range * FRAME_HEIGHT * 0.5

        self._surface.setData(z=z_scaled, colors=colors)

        # Update recording indicator
        if self._recording:
            self._rec_label.move(self._glw.width() - self._rec_label.width() - 8, 8)
            self._rec_label.show()
        else:
            self._rec_label.hide()

    def _make_vertex_colors(self, temp_map: np.ndarray, processor: FrameProcessor) -> np.ndarray:
        """Convert temperature map to RGBA vertex colors using active palette."""
        t_min = processor.min_temp
        t_range = processor.max_temp - t_min
        if t_range < 0.1:
            t_range = 0.1

        normalized = np.clip(
            (temp_map - t_min) / t_range * 255, 0, 255
        ).astype(np.uint8)

        bgr = apply_palette(normalized, processor.palette_idx)

        rgba = np.zeros((*temp_map.shape, 4), dtype=np.float32)
        rgba[:, :, 0] = bgr[:, :, 2] / 255.0  # R
        rgba[:, :, 1] = bgr[:, :, 1] / 255.0  # G
        rgba[:, :, 2] = bgr[:, :, 0] / 255.0  # B
        rgba[:, :, 3] = 1.0
        return rgba

    def render_composited_frame(self) -> np.ndarray:
        """Grab the current 3D view as a BGR numpy array for recording."""
        import cv2
        from PyQt6.QtGui import QImage

        self._rec_label.hide()
        pixmap = self.grab(self.rect())
        if self._recording:
            self._rec_label.show()
        qimage = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB888)
        qimage = qimage.scaled(DISPLAY_WIDTH, DISPLAY_HEIGHT)
        ptr = qimage.bits()
        ptr.setsize(qimage.sizeInBytes())
        arr = np.frombuffer(ptr, dtype=np.uint8).reshape(
            DISPLAY_HEIGHT, DISPLAY_WIDTH, 3).copy()
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

    def sizeHint(self) -> QSize:
        return QSize(DISPLAY_WIDTH, DISPLAY_HEIGHT)
