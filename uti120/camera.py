"""USB communication with UNI-T UTi120 Mobile thermal camera."""

from __future__ import annotations

import logging
import time
import struct
import threading
import zlib

import numpy as np

try:
    import usb.core
    import usb.util
except ImportError as exc:
    raise ImportError("pyusb not installed. Run: pip install pyusb") from exc

logger = logging.getLogger(__name__)

from .constants import (
    USB_VID, USB_PID, FRAME_SIZE, BULK_CHUNK_SIZE, BULK_TIMEOUT_MS,
    BULK_MAX_ITERS, FUNC_WRITE_REG, FUNC_READ_REG, FUNC_SENSOR_CMD,
    REG_RUN_STATUS, STATUS_IDLE, STATUS_IMAGE_UPLOAD,
    SENSOR_SHUTTER, SENSOR_NUC, SENSOR_MEASURE_RANGE, CMD_REQUEST_FRAME,
    FRAME_WIDTH, FRAME_HEIGHT, FRAME_PIXELS, PIXEL_OFFSET,
    FUNC_TRANSFER, STATUS_PARAM_UPLOAD,
    TRANSFER_BEGIN, TRANSFER_CRC, TRANSFER_END, CALIB_CHUNK_SIZE,
    CALIB_HIGH_FLASH_ADDR, CALIB_LOW_FLASH_ADDR,
    REG_PKG_LENGTH_HIGH, REG_PKG_LENGTH_LOW,
)


class UTi120Camera:
    """USB communication with UTi120 Mobile thermal camera."""

    def __init__(self) -> None:
        self.dev = None
        self.bulk_in = None
        self.int_out = None
        self.int_in = None
        self._lock = threading.Lock()

    def find_and_connect(self) -> bool:
        """Find the UTi120 camera by VID:PID and connect."""
        logger.info("Searching for UTi120 camera...")

        self.dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID)
        if self.dev is None:
            # Fallback: scan for devices with matching interface layout
            for dev in usb.core.find(find_all=True):
                try:
                    cfg = dev.get_active_configuration()
                except usb.core.USBError:
                    try:
                        dev.set_configuration()
                        cfg = dev.get_active_configuration()
                    except usb.core.USBError:
                        continue
                if cfg.bNumInterfaces >= 2:
                    self.dev = dev
                    break

        if self.dev is None:
            logger.warning("No UTi120 camera found. Connected USB devices:")
            for dev in usb.core.find(find_all=True):
                try:
                    product = usb.util.get_string(dev, dev.iProduct) if dev.iProduct else "?"
                except (usb.core.USBError, ValueError):
                    product = "?"
                logger.warning("  %04x:%04x - %s", dev.idVendor, dev.idProduct, product)
            return False

        logger.info("Found camera: %04x:%04x", self.dev.idVendor, self.dev.idProduct)

        # Reset USB device for clean state
        try:
            self.dev.reset()
            time.sleep(0.5)
            self.dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID)
            if self.dev is None:
                logger.error("Camera lost after USB reset!")
                return False
        except usb.core.USBError:
            pass

        return self._setup_endpoints()

    def _setup_endpoints(self) -> bool:
        """Set up USB endpoints for communication."""
        try:
            for i in range(2):
                try:
                    if self.dev.is_kernel_driver_active(i):
                        self.dev.detach_kernel_driver(i)
                        logger.debug("Detached kernel driver from interface %d", i)
                except (usb.core.USBError, NotImplementedError):
                    pass

            try:
                self.dev.set_configuration()
            except usb.core.USBError:
                pass

            cfg = self.dev.get_active_configuration()

            # Interface 0: Bulk transfer (frame data)
            for ep in cfg[(0, 0)]:
                if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                    self.bulk_in = ep

            # Interface 1: Interrupt (commands)
            for ep in cfg[(1, 0)]:
                d = usb.util.endpoint_direction(ep.bEndpointAddress)
                if d == usb.util.ENDPOINT_OUT:
                    self.int_out = ep
                elif d == usb.util.ENDPOINT_IN:
                    self.int_in = ep

            usb.util.claim_interface(self.dev, 0)
            usb.util.claim_interface(self.dev, 1)

            logger.debug("Bulk IN:  EP 0x%02x (max %d)", self.bulk_in.bEndpointAddress, self.bulk_in.wMaxPacketSize)
            logger.debug("Int OUT:  EP 0x%02x", self.int_out.bEndpointAddress)
            logger.debug("Int IN:   EP 0x%02x", self.int_in.bEndpointAddress)
            return True

        except usb.core.USBError as e:
            logger.error("USB setup error: %s", e)
            logger.error("Try running with sudo, or set up a udev rule.")
            return False

    def close(self) -> None:
        """Release USB resources."""
        if self.dev:
            # Broad catches intentional: teardown must not raise
            try:
                self.set_run_status(STATUS_IDLE)
            except Exception:
                pass
            try:
                usb.util.release_interface(self.dev, 0)
                usb.util.release_interface(self.dev, 1)
            except Exception:
                pass
            try:
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
            self.dev = None

    def reconnect(self, max_attempts: int = 10, delay: float = 2.0) -> bool:
        """Attempt to reconnect to the camera after a USB disconnect."""
        logger.warning("Camera disconnected. Attempting to reconnect...")
        # Release stale resources
        try:
            usb.util.dispose_resources(self.dev)
        except usb.core.USBError:
            pass
        self.dev = None
        self.bulk_in = None
        self.int_out = None
        self.int_in = None

        for attempt in range(1, max_attempts + 1):
            logger.info("Reconnect attempt %d/%d...", attempt, max_attempts)
            time.sleep(delay)
            self.dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID)
            if self.dev is None:
                continue
            try:
                self.dev.reset()
                time.sleep(0.5)
                self.dev = usb.core.find(idVendor=USB_VID, idProduct=USB_PID)
                if self.dev is None:
                    continue
            except usb.core.USBError:
                continue
            if self._setup_endpoints():
                logger.info("Reconnected! Reinitializing...")
                try:
                    self.set_run_status(STATUS_IDLE)
                    time.sleep(0.2)
                    self.set_run_status(STATUS_IMAGE_UPLOAD)
                    time.sleep(0.3)
                    self.trigger_shutter()
                    time.sleep(0.5)
                except usb.core.USBError as e:
                    logger.warning("Warning during reinit: %s", e)
                return True
        logger.error("Failed to reconnect after all attempts.")
        return False

    def _send_interrupt(self, data: bytes, response_size: int = 0, timeout: int = 500) -> bytes | None:
        """Send data via interrupt OUT and optionally read response."""
        with self._lock:
            self.int_out.write(data, timeout=50)
            if response_size > 0:
                try:
                    resp = self.int_in.read(response_size, timeout=timeout)
                    return bytes(resp)
                except usb.core.USBTimeoutError:
                    return None
            return None

    def set_run_status(self, status: int) -> bool:
        """Set the device run status (0=idle, 2=image upload)."""
        value = struct.pack(">i", status)
        cmd = bytes([FUNC_WRITE_REG, REG_RUN_STATUS & 0xFF, 0x01]) + value
        resp = self._send_interrupt(cmd, response_size=64)
        if resp and len(resp) >= 3:
            return resp[0] == FUNC_WRITE_REG
        return False

    def shutter_close(self) -> None:
        cmd = bytes([FUNC_SENSOR_CMD, SENSOR_SHUTTER, 0x01]) + struct.pack(">i", 1)
        self._send_interrupt(cmd, response_size=64)

    def shutter_open(self) -> None:
        cmd = bytes([FUNC_SENSOR_CMD, SENSOR_SHUTTER, 0x01]) + struct.pack(">i", 0)
        self._send_interrupt(cmd, response_size=64)

    def trigger_nuc(self) -> None:
        cmd = bytes([FUNC_SENSOR_CMD, SENSOR_NUC, 0x01]) + struct.pack(">i", 1)
        self._send_interrupt(cmd, response_size=64)

    def set_measure_range(self, gain: int, integration: int, resolution: int) -> None:
        """Send SENSOR_MEASURE_RANGE command to switch hardware sensor params.

        Matches APK: sendSwitchTempLevelCmd(tempLevel) which sends
        [FUNC_SENSOR_CMD, 0x09, 0x01, 0, res, int, gain].
        """
        cmd = bytes([FUNC_SENSOR_CMD, SENSOR_MEASURE_RANGE, 0x01,
                     0, resolution, integration, gain])
        self._send_interrupt(cmd, response_size=64)

    def trigger_shutter(self) -> None:
        """Full shutter calibration: close -> NUC -> open."""
        logger.info("Triggering shutter calibration...")
        self.set_run_status(STATUS_IDLE)
        time.sleep(0.1)
        self.shutter_close()
        time.sleep(0.5)
        self.trigger_nuc()
        time.sleep(0.5)
        self.shutter_open()
        time.sleep(0.5)
        self.set_run_status(STATUS_IMAGE_UPLOAD)
        time.sleep(0.2)

    def trigger_shutter_with_dark_capture(self, n_frames: int = 5) -> tuple[np.ndarray | None, float | None, float | None, float | None]:
        """Shutter calibration with dark frame capture.

        Captures frames while the shutter is closed to build a dark frame
        reference, then performs NUC and reopens. The dark frame is the
        per-pixel DC offset that must be subtracted from raw Y16 before
        calibration curve lookup.

        Args:
            n_frames: Number of frames to average for dark frame (default 5)

        Returns:
            Dark frame as float32 array (90x120), or None on failure
        """
        logger.info("Triggering shutter calibration with dark capture...")

        # Read several live frames to get stable header temperatures
        # (single frames can be garbled during transitions)
        from .constants import HDR_SHUTTER_TEMP_RT, HDR_FP_TEMP, HDR_LENS_TEMP
        shutter_temp = None
        lens_temp = None
        fpa_temp = None
        fpa_temp_raw = 0
        for _ in range(10):
            raw = self.request_frame()
            if raw and len(raw) == FRAME_SIZE:
                shorts = np.frombuffer(raw, dtype='<u2')
                st = shorts[HDR_SHUTTER_TEMP_RT]
                lt = shorts[HDR_LENS_TEMP]
                fp = shorts[HDR_FP_TEMP]
                # Only accept reasonable temps (5-50°C range → 500-5000 raw)
                if 500 < st < 5000:
                    shutter_temp = st / 100.0
                    lens_temp = lt / 100.0
                    fpa_temp = fp / 100.0
                    fpa_temp_raw = int(fp)
            time.sleep(0.02)

        if shutter_temp:
            logger.info("  Pre-capture temps: shutter=%.1f°C lens=%.1f°C fpa=%.1f°C",
                        shutter_temp, lens_temp, fpa_temp)

        self.set_run_status(STATUS_IDLE)
        time.sleep(0.1)
        self.shutter_close()
        time.sleep(0.5)

        # Do NOT trigger hardware NUC here — it adjusts per-pixel offsets
        # internally, which flattens the signal and corrupts our software
        # NUC pipeline. We need the raw, unmodified sensor response at
        # shutter temperature as our dark frame reference.

        # Capture dark frames with shutter closed (raw sensor response)
        self.set_run_status(STATUS_IMAGE_UPLOAD)
        time.sleep(0.3)

        # Discard initial frames — sensor still settling after shutter close,
        # residual scene thermal signal causes ghost pattern in dark frame
        for _ in range(8):
            self.request_frame()
            time.sleep(0.02)

        dark_frames = []
        for _ in range(n_frames * 3):  # Try up to 3x to get enough frames
            raw = self.request_frame()
            if raw and len(raw) == FRAME_SIZE:
                shorts = np.frombuffer(raw, dtype='<u2')
                pixels = shorts[PIXEL_OFFSET:PIXEL_OFFSET + FRAME_PIXELS]
                if len(pixels) == FRAME_PIXELS:
                    dark_frames.append(
                        pixels.reshape(FRAME_HEIGHT, FRAME_WIDTH).astype(np.float32))
            if len(dark_frames) >= n_frames:
                break
            time.sleep(0.02)

        # Average dark frames
        dark_frame = None
        if dark_frames:
            dark_frame = np.mean(dark_frames, axis=0)
            logger.info("  Dark frame: %d frames, mean=%.0f, std=%.0f",
                        len(dark_frames), dark_frame.mean(), dark_frame.std())
            if shutter_temp is not None:
                logger.info("  Shutter temp at capture: %.1f°C", shutter_temp)

        # Open shutter and resume (no NUC — we handle correction in software)
        self.set_run_status(STATUS_IDLE)
        time.sleep(0.1)
        self.shutter_open()
        time.sleep(0.5)
        self.set_run_status(STATUS_IMAGE_UPLOAD)
        time.sleep(0.2)

        return dark_frame, shutter_temp, lens_temp, fpa_temp

    def request_frame(self) -> bytes | None:
        """Request next frame (0x81) and read via bulk transfer."""
        # Send frame request via interrupt endpoint
        self.int_out.write(CMD_REQUEST_FRAME, timeout=50)
        time.sleep(0.001)  # Device needs time to prepare bulk data

        # Read bulk data in chunks (matching APK's getDataByBulkTransfer_Ext_Block)
        frame_buf = bytearray(FRAME_SIZE)
        total = 0

        for it in range(BULK_MAX_ITERS):
            try:
                chunk = self.dev.read(
                    self.bulk_in.bEndpointAddress,
                    BULK_CHUNK_SIZE,
                    timeout=BULK_TIMEOUT_MS
                )
                if chunk:
                    n = len(chunk)
                    copy_len = min(n, FRAME_SIZE - total)
                    if copy_len > 0:
                        frame_buf[total:total + copy_len] = chunk[:copy_len]
                        total += copy_len
            except usb.core.USBTimeoutError:
                if it == 0:
                    return None  # First read failed — no data available
            except usb.core.USBError:
                raise  # Device disconnected — let caller handle reconnect

            if total >= FRAME_SIZE:
                break

        if total < FRAME_SIZE:
            return None

        return bytes(frame_buf)

    def _read_register_int(self, offset: int) -> int | None:
        """Read a single 4-byte register as a signed big-endian int."""
        resp = self._send_interrupt(
            bytes([FUNC_READ_REG, offset, 0x01]),
            response_size=6, timeout=1000,
        )
        if resp and len(resp) >= 6:
            return struct.unpack('>i', bytes(resp[2:6]))[0]
        return None

    VENDOR_NAMES = {0: "UNI-T", 1: "KT"}
    PRODUCT_NAMES = {0: "UTi120M", 1: "TI220", 2: "Thermal", 3: "LUTi120M"}

    def get_device_info(self) -> dict[str, str]:
        """Read device identification registers."""
        info = {}

        # Vendor / Factory ID (register 0x00)
        resp = self._send_interrupt(
            bytes([FUNC_READ_REG, 0x00, 1]),
            response_size=6
        )
        if resp and len(resp) >= 6:
            vid = struct.unpack('>I', bytes(resp[2:6]))[0]
            info['vendor'] = self.VENDOR_NAMES.get(vid, f"unknown({vid})")

        # Product ID (register 0x01)
        resp = self._send_interrupt(
            bytes([FUNC_READ_REG, 0x01, 1]),
            response_size=6
        )
        if resp and len(resp) >= 6:
            pid = struct.unpack('>I', bytes(resp[2:6]))[0]
            info['model'] = self.PRODUCT_NAMES.get(pid, f"unknown({pid})")

        # HW / SW versions (registers 0x02, 0x03)
        for name, offset in [('hw_version', 0x02), ('sw_version', 0x03)]:
            resp = self._send_interrupt(
                bytes([FUNC_READ_REG, offset, 1]),
                response_size=6
            )
            if resp and len(resp) >= 6:
                ver = struct.unpack('>I', bytes(resp[2:6]))[0]
                info[name] = f"{(ver >> 16) & 0xFF}.{(ver >> 8) & 0xFF}.{ver & 0xFF}"

        # Serial number (register 0x07, 5 words)
        resp = self._send_interrupt(
            bytes([FUNC_READ_REG, 0x07, 0x05]),
            response_size=22
        )
        if resp and len(resp) > 2:
            try:
                info['serial'] = bytes(resp[2:]).decode('utf-8', errors='ignore').strip('\x00')
            except (UnicodeDecodeError, ValueError):
                info['serial'] = resp[2:].hex()

        return info

    def read_calibration_points(self) -> list[tuple[float, float]] | None:
        """Read blackbody calibration points from device registers 17-34.

        Returns list of (reference_temp, camera_reading) tuples in °C,
        or None on failure. Values stored as int32 / 10000 on device.
        """
        points = []
        for i in range(9):
            base = self._read_register_int(17 + i * 2)
            real = self._read_register_int(18 + i * 2)
            if base is None or real is None:
                return None
            points.append((base / 10000.0, real / 10000.0))
        return points

    def _read_register_uint(self, offset: int) -> int | None:
        """Read a single 4-byte register as an unsigned big-endian int."""
        resp = self._send_interrupt(
            bytes([FUNC_READ_REG, offset, 0x01]),
            response_size=6, timeout=1000,
        )
        if resp and len(resp) >= 6:
            return struct.unpack('>I', bytes(resp[2:6]))[0]
        return None

    def _drain_bulk(self) -> int:
        """Read and discard any stale data from bulk endpoint."""
        drained = 0
        while True:
            try:
                data = self.dev.read(
                    self.bulk_in.bEndpointAddress, 2048, timeout=100)
                if data and len(data) > 0:
                    drained += len(data)
                else:
                    break
            except usb.core.USBError:
                break
        return drained

    def _send_transfer_cmd(self, sub_cmd: int, reg_count: int, payload: bytes = b'') -> bytes | None:
        """Send a TRANSFER (0x09) command and return the response."""
        cmd = bytes([FUNC_TRANSFER, sub_cmd, reg_count]) + payload
        resp = self._send_interrupt(cmd, response_size=64, timeout=2000)
        if resp and resp[0] != FUNC_TRANSFER:
            return None
        return resp

    def download_calibration_package(self, range_id: int) -> bytes | None:
        """Download a calibration package from device flash.

        Args:
            range_id: 0 = low-temp (-30 to 180°C), 1 = high-temp (-20 to 430°C)

        Returns:
            bytes of the calibration package, or None on failure.
        """
        if range_id == 1:
            flash_addr = CALIB_HIGH_FLASH_ADDR
            length_reg = REG_PKG_LENGTH_HIGH
            name = "high-temp"
        else:
            flash_addr = CALIB_LOW_FLASH_ADDR
            length_reg = REG_PKG_LENGTH_LOW
            name = "low-temp"

        logger.info("  Downloading %s calibration from flash 0x%06X...", name, flash_addr)

        # Read package length
        pkg_length = self._read_register_uint(length_reg)
        if pkg_length is None or pkg_length == 0 or pkg_length > 0x200000:
            logger.error("  Failed: invalid package length %s", pkg_length)
            return None
        logger.info("  Package size: %d bytes (%.1f KB)", pkg_length, pkg_length / 1024)

        # Drain stale bulk data
        self._drain_bulk()

        # Set run_status = IDLE, then PARAM_UPLOAD
        self.set_run_status(STATUS_IDLE)
        time.sleep(0.1)
        value = struct.pack(">i", STATUS_PARAM_UPLOAD)
        cmd = bytes([FUNC_WRITE_REG, REG_RUN_STATUS & 0xFF, 0x01]) + value
        self._send_interrupt(cmd, response_size=64, timeout=1000)
        time.sleep(0.1)

        # TRANSFER BEGIN: send flash address + length
        addr_bytes = struct.pack('>I', flash_addr)
        len_bytes = struct.pack('>I', pkg_length)
        resp = self._send_transfer_cmd(TRANSFER_BEGIN, 0x02,
                                       addr_bytes + len_bytes)
        if not resp:
            logger.error("  Failed: no response to TRANSFER BEGIN")
            self.set_run_status(STATUS_IDLE)
            return None

        # Read chunks with CRC verification
        result = bytearray()

        while len(result) < pkg_length:
            time.sleep(0.01)

            # Read bulk chunk
            chunk_data = None
            for attempt in range(3):
                try:
                    data = self.dev.read(self.bulk_in.bEndpointAddress,
                                        CALIB_CHUNK_SIZE, timeout=2000)
                    if data and len(data) > 0:
                        chunk_data = bytes(data)
                        break
                except usb.core.USBTimeoutError:
                    time.sleep(0.01)
                except usb.core.USBError:
                    time.sleep(0.01)

            if chunk_data is None:
                logger.error("  Failed: no data at byte %d/%d", len(result), pkg_length)
                break

            # CRC acknowledgement
            crc = zlib.crc32(chunk_data) & 0xFFFFFFFF
            crc_bytes = struct.pack('>I', crc)
            len_ack = struct.pack('>I', len(chunk_data))
            resp = self._send_transfer_cmd(TRANSFER_CRC, 0x02,
                                           crc_bytes + len_ack)
            if not resp:
                logger.error("  Failed: CRC ack failed at byte %d", len(result))
                break

            copy_len = min(len(chunk_data), pkg_length - len(result))
            result.extend(chunk_data[:copy_len])

            if len(chunk_data) < CALIB_CHUNK_SIZE:
                break

        # TRANSFER END
        total_crc = zlib.crc32(bytes(result)) & 0xFFFFFFFF
        self._send_transfer_cmd(TRANSFER_END, 0x01,
                                struct.pack('>I', total_crc))

        # Restore IDLE
        self.set_run_status(STATUS_IDLE)

        if len(result) != pkg_length:
            logger.error("  Failed: got %d/%d bytes", len(result), pkg_length)
            return None

        logger.info("  Downloaded %d bytes OK (CRC 0x%08X)", len(result), total_crc)
        return bytes(result)
