# py-opendisplay

[![PyPI](https://img.shields.io/pypi/v/py-opendisplay?style=flat-square)](https://pypi.org/project/py-opendisplay/)
[![Python](https://img.shields.io/pypi/pyversions/py-opendisplay?style=flat-square)](https://pypi.org/project/py-opendisplay/)
[![License](https://img.shields.io/github/license/OpenDisplay-org/py-opendisplay?style=flat-square)](LICENSE)
[![Tests](https://img.shields.io/github/actions/workflow/status/OpenDisplay-org/py-opendisplay/test.yml?style=flat-square&label=tests)](https://github.com/OpenDisplay-org/py-opendisplay/actions/workflows/test.yml)
[![Lint](https://img.shields.io/github/actions/workflow/status/OpenDisplay-org/py-opendisplay/lint.yml?style=flat-square&label=lint)](https://github.com/OpenDisplay-org/py-opendisplay/actions/workflows/lint.yml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json&style=flat-square)](https://github.com/astral-sh/ruff)
[![mypy](https://img.shields.io/badge/mypy-strict-blue?style=flat-square)](https://mypy.readthedocs.io/)

Python library for communicating with OpenDisplay BLE e-paper displays.

## Installation

```bash
pip install py-opendisplay
```

## Quick Start

### Option 1: Using MAC Address

```python
from opendisplay import OpenDisplayDevice
from PIL import Image

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    image = Image.open("photo.jpg")
    await device.upload_image(image)
```

### Option 2: Using Device Name (Auto-Discovery)

```python
from opendisplay import OpenDisplayDevice, discover_devices
from PIL import Image

# List available devices
devices = await discover_devices()
print(devices)  # {"OpenDisplay-A123": "AA:BB:CC:DD:EE:FF", ...}

# Connect using name
async with OpenDisplayDevice(device_name="OpenDisplay-A123") as device:
  image = Image.open("photo.jpg")
  await device.upload_image(image)
```

## CLI

Run without installing (requires [uv](https://docs.astral.sh/uv/)):

```bash
uvx --from "py-opendisplay[cli]" opendisplay --help
```

Or after `pip install py-opendisplay[cli]`:

```bash
# Discover nearby devices
opendisplay scan

# Read device info (size, color, firmware, board)
opendisplay info --device AA:BB:CC:DD:EE:FF

# Upload an image
opendisplay upload --device AA:BB:CC:DD:EE:FF photo.jpg
opendisplay upload --device AA:BB:CC:DD:EE:FF photo.jpg --fit cover --refresh-mode fast

# Reboot the device
opendisplay reboot --device AA:BB:CC:DD:EE:FF

# Export / write device configuration
opendisplay export-config --device AA:BB:CC:DD:EE:FF config.json
opendisplay write-config --device AA:BB:CC:DD:EE:FF config.json
```

Encrypted devices require `--key HEX` (32 hex characters). Pass `-v` / `--verbose` for debug logging.

## Image Fitting

Images are automatically fitted to the display dimensions. Control how aspect ratio mismatches are handled with the `fit` parameter:

```python
from opendisplay import OpenDisplayDevice, FitMode
from PIL import Image

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    image = Image.open("photo.jpg")

    # Default: scale to fit, pad with white (no distortion, no cropping)
    await device.upload_image(image, fit=FitMode.CONTAIN)

    # Scale to cover display, crop overflow (no distortion, fills display)
    await device.upload_image(image, fit=FitMode.COVER)

    # Distort to fill exact dimensions
    await device.upload_image(image, fit=FitMode.STRETCH)

    # No scaling, center-crop at native resolution (pad if smaller)
    await device.upload_image(image, fit=FitMode.CROP)
```

| Mode                | Aspect Ratio | Fills Display          | Content Loss            |
|---------------------|--------------|------------------------|-------------------------|
| `CONTAIN` (default) | Preserved    | No (white padding)     | None                    |
| `COVER`             | Preserved    | Yes                    | Edges cropped           |
| `STRETCH`           | Distorted    | Yes                    | None (but distorted)    |
| `CROP`              | Preserved    | Depends on source size | Edges cropped if larger |

## Image Rotation

Rotate source images before fitting/encoding using the `rotate` parameter:

```python
from opendisplay import OpenDisplayDevice, Rotation
from PIL import Image

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    image = Image.open("photo.jpg")
    await device.upload_image(image, rotate=Rotation.ROTATE_90)
```

Rotation is applied before `fit`, so crop/pad behavior matches the rotated orientation.
Rotation angles use clockwise semantics (`ROTATE_90` = 90 degrees clockwise).

## Dithering Algorithms

E-paper displays have limited color palettes, requiring dithering to convert full-color images. py-opendisplay supports 9 dithering algorithms with different quality/speed tradeoffs:

### Available Algorithms

- **`none`** - Direct palette mapping without dithering (fastest, lowest quality)
- **`ordered`** - Bayer/ordered dithering using pattern matrix (fast, visible patterns)
- **`burkes`** - Burkes error diffusion (default, good balance)
- **`floyd-steinberg`** - Floyd-Steinberg error diffusion (most popular, widely used)
- **`sierra-lite`** - Sierra Lite (fast, simple 3-neighbor algorithm)
- **`sierra`** - Sierra-2-4A (balanced quality and performance)
- **`atkinson`** - Atkinson (designed for early Macs, artistic look)
- **`stucki`** - Stucki (high quality, wide error distribution)
- **`jarvis-judice-ninke`** - Jarvis-Judice-Ninke (highest quality, smooth gradients)

### Usage Example

```python
from opendisplay import OpenDisplayDevice, RefreshMode, DitherMode
from PIL import Image

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    image = Image.open("photo.jpg")

    # Use Floyd-Steinberg dithering
    await device.upload_image(
        image,
        dither_mode=DitherMode.FLOYD_STEINBERG,
        refresh_mode=RefreshMode.FULL
    )
```
### Comparing Dithering Modes

To preview how different dithering algorithms will look on your e-paper display, use the **[img2lcd.com](https://img2lcd.com/)** online tool.
Upload your image and compare the visual results before choosing an algorithm.

**Quality vs Speed Tradeoff:**

| Category                 | Algorithms                            |
|--------------------------|---------------------------------------|
| Fastest / Lowest Cost    | `none`, `ordered`, `sierra-lite`      |
| Best Cost-to-Quality     | `floyd-steinberg`, `burkes`, `sierra` |
| Heavy / Rarely Worth It  | `stucki`, `jarvis-judice-ninke`       |
| Stylized / High Contrast | `atkinson`                            |

## Color Palettes

py-opendisplay automatically selects the best color palette for your display based on its hardware specifications.

### Measured vs Theoretical Palettes

**Measured Palettes** (default): Use actual measured color values from physical e-paper displays for more accurate color reproduction. These palettes are calibrated for specific display models:
- Spectra 7.3" 6-color (ep73_spectra_800x480)
- 4.26" Monochrome (ep426_800x480)
- Solum 2.6" BWR (ep26r_152x296)

**Theoretical Palettes**: Use ideal RGB color values (pure black, white, red, etc.) from the ColorScheme specification.

### Disabling Measured Palettes

If you want to force the use of theoretical ColorScheme palettes instead of measured palettes (useful for testing or comparison):

```python
from opendisplay import OpenDisplayDevice

# Use theoretical ColorScheme palettes instead of measured palettes
async with OpenDisplayDevice(
    mac_address="AA:BB:CC:DD:EE:FF",
    use_measured_palettes=False
) as device:
    await device.upload_image(image)
```

By default, `use_measured_palettes=True` and the library will automatically use measured palettes when available, falling back to theoretical palettes for unknown displays.

### Tone Compression

E-paper displays can't reproduce the full luminance range of digital images. Tone compression remaps image luminance to the display's actual range before dithering, producing smoother results. It is enabled by default (`"auto"`) and only applies when using measured palettes.

```python
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    # Default: auto tone compression (analyzes image, maximizes contrast)
    await device.upload_image(image)

    # Fixed linear compression
    await device.upload_image(image, tone_compression=1.0)

    # Disable tone compression
    await device.upload_image(image, tone_compression=0.0)
```

## Refresh Modes

Control how the display updates when uploading images:

```python
from opendisplay import RefreshMode

await device.upload_image(
    image,
    refresh_mode=RefreshMode.FULL  # Options: FULL, FAST
)
```

### Available Modes

| Mode               | Description                                                                                              |
|--------------------|----------------------------------------------------------------------------------------------------------|
| `RefreshMode.FULL` | Full display refresh \(default\). Cleanest image quality; eliminates ghosting; slower \(~5–15 seconds\). |
| `RefreshMode.FAST` | Fast refresh. Quicker updates; may show slight ghosting. Only supported on some B/W displays.            |

Note: Fast refresh support varies by display hardware. Color and grayscale displays only support full refresh.

## Advanced Features

### Device Interrogation

Query the complete device configuration including hardware specs, sensors, and capabilities:

```python
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    # Automatic interrogation on first connect
    config = device.config
    
    print(f"IC Type: {config.system.ic_type_enum.name}")
    print(f"Displays: {len(config.displays)}")
    print(f"Sensors: {len(config.sensors)}")
    print(f"WiFi config present: {config.wifi_config is not None}")
```

Skip interrogation if the device info is already cached:
```python

# Provide cached config to skip interrogation
device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", config=cached_config)

# Or provide minimal capabilities
capabilities = DeviceCapabilities(296, 128, ColorScheme.BWR, 0)
device = OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", capabilities=capabilities)
```

### Firmware Version

Read the device firmware version including git commit SHA:

```python
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    fw = await device.read_firmware_version()
    print(f"Firmware: {fw['major']}.{fw['minor']}")
    print(f"Git SHA: {fw['sha']}")

    # Example output:
    # Firmware: 0.65
    # Git SHA: e63ae32447a83f3b64f3146999060ca1e906bf15
```

### Writing Configuration

Modify device settings and write them back to the device:

```python
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    # Read current config
    config = device.config

    # Modify settings
    config.displays[0].rotation = 1

    # Write config back to device
    await device.write_config(config)

    # Reboot to apply changes
    await device.reboot()
```

**Note:** Many configuration changes (rotation, pin assignments, IC type) require a device reboot to take effect.
`write_config()` requires `system`, `manufacturer`, `power`, and at least one display.
When present, optional `wifi_config` (packet `0x26`) is preserved on write.

#### JSON Import/Export

Export and import configurations using JSON files compatible with the [Open Display Config Builder](https://opendisplay.org/firmware/config/) web tool:

```python
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    # Export current config to JSON
    device.export_config_json("my_device_config.json")

# Import config from JSON file
config = OpenDisplayDevice.import_config_json("my_device_config.json")

# Write imported config to another device
async with OpenDisplayDevice(mac_address="BB:CC:DD:EE:FF:00") as device:
    await device.write_config(config)
    await device.reboot()
```

`import_config_json()` raises `ValueError` if required packets (`system`, `manufacturer`, `power`) or all display packets are missing.
JSON packet id `38` (`wifi_config` / TLV `0x26`) is supported for import/export.

### Encryption

Devices with firmware encryption enabled require authentication before accepting any commands (except `read_firmware_version`). Pass the 16-byte AES-128 master key to the constructor — authentication and session setup happen automatically before the first interrogation.

```python
from opendisplay import OpenDisplayDevice

key = bytes.fromhex("0102030405060708090a0b0c0d0e0f10")

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", encryption_key=key) as device:
    await device.upload_image(image)
```

All commands are transparently encrypted after authentication. Devices without encryption enabled work exactly as before — the `encryption_key` parameter is ignored if the device does not require it.

#### Getting the key

The encryption key is set when configuring the device via the [Open Display Config Builder](https://opendisplay.org/firmware/config/) web tool. It can be read from the device config once authenticated:

```python
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF", encryption_key=key) as device:
    sc = device.config.security_config
    print(sc.encryption_key.hex())         # 32-char hex string
    print(sc.encryption_enabled_flag)      # True
    print(sc.rewrite_allowed)              # True if WRITE_CONFIG works without auth
    print(sc.session_timeout_seconds)      # How long before re-authentication is needed
```

#### Error handling

```python
from opendisplay import AuthenticationRequiredError, AuthenticationFailedError

try:
    async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
        pass
except AuthenticationRequiredError:
    # Device has encryption enabled but no key was provided
    # (or the session expired and re-authentication is needed)
    print("This device requires an encryption key")
except AuthenticationFailedError:
    # A key was provided but the device rejected it (wrong key or rate-limited)
    print("Wrong encryption key")
```

Both exceptions are subclasses of `AuthenticationError`, which can be used as a catch-all when the distinction doesn't matter.

### Rebooting the Device

Remotely reboot the device (useful after configuration changes or troubleshooting):

```python
async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    await device.reboot()
    # Device will reset after ~100ms
    # BLE connection will drop (this is expected)
```

**Note:** The device performs an immediate system reset and does not send an ACK response. The BLE connection will be terminated when the device resets. Wait a few seconds before attempting to reconnect.

### LED Activation (Firmware 1.0+)

Trigger the firmware LED flash routine (`0x0073`):

```python
from opendisplay import LedFlashConfig, OpenDisplayDevice

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    # Provide a typed flash pattern for this activation
    flash_config = LedFlashConfig.single(
        color=0xE0,            # RGB packed color byte used by firmware
        flash_count=2,         # Pulses per loop (0-15)
        loop_delay_units=2,    # 100ms units (0-15)
        inter_delay_units=5,   # 100ms units (0-255)
        brightness=8,          # 1-16
        group_repeats=1,       # 1-255, or None for infinite
    )
    await device.activate_led(led_instance=0, flash_config=flash_config, timeout=30.0)
```

`activate_led()` waits for the firmware response after the LED routine finishes. If firmware returns an LED-specific error response (`0xFF73`), the method raises `ProtocolError`.
It validates firmware version first and raises on versions below `1.0` where command `0x0073` is not supported.

### Configuration Inspection

Access detailed device configuration:

```python
from opendisplay import BoardManufacturer

async with OpenDisplayDevice(mac_address="AA:BB:CC:DD:EE:FF") as device:
    # Board manufacturer (requires config from interrogation or config=...)
    manufacturer = device.get_board_manufacturer()
    if isinstance(manufacturer, BoardManufacturer):
        print(f"Manufacturer: {manufacturer.name}")
    else:
        print(f"Manufacturer ID (unknown): {manufacturer}")
    mfg = device.config.manufacturer
    print(f"Manufacturer slug: {mfg.manufacturer_name or f'unknown({mfg.manufacturer_id})'}")
    print(f"Board model: {mfg.board_type_name or f'unknown({mfg.board_type})'}")
    print(f"Board revision: {mfg.board_revision}")

    # Display configuration
    display = device.config.displays[0]
    print(f"Panel IC: {display.panel_ic_type}")
    print(f"Rotation: {display.rotation}")
    print(f"Diagonal: {display.screen_diagonal_inches:.1f}\"" if display.screen_diagonal_inches is not None else "Diagonal: unknown")
    print(f"Supports ZIP: {display.supports_zip}")
    print(f"Supports Direct Write: {display.supports_direct_write}")

    # System configuration
    system = device.config.system
    print(f"IC Type: {system.ic_type_enum.name}")
    print(f"Has external power pin: {system.has_pwr_pin}")

    # Power configuration
    power = device.config.power
    print(f"Battery: {power.battery_mah}mAh")
    print(f"Power mode: {power.power_mode_enum.name}")

    # Optional WiFi configuration (firmware packet 0x26)
    wifi = device.config.wifi_config
    if wifi is not None:
        print(f"WiFi SSID: {wifi.ssid_text}")
        print(f"WiFi encryption: {wifi.encryption_type}")
        print(f"WiFi server: {wifi.server_url_text}:{wifi.server_port}")
```
### Advertisement Parsing

Parse real-time sensor data from BLE advertisements:

```python
from opendisplay import parse_advertisement

# Parse manufacturer data from BLE advertisement
adv_data = parse_advertisement(manufacturer_data)
print(f"Battery: {adv_data.battery_mv}mV")
print(f"Temperature: {adv_data.temperature_c}°C")
print(f"Loop counter: {adv_data.loop_counter}")
print(f"Format: {adv_data.format_version}")  # "legacy" or "v1"

if adv_data.format_version == "v1":
    print(f"Reboot flag: {adv_data.reboot_flag}")
    print(f"Connection requested: {adv_data.connection_requested}")
    print(f"Dynamic bytes: {adv_data.dynamic_data.hex()}")
    print(f"Button byte 0 pressed: {adv_data.is_pressed(0)}")
```

`parse_advertisement()` auto-detects both firmware formats without connecting:
- Legacy payload: 11 bytes (`battery_mv`, signed `temperature_c`, `loop_counter`)
- v1 payload: 14 bytes (firmware 1.0+, encoded temperature/battery + status flags)

It also accepts payloads where the manufacturer ID (`0x2446`) is still prefixed.

Track button up/down transitions across packets with `AdvertisementTracker`:

```python
from opendisplay import AdvertisementTracker, parse_advertisement

tracker = AdvertisementTracker()

adv = parse_advertisement(manufacturer_data)
for event in tracker.update(address, adv):
    print(event.event_type, event.button_id, event.pressed, event.press_count)
```

#### Live Listener Script

Use the included script to scan and print parsed advertisement data live,
including v1 button transition events (`button_down`, `button_up`, `press_count_changed`):

```bash
uv run python examples/listen_advertisements.py --duration 60 --all
```

### Device Discovery

List all nearby OpenDisplay devices:
```python
from opendisplay import discover_devices

# Scan for 10 seconds
devices = await discover_devices(timeout=10.0)

for name, mac in devices.items():
    print(f"{name}: {mac}")

# Output:
# OpenDisplayA123: AA:BB:CC:DD:EE:FF
# OpenDisplayB456: 11:22:33:44:55:66
```

## Connection Reliability

py-opendisplay uses `bleak-retry-connector` for robust BLE connections with:
- Automatic retry logic with exponential backoff
- Connection slot management for ESP32 Bluetooth proxies
- GATT service caching for faster reconnections
- Better error categorization

### Home Assistant Integration

When using py-opendisplay in Home Assistant custom integrations, pass the `BLEDevice` object for optimal performance:

```python
from homeassistant.components import bluetooth
from opendisplay import OpenDisplayDevice

# Get BLEDevice from Home Assistant
ble_device = bluetooth.async_ble_device_from_address(hass, mac_address)

async with OpenDisplayDevice(mac_address=mac_address, ble_device=ble_device) as device:
    await device.upload_image(image)
```

### Retry Configuration

Configure retry behavior for unreliable environments:

```python
# Increase retry attempts for poor BLE conditions
async with OpenDisplayDevice(
    mac_address="AA:BB:CC:DD:EE:FF",
    max_attempts=6,  # Try up to 6 times (default: 4)
) as device:
    await device.upload_image(image)

# Disable service caching after firmware updates
async with OpenDisplayDevice(
    mac_address="AA:BB:CC:DD:EE:FF",
    use_services_cache=False,  # Force fresh service discovery
) as device:
    await device.upload_image(image)
```



## Development

```bash
uv sync --all-extras
uv run pytest
```
