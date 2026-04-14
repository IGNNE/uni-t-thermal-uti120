"""Headless camera daemon"""

import logging
import subprocess
import os.path
from typing import Tuple

import cv2
import numpy as np

from PyQt6.QtCore import pyqtSlot, QObject


from .processor import FrameProcessor
from .camera_thread import CameraThread
from .constants import DISPLAY_WIDTH, DISPLAY_HEIGHT, FRAME_HEIGHT, FRAME_WIDTH
from .palettes import apply_palette
from .config import DaemonConfig

logger = logging.getLogger(__name__)


class Daemon(QObject):

    def __init__(
        self,
        config: DaemonConfig,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)

        # Camera thread
        self.cam_thread = CameraThread(config)
        self.cam_thread.frame_ready.connect(self._on_frame)

        self.ffmpeg_process: None | subprocess.Popen = None

        self.config = config

    def start(self) -> None:
        assert self.ffmpeg_process is None and not self.cam_thread.isRunning()

        if not os.path.exists(self.config.dev_video_file):
            logger.error(
                f"Webcam file {self.config.dev_video_file} does not exist. You need to set up "
                + "v4l2loopback and point this daemon at the newly created /dev/videoX"
            )
            raise FileNotFoundError("Webcam file does not exist")

        # one-time setup of ffmpeg
        # I am deliberately not using PyAV, there are enough dependencies in the world already
        # plus, plain old `ffmpeg -do-stuff` does the job just fine
        #
        self.ffmpeg_process = subprocess.Popen(
            (
                "ffmpeg -y -f rawvideo -vcodec rawvideo -pix_fmt bgr24 "
                + f"-s {DISPLAY_WIDTH}x{DISPLAY_HEIGHT} "
                + f"-r 25 -i - -vf format=yuv420p -f v4l2 {self.config.dev_video_file}"
            ).split(),
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        # hack for debugging
        if self.config.debug_ffmpeg:
            import threading

            def print_ffmpeg_output():
                for line in self.ffmpeg_process.stdout:
                    logger.info(" ffmpeg " + line.decode("utf-8").strip())

            threading.Thread(target=print_ffmpeg_output, daemon=True).start()
        self.cam_thread.start()
        logger.info("Daemon started")

    @pyqtSlot(object, object)
    def _on_frame(self, display_bgr: np.ndarray, processor: FrameProcessor) -> None:
        # logger.info("new frame")
        # no special mode support for now

        h, w = display_bgr.shape[:2]

        self._draw_overlay(display_bgr, processor, w, h)

        # no timestamp, that is the job of a generic OSD, not our job

        self.ffmpeg_process.stdin.write(display_bgr)
        self.ffmpeg_process.stdin.write(display_bgr)

    def _draw_overlay(self, frame: np.ndarray, proc: FrameProcessor, w: int, h: int):
        """Draw temperature overlay elements using OpenCV

        Bits and pieces vibe-coded based on a down-stripped version of the original function
        (which was also vibe-coded, so nothing of value was lost)

        """

        def pretty_put_text(frame: np.ndarray, text: str, coords: Tuple[int, int]):
            cv2.putText(
                frame,
                text,
                coords,
                cv2.FONT_HERSHEY_DUPLEX,
                0.5,
                (0, 0, 0),
                2,
                cv2.LINE_AA,
            )
            cv2.putText(
                frame,
                text,
                coords,
                cv2.FONT_HERSHEY_DUPLEX,
                0.5,
                (255, 255, 255),
                1,
                cv2.LINE_AA,
            )

        # TODO: wait a minute. Do we overwrite some of our already-scarce pixels for the colorbar?
        # it is the same in the old qt version, btw
        # why even?! we are upscaling anyway, screen pixels are much cheaper than thermal pixels!
        # actually, now that I think about it, the color bar is nice, but not terribly required

        if self.config.show_center_temp:
            cx, cy = w // 2, h // 2
            cross = 15
            cv2.line(frame, (cx - cross, cy), (cx + cross, cy), (0, 0, 0), 2)
            cv2.line(frame, (cx, cy - cross), (cx, cy + cross), (0, 0, 0), 2)
            cv2.line(frame, (cx - cross, cy), (cx + cross, cy), (255, 255, 255), 1)
            cv2.line(frame, (cx, cy - cross), (cx, cy + cross), (255, 255, 255), 1)
            center_text = f"CEN {proc.center_temp:.1f}C"
            text_x = cx + 15
            text_y = cy + 5
            pretty_put_text(frame, center_text, (text_x, text_y))

        if self.config.show_min_max_temp:
            mx, my = self._sensor_to_img(proc.max_pos[0], proc.max_pos[1])
            cv2.line(frame, (mx, my - 6), (mx - 5, my + 4), (0, 0, 255), 2)
            cv2.line(frame, (mx - 5, my + 4), (mx + 5, my + 4), (0, 0, 255), 2)
            cv2.line(frame, (mx + 5, my + 4), (mx, my - 6), (0, 0, 255), 2)
            max_text = f"MAX {proc.max_temp:.1f}C"
            text_x = mx + 10
            text_y = my + 4
            pretty_put_text(frame, max_text, (text_x, text_y))

            nx, ny = self._sensor_to_img(proc.min_pos[0], proc.min_pos[1])
            cv2.line(frame, (nx, ny + 6), (nx - 5, ny - 4), (255, 0, 0), 2)
            cv2.line(frame, (nx - 5, ny - 4), (nx + 5, ny - 4), (255, 0, 0), 2)
            cv2.line(frame, (nx + 5, ny - 4), (nx, ny + 6), (255, 0, 0), 2)
            min_text = f"MIN {proc.min_temp:.1f}C"
            text_x = nx + 10
            text_y = ny + 8
            pretty_put_text(frame, min_text, (text_x, text_y))

        if self.config.show_colorbar:
            bar_w = 20
            bar_x = w - bar_w - 45
            bar_top = 30
            bar_bottom = h - 30
            bar_h = bar_bottom - bar_top

            gradient = np.linspace(255, 0, bar_h, dtype=np.uint8).reshape(-1, 1)
            bar_colored = apply_palette(gradient, proc.palette)
            bar_strip = np.repeat(bar_colored, bar_w, axis=1)

            # Draw the color bar
            frame[bar_top:bar_bottom, bar_x : bar_x + bar_w] = bar_strip

            # Draw border
            cv2.rectangle(
                frame, (bar_x, bar_top), (bar_x + bar_w, bar_bottom), (200, 200, 200), 1
            )

            # Draw labels
            pretty_put_text(frame, f"{proc.max_temp:.0f}C", (bar_x - 5, bar_top - 5))
            pretty_put_text(
                frame, f"{proc.min_temp:.0f}C", (bar_x - 5, bar_bottom + 15)
            )

    def _sensor_to_img(
        self, sensor_x: int, sensor_y: int, rotation: int = 0, flip: bool = False
    ) -> tuple[int, int]:
        W, H = DISPLAY_WIDTH, DISPLAY_HEIGHT
        dx = (W - 1 - sensor_x) if flip else sensor_x
        dy = sensor_y
        if rotation == 90:
            dx, dy = H - 1 - dy, dx
            return round(dx * DISPLAY_WIDTH / FRAME_HEIGHT), round(
                dy * DISPLAY_HEIGHT / FRAME_WIDTH
            )
        elif rotation == 180:
            dx, dy = W - 1 - dx, H - 1 - dy
            return round(dx * DISPLAY_WIDTH / FRAME_WIDTH), round(
                dy * DISPLAY_HEIGHT / FRAME_HEIGHT
            )
        elif rotation == 270:
            dx, dy = dy, W - 1 - dx
            return round(dx * DISPLAY_WIDTH / FRAME_HEIGHT), round(
                dy * DISPLAY_HEIGHT / FRAME_WIDTH
            )
        else:
            return round(dx * DISPLAY_WIDTH / FRAME_WIDTH), round(
                dy * DISPLAY_HEIGHT / FRAME_HEIGHT
            )
