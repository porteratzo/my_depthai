# Camera Manager

A comprehensive manager for multiple DepthAI camera instances with health monitoring and configuration management.

## Features

- **Multi-Camera Support**: Manage multiple BasicDAICamera instances simultaneously
- **Configuration File Support**: Load camera settings from JSON files
- **Health Monitoring**: Automatic monitoring of camera connections and data flow
- **Logging**: Comprehensive logging of camera events and issues
- **Context Manager**: Clean resource management with Python's `with` statement
- **Flexible Initialization**: Support for both MXID strings and device indices

## Configuration Format

The camera manager uses JSON configuration files with the following structure:

```json
{
    "camera_id": {
        "id": "14442C10D13597D000",  // MXID string or numeric index
        "camera_type": "color",       // "color", "left", or "right"
        "outputs": ["preview", "video"],
        "sizes": [[300, 300], [1280, 720]],
        "fpss": [20, 20],
        "settings": {
            "iso": 100,
            "exposure": 20000,
            "focus": 125
        }
    }
}
```

### Configuration Fields

- **id**: Camera identifier - can be:
  - Device MXID string (e.g., `"14442C10D13597D000"`)
  - Numeric index (e.g., `0` for first device)

- **camera_type**: Type of camera sensor to use
  - `"color"`: RGB camera
  - `"left"`: Left mono camera
  - `"right"`: Right mono camera

- **outputs**: Array of output streams to create
  - `"preview"`: Fast preview stream (default: 300x300 @ 20fps)
  - `"video"`: Video stream (default: 1280x720 @ 20fps)
  - `"still"`: Still image capture

- **sizes**: Array of resolution tuples `[width, height]` for each output
  - Use `null` for default resolution
  - Must match length of `outputs` array

- **fpss**: Array of frame rates for each output
  - Use `null` for default fps
  - Must match length of `outputs` array

- **settings**: Optional camera settings
  - `iso`: ISO sensitivity (e.g., 100, 200, 400)
  - `exposure`: Exposure time in microseconds
  - `focus`: Manual focus distance (0-255)

## Usage Examples

### Basic Usage with Config File

```python
from mydepthai.camera_manager.camera_manager import CameraManager

# Initialize with config file
manager = CameraManager(
    config_path="configs/defaults/camera_manager_config.json",
    health_check_interval=5.0,
    data_timeout=10.0
)

# Initialize all cameras
manager.initialize_cameras()

# Start health monitoring
manager.start_monitoring()

# Get frames
frame = manager.get_frame("camera_1", cam_type='color', output_type='preview')

# Close when done
manager.close_all()
```

### Using Context Manager

```python
with CameraManager(config_path="config.json") as manager:
    manager.initialize_cameras()
    manager.start_monitoring()

    # Use cameras...
    frames = manager.get_all_frames()

# Automatically closes all cameras - no manual cleanup needed!
```

**Note**: Each camera closes its own pipeline and device; `close_all()` (or the context manager) closes every camera.

### Using Configuration Dictionary

```python
config = {
    "main_cam": {
        "id": 0,
        "camera_type": "color",
        "outputs": ["preview", "video"],
        "sizes": [[640, 480], [1920, 1080]],
        "fpss": [30, 30]
    }
}

manager = CameraManager(config_dict=config)
manager.initialize_cameras()
```

### Accessing Individual Cameras

```python
# Get a specific camera instance
camera = manager.get_camera("camera_1")

# Use BasicDAICamera methods directly
still = camera.trigger_and_get_still()
```

## Health Monitoring

The camera manager includes automatic health monitoring:

- **Connection Status**: Monitors if cameras remain connected
- **Data Flow**: Warns when cameras haven't produced new data within timeout
- **Logging**: All events are logged with appropriate severity levels

### Monitoring Configuration

```python
manager = CameraManager(
    config_path="config.json",
    health_check_interval=5.0,  # Check every 5 seconds
    data_timeout=10.0            # Warn if no data for 10 seconds
)

# Start monitoring thread
manager.start_monitoring()

# Stop monitoring
manager.stop_monitoring()
```

### Log Messages

The manager provides detailed logging:

```
INFO - Initializing camera 'camera_1' with identifier: 0
INFO - Camera 'camera_1' connected successfully
INFO - Camera 'camera_1' initialized and started successfully
WARNING - Camera 'camera_2' has not produced new data for 12.3 seconds
ERROR - Camera 'camera_3' failed to connect within timeout
```

## API Reference

### CameraManager Class

#### Constructor

```python
CameraManager(
    config_path: Optional[Union[str, Path]] = None,
    config_dict: Optional[Dict[str, Any]] = None,
    health_check_interval: float = 5.0,
    data_timeout: float = 10.0
)
```

#### Methods

- `initialize_cameras(use_superspeed: bool = False)`: Initialize all configured cameras
- `start_monitoring()`: Start health monitoring thread
- `stop_monitoring()`: Stop health monitoring thread
- `get_frame(cam_id, cam_type='color', output_type='preview')`: Get frame from specific camera
- `get_all_frames(cam_type='color', output_type='preview')`: Get frames from all cameras
- `get_camera(cam_id)`: Get BasicDAICamera instance by ID
- `close_all()`: Close all cameras and stop monitoring

#### Properties

- `cameras`: Dictionary of active camera instances
- `config`: Loaded configuration dictionary
- `__len__()`: Returns number of active cameras
- `__contains__(cam_id)`: Check if camera ID exists

## Examples

See `examples/camera_manager_example.py` for complete examples:

1. **Configuration File Example**: Using JSON config with multiple cameras
2. **Configuration Dictionary Example**: Using Python dict configuration
3. **Single Camera Example**: Minimal setup for one camera
4. **Still Capture Example**: Capturing high-resolution still images

Run examples:
```bash
python examples/camera_manager_example.py 1  # Config file example
python examples/camera_manager_example.py 2  # Config dict example
python examples/camera_manager_example.py 3  # Single camera
python examples/camera_manager_example.py 4  # Still capture
```

## Logging Configuration

Configure logging level for detailed output:

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
```

Use `DEBUG` level for even more detailed information during development.

## Error Handling

The camera manager handles various error conditions:

- Missing or invalid configuration files
- Failed camera connections
- Cameras becoming disconnected during operation
- Missing camera data streams

All errors are logged appropriately and don't crash the manager - other cameras continue operating.

## Resource Ownership

Each `BasicDAICamera` owns its DepthAI pipeline and device. `CameraManager` does **not** share an
`ExitStack` between cameras: a stack shared across reconnects only grows and re-exits dead
pipelines. Instead:

1. `BasicDAICamera.close()` exits the pipeline and then closes the device, each at most once, so
   calling it again (or after a reconnect) is a no-op.
2. A reconnect force-closes the old camera before building a new one, so no device handle is left open.
3. `close_all()` (or leaving the manager's `with` block) closes every camera.

### Standalone Camera Usage

You can still use `BasicDAICamera` without ExitStack:

```python
# Without ExitStack (manual management)
camera = BasicDAICamera(identifier=0)
camera.wait_for_cam()
camera.create_camera(outputs=['preview'])
camera.build()
camera.start_device()
# ... use camera ...
camera.close()  # Must manually close

# With ExitStack (automatic management)
from contextlib import ExitStack

with ExitStack() as stack:
    camera = BasicDAICamera(identifier=0, exit_stack=stack)
    camera.wait_for_cam()
    camera.create_camera(outputs=['preview'])
    camera.build()
    camera.start_device()
    # ... use camera ...
    # Automatically closed when exiting the with block
```

## Best Practices

1. **Use Context Manager**: Ensures proper cleanup
2. **Enable Health Monitoring**: Catches issues early
3. **Adjust Timeouts**: Based on your application needs
4. **Handle Missing Frames**: Always check if `get_frame()` returns `None`
5. **Use Appropriate Output Types**: `preview` for monitoring, `video` for processing
6. **Log Configuration**: Set appropriate logging levels for production vs development

## Troubleshooting

**Camera not connecting:**
- Check USB permissions (see BasicDAICamera docs)
- Verify MXID is correct (use `dai.Device.getAllAvailableDevices()`)
- Try increasing connection timeout

**No data warnings:**
- Check if camera is physically connected
- Verify USB cable and port
- Check for USB bandwidth issues with multiple cameras

**Performance issues:**
- Reduce frame rates in configuration
- Lower resolution for preview streams
- Use USB SuperSpeed if supported
- Limit number of simultaneous cameras
