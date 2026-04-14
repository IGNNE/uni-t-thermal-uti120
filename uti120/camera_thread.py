"""Background thread for USB camera acquisition and recording."""

from __future__ import annotations

import logging
import struct
import time
from threading import Thread, Event

import usb.core
import numpy as np

from .constants import (
    DISPLAY_WIDTH,
    DISPLAY_HEIGHT,
    STATUS_IDLE,
    STATUS_IMAGE_UPLOAD,
    RECONNECT_FAIL_THRESHOLD,
)
from .camera import UTi120Camera
from .processor import FrameProcessor
from .calibration import (
    load_calibration_cache,
    save_calibration_cache,
    CalibrationPackage,
)
from .shutter_handler import ShutterHandler
from .config import DaemonConfig

__all__ = ["CameraThread"]

logger = logging.getLogger(__name__)


class CameraThread(Thread):
    """USB camera loop running on a background thread."""

    def __init__(self, config: DaemonConfig) -> None:
        super().__init__()
        self.camera = UTi120Camera()
        self.processor = FrameProcessor(config)
        self.shutter_handler = ShutterHandler()
        self.running = False
        self._do_shutter = False
        self._do_nuc = False

        self.event_frame_ready: Event = Event()
        self.current_frame: np.ndarray = np.zeros(0)  # colored bgr ndarray

    def run(self) -> None:
        self.running = True

        # --- Init camera ---
        if not self.camera.find_and_connect():
            raise FileNotFoundError("Failed to connect. Is the camera plugged in?")
            return

        info = self.camera.get_device_info()
        info_str = ", ".join(f"{k}: {v}" for k, v in info.items())
        logger.info("Device info: %s", info_str)
        logger.info(f"Connected: {info_str}")

        # Calibration points (fallback)
        cal_points = self.camera.read_calibration_points()
        if not cal_points:
            raise IOError("Failed to read calibration points from device.")
            return
        self.processor.set_calibration(cal_points)

        # Factory calibration packages (serial-aware cache)
        serial = info.get("serial", "")
        logger.info("Device serial: %s", serial)
        calib_pkgs = load_calibration_cache(serial)
        if calib_pkgs:
            logger.info("Calibration cache valid for serial %s", serial)
            logger.info("Loaded calibration from cache")
        else:
            logger.info(
                "Calibration cache miss — downloading from device " "(serial=%s)",
                serial,
            )
            calib_pkgs = {}
            raw_data: dict[int, bytes] = {}
            for range_id, label in [(0, "low-temp"), (1, "high-temp")]:
                pkg_data = self.camera.download_calibration_package(range_id)
                if pkg_data:
                    try:
                        calib_pkgs[range_id] = CalibrationPackage(data=pkg_data)
                        raw_data[range_id] = pkg_data
                        logger.info(f"Downloaded {label} calibration")
                    except (struct.error, ValueError, AssertionError) as e:
                        logger.warning(f"{label} parse failed: {e}")
            if raw_data:
                save_calibration_cache(serial, raw_data.get(0), raw_data.get(1))

        low_pkg = calib_pkgs.get(0)
        high_pkg = calib_pkgs.get(1)
        if low_pkg:
            self.processor.set_calibration_packages(low_pkg, high_pkg)

        # Set image upload mode
        self.camera.set_run_status(STATUS_IDLE)
        time.sleep(0.2)
        # Drain stale bulk data to prevent frame misalignment on startup
        drained = self.camera._drain_bulk()
        if drained:
            logger.debug("Drained %d stale bytes from bulk endpoint", drained)
        self.camera.set_run_status(STATUS_IMAGE_UPLOAD)
        time.sleep(0.3)

        # Initial NUC
        logger.info("Initial NUC...")
        self.camera.trigger_shutter()
        time.sleep(0.5)
        # Drain again after shutter (trigger_shutter re-enables streaming)
        self.camera._drain_bulk()
        for _ in range(10):
            self.camera.request_frame()
            time.sleep(0.02)

        # Initial dark frame
        self._do_shutter_calibration()

        # Warmup
        for _ in range(5):
            self.camera.request_frame()

        logger.info("Streaming")

        # --- Main frame loop ---
        fail_count = 0

        while self.running:
            # Handle pending commands
            if self._do_shutter:
                self._do_shutter = False
                logger.info("Shutter calibration...")
                self._do_shutter_calibration()
                logger.info("Streaming")
                continue

            if self._do_nuc:
                self._do_nuc = False
                logger.info("NUC calibration...")
                self.camera.trigger_shutter()
                time.sleep(0.5)
                self._do_shutter_calibration()
                self.shutter_handler.did_nuc(self.processor.fpa_temp)
                logger.info("Streaming")
                continue

            # Read frame
            try:
                raw = self.camera.request_frame()
                if raw is None:
                    fail_count += 1
                    if fail_count > RECONNECT_FAIL_THRESHOLD:
                        logger.warning(
                            f"More than {RECONNECT_FAIL_THRESHOLD} failed frames, reconnecting"
                        )
                        raise IOError()
                    else:
                        continue
            except (usb.core.USBError, IOError):
                if not self.camera.reconnect():
                    logger.error("Connection lost")
                    raise IOError("Connection lost")
                fail_count = 0
                continue

            fail_count = 0

            # Process
            start_time = time.time()
            colored = self.processor.process(raw)
            if colored is None:
                continue
            logger.debug(f"processing took {time.time() - start_time} s")

            # Auto-recalibration
            action = self.shutter_handler.check(
                self.processor.fpa_temp, self.processor.frame_counter
            )
            if action == "nuc":
                logger.info(f"Auto-NUC (FPA={self.processor.fpa_temp:.2f}°C)")
                self.camera.trigger_shutter()
                time.sleep(0.5)
                self._do_shutter_calibration()
                self.shutter_handler.did_nuc(self.processor.fpa_temp)
                continue
            elif action == "shutter":
                logger.info(f"Auto-shutter (FPA={self.processor.fpa_temp:.2f}°C)")
                self._do_shutter_calibration()
                self.shutter_handler.did_shutter(self.processor.fpa_temp)
                continue

            # Range switching
            new_range = self.processor.check_range_switch()
            if new_range is not None:
                label = "HIGH" if new_range == 1 else "LOW"
                logger.info(f"Switching to {label} range...")
                pkg = self.processor.switch_range(new_range)
                self.camera.set_measure_range(
                    pkg.sensor_gain, pkg.sensor_int, pkg.sensor_res
                )
                time.sleep(0.3)
                self._do_shutter_calibration()
                logger.info("Streaming")
                continue

            start_time = time.time()
            self.current_frame = self.processor.upscale(
                colored, DISPLAY_WIDTH, DISPLAY_HEIGHT
            )
            logger.debug(f"upscaling took {time.time() - start_time} s")
            self.event_frame_ready.set()

        self.camera.close()

    def _do_shutter_calibration(self) -> None:
        result = self.camera.trigger_shutter_with_dark_capture()
        dark, shutter_temp, lens_temp, fpa_temp = result
        if dark is not None:
            self.processor.set_dark_frame(dark, shutter_temp, lens_temp, fpa_temp)
        else:
            self.camera.trigger_shutter()
        time.sleep(0.3)
        for _ in range(3):
            self.camera.request_frame()
