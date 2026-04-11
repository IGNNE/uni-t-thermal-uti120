"""Factory calibration data parsing and Y16-to-temperature conversion.

Reconstructed from libguide_sdk_unitrend.so disassembly analysis.
Reconstructed from libguide_sdk_unitrend.so x86_64 disassembly.
"""
from __future__ import annotations

import logging
import os
import struct
from pathlib import Path

import numpy as np

from .constants import FRAME_WIDTH, FRAME_HEIGHT

logger = logging.getLogger(__name__)

class CalibrationPackage:
    """Parsed calibration package from device flash.

    Format:
        Header (216 bytes): method string, range info, dimensions, sizes
        Sub-header (variable): focus temperature brackets
        Section 1: Y16 lookup curves (version_sets * focus_groups * curve_steps uint16)
        Section 2: Per-pixel gain correction tables (7 * 90 * 120 uint16)
    """

    def __init__(self, filepath: str | Path | None = None, data: bytes | None = None) -> None:
        if data is not None:
            self.raw = bytes(data)
        elif filepath is not None:
            with open(filepath, 'rb') as f:
                self.raw = f.read()
        else:
            raise ValueError("Either filepath or data must be provided")

        self.header_size = struct.unpack('<I', self.raw[0:4])[0]
        assert self.header_size == 216

        method_end = self.raw.index(0, 4)
        self.method = self.raw[4:method_end].decode('ascii')

        self.range_id = self.raw[0x40]
        self.version = self.raw[0x41]
        self.min_ref = struct.unpack('<h', self.raw[0x42:0x44])[0]
        self.t_val2 = struct.unpack('<H', self.raw[0x46:0x48])[0]
        self.focus_count = self.raw[0x4B]
        self.width = struct.unpack('<H', self.raw[0x4C:0x4E])[0]
        self.height = struct.unpack('<H', self.raw[0x4E:0x50])[0]
        self.curve_steps = struct.unpack('<H', self.raw[0x50:0x52])[0]
        self.focus_len = struct.unpack('<H', self.raw[0x52:0x54])[0]
        self.section1_size = struct.unpack('<I', self.raw[0x54:0x58])[0]
        self.section2_size = struct.unpack('<I', self.raw[0x58:0x5C])[0]

        # Sub-header
        self.sub_header_size = (len(self.raw) - self.header_size) - \
                               (self.section1_size + self.section2_size)
        self.section1_start = self.header_size + self.sub_header_size
        self.section2_start = self.section1_start + self.section1_size

        # Focus buffer (temperature brackets for curve selection)
        # Native reads from section1_start - focus_len (byte 246), NOT header_size (216).
        # The 30-byte gap between header end and focus data contains sub-header metadata.
        focus_start = self.section1_start - self.focus_len
        self.focus_buf = []
        for i in range(0, self.focus_len, 2):
            val = struct.unpack('<h', self.raw[focus_start + i:
                                              focus_start + i + 2])[0]
            self.focus_buf.append(val)

        # Section 1: Curve data
        sec1_data = self.raw[self.section1_start:self.section2_start]
        self.curves = np.frombuffer(sec1_data, dtype='<u2').copy()

        expected = self.version * self.focus_count * self.curve_steps
        assert len(self.curves) == expected, \
            f"Section 1 size mismatch: {len(self.curves)} != {expected}"

        # Section 2: Per-pixel gain correction tables
        sec2_data = self.raw[self.section2_start:
                             self.section2_start + self.section2_size]
        n_tables = self.section2_size // (2 * self.width * self.height)
        self.corrections = np.frombuffer(sec2_data, dtype='<u2').reshape(
            n_tables, self.height, self.width).copy()

        # Sensor hardware parameters (used for range switching USB command)
        self.sensor_gain = self.raw[0x46]
        self.sensor_int = self.raw[0x47]
        self.sensor_res = self.raw[0x48]

        # Focus distance parameters from DataHeader[0x74..] (uint16 / 10.0)
        # Used for distance-based interpolation between focus groups
        # (ARM64 guideCoreParsePackage: HOST_PARAM[0x28 + i*4] = DataHeader[0x74 + i*2] / 10.0)
        self.focus_distance_params = []
        for i in range(self.focus_count):
            val = struct.unpack('<H', self.raw[0x74 + i * 2:0x76 + i * 2])[0]
            self.focus_distance_params.append(val / 10.0)

        self.core_body_temp = float(self.min_ref)
        self.temp_min = self.core_body_temp
        self.temp_max = (self.curve_steps - 1) / 10.0 + self.core_body_temp

    def get_curve_block(self, version_idx: int, focus_idx: int) -> np.ndarray:
        """Get a specific curve block (curve_steps entries)."""
        block_size = self.curve_steps
        stride = self.focus_count * block_size
        start = version_idx * stride + focus_idx * block_size
        return self.curves[start:start + block_size]

    def get_nuc_gain(self, version_idx: int) -> np.ndarray:
        """Get per-pixel NUC gain for a given version set.

        Section 2 correction tables are the K-buffer used by the firmware's
        NUCbyTwoPoint as a multiplicative gain (Ghidra-confirmed):
            output[i] = ((raw[i] - dark[i]) * (K[i] & 0x7FFF)) >> 13

        Bit 15 flags bad pixels (masked out by & 0x7FFF). The gain is in
        Q13 fixed-point: K/8192 gives the per-pixel gain factor (~0.99–1.28).

        Args:
            version_idx: Version set index (0..n_version-1), matching the
                        curve version selected by FPA temperature bracket.

        Returns:
            float32 array (height, width) of per-pixel gain factors.
        """
        vi = max(0, min(version_idx, self.corrections.shape[0] - 1))
        table = self.corrections[vi]

        # Mask out bit 15 (bad pixel flag) and convert to float gain
        gain = (table & 0x7FFF).astype(np.float32) / 8192.0

        return gain

    def __repr__(self) -> str:
        name = "LOW" if self.range_id == 0 else "HIGH"
        return (f"CalibrationPackage({name}, "
                f"range={self.temp_min:.1f} to {self.temp_max:.1f}°C, "
                f"steps={self.curve_steps})")


def get_curve_segments(calib: CalibrationPackage, fpa_temp_raw: int) -> tuple[list[np.ndarray], int, float]:
    """Select 4 curve segments based on FPA temperature.

    Reconstructs getCurve() (0x155b0) logic: finds the version set bracket
    matching the current FPA temperature, then returns 4 curve segments
    (2 adjacent version sets × 2 focus groups), plus bracket info for
    FPA-weighted interpolation.

    Args:
        calib: CalibrationPackage instance
        fpa_temp_raw: FPA temperature as raw uint16 from frame header

    Returns:
        (segments, version_idx, fpa_weight): list of 4 curve arrays,
        selected version index, and FPA interpolation weight (0.0–1.0,
        weight for the lower bracket; 1-weight for upper).
    """
    focus_buf = calib.focus_buf
    n_version = calib.version
    n_focus = calib.focus_count

    version_idx = 0
    if len(focus_buf) >= n_version:
        if fpa_temp_raw < focus_buf[0]:
            version_idx = 0
        elif fpa_temp_raw >= focus_buf[n_version - 1]:
            version_idx = n_version - 1
        else:
            for i in range(1, n_version):
                if fpa_temp_raw < focus_buf[i]:
                    version_idx = i - 1
                    break

    vi = max(0, min(version_idx, n_version - 1))
    vi_next = min(vi + 1, n_version - 1)

    # FPA interpolation weight (Ghidra-confirmed from GetLowTemperature)
    if vi == vi_next:
        # At boundary — no interpolation needed
        fpa_weight = 1.0
    else:
        lower_bracket = focus_buf[vi]
        upper_bracket = focus_buf[vi_next]
        if upper_bracket != lower_bracket:
            fpa_weight = float(upper_bracket - fpa_temp_raw) / \
                         float(upper_bracket - lower_bracket)
            fpa_weight = max(0.0, min(1.0, fpa_weight))
        else:
            fpa_weight = 1.0

    segments = [
        calib.get_curve_block(vi, 0),
        calib.get_curve_block(vi, min(1, n_focus - 1)),
        calib.get_curve_block(vi_next, 0),
        calib.get_curve_block(vi_next, min(1, n_focus - 1)),
    ]
    return segments, vi, fpa_weight


# ============================================================================
# Drift correction functions — reconstructed from libguide_sdk_unitrend.so
# ============================================================================

# LensDriftCorrectZX01C constants
_LENS_DRIFT_MIN_THRESHOLD = 0.1   # 0x42d30
_LENS_DRIFT_COEFF_LOW = -300.0    # from rodata 0x42a10 (MP[0x78])
_LENS_DRIFT_COEFF_HIGH = -200.0   # from rodata 0x42a10 (MP[0x74])


def lens_drift_correct_zx01c(current_lens_temp: float, nuc_lens_temp: float,
                              nuc_fpa_temp: float, is_high: bool = False) -> float:
    """LensDriftCorrectZX01C (0x21110) — lens temperature drift correction.

    Compensates for lens warming/cooling affecting sensor readings.
    Gated on minimum temperature thresholds (0.1°C).

    Args:
        current_lens_temp: HDR_LENS_TEMP / 100 (from current frame header)
        nuc_lens_temp: Lens temp saved at last NUC/dark capture
        nuc_fpa_temp: FPA temp saved at last NUC/dark capture
        is_high: True for high temp range, False for low

    Returns:
        Y16 drift value (subtracted from Y16 before curve lookup)
    """
    if nuc_fpa_temp < _LENS_DRIFT_MIN_THRESHOLD:
        return 0.0
    if nuc_lens_temp < _LENS_DRIFT_MIN_THRESHOLD:
        return 0.0

    coeff = _LENS_DRIFT_COEFF_HIGH if is_high else _LENS_DRIFT_COEFF_LOW
    return (current_lens_temp - nuc_lens_temp) * coeff


def y16_to_temperature_array(y16_array: np.ndarray, curve_buf: np.ndarray, curve_steps: int, core_body_temp: float,
                              shutter_temp: float = 25.0, coeff: float = 1.0,
                              shutter_drift: float = 0.0, lens_drift: float = 0.0) -> np.ndarray:
    """Vectorized Y16-to-temperature conversion for an entire frame.

    Implements the core of GetSingleCurveTemperatures in vectorized form:
    1. Apply drift corrections to Y16 (lens subtracted, shutter added)
    2. Compute deltaIdx from shutter temp
    3. Get bias from curve at deltaIdx
    4. lookupVal = adjusted_y16 * coeff + bias
    5. searchsorted in curve for each lookupVal
    6. temp = index / 10.0 + core_body_temp

    Args:
        y16_array: 2D numpy array of NUC-corrected Y16 values (raw - dark)
        curve_buf: 1D curve lookup table (curve_steps uint16 entries)
        curve_steps: Number of entries in curve
        core_body_temp: Base temperature offset (min_ref from calibration)
        shutter_temp: Shutter temperature for bias calculation (°C)
        coeff: Gain coefficient (default 1.0, confirmed from disassembly)
        shutter_drift: ShutterDriftCorrect3 result (added to Y16)
        lens_drift: LensDriftCorrectZX01C result (subtracted from Y16)

    Returns:
        2D numpy array of temperatures in °C
    """
    y16_float = y16_array.astype(np.float32)

    # Apply drift corrections (matches GetSingleCurveTemperatures order):
    # 1. Lens drift is subtracted from Y16
    if lens_drift != 0.0:
        y16_float = y16_float - lens_drift
    # 2. Shutter drift is added to Y16
    if shutter_drift != 0.0:
        y16_float = y16_float + shutter_drift

    delta_idx = int((shutter_temp - core_body_temp) * 10.0)

    if 0 < delta_idx < curve_steps:
        bias = float(curve_buf[delta_idx])
    else:
        bias = 0.0

    lookup_vals = (y16_float * coeff + bias).astype(np.int32)

    indices = np.searchsorted(curve_buf, lookup_vals, side='right') - 1
    indices = np.clip(indices, 0, curve_steps - 1)

    temps = indices.astype(np.float64) / 10.0 + core_body_temp
    return temps


def y16_to_temperature_interpolated(y16_array: np.ndarray, segments: list[np.ndarray], fpa_weight: float,
                                     curve_steps: int, core_body_temp: float,
                                     shutter_temp: float = 25.0, lens_drift: float = 0.0,
                                     distance: float = 1.0,
                                     focus_distance_params: list[float] | None = None) -> np.ndarray:
    """Multi-curve bilinear temperature conversion (ARM64-confirmed).

    Uses all 4 curve segments with two interpolation dimensions,
    matching GetLowTemperature/GetHighTemperature ARM64 decompilation:
    - FPA bracket weights: between version sets vi and vi+1
    - Focus distance weights: between focus group 0 and 1, based on
      object distance vs calibration focus distance params

    Args:
        y16_array: 2D NUC-corrected Y16 values
        segments: list of 4 curve arrays [vi_f0, vi_f1, vi+1_f0, vi+1_f1]
        fpa_weight: weight for lower bracket (0.0–1.0)
        curve_steps: entries per curve
        core_body_temp: base temperature offset
        shutter_temp: shutter temperature for bias (°C)
        lens_drift: LensDriftCorrectZX01C result
        distance: object distance in metres (default 1.0)
        focus_distance_params: [fd_lo, fd_hi] from CalibrationPackage

    Returns:
        2D numpy array of temperatures in °C
    """
    temps = []
    for curve in segments:
        t = y16_to_temperature_array(
            y16_array, curve, curve_steps, core_body_temp,
            shutter_temp=shutter_temp, lens_drift=lens_drift)
        temps.append(t)

    # Focus distance weights (ARM64 GetLowTemperature lines 273-287)
    # Compares MEASURE_PARAM[0x98] (distance) against HOST_PARAM[0x28]/[0x2C]
    if focus_distance_params and len(focus_distance_params) >= 2:
        fd_lo, fd_hi = focus_distance_params[0], focus_distance_params[1]
        if distance <= fd_lo:
            focus0_w, focus1_w = 1.0, 0.0
        elif distance >= fd_hi:
            focus0_w, focus1_w = 0.0, 1.0
        else:
            d_lo = distance - fd_lo
            d_hi = fd_hi - distance
            focus0_w = d_hi / (d_lo + d_hi)
            focus1_w = d_lo / (d_lo + d_hi)
    else:
        focus0_w, focus1_w = 1.0, 0.0  # fallback: focus group 0 only

    # Bilinear interpolation: FPA weight × focus distance weight
    # ARM64: result = (f1_temps * fpa_w) * focus1_w + (f0_temps * fpa_w) * focus0_w
    result = ((temps[1] * fpa_weight + temps[3] * (1.0 - fpa_weight)) * focus1_w +
              (temps[0] * fpa_weight + temps[2] * (1.0 - fpa_weight)) * focus0_w)
    return result


def _load_nemiss_curve() -> np.ndarray:
    """Load the nEmissCurve table (16,384 × int16) extracted from the native SDK.

    The table maps radiance indices to temperature × 10 (e.g., 354 = 35.4°C).
    Monotonically increasing, range -450 to 8500 (-45.0°C to 850.0°C).
    """
    path = Path(__file__).resolve().parent / 'data' / 'nEmissCurve.npy'
    table = np.load(str(path)).view(np.int16)
    assert table.shape == (16384,), f"nEmissCurve shape mismatch: {table.shape}"
    return table


# Module-level lazy load
_nemiss_curve: np.ndarray | None = None


def _get_nemiss_curve() -> np.ndarray:
    """Get the cached nEmissCurve table."""
    global _nemiss_curve
    if _nemiss_curve is None:
        _nemiss_curve = _load_nemiss_curve()
    return _nemiss_curve


def emiss_correct(temp_celsius: np.ndarray, ambient_temp: float, emissivity: float, curve: np.ndarray, curve_steps: int,
                  core_body_temp: float) -> np.ndarray:
    """Apply emissivity correction using the native nEmissCurve table.

    Reimplementation of the native EmissCor() function from
    libguide_sdk_unitrend.so (@ 0x26670). Uses a 16,384-entry radiance-to-
    temperature lookup table for radiometric correction in index (radiance)
    space, matching the manufacturer's algorithm exactly.

    Algorithm:
      1. Binary search temp×10 in nEmissCurve → meas_idx
      2. Binary search ambient×10 in nEmissCurve → amb_idx
      3. adjusted_idx = (meas_idx×100 - amb_idx×(100 - e%)) / e%
      4. result = nEmissCurve[clamp(adjusted_idx, 0, 0x3FFF)] / 10.0

    Args:
        temp_celsius: measured temperature array (°C), from curve lookup.
        ambient_temp: ambient/reflected temperature (°C), scalar.
        emissivity: surface emissivity (0.0–1.0).
        curve: calibration curve (unused, kept for API compatibility).
        curve_steps: number of entries in curve (unused).
        core_body_temp: base temperature offset (unused).

    Returns:
        Corrected temperature array (°C).
    """
    if emissivity >= 0.98:
        return temp_celsius

    nemiss = _get_nemiss_curve()
    e_pct = int(emissivity * 100)

    if e_pct < 1:
        # Native: returns nEmissCurve[0x3FFE] for all pixels
        val = float(nemiss[0x3FFE]) / 10.0
        return np.full_like(temp_celsius, val)
    if e_pct > 99:
        return temp_celsius

    # Vectorized binary search: temp×10 → index in nEmissCurve
    flat = temp_celsius.ravel()
    meas_t10 = (flat * 10.0).astype(np.int16)
    meas_idx = np.searchsorted(nemiss, meas_t10, side='right') - 1
    meas_idx = np.clip(meas_idx, 0, 0x3FFF)

    # Scalar: ambient → index
    amb_t10 = np.int16(ambient_temp * 10.0)
    amb_idx = int(np.searchsorted(nemiss, amb_t10, side='right') - 1)
    amb_idx = max(0, min(0x3FFF, amb_idx))

    # Radiometric correction in index (radiance) space
    # adjusted_idx = (meas_idx * 100 - amb_idx * (100 - e_pct)) / e_pct
    adjusted = (meas_idx.astype(np.int64) * 100 - amb_idx * (100 - e_pct)) // e_pct
    adjusted = np.clip(adjusted, 0, 0x3FFF).astype(int)

    result = nemiss[adjusted].astype(np.float64) / 10.0
    return result.reshape(temp_celsius.shape)


def validate_calibration_file(path: str | Path) -> bool:
    """Quick structural validation of a calibration .bin file.

    Checks header_size == 216, file size matches declared sections,
    and range_id is 0 or 1. Does NOT fully parse the package.

    Returns:
        True if the file appears to be a valid calibration package.
    """
    try:
        size = Path(path).stat().st_size
        if size < 216:
            return False
        with open(path, 'rb') as f:
            hdr = f.read(0x5C)
        if len(hdr) < 0x5C:
            return False
        header_size = struct.unpack('<I', hdr[0:4])[0]
        if header_size != 216:
            return False
        range_id = hdr[0x40]
        if range_id not in (0, 1):
            return False
        section1_size = struct.unpack('<I', hdr[0x54:0x58])[0]
        section2_size = struct.unpack('<I', hdr[0x58:0x5C])[0]
        # File must be large enough for header + sections
        if size < header_size + section1_size + section2_size:
            return False
        return True
    except (OSError, struct.error, ValueError):
        return False


def _calibration_cache_dir() -> Path:
    """Return the OS-appropriate cache directory for calibration files."""
    import sys
    if sys.platform == 'win32':
        base = Path.home() / 'AppData' / 'Local'
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Caches'
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    return base / 'uti120'


def _cache_path() -> Path:
    """Return the path to the single calibration cache file."""
    return _calibration_cache_dir() / 'calibration_cache.npz'



def load_calibration_cache(serial: str) -> dict[int, CalibrationPackage] | None:
    """Load cached calibration packages if they match the given serial.

    Args:
        serial: camera serial number to verify against cache

    Returns:
        dict mapping range_id to CalibrationPackage, or None if cache
        is missing, invalid, or belongs to a different camera.
    """
    path = _cache_path()
    if not path.exists():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            cached_serial = bytes(data['serial']).decode('utf-8')
            if cached_serial != serial:
                logger.info("Calibration cache serial mismatch: "
                            "cached=%s, device=%s", cached_serial, serial)
                path.unlink()
                return None
            pkgs: dict[int, CalibrationPackage] = {}
            for range_id, key in [(0, 'low_temp'), (1, 'high_temp')]:
                if key in data and len(data[key]) > 0:
                    pkgs[range_id] = CalibrationPackage(
                        data=bytes(data[key]))
            return pkgs if pkgs else None
    except (OSError, KeyError, struct.error, ValueError,
            AssertionError, UnicodeDecodeError) as e:
        logger.warning("Failed to load calibration cache: %s", e)
        path.unlink(missing_ok=True)
        return None




def save_calibration_cache(serial: str,
                           low_data: bytes | None,
                           high_data: bytes | None) -> None:
    """Save calibration data and camera serial to a single cache file.

    Args:
        serial: camera serial number
        low_data: raw bytes of low-temp calibration package (or None)
        high_data: raw bytes of high-temp calibration package (or None)
    """
    cache_dir = _calibration_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    arrays: dict[str, np.ndarray] = {
        'serial': np.frombuffer(serial.encode('utf-8'), dtype=np.uint8),
    }
    if low_data:
        arrays['low_temp'] = np.frombuffer(low_data, dtype=np.uint8)
    if high_data:
        arrays['high_temp'] = np.frombuffer(high_data, dtype=np.uint8)
    path = _cache_path()
    np.savez(path, **arrays)
    logger.info("Saved calibration cache to %s (serial=%s)", path, serial)
