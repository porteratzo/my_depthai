# my_depthai (`mydepthai`)

Python library for Intel OAK-D camera control with multi-camera management and Redis Streams integration. This package provides simplified interfaces for OAK cameras with support for video capture, health monitoring, and distributed frame publishing.

## Overview

The `my_depthai` module is the camera interface layer for the distributed manufacturing inspection system. It manages OAK-D cameras, captures JPEG frames, and publishes them to Redis Streams for consumption by downstream services.

## Features

- **Multi-Camera Management**: Orchestrate multiple OAK cameras with centralized configuration
- **Redis Streams Integration**: Publish frames to Redis for distributed consumption
- **Health Monitoring**: Auto-reconnect on camera failures with configurable intervals
- **Modular Architecture**: Reusable camera, depth, detection, and recorder modules
- **USB Speed Detection**: Automatic USB2/USB3 mode selection
- **Flexible Frame Capture**: Support for preview, video, still, and full-resolution modes
- **Thread-Safe**: Concurrent camera initialization and frame retrieval
- **Camera Diagnostics**: Built-in diagnostic tool for troubleshooting

## Project Structure

```
my_depthai/
├── camera_broker.py              # Launcher for dev scripts; installed users run `mydepthai-broker`
├── diagnostics.py                # Camera diagnostic tool
├── mydepthai/                    # Main package
│   ├── broker/
│   │   └── camera_broker.py      # Main entry point: Redis frame publisher (`mydepthai-broker`)
│   ├── web/                      # FastAPI management UI (templates ship in the wheel)
│   ├── camera_manager/
│   │   └── camera_manager.py     # Multi-camera orchestrator
│   └── depthai_controllers/
│       ├── basic_dai_camera.py   # Core camera controller
│       └── modules/              # Pluggable pipeline modules
│           ├── base_module.py    # Abstract base for modules
│           ├── camera_module.py  # Camera implementation
│           ├── camera_frame_module.py      # Frame retrieval
│           └── camera_config_module.py     # Camera control (exposure, focus, etc.)
│   └── configs/defaults/         # Shipped in the wheel (camera_manager_config.json, sampleconfig_camera.json)
├── examples/                     # Usage examples
│   ├── basic_example.py
│   ├── camera_manager_example.py
│   ├── multiple_cameras.py
│   └── exitstack_example.py
├── tests/                        # Test suite
├── setup.py                      # Package setup
├── requirements.txt              # Runtime dependencies
└── Makefile                      # Development automation
```

## Installation

### Prerequisites

**Linux udev rules** (required for non-root access):
```bash
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="03e7", MODE="0666"' | sudo tee /etc/udev/rules.d/80-movidius.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

**Redis** (required for camera_broker):
```bash
sudo apt install redis-server
pip install redis
```

### From Source

```bash
git clone https://github.com/porteratzo/my_depthai.git
cd my_depthai
pip install -e .
```

`mydepthai` depends on [`tep-shared`](https://github.com/porteratzo/tep-shared), which is not on PyPI; the
dependency is pinned to a release tag of that repository, so `pip` fetches it from GitHub automatically.

### Development Installation

```bash
pip install -e ".[dev]"
make setup-precommit    # Optional: setup pre-commit hooks
```

## Quick Start

### Camera Broker (Production)

The camera broker publishes frames from multiple cameras to Redis Streams:

```bash
# Start camera broker with default config
mydepthai-broker --config-path mydepthai/configs/defaults/camera_manager_config.json

# With custom Redis settings
mydepthai-broker \
  --config-path mydepthai/configs/defaults/camera_manager_config.json \
  --redis-host localhost \
  --redis-port 6379 \
  --fps 5 \
  --jpeg-quality 85 \
  --max-stream-length 100
```

**Command-Line Options:**
- `--config-path`: Path to camera manager JSON config (required)
- `--redis-host`: Redis server host (default: localhost)
- `--redis-port`: Redis server port (default: 6379)
- `--fps`: Frame publishing rate (default: 3 FPS)
- `--jpeg-quality`: JPEG compression quality 0-100 (default: 85)
- `--max-stream-length`: Max messages in Redis stream (default: 100)
- `--threaded-init`: Enable parallel camera initialization

### Camera Diagnostics

Diagnose OAK camera connectivity and configuration:

```bash
# Find all connected OAK devices
python diagnostics.py --find

# Show detailed device information
python diagnostics.py --info

# Test connection to specific device
python diagnostics.py --test <MXID>
```

### Basic Camera Usage

```python
from mydepthai.depthai_controllers.basic_dai_camera import BasicDAICamera

# Initialize camera (0 = first device, speed=True for USB3)
cam = BasicDAICamera(0, speed=True)

# Create color camera with preview output
cam.create_camera("color", outputs=["preview"])
cam.build()
cam.start_device()

# Capture frames
if cam.components["color"].has_data("preview"):
    frame = cam.get_frame("color", "preview")  # Returns numpy array
    # Process frame...

cam.close()
```

### Multi-Camera Management

```python
from mydepthai.camera_manager.camera_manager import CameraManager

# Load from JSON config
manager = CameraManager("mydepthai/configs/defaults/camera_manager_config.json")
manager.initialize_cameras(threaded=True)  # Parallel initialization
manager.start_monitoring()  # Enable health checks

# Get frames from all cameras
frames = manager.get_frames_all()
# Returns: {camera_id: {cam_type: {output_type: frame_array}}}

# Access specific camera frame
if "camera_1" in frames and "color" in frames["camera_1"]:
    color_frame = frames["camera_1"]["color"]["preview"]

# Cleanup
manager.close_all()
```

### Camera Configuration (JSON)

Example `camera_manager_config.json`:

```json
{
  "camera_1": {
    "id": "18443010110DF60800",
    "camera_type": "color",
    "outputs": ["video"],
    "sizes": [[1920, 1080]],
    "fpss": [3],
    "settings": {
      "exposure": 10000,
      "focus": 125,
      "iso": 400
    }
  },
  "camera_2": {
    "id": 0,
    "camera_type": "color",
    "outputs": ["preview", "still"],
    "sizes": [[640, 480], [1920, 1080]],
    "fpss": [30, 1]
  }
}
```

**Configuration Fields:**
- `id`: MXID string (e.g., "18443010110DF60800") or device index (0, 1, 2...)
- `camera_type`: "color", "left", or "right"
- `outputs`: Array of ["preview", "video", "still", "fullvideo"]
- `sizes`: [width, height] for each output (must match outputs length)
- `fpss`: Frame rate for each output (must match outputs length)
- `settings` (optional):
  - `exposure`: Exposure time in microseconds (1-33333)
  - `focus`: Lens position (1-254 or distance in meters)
  - `iso`: ISO sensitivity (100-1600)

## Key Classes

### CameraManager

Orchestrates multiple `BasicDAICamera` instances with health monitoring.

**Key Methods:**
- `__init__(config)`: Initialize with JSON file path or dictionary
- `initialize_cameras(threaded=False)`: Setup cameras (parallel if threaded=True)
- `start_monitoring()`: Begin health monitoring thread
- `get_frames_all()`: Retrieve frames from all cameras
- `close_all()`: Graceful shutdown

**Features:**
- Health checks with configurable intervals (default: 5.0s)
- Data timeout detection (default: 10.0s)
- Automatic reconnection with max attempts
- Thread-safe camera operations
- ExitStack context manager support

### BasicDAICamera

Direct DepthAI API wrapper for single camera control.

**Key Methods:**
- `create_camera(cam_type, outputs, sizes, fpss)`: Add camera component
- `build()`: Build DepthAI pipeline
- `start_device()`: Start pipeline and components
- `get_frame(cam, out_type)`: Retrieve frame (blocking)
- `get_still()`: Capture high-resolution still image
- `trigger_still()`: Trigger still capture
- `wait_for_cam(timeout, reconnect_on_timeout)`: Wait for connection
- `reconnect()`: Attempt reconnection

**Features:**
- Supports color, left, right camera types
- Thread-based asynchronous device connection
- Still image capture with fallback mechanisms
- USB speed options (HIGH for USB2, SUPER for USB3)
- Device reconnection with exponential backoff

### CameraBroker

Publishes frames from CameraManager to Redis Streams.

**Features:**
- Configurable frame rate and JPEG quality
- Health monitoring and auto-reconnect
- Stream length management (MAXLEN)
- Threaded camera initialization
- Graceful shutdown on SIGINT/SIGTERM

**Redis Message Format:**
```python
{
    'camera_id': 'camera_1',
    'timestamp': '2026-01-22T12:34:56.789012',
    'camera_type': 'color',
    'output_type': 'video',
    'width': 1920,
    'height': 1080,
    'format': 'jpeg',
    'data': b'<JPEG bytes>'
}
```

## Camera Control Features

The `camera_config_module` provides 30+ camera control methods:

**Exposure Control:**
- `set_exposure(expTime_us, sensIso)`: Manual exposure
- `set_Exposure_Region(x, y, w, h)`: Auto-exposure region
- `set_antibanding(option)`: Anti-banding mode

**Focus Control:**
- `set_focus(lens_step, distance)`: Manual focus (1-254 or meters)
- `set_Focus_Region(x, y, w, h)`: Auto-focus region
- `trigger_auto_focus()`: Single-shot auto focus
- `trigger_auto_focus_macro()`: Macro mode focus
- `continuous_auto_focus()`: Continuous video focus

**Focus Modes:**
- `AUTO`, `MACRO`, `EDOF`, `CONTINUOUS_VIDEO`, `CONTINUOUS_PICTURE`

**Still Capture:**
- `trigger_still()`: Trigger high-resolution still image

## Examples

The `examples/` directory contains usage examples:

| Example | Description |
|---------|-------------|
| `basic_example.py` | Basic camera capture and display |
| `camera_manager_example.py` | Multi-camera management demo |
| `multiple_cameras.py` | Working with multiple cameras |
| `exitstack_example.py` | Context manager usage |

Run any example:
```bash
python examples/basic_example.py
```

## Testing

### Run Tests

```bash
# Via pytest
pytest tests/

# Via make
make test

# With coverage
pytest tests/ --cov=mydepthai --cov-report=html

# Via shell script
bash run_tests.sh
```

### Test Files

- `test_camera_manager.py`: CameraManager initialization, configuration, error handling

**Note:** Tests use mock configurations and do not require actual hardware.

## Development

### Code Formatting

```bash
make format
# Runs: black + isort with line length 100
```

### Linting

```bash
make lint
# Runs: flake8 with custom config
```

### Type Checking

```bash
make type-check
# Runs: mypy
```

### All Checks

```bash
make check
# Runs: format check + lint
```

### Available Make Commands

```bash
make help            # Show all commands
make install         # Install package dependencies
make install-dev     # Install package + dev dependencies
make format          # Format code with black and isort
make lint            # Run flake8 linting
make check           # Run all checks (format + lint)
make type-check      # Run mypy type checking
make test            # Run pytest
make setup-precommit # Install pre-commit hooks
make clean           # Remove build artifacts
```

## Configuration

**pyproject.toml** - Tool configurations:
- **Black**: Line length 100, Python 3.7+
- **isort**: Black-compatible profile
- **Pylint**: Disabled rules for code style flexibility (C0103, C0114-C0116, R0902/R0913/R0914, W0212)
- **mypy**: Basic type checking without strict enforcement

**.config/flake8** - Linting rules

### Web UI nav links

The broker's nav bar can link to other tools. Configure them under `services.camera_broker.nav_links`
(a list of `label` + `url`; only `http://` and `https://` URLs are accepted). With no entry, no links are shown:

```yaml
services:
  camera_broker:
    nav_links:
      - label: Dashboard
        url: http://localhost:5000
```

### Web UI security (port 5002)

The broker's management UI binds to `127.0.0.1` by default (`services.camera_broker.host` / `CAMERA_BROKER_HOST`);
a non-loopback value is an explicit choice and logs a WARNING. Its state-changing routes
(`POST /api/camera_config`, `/api/cameras/reload`, `/api/cameras/{id}/control`) require the token from
`services.camera_broker.token` / `CAMERA_BROKER_TOKEN` in an `X-Broker-Token` header; if unset, a random token is
generated at startup. The pages the broker serves embed the token for their own JavaScript, so anyone who can load
a page gets it: this blocks non-local and cross-origin callers, not local users. Remote access is best done with an
SSH tunnel. Scripts calling the POST routes must send the header:

```bash
curl -X POST -H "X-Broker-Token: $CAMERA_BROKER_TOKEN" http://127.0.0.1:5002/api/cameras/reload
```

## Dependencies

### Runtime Dependencies

```
opencv-python     # Computer vision operations
depthai          # DepthAI/OAK SDK (core dependency)
numpy            # Numerical computing
porter-bench[csv] # Benchmarking utilities
redis            # Redis Streams client
```

### Development Dependencies

```
black            # Code formatting
isort            # Import sorting
flake8           # Linting
mypy             # Type checking
pytest           # Testing framework
pytest-cov       # Coverage reporting
pytest-mock      # Mocking utilities
pre-commit       # Git hooks
```

## System Integration

my_depthai is a standalone library; a typical deployment looks like this:

1. **Frame Source**: camera_broker publishes frames to Redis stream `camera_frames`
2. **Consumers**: any service that reads the stream (e.g. an inspection or recording service)
3. **Health Monitoring**: Tracks camera status and auto-reconnects
4. **Configuration**: Centralized JSON-based camera configuration

**Frame message contract.** The `camera_frames` entries the broker publishes are defined, and versioned
(`WIRE_FORMAT_VERSION`), in `shared.core.camera_stream` (the `tep-shared` package): the field table, the
rules for compatible changes (a new field is compatible; removing, renaming or retyping one is not and
bumps the version), and a changelog. A project that reads or reproduces these messages should depend on
that module rather than hand-decode the fields.

**Data Flow:**
```
OAK-D Cameras → CameraManager → CameraBroker → Redis Streams → Downstream Services
```

## Troubleshooting

### Camera Not Detected

```bash
# Check USB connection and permissions
python diagnostics.py --find

# Verify udev rules
ls -la /etc/udev/rules.d/80-movidius.rules
```

### USB Speed Issues

- **USB2 Mode**: Limited to ~30 FPS, suitable for low-resolution
- **USB3 Mode**: Required for high FPS and multiple outputs
- Use `speed=True` in BasicDAICamera for USB3 mode

### Camera Connection Timeout

- Check USB cable quality (USB3 requires high-quality cables)
- Verify USB port is USB3 (blue connector)
- Increase timeout in `wait_for_cam(timeout=...)`

### Health Monitoring Failures

- Adjust `health_check_interval` and `data_timeout` in CameraManager
- Check camera power supply (some hubs provide insufficient power)
- Review logs for reconnection attempts

## License

MIT License

## Author

porteratzo ([github.com/porteratzo](https://github.com/porteratzo))

## Repository

https://github.com/porteratzo/my_depthai

## Version

0.1.1
