# UNI-T UTi120 Mobile — Reverse Engineering Reference

All protocol and calibration details were obtained by decompiling the official
Android APK (`UT-Mobile_v1.1.18.apk`, version 1.1.18) using JADX, disassembling
the native library with Ghidra (`libguide_sdk_unitrend.so`), and validating against live
hardware.

---

## USB Protocol

### Device Identification

| Field | Value |
|-------|-------|
| Vendor ID | `0x5656` (Uni-Trend Group Limited) |
| Product ID | `0x1201` |
| Product String | `UNI-T UTi120Mobile` |
| Manufacturer | `UNIT` |
| USB Speed | Full Speed (12 Mbps) |
| Max Packet Size | 64 bytes |

### Device Topology

```
Interface 0 — Bulk Transfer (frame data)
  ├── EP 0x02 OUT (bulk, 64 bytes) — not used for normal operation
  └── EP 0x82 IN  (bulk, 64 bytes) — receives frame data (25,600 bytes/frame)

Interface 1 — Command/Control
  ├── EP 0x01 OUT (interrupt, 64 bytes) — send commands
  └── EP 0x81 IN  (interrupt, 64 bytes) — receive responses
```

### Command Protocol

Commands are sent on Interface 1 (interrupt OUT endpoint) as raw byte arrays:

```
Byte 0: function_code
Byte 1: register_offset
Byte 2: register_count (N)
Bytes 3..(3+N*4-1): value (N registers × 4 bytes each, big-endian)
```

Responses are read from the interrupt IN endpoint. Write commands echo back
`[function_code, offset, count]` (3 bytes). Read commands return
`[function_code, offset, count, ...data]` where data is `count × 4` bytes.

#### Function Codes

| Code | Name | Direction | Description |
|------|------|-----------|-------------|
| `0x04` | `WRITE_REG` | Write | Write to config/system register |
| `0x05` | `READ_REG` | Read | Read from config/system register |
| `0x09` | `TRANSFER` | Write | Bulk transfer control (param package download) |
| `0x0A` | `SENSOR_CMD` | Write | Sensor commands (shutter, NUC, measure range) |
| `0x0B` | `READ_SENSOR` | Read | Read sensor values (shutter status, temps) |

#### System Registers (function 0x04 write / 0x05 read)

| Offset | Name | Values | Description |
|--------|------|--------|-------------|
| `0xF0` | Run Status | 0=idle, 1=close, 2=image upload, 3=param upload | Device operating mode |
| `0xE0` | Reboot | 1 | Writing 1 triggers device reboot |
| `0x01` | Product ID | 4 bytes | Product model identifier |
| `0x02` | HW Version | 4 bytes | Hardware version |
| `0x03` | SW Version | 4 bytes | Firmware version |
| `0x07` | Serial Number | 20 bytes (5 regs) | Device serial number (UTF-8) |
| `0x0C` | Pkg Length (high) | 4 bytes | Calibration package size (high-temp range) |
| `0x0D` | Pkg Length (low) | 4 bytes | Calibration package size (low-temp range) |

#### Sensor Commands (function 0x0A write)

| Offset | Name | Value Format | Description |
|--------|------|-------------|-------------|
| `0x00` | Reflect Rate | int32 BE | Set emissivity/reflect rate |
| `0x01` | Reflect Temp | int32 BE | Set reflected temperature |
| `0x02` | Reflect Distance | int32 BE | Set measurement distance |
| `0x03` | Shutter | 0=open, 1=close | Mechanical shutter control |
| `0x04` | NUC | 1=trigger | Non-uniformity correction |
| `0x09` | Measure Range | 4 bytes: `[0, res, int, gain]` | Switch temperature range |

#### Sensor Reads (function 0x0B read)

| Offset | Name | Description |
|--------|------|-------------|
| `0x00` | Reflect Rate | Current emissivity setting |
| `0x01` | Reflect Temp | Current reflected temperature |
| `0x02` | Reflect Distance | Current distance setting |
| `0x03` | Shutter Status | 0=open, 1=closed |
| `0x05` | Shutter Temp (realtime) | Current shutter temperature |
| `0x06` | Tube Temperature | Lens tube temperature |
| `0x07` | Focal Plane Temp | FPA (sensor) temperature |
| `0x08` | Shutter Temp (startup) | Shutter temp at power-on |

#### Transfer Control (function 0x09)

Used to download calibration packages from device flash:

| Offset | Name | Value | Description |
|--------|------|-------|-------------|
| `0x00` | Begin | 8 bytes: `[addr(4), length(4)]` | Start bulk download |
| `0x02` | CRC | 8 bytes: `[crc32(4), chunk_len(4)]` | Per-chunk CRC verification |
| `0x04` | End | 4 bytes: `[total_crc32]` | Finalize download |

Flash addresses for calibration packages:
- **High-temp range:** `0x100000` (1,048,576), length from register `0x0C`
- **Low-temp range:** `0x132000` (1,253,376), length from register `0x0D`

---

## Frame Data Format

### Acquisition Sequence

```
1. USB reset device            → ensures clean state (required on Linux)
2. set_run_status(2)           → puts device in image streaming mode
3. wait ~300ms                 → let device settle
4. send 0x81 via int OUT       → requests next frame
5. wait ~1ms
6. read 25,600 bytes bulk IN   → one complete frame (in 4096-byte chunks, 13ms timeout each)
7. repeat from step 4
```

**Frame request byte:** `0x81` (APK sends `new byte[]{-127}`). `0x7F` returns nothing.

The APK reads in chunks of 4096 bytes with a **13ms timeout** per chunk,
up to 8 iterations until 25,600 bytes are received.

Frame delivery is ~70% reliable. First bulk read timeout = no data available,
skip and retry. After 20+ consecutive failures, re-send `set_run_status(2)`.

### Frame Layout

Total frame size: **25,600 bytes** = **12,800 uint16 LE shorts**

```
Bytes [0 .. 719]        (720 bytes):    Header / parameters
Bytes [720 .. 22319]    (21,600 bytes): Y16 pixel data — 90 rows × 120 columns
Bytes [22320 .. 25599]  (3,280 bytes):  Zero padding
```

Equivalently in uint16 shorts:
```
Shorts [0 .. 359]       Header / parameters
Shorts [360 .. 11159]   10,800 pixel values (90 rows × 120 cols)
Shorts [11160 .. 12799] Zero padding
```

#### Header Fields

| Byte Offset | Type | Field | Notes |
|-------------|------|-------|-------|
| 0 | uint16 LE | Magic | `0xAA55` |
| 2 | uint16 LE | Frame counter | Increments each frame |
| 4 | uint16 LE | Width | 120 |
| 6 | uint16 LE | Height | 92 (includes param rows) |
| 16 | uint16 LE | Shutter temp (startup) | value/100 = °C |
| 18 | uint16 LE | Shutter temp (realtime) | value/100 = °C |
| 20 | uint16 LE | Lens temp | value/100 = °C |
| 22 | uint16 LE | Focal plane temp | value/100 = °C |
| 24 | uint16 LE | Shutter status | 0=open, non-zero=closed |
| 26 | uint16 LE | NUC status | — |

#### Pixel Data

- **10,800 uint16 LE values** starting at short offset 360 (byte offset 720)
- **90 rows × 120 columns**, row-major order: `pixel[row][col] = shorts[360 + (row × 120) + col]`
- Typical room temperature raw values: ~5700–5900
- Raw data has **fixed-pattern noise** (checkerboard from ROIC 2-tap readout)

---

## Y16 to Temperature Conversion

### Full Calibration Pipeline

The accurate conversion requires NUC correction, factory calibration curves, and drift
correction. See **Native Library Disassembly** section for algorithm details.

**Pipeline steps** (unified — same NUC'd Y16 feeds both temperature and display):
1. **NUC**: `nuc_y16 = raw_pixels - dark_frame`
2. **Section 2 gain (K-buffer)**: `nuc_corrected = nuc_y16 * (K & 0x7FFF) / 8192` — per-pixel gain
3. **Bad pixel replacement**: factory mask + outlier detection
4. **TFF (TimeNoiseFilter)**: motion-adaptive temporal bilateral filter on Y16
5. **Lens drift**: `drift = (current_lens - nuc_lens) * -300.0` (low) or `-200.0` (high)
6. **Bias**: `deltaIdx = (shutterTemp - coreBodyTemp) * 10`, `bias = curve[deltaIdx]`
7. **Curve lookup**: `lookupVal = nuc_corrected - lens_drift + bias`, binary search → temperature
8. **Emissivity correction**: via nEmissCurve lookup (when e < 0.98, manufacturer uses e=0.96)

> **Python impl note:** Our code defaults to emissivity 0.95 (`constants.py`), while the
> manufacturer APK defaults to 0.96 (from `SRateBean.java`). Steps 4 (TFF) is configurable
> and can be disabled. Stripe removal (native steps 4–5 in InfraredImageProcess) is not
> implemented — see [Python Implementation Notes](#python-implementation-notes) below.

### One Pixel's Journey

Tracking pixel (45, 60) through the full pipeline at room temperature, low-temp range
(coreBodyTemp = −30°C). Header reads: shutter = 24.5°C, lens = 23.8°C, FPA = 32.7°C.
Dark frame was captured with lens = 23.2°C, FPA = 31.0°C.

1. **Raw Y16 from USB frame**: 5847
2. **Dark frame subtraction** (NUC): 5847 − 5764 = 83
3. **Per-pixel gain** (K-buffer, Q13): 83 × (9105 / 8192) = 92.2
4. **Bad pixel check**: not flagged, not an outlier → passes through
5. **TFF** (temporal noise filter): blended with previous frame via Gaussian-weighted
   difference; small Δ → ~90% previous weight. Result ≈ 92.0
6. **Lens drift**: drift = (23.8 − 23.2) × −300.0 = −180.0, then Y16 = 92.0 − (−180.0) = 272.0
7. **Shutter bias**: deltaIdx = (24.5 − (−30)) × 10 = 545, bias = curve[545] = 5521
8. **Curve lookup**: lookupVal = 272.0 + 5521 = 5793, searchsorted → index 547, temp = 547 / 10 + (−30) = 24.7°C
9. **Bilinear blend**: 4 curves (2 FPA brackets × 2 focus groups) weighted by FPA position
   and object distance → ≈ 24.8°C
10. **Emissivity correction** (if ε < 0.98): radiometric formula via nEmissCurve lookup
    table in radiance space → ≈ 24.6°C

Key insight: raw Y16 ≈ 5847 is far too large for the curves. After dark subtraction +
gain correction the signal drops to ≈ 92, which the shutter bias (≈ 5521) brings into
curve range. The bias encodes the expected sensor response at the current shutter
temperature relative to the calibration reference (−30°C).

### Calibration Packages (from device flash)

- **High-temp:** 277,460 bytes at `0x100000` (range ID 1, 4500 curveSteps, -20 to 430°C)
- **Low-temp:** 210,260 bytes at `0x132000` (range ID 0, 2100 curveSteps, -30 to 180°C)

#### Package Format

```
Header (216 bytes):
  [0x00] uint32: header_size = 216
  [0x04] string: "TI_CAL_METHOD_REX_DISTANCE_EMISS_BG_TRANSMISSIVITY_2"
  [0x40] byte:   range_id (0=low, 1=high)
  [0x41] byte:   n_version_sets (7)
  [0x42] int16:  min_ref (-30 low, -20 high) — coreBodyTemp base
  [0x44] uint16: max_range (180 low, 430 high)
  [0x46] uint16: t_val2 (15366 low, 3591 high)
  [0x4B] byte:   n_focus_groups (2)
  [0x4C] uint16: width = 120
  [0x4E] uint16: height = 90
  [0x50] uint16: curveSteps (2100 low, 4500 high)
  [0x54] uint32: section1_size (58800 low, 126000 high)
  [0x58] uint32: section2_size = 151200 (both)
  [0x5C] float32 × 6: polynomial coefficients (zeros in low-temp)

Sub-header / Focus Buffer:
  Located at package byte offset **0xF6 (246)**, after a 30-byte gap (0xD8–0xF5)
  following the 216-byte header (padding or additional header fields).

  Focus buffer length: header[0x52] bytes (uint16 field).
  Contains int16 FPA temperature brackets (one per n_version_sets).
  getCurve divides these by 100.0 to get °C, using **signed int16** comparison.

  Low-temp example: first 7 int16 values from focus buffer represent FPA brackets.
  Values like 828, 1767, 2677, 3616, 4558, 6500 → 8.28°C, 17.67°C, etc.

Section 1 (Y16 lookup curves):
  n_version_sets × n_focus_groups × curveSteps contiguous uint16 entries.
  Low:  7 × 2 × 2100 = 29,400 entries = 58,800 bytes
  High: 7 × 2 × 4500 = 63,000 entries = 126,000 bytes
  Each block: monotonically increasing Y16 lookup table.
  Temperature for index i: T = i/10.0 + min_ref

Section 2 (per-pixel correction tables):
  7 × 10800 uint16 values = 7 tables of 90×120 pixels
  Values ~8000–10000 (with ~0.5% outliers up to 50000)
  Tables 0==1 and 5==6 (5 unique tables)
  K-buffer: used as per-pixel NUC gain = (K & 0x7FFF) / 8192.0, multiplied with (raw - dark)
```

### Device Calibration Registers

**Calibration points** (registers 17–34, 9 pairs, stored as int32/10000):

| Reg Pair | Reference Temp | Camera Reading | Error |
|----------|---------------|----------------|-------|
| 17, 18 | -10.0°C | -6.6°C | +3.4°C |
| 19, 20 | -5.0°C | -2.8°C | +2.2°C |
| 21, 22 | 0.0°C | 2.3°C | +2.3°C |
| 23, 24 | 20.0°C | 21.4°C | +1.4°C |
| 25, 26 | 45.0°C | 43.7°C | -1.3°C |
| 27, 28 | 80.0°C | 74.9°C | -5.1°C |
| 29, 30 | 120.0°C | 112.9°C | -7.1°C |
| 31, 32 | 250.0°C | 233.6°C | -16.4°C |
| 33, 34 | 350.0°C | 329.6°C | -20.4°C |

**Linear correction coefficients** (registers 49–56, stored as int32/10000):

| Set | k (slope) | b (intercept) | Use |
|-----|-----------|---------------|-----|
| 1 | 1.1157 | -2.3596 | Low-temp range |
| 2 | 1.1053 | -3.0705 | Low-temp range |
| 3 | 1.0526 | 1.1579 | High-temp range |
| 4 | 1.0620 | 0.6527 | High-temp range |

Applied as `T_corrected = k × T_measured + b`, then piecewise correction from
the 9 calibration points further refines accuracy.

### Shutter / NUC Behavior

- **Shutter close:** blocks sensor for dark-frame reference
- **NUC:** corrects per-pixel gain/offset
- **Shutter open:** resumes imaging
- Shutter delay: 400ms
- Auto-shutter schedule: initially off, enabled at 360s (every 60s)

---

## APK Initialization Flow

### Full Startup Sequence (from `InitThread.java` / `UsbCameraHelper.java`)

```
UsbCameraFrag
  └─► UsbCameraHelper.init(context, usbDevice)
        └─► initArmInterfaces()
              └─ unitCoreInit(context, connectListener, device)
                    │
                    ▼  onConnected callback
                    └─ InitThread (new thread)
                          ├─ waitInitStatus()        ← polls getInitStatus() ×10, 500ms apart, waits for ==1
                          ├─ initModelVersion()      ← reads SW version register
                          ├─ initModelInfo()         ← reads vendor/product ID registers
                          ├─ getGuiderPackage(1)     ← downloads HIGH-temp calibration pkg
                          ├─ getGuiderPackage(0)     ← downloads LOW-temp calibration pkg
                          ├─ initCalibrationInfo()   ← reads registers 49–56 (4 k/b pairs)
                          └─ [if cal mode] initCalibrationDatas() ← registers 17–36
                                │
                                ▼  InitThread.finish(true)
                                startImg()
                                  ├─ destroyGuideLib()       ← clean up previous instance
                                  ├─ waitImgStatus()         ← setRunStatus(2) → image mode
                                  └─ initGuideInterface()    ← *** native library init ***
                                        ├─ new GuideInterface → loads libguide_sdk_unitrend.so
                                        ├─ guideCoreParsePackage(0, lowPkgBytes)
                                        ├─ guideCoreParsePackage(1, highPkgBytes)
                                        ├─ guideCoreInit(scale=2.0)
                                        ├─ setShutterDelayTime(400)
                                        ├─ setAutoShutter(false, 60000, 0)
                                        ├─ switchShutterRecover(true)
                                        ├─ setTffParam(5)       ← temporal filter strength
                                        ├─ setFrameRate(10)     ← 10ms delay between frames
                                        ├─ setShutterDelta(80)  ← initial shutter delta
                                        └─ setNucDelta(120)     ← initial NUC delta
                                              │
                                              ▼  onStart()
                                              ├─ startGetImage(callback) ← spawns frame thread
                                              └─ setFrameRate(10)
```

**Post-init configuration** (`setConfigData()`):
- `changePalette(colorPalate + paletteCount)`
- `setBright(bright)`, `setContrast(contrast)`
- `setDistance(refDistant)`
- `setEmissivity(mSRateBean.value)` — typically 0.96
- `setEnvironmentTemp(environmentTemp)`
- `isShowIrImage(true)`

### Shutter/NUC Delta Schedule

| Time Window | Shutter Delta | NUC Delta |
|-------------|---------------|-----------|
| 0–180s      | 80            | 120       |
| 180–360s    | 50            | 100       |
| 360–480s    | 30            | 60        |

At 360s, auto-shutter enabled (`setAutoShutter(true, 60000, 0)`) — every 60s.

### Auto Measure Range Switching

Two calibration ranges: low-temp (-30°C to 180°C) and high-temp (-20°C to 430°C).

**Hysteresis thresholds** (from `UsbCameraHelper.java`):
- **Switch to HIGH range**: max temp > 150°C
- **Switch to LOW range**: max temp < 120°C (30°C hysteresis band)
- Minimum 5 seconds between range switches

**USB command** (`sendSwitchTempLevelCmd`): Sends sensor params for the new range:
```
[FUNC_SENSOR_CMD=0x0A, offset=0x09, count=0x01, 0, res, int, gain]
```

**Sensor parameters** from calibration header (Ghidra: `guideCoreGetGain/Int/Res`):

| Parameter | Header offset | Low-temp (range 0) | High-temp (range 1) |
|-----------|--------------|---------------------|----------------------|
| gain      | 0x46         | 6                   | 7                    |
| int       | 0x47         | 60                  | 14                   |
| res       | 0x48         | 9                   | 9                    |

**Native SDK** (`guideCoreSetMeasureMode`): Sets `mMeasureMode`, copies the
appropriate K-buffer (Section 2 gain table) into CInfraredCore+0x4440, and resets
gear indices to force `updateK` re-selection.

**Switch sequence**:
1. Send USB command with new sensor params → hardware changes integration/gain
2. Swap calibration package in processor (new curves, offsets, temp range)
3. Perform shutter calibration (new dark frame for new hardware config)

### Key APK Source Files

| File | What it reveals |
|------|----------------|
| `com/unit/usblib/armlib/UnitArmInterface_ByGuide.java` | Command protocol, frame request, bulk read |
| `com/unit/usblib/armlib/guide/UploadThread_ParamPkg.java` | Calibration package download |
| `com/unitrend/guidelib/GuideInterface.java` | Guide SDK wrapper |
| `com/unitrend/guidelib/GuideSdkUnitrend.java` | JNI native methods |
| `com/unitrend/uti120_mob/camera/FrameParamReader.java` | Frame header byte offsets |
| `com/unitrend/uti120_mob/camera/UsbCameraHelper.java` | Init sequence, frame callback |
| `com/unitrend/uti120_mob/camera/InitThread.java` | Device init: polling, firmware, calibration |

```bash
# To re-decompile:
jadx -d apk_decompiled UT-Mobile_v1.1.18.apk --no-res
```


### Call Chain

```
JNI: guideCoreMeasureTempByY16(int y16)    @ 0x15a90
  -> getCurve(hostParam, calibData)         @ 0x155b0
  -> GetLowTemperatures(...)                @ 0x21dd0  (or GetHighTemperatures @ 0x21300)
       -> GetSingleCurveTemperatures() x4   @ 0x20e10  (4 curve segments)
            -> LensDriftCorrectZX01C()      @ 0x21110  (ACTIVE, MP[0xa0]=1)
            -> ShutterDrift (SKIPPED)       (MP[0xa1]=0, inactive for UTi120)
       -> Weighted interpolation (MP[0xa2]=1)
       -> EmissCorr via nEmissCurve         @ 0x26670  (if MP[0xa3]=1 && emiss < 0.98)
       -> Distance correction               (MP[0xa4]=0, DISABLED)
       -> AmbTempCorrect                    (MP[0xa5]=0, DISABLED)
```

### getCurve: Curve Segment Selection (0x155b0)

Selects 4 curve segments from Section 1 based on FPA temperature via **linear forward scan**.

**Key header fields** (two distinct bytes):
- `DataHeader[0x41]` = **n_version_sets** (7) — number of FPA temperature brackets
- `DataHeader[0x4b]` = **n_focus_groups** (2) — sub-curves per bracket

Curve data is organized as `n_version_sets × n_focus_groups × curveSteps` uint16 entries.
Stride per version set = `n_focus_groups × curveSteps`.

**Three cases:**
1. **FPA < first bracket**: copy bracket 0's 2 sub-curves → MeasureCurveBuf slots 2,3. Set versionIdx=0.
2. **FPA > last bracket**: copy last bracket's 2 sub-curves → slots 0,1. Set versionIdx=last.
3. **FPA within range** (linear scan): find bracket `[i, i+1]` where `bracket[i] <= FPA <= bracket[i+1]`.
   Copy bracket i's 2 sub-curves → slots 0,1 and bracket (i+1)'s 2 sub-curves → slots 2,3.
   Set `MP[0xAB] = i+1` (1-based version index).

Result: 4 sub-curves in MeasureCurveBuf (2 per bracket × 2 brackets).

### GetSingleCurveTemperatures (0x20e10) — Core Algorithm
**Two coefficient sets** selected by `param_5` (sub-curve index 0 or 1):
```
sub-curve 0: coeff = MP[0x4c] * MP[0x5c], offsets = MP[0x50] + MP[0x60]
sub-curve 1: coeff = MP[0x54] * MP[0x64], offsets = MP[0x58] + MP[0x68]
```
All coefficients are 1.0 and offsets are 0.0 after init.

```python
# 1. Apply lens drift (SUBTRACT from Y16)
if MP[0xa0]: y16 -= LensDriftCorrectZX01C(...)

# 2. Apply shutter drift (ADD to Y16) — inactive for UTi120
if MP[0xa1]: y16 += ShutterDriftCorrect(...)

# 3. Shutter-temperature-based bias from curve table
deltaIdx = int((shutterTemp - coreBodyTemp) * 10)
bias = curveBuf[deltaIdx] if 0 < deltaIdx < curveSteps else 0.0

# 4. NUC-corrected Y16 + bias → lookup value
lookupVal = int(nuc_y16 * coeff + bias)   # coeff = 1.0

# 5. LINEAR search in curve table (not binary) — native implementation
# Finds first index where curve[index] > lookupVal ("ceiling" search)
# Python impl uses np.searchsorted(side='right') - 1 for equivalent results.
curve_index = 0
for i in range(1, curveSteps):
    if curveBuf[i] > lookupVal:
        curve_index = i
        break

# 6. Index → temperature
temperature = curve_index / 10.0 + offset_sum + coreBodyTemp
# offset_sum = MP[0x50]+MP[0x60] or MP[0x58]+MP[0x68], normally 0.0
```

**Why raw Y16 doesn't work**: Raw USB Y16 ≈ 5935, NUC'd Y16 ≈ 83 (for 25°C scene
with 24°C shutter). The curve expects the small NUC-corrected value.

### GetLowTemperature / GetHighTemperature — Interpolation
Calls `GetSingleCurveTemperature` 2 or 4 times depending on version index:
- **versionIdx == 0** (FPA below first bracket): 2 calls only (slots 2,3)
- **versionIdx == last** (FPA above last bracket): 2 calls only (slots 0,1)
- **Otherwise**: 4 calls (all slots)

**Interpolation weights** (FPA-temperature-based):
```python
if versionIdx == 0 or versionIdx == last:
    weight_lower, weight_upper = 1.0, 0.0  # no interpolation
else:
    ratio = (upper_bracket_FPA - current_FPA) / (upper_bracket_FPA - lower_bracket_FPA)
    weight_lower = ratio
    weight_upper = 1.0 - ratio
```

**Focus distance weighting** (distance-based): second dimension of interpolation between the
2 focus groups within each bracket. Compares `MEASURE_PARAM[0x98]` (object distance) against
`HOST_PARAM[0x28]`/`[0x2C]` (focus distance params from `DataHeader[0x74..0x77]`, uint16/10.0).
With default distance=1.0m and params [0.5, 1.2]: focus0_w≈0.286, focus1_w≈0.714.

**Post-interpolation corrections:**
1. **EmissCorr** (table-based, MP[0xb0]=1): `EmissCor(temp*10, GetY16FromT(ambient*10), emiss*100)`
2. **Distance correction** (MP[0xa4]=0, disabled): `temp /= distance`
3. **Ambient temp correction** (MP[0xa5]=0, disabled): `AmbTempCorrect(FPA_temp)`

### getTempMatrix — Frame Temperature Optimization (native only)
Instead of calling `GetLowTemperature` for all 10,800 pixels, the native SDK uses **10-point
piecewise linear interpolation** for ~1000× speedup:

```python
# 1. Find min/max Y16 across frame
# 2. Create 10 evenly-spaced Y16 knots from min to max
# 3. Call GetLowTemperature only at these 10 knots
# 4. Compute slopes between adjacent knots
# 5. For each pixel: binary search knot interval, linear interpolate
```

> **Python impl note:** The Python code performs full per-pixel conversion via
> `y16_to_temperature_interpolated()` with NumPy vectorization, prioritizing accuracy over
> the native 10-point approximation. `getTempMatrix` is defined but never called via JNI —
> the APK uses `guideCoreMeasureTempByY16` per-pixel.

### HOST_PARAM Layout (from ParsePackage)

```
0x00    float[7] FPA bracket temps         focusBuf int16 / 100.0 (one per n_version_sets)
0x28    float[2] Focus group params        DataHeader[0x74..] uint16 / 10.0
0x38    float    Emissivity factor        0.95 (default)
0x3c    float    Reflect temperature      0.0
0x40    float    Ambient temperature      23.0
0x44    float    coreBodyTemp (min_ref)   e.g., -30.0 (from header[0x42] as int16)
0x48    float    max range value          e.g., 180.0 (from header[0x44] as int16)
0x4c    uint16   (zeroed)
0x4e    uint16   (zeroed)
0x50    uint16   curveSteps               2100 or 4500 (from header[0x50])
0x52    byte     n_version_sets           7 (from header[0x41])
0x53    byte     n_focus_groups           2 (from header[0x4b])
```

### MEASURE_PARAM Layout (from initMeasureParam)

```
0x00    func_ptr LensDriftCorrectZX01C    selected drift function
0x08    func_ptr ShutterDriftCorrect3     selected shutter drift function
0x10    float    Startup shutter temp     from frame header short[8] / 100.0
0x14    float    Last-last shutter temp   from previous shutter-open event
0x18    float    Last shutter temp (NUC)  captured at shutter-open
0x1c    float    Current shutter RT temp  from frame header short[9] / 100.0
0x20    float    Current lens temp        from frame header short[10] / 100.0
0x24    float    Last lens temp (NUC)     captured at shutter-open
0x28    float    Last-last lens temp
0x2c    float    Current FPA temp         from frame header short[11] / 100.0
0x4c    float    coeff_a (sub-curve 0)    1.0
0x50    float    offset_a (sub-curve 0)   0.0
0x54    float    coeff_b (sub-curve 1)    1.0
0x58    float    offset_b (sub-curve 1)   0.0
0x5c    float    coeff_e0                 1.0
0x60    float    offset_e0                0.0
0x64    float    coeff_e1                 1.0
0x68    float    offset_e1                0.0
0x6c    float    Lens drift coeff HIGH    -200.0
0x78    float    Lens drift coeff LOW     -300.0
0x7c    float    NUC FPA temp             captured at shutter-open
0x98    float    Distance                 0.8 (object distance in metres; set by guideCoreSetDistance)
0x9c    uint16   mAvgB (frame avg)        from guideCoreUpdateB
0x9e    uint16   mLastAvgB (prev avg)

Enable flags (0xa0–0xa5, packed as 0x01010001 at init):
0xa0    byte     Lens drift               1 (ON)
0xa1    byte     Shutter drift            0 (OFF for UTi120)
0xa2    byte     Interpolation            1 (ON)
0xa3    byte     Emissivity correction    1 (ON)
0xa4    byte     Distance correction      0 (OFF)
0xa5    byte     Ambient temp correction  0 (OFF)

0xAB    byte     Current version index    set by getCurve (1-based bracket)
0xAC    byte     Measure mode             0=low, 1=high
0xB0    uint32   EmissCorr algorithm ver  1 (table-based; >=3 uses polynomial)
```

### Drift Corrections
**LensDriftCorrectZX01C** (active, MP[0xa0]=1):
```python
if nuc_fpa_temp >= threshold and nuc_lens_temp >= threshold:
    if mode == 0:  # low range
        coeff = drift_param[0x44]  # -300.0
    else:          # high range
        coeff = drift_param[0x40]  # -200.0
    drift = (current_lens - nuc_lens) * coeff
    return drift
return 0.0
```

**Three drift models exist** (selected per device):
- `LensDriftCorrect1`: Simple `(lens_current - lens_nuc) * coeff`
- `LensDriftCorrectZX01C`: Same + guard (returns 0 if NUC temps uninitialized)
- `LensDriftCorrectBody`: Two-variable model: `-(Δlens * A + Δbody * B + C)`

**ShutterDrift functions are NOT dead code in general**, but are inactive for UTi120
(MP[0xa1]=0). Three models exist:
- `ShutterDriftCorrect1`: `(shutterTemp + CONST) * coeff`
- `ShutterDriftCorrect2`: `(shutterTemp_current - shutterTemp_nuc) * coeff`
- `ShutterDriftCorrect3`: Piecewise-linear coefficient based on NUC shutter temp

### EmissCorr: Emissivity Correction

Skipped when emissivity >= 0.98. Uses `nEmissCurve` lookup table (**16,384 int16 entries**, indices 0–0x3FFF).

**nEmissCurve table**: Extracted from `libguide_sdk_unitrend.so` at symbol `nEmissCurve`
(vaddr 0x54c10, .data section, 32,768 bytes). Monotonically increasing, maps radiance indices
to temperature × 10 (range: -450 to 8500 → -45.0°C to 850.0°C). Saved as `uti120/nEmissCurve.npy`.

```python
# EmissCorr formula (standard radiometric: S_obj = (S_meas - (1-e)*S_amb) / e)
# 1. Binary search temp*10 in nEmissCurve → meas_idx (range [0, 0x3FFF])
# 2. Binary search ambient*10 in nEmissCurve → amb_idx
# 3. adjusted_idx = (meas_idx * 100 - amb_idx * (100 - emiss_pct)) / emiss_pct
# 4. Clamp to [0, 0x3FFF], return nEmissCurve[adjusted_idx] / 10.0
```

**DeEmissCor** (inverse): `idx = ambient*(100-e%) + measured_idx*e%`, lookup `nEmissCurve[idx/100]`.

### guideCoreParsePackage — Package Parsing
Data sections start at byte **0xF6 (246)** in the package:
```
[0x00  .. 0xD7]   Header (216 bytes) — copied to mLowDataHeader or mHighDataHeader
[0xD8  .. 0xF5]   Gap (30 bytes) — sub-header, not separately copied
[0xF6  .. 0xF6+focusLen-1]   Focus buffer (FPA bracket temps, int16[])
[... +focusLen]               Section 1 curves (curveLen bytes)
[... +curveLen]               Section 2 K-tables (kLen bytes)
```

`focusLen` comes from header offset **0x52** (uint16, NOT uint32).

**Note:** The focus buffer starts at byte 246 (`section1_start - focus_len`), not at byte 216
(`header_size`). Correct values are monotonically increasing FPA brackets, e.g.
`[0, 828, 1767, 2677, 3616, 4558, 6500]`.

### Shutter Temperature for Bias Calculation
The native `updateMeasureParam()` sets `MP[0x1c] = realtimeShutterTemp / 100.0` **every
frame** from the current frame's header (short[9]). `GetSingleCurveTemperature` uses
`MP[0x1c]` to compute `deltaIdx = (shutterTemp - coreBodyTemp) * 10`, which indexes into
the curve for the shutter-temperature-dependent bias.

### guideCoreUpdateB — Dark Frame Capture
```python
# Copies pixel data (skipping header) into B-frame buffer
memcpy(core.darkBuf, rawFrame[360:], 21600)  # 720 bytes offset, 21600 bytes

# Computes average B value using SIMD across pixel range
mAvgB = average(frame[0x17A:0x2BAA:0x14])  # subsampled average
```

### smoothFocusTemp — FPA Temperature Smoothing
Maintains a ring buffer of 15 FPA temperature readings. Once full, returns the
**median** (7th element after sorting). Prevents noise in FPA temperature from
causing erratic gain table or curve selection changes.

### InfraredImageProcess: Full Frame Pipeline
Called by `guideCoreConvertXToImage`. Each step gated by CInfraredCore byte offset:

```
Offset  Step
0x00    1. NUCbyTwoPoint()           — Per-pixel NUC correction + ReplaceBadPoint
0x01    2. ReplaceBadPoint()         — Second pass on secondary buffer (if gainBuf[0] != 0)
0x04    3. TimeNoiseFilter()         — Temporal noise filter (params at 0x0C, 0x10)
0x14    4. RemoveVerStripe()         — Vertical stripe removal (kernel at 0x320, params 0x18/0x1C/0x24)
0x28    5. RemoveHorStripe()         — Horizontal stripe removal (kernel at 0xB20, params 0x2C/0x30/0x38)
0x3C    6. GaussianFilter/FixedPointGray — Spatial smoothing (type at 0x40, kernel at 0x1348)
0x50    7. Flip()                    — Horizontal/vertical flip (mode at 0x54)
0x58    8. Rotation()                — Image rotation (angle at 0x5C)
0x43D0  9. Y16 snapshot copy         — To measurement buffer
0x60   10. ModelDRT or IIE           — Image enhancement (0=DRT auto, 1=IIE manual)
0xBC   11. LaplaceSharpen()          — Edge sharpening (strength at 0xC0)
0xC4   12. Brightness/Contrast       — Y8 adjustment (bright=0xC8, contrast=0xCC)
0x43D1 13. Y8 snapshot copy
0xD0   14. Resize()                  — Scale if != 1.0 (interp type at 0xD4)
0xD8   15. PseudoColor()             — Palette colormap (index at 0xDC)
```

### NUCbyTwoPoint formula

```
out[i] = ((raw[i] - dark[i]) * (gain[i] & 0x7FFF)) >> 13
```
Gain bit 15 is a **bad pixel flag** (used by ReplaceBadPoint). Gain=8192 (0x2000) is unity.
With average gain ~9026, this is ~1.1× scale factor. SIMD (SSE4.1) path processes 8 pixels at a time.

### ReplaceBadPoint — Dead Pixel Replacement
- **Bad pixel** = gain buffer bit 15 set (value ≥ 0x8000, i.e., `(short)gain < 0`)
- **Algorithm**: 3×3 median filter using only valid (non-bad) neighbors
- Even neighbor count: average of two middle values; odd: exact median
- Skips center pixel and other bad neighbors in the median calculation

Factory-flagged bad pixels typically have abnormal gain values (1.3×–2.2× vs normal ~1.1×).
The firmware applies ReplaceBadPoint *before* gain correction (NUCbyTwoPoint), so the
replaced median value gets multiplied by the bad pixel's own abnormal gain factor.

> **Python impl note:** Uses `scipy.ndimage.median_filter(size=3)` on the full neighborhood
> (doesn't skip bad neighbors) plus a secondary outlier detection pass (>5× MAD from 3×3
> median) to catch bad pixels not in the factory mask. Simpler but effective.

### RemoveVerStripe / RemoveHorStripe — Stripe Removal
Both use identical algorithm (transposed):
1. **Smooth** image using `FixedPoint_GrayFilter_16bit_RSN` (vertical-only or horizontal-only)
2. **Compute residuals** = original − smoothed, clamped to `[-clamp_range, +clamp_range]`
3. **Average residuals per column/row** (weighted by confidence from step 1)
4. **Subtract** per-column or per-row correction from all pixels

Scratch buffers: smoothed (0x1320), weights (0x1328), residuals (0x1330),
column corrections (0x1338), row corrections (0x1340).

> **Python impl note:** Stripe removal is not implemented in the Python pipeline. The native
> algorithm uses SIMD-optimized fixed-point filtering, and the visual impact is minimal after
> NUC correction.

### Brightness/Contrast
```python
mean = average(Y8_output)
for pixel in Y8:
    adjusted = ((pixel - mean) * contrast) >> 7 + mean + brightness - 128
    output = clamp(adjusted, 0, 255)
```

### CInfraredCore Struct Layout (total 0x4450 = 17,488 bytes)

```
Configuration flags and parameters:
0x00     byte     NUCbyTwoPoint enable
0x01     byte     ReplaceBadPoint enable (second pass)
0x04     byte     TimeNoiseFilter enable
0x0C     int      TimeNoiseFilter param1
0x10     int      TimeNoiseFilter param2
0x14     byte     RemoveVerStripe enable
0x18     int      RemoveVerStripe kernel size
0x1C     int      RemoveVerStripe min weight
0x24     int      RemoveVerStripe clamp range
0x28     byte     RemoveHorStripe enable
0x2C     int      RemoveHorStripe kernel size
0x30     int      RemoveHorStripe min weight
0x38     int      RemoveHorStripe clamp range
0x3C     byte     Spatial filter enable
0x40     int      Spatial filter type (0=Gaussian, 1=FixedPointGray)
0x4C     int      Spatial filter kernel size
0x50     byte     Flip enable
0x54     int      Flip mode
0x58     byte     Rotation enable
0x5C     int      Rotation angle
0x60     byte     IIE enable (0=ModelDRT, 1=FixedPoint_IIE)
0xB0     int      300 (stripe removal threshold)
0xBC     byte     LaplaceSharpen enable
0xC0     float    LaplaceSharpen strength (1.0)
0xC4     byte     Brightness/Contrast enable
0xC8     int      Brightness offset
0xCC     int      Contrast multiplier
0xD0     float    Resize scale factor
0xD4     int      Resize interpolation (1=bilinear)
0xD8     byte     PseudoColor enable
0xDC     int      Palette index
0xE0     int      PseudoColor param2
0xE4     int      Image width (120)
0xE8     int      Image height (90)
0xF0-0x16F       Radiation curve LUT (32 entries)
0x320-0xB1F      Vertical stripe correction kernel (512 entries)
0xB20-0x131F     Horizontal stripe correction kernel (512 entries)
0x1320   ptr      Smoothed image buffer
0x1328   ptr      Filter weight/confidence map
0x1330   ptr      Clamped residuals (int32)
0x1338   ptr      Per-column corrections (int32)
0x1340   ptr      Per-row corrections (int32)
0x1348   int[9]   Gaussian 3x3 kernel coefficients (Q12 fixed-point)

Image pipeline buffers:
0x43D0   byte     Y16 snapshot enable
0x43D1   byte     Y8 snapshot enable
0x43F8   int      Working width
0x43FC   int      Working height
0x4400   int      Total pixel count
0x4408   short*   Previous frame / temporal buffer
0x4410   uchar*   Y8 output buffer
0x4418   int      Output width
0x441C   int      Output height
0x4420   int      Output pixel count
0x4428   short*   outBuf     — NUC-corrected Y16 (for temp measurement)
0x4430   uchar*   Y8 working buffer
0x4438   short*   darkBuf    — Dark frame reference (B-frame)
0x4440   ushort*  gainBuf    — Per-pixel gain from Section 2 (loaded by updateK)
0x4448   short*   frameBuf   — Raw frame data from USB
```

### updateK: Gain Table Selection
Linear scan through FPA bracket temperatures (same as getCurve). Selects one of
n_version_sets (7) gain tables from Section 2, copying `width × height × 2` bytes
into `CInfraredCore + 0x4440`. Caches current bracket index in `mHighGear`/`mLowGear`
to skip redundant copies.

---

## Auto-Recalibration (ShutterHandler)

Reconstructed from `ShutterHandler.java` and `UsbCameraHelper.java`. The APK monitors
FPA sensor temperature drift every frame and triggers shutter/NUC operations automatically.
This is distinct from the high/low range switching (150°C/120°C thresholds).

### Three Mechanisms

1. **FPA delta → NUC+Shutter** (rare): `|currentFPA - baseFPA| >= NucDelta` triggers
   full hardware NUC + shutter close/open. 30-second cooldown between NUC operations.
2. **FPA delta → Shutter-only** (frequent): `|currentFPA - baseShutterFPA| >= ShutterDelta`
   triggers shutter close/open to refresh dark frame. No cooldown.
3. **Periodic timer** (after 6 min): Every 60 seconds, shutter-only refresh.

### Time-Scaled Thresholds

Thresholds tighten as the camera warms up (from `UsbCameraHelper.setAutoShutter`):

| Time since startup | ShutterDelta (°C) | NucDelta (°C) |
|---|---|---|
| 0–180s | 0.80 | 1.20 |
| 180–360s | 0.50 | 1.00 |
| 360s+ | 0.30 | 0.60 |

---

## Critical NUC Sequencing

1. Hardware NUC **MUST** run at least once (e.g., on startup) to normalize per-pixel
   offsets. Without this, dark frame std is ~7500 instead of ~160.
2. Dark frame capture **MUST NOT** trigger hardware NUC. HW NUC flattens the raw
   signal (NUC Y16 drops from ~290 to ~85), corrupting the software NUC pipeline.
3. Sequence: NUC → open → capture live → close → capture dark → open → capture live
4. Both dark and live frames use the same HW NUC baseline, so it cancels in subtraction.

**APK dark frame approach**:
- Native `guideCoreUpdateB(byte[])` captures a **single** raw frame as dark reference
- `switchShutterRecover(true)` + `mShutterRecoverFrameCount = 150` — discards 150 frames
  after shutter opens to let sensor stabilize before resuming display

---

## Python Implementation Notes

Key differences between the Python implementation (`uti120/`) and the native SDK:

| Feature | Native SDK | Python Implementation |
|---------|-----------|----------------------|
| Default emissivity | 0.96 (SRateBean.java) | 0.95 (constants.py) |
| Curve search | Linear scan (for loop) | Binary search (`np.searchsorted`) |
| Bad pixel replacement | 3×3 median skipping bad neighbors | `scipy.ndimage.median_filter` + outlier pass (>5× MAD) |
| Stripe removal | SIMD fixed-point filter + per-col/row correction | Not implemented (minimal impact after NUC) |
| getTempMatrix | 10-point piecewise linear approximation | Full per-pixel vectorized conversion |
| Flatfield FPN | Not present | Optional `flatfield_fpn.npy` subtracted post-NUC |
| TFF | Always active | Configurable, can be disabled |
| Image processing (DRT, sharpen, etc.) | Full 15-step pipeline | Only NUC, gain, bad pixels, TFF, flip |
