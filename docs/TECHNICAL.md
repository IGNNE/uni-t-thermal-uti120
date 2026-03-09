# Technical Reference

This document covers implementation details for the UNI-T UTi120 thermal viewer.
For usage and installation see [README.md](../README.md). For reverse engineering
notes see [REVERSE_ENGINEERING.md](REVERSE_ENGINEERING.md).

## Image Processing Pipeline

- **Dark frame NUC** (Non-Uniformity Correction): subtracts a shutter-captured reference frame for per-pixel offset correction
- **Per-pixel gain correction** from factory K-buffer tables (Q13 fixed-point gain factors)
- **Bad pixel replacement**: factory-flagged bad pixels (gain table bit 15) are replaced via mask, plus any per-frame outlier deviating >8x MAD (Median Absolute Deviation) from its 3x3 median is unconditionally replaced — catching stuck, sluggish, and bad-gain pixels without detection delays
- **Vertical and horizontal stripe removal**: Ghidra-confirmed destriping algorithm using directional Gaussian smoothing and clamped residual correction
- **Temporal noise filter (TFF)**: motion-adaptive bilateral filter that smooths stationary pixels while preserving moving edges
- **Automatic shutter calibration**: FPA drift-triggered and periodic dark frame refresh (see below)

## Automatic Shutter Calibration

The camera performs automatic shutter calibration (dark frame refresh) to maintain
temperature measurement accuracy. Microbolometer sensors like the one in the UTi120
are subject to **FPA (Focal Plane Array) temperature drift**: as the sensor heats up
during operation, the baseline signal shifts, introducing measurement error. The shutter
briefly closes to capture a "dark frame" reference that is subtracted from live frames
to correct for this drift.

The auto-calibration logic (reconstructed from the APK's `ShutterHandler.java`) uses
three mechanisms:

1. **FPA drift → NUC + Shutter** (rare): When the FPA temperature drifts significantly
   from its baseline (≥0.6–1.2°C depending on warmup phase), a full hardware
   Non-Uniformity Correction is triggered along with a dark frame refresh.
2. **FPA drift → Shutter only** (frequent): On moderate drift (≥0.3–0.8°C), only the
   shutter closes for a quick dark frame update.
3. **Periodic timer** (after 6 min): Once the camera has been running for 6 minutes and
   is thermally stable, a shutter calibration is forced every 60 seconds regardless of
   drift, to prevent slow undetected error accumulation.

The drift thresholds tighten over time as the camera warms up:

| Time window | Shutter threshold | NUC threshold |
|-------------|-------------------|---------------|
| 0–3 min     | 0.80°C            | 1.20°C        |
| 3–6 min     | 0.50°C            | 1.00°C        |
| 6+ min      | 0.30°C            | 0.60°C        |

During calibration the image briefly freezes (~0.5s). This is normal and expected.

## Radiometric .npz File Format

Each screenshot saves a `.npz` file alongside the PNG. The `.npz` format is a standard numpy compressed ZIP archive — internally each array is stored as a `.npy` file (e.g. `temp_map.npy`).

**Contents:**

| Key | Type | Shape | Description |
|-----|------|-------|-------------|
| `temp_map` | float32/float64 | (90, 120) | Calibrated temperature map in °C. Each element is the temperature of the corresponding pixel after full pipeline processing (NUC, gain correction, bad pixel replacement, lens drift, curve lookup, FPA interpolation, and emissivity correction) |
| `emissivity` | float | scalar | Surface emissivity value (0.01–1.00) used when the capture was taken |
| `fpa_temp` | float | scalar | Focal Plane Array sensor temperature in °C at capture time |

**Loading in Python:**

```python
import numpy as np

data = np.load("thermal_20260308_143022.npz")
temp_map   = data["temp_map"]      # (90, 120) array of temperatures in °C
emissivity = float(data["emissivity"])
fpa_temp   = float(data["fpa_temp"])

print(f"Min: {temp_map.min():.1f}°C  Max: {temp_map.max():.1f}°C")
print(f"Emissivity: {emissivity:.2f}  FPA: {fpa_temp:.1f}°C")
```

## GUI Stack

The viewer uses **PyQt6** for the GUI with a custom `QPainter`-based thermal display
widget and a dark Fusion theme. OpenCV is used internally for image processing
(resize, color conversion, palette application) but not for display. The camera USB
loop runs on a `QThread` with frame data delivered via signals to the main thread.
