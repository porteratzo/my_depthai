# Learning Guide — `my_depthai`

This package handles everything camera-side: initialising Intel OAK-D cameras, grabbing frames, and publishing them to Redis so other processes can consume them independently. If you know OpenCV well, most of this will feel familiar — the main new concept is the DepthAI SDK and how Redis Streams replace shared memory / queues for inter-process frame passing.

---

## Tech stack at a glance

| Library | Role |
|---------|------|
| **DepthAI SDK** (`depthai`) | Low-level interface to OAK cameras; configures pipelines, reads output queues |
| **OpenCV** (`cv2`) | JPEG encoding, frame drawing, image manipulation |
| **NumPy** | Frame data as `np.ndarray` (HWC, BGR) |
| **redis-py** | Publishing frames to Redis Streams (`xadd`) |

---

## Directory layout

```
my_depthai/
├── camera_broker.py              ← launcher only; the code is mydepthai/broker/camera_broker.py
├── diagnostics.py                ← CLI tool to check camera connectivity
├── mydepthai/
│   ├── broker/                   ← camera_broker.py (entry point: publishes frames to Redis), BrokerSettings, web-UI auth helpers, CLI parser
│   ├── web/                      ← FastAPI management UI on :5002 (config, status, controls, revive)
│   ├── configs/defaults/         ← shipped default camera configs
│   ├── camera_manager/
│   │   └── camera_manager.py     ← manages multiple cameras, health monitoring
│   └── fallback_frames.py        ← generates "CAMERA OFFLINE" placeholder images
├── benchmark.py                  ← per-resolution/fps throughput benchmark (subprocess-per-run)
├── benchmark_nv12.py             ← NV12 vs BGR888p format benchmark, reuses benchmark.py's helpers
├── examples/                     ← standalone usage examples
└── tests/
```

---

## File-by-file walkthrough

### [`camera_broker.py`](mydepthai/broker/camera_broker.py#L41) — `CameraBroker`

The entry point for production use. It wraps `CameraManager` (multi-camera lifecycle) and adds the Redis publishing layer.

#### Initialisation

```python
broker = CameraBroker(
    config_path="mydepthai/configs/defaults/camera_manager_config.json",
    redis_host="localhost",
    stream_name="camera_frames",
    fps=10,
    jpeg_quality=85,
)
broker.start_cameras()
broker.run()
```

[`start_cameras()`](mydepthai/broker/camera_broker.py#L304) calls [`CameraManager.initialize_cameras()`](mydepthai/camera_manager/camera_manager.py#L198) and then [`start_monitoring()`](mydepthai/camera_manager/camera_manager.py#L264) (a health-check background thread). [`run()`](mydepthai/broker/camera_broker.py#L443) is the main loop.

#### Main loop ([`run`](mydepthai/broker/camera_broker.py#L443))

```python
while self.running:
    for cam_id, cam_config in self.camera_manager.config.items():
        for output_type in cam_config.get("outputs", ["preview"]):
            frame = self.camera_manager.get_frame(cam_id, cam_type, output_type)

            is_fallback = frame is None
            if is_fallback:
                frame = create_camera_error_frame(cam_id, ...)  # placeholder

            self.publish_frame(cam_id, cam_type, output_type, frame, is_fallback)
```

Key design decision: **even when a camera is offline, a frame is published**. The `is_fallback=True` field in the Redis message tells consumers to skip it for AI processing while still allowing operators to see which cameras are down in a monitoring dashboard.

#### [`publish_frame`](mydepthai/broker/camera_broker.py#L370)

Encodes the numpy frame as JPEG, then writes to Redis:

```python
success, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
frame_bytes = buffer.tobytes()

message = {
    "camera_id": camera_id.encode(),
    "cam_type": cam_type.encode(),
    "output_type": output_type.encode(),
    "timestamp": datetime.utcnow().isoformat().encode(),
    "frame": frame_bytes,          # raw JPEG bytes
    "encoding": b"jpeg",
    "is_fallback": b"1" if is_fallback else b"0",
}
self.redis_client.xadd(self.stream_name, message, maxlen=self.maxlen, approximate=True)
```

`xadd` appends a message to the stream. `maxlen=N, approximate=True` trims the stream to approximately N entries using Redis's efficient background trimming — this prevents unbounded memory growth.

All values in a Redis Stream message are bytes. That's why strings are `.encode()`d and the boolean `is_fallback` becomes `b"0"` or `b"1"`.

#### Redis reconnect logic

The broker reconnects without ever blocking the capture loop ([`_reconnect_redis`](mydepthai/broker/camera_broker.py)). Attempts are rate-limited by the `ReconnectBackoff` from `tep-shared` (`shared.core.reconnect`, also used by `CameraManager`):

```python
def _reconnect_redis(self) -> bool:
    if not self._redis_backoff.ready():      # inside the backoff window: no attempt, no sleep
        return False
    try:
        self.redis_client = self._create_redis_client()
        self.redis_client.ping()
    except redis.RedisError:
        self._redis_backoff.record_failure()  # next attempt in 1, 2, 4, ... s
        return False
    self._redis_backoff.reset()
    return True
```

The delay is capped at `2 ** (redis_max_retries - 1)` seconds (16 s by default) and the broker **never gives up**: capture keeps running, frames are cached for the ROI UI, and `/api/cameras/status` reports `redis_connected: false` until Redis returns. The broker also starts when Redis is down at startup.

---

### [`mydepthai/camera_manager/camera_manager.py`](mydepthai/camera_manager/camera_manager.py#L30) — `CameraManager`

Manages one or more OAK cameras simultaneously. Key responsibilities:
- Load camera config from JSON
- Initialise each camera (optionally in parallel threads with `threaded_init=True`)
- Expose [`get_frame(cam_id, cam_type, output_type)`](mydepthai/camera_manager/camera_manager.py#L441) — returns the latest numpy frame or `None`
- Run a health-check thread that monitors frame freshness and attempts reconnection

The `CameraManager` is the bridge between the DepthAI SDK (camera-specific) and the rest of the system (camera-agnostic numpy arrays). Once frames are numpy arrays, the rest of the pipeline doesn't care that they came from an OAK camera specifically.

---

### [`mydepthai/fallback_frames.py`](mydepthai/fallback_frames.py#L125) — `create_camera_error_frame`

Generates a visually distinct placeholder frame when a camera is offline:

```
┌────────────────────────────────┐  (red border)
│                                │
│       CAMERA OFFLINE           │  (large text)
│        ID: camera_0            │
│   Connection timeout           │
│                    2024-01-15  │  (live timestamp)
└────────────────────────────────┘
```

The interesting engineering here is the **[template cache](mydepthai/fallback_frames.py#L34)**. The static parts (border, title, camera ID, reason text) are [rendered once](mydepthai/fallback_frames.py#L37) and cached by `(cam_id, reason, width, height)`. Only the timestamp is re-drawn per call. At 10 FPS with 4 cameras, this saves ~40 full renders per second when all cameras are offline.

```python
if cache_key not in _template_cache:
    _template_cache[cache_key] = _build_template(cam_id, reason, width, height)

frame = _template_cache[cache_key].copy()   # copy so timestamp draw doesn't corrupt cache
cv2.putText(frame, timestamp, ...)
```

The `.copy()` is critical — without it, you'd be drawing the timestamp onto the cached template, and every subsequent call would show the previous call's timestamp.

---

### [`mydepthai/configs/defaults/camera_manager_config.json`](mydepthai/configs/defaults/camera_manager_config.json)

Example structure:

```json
{
  "camera_0": {
    "id": 0,
    "camera_type": "color",
    "outputs": ["preview", "video"],
    "sizes": [[640, 480], [1280, 720]],
    "fpss": [10, 5]
  },
  "camera_1": {
    "id": 1,
    "camera_type": "color",
    "outputs": ["video"],
    "sizes": [[1920, 1080]],
    "fpss": [5]
  }
}
```

Each key is the logical camera name (used as `camera_id` in Redis messages). `id` is the physical device index (0 = first USB device). `outputs` lists which pipelines to configure — `"preview"` is lower-resolution, `"video"` is higher.

The camera broker iterates all configured cameras and outputs on each tick, publishing one Redis message per (camera, output) pair.

---

### [`benchmark.py`](benchmark.py) / [`benchmark_nv12.py`](benchmark_nv12.py)

Standalone throughput/quality benchmarks — not imported by production code. Each
resolution/fps/format configuration runs in its **own subprocess**, opening exactly one fresh
`dai.Device` connection, measuring for a fixed duration, then exiting: a second XLink connection
within one process reliably fails, and reusing one `dai.Device` across multiple `dai.Pipeline`
objects segfaults, so `benchmark.py` sidesteps both by never doing either.

`benchmark_nv12.py` compares two ways of requesting the same resolution — **NV12**
(4:2:0 chroma-subsampled, ~1.5 bytes/px on the wire; what `CameraModule.build()` already requests
for the `"preview"` output) against **BGR888p** (full-resolution color, 3 bytes/px; requested for
`"video"`) — and measures two things beyond raw fps: the `cv2.cvtColor` YUV→BGR reconversion cost
`frame.getCvFrame()` pays for NV12 vs. the cheaper planar→interleaved reshape BGR888p pays, and
image quality (NV12's subsampled chroma is visibly coarser at the same pixel size — run `benchmark_nv12.py` for
a saved-PNG comparison). Useful context before changing `outputs`/`sizes` in
`camera_manager_config.json`: NV12 buys wire/decode throughput at a real, measurable color-detail
cost.

---

### [`diagnostics.py`](diagnostics.py)

A standalone diagnostic tool — run `python diagnostics.py` to check:
- Which OAK cameras are detected
- USB speed (USB2 vs USB3 vs PoE)
- Whether the DepthAI pipeline initialises successfully
- Whether frames can be captured

Useful when setting up hardware or debugging connection issues. It doesn't touch Redis or any other service.

---

## Key concepts

### DepthAI pipeline model

OAK cameras are programmed with a **pipeline** — a graph of nodes (ColorCamera, MonoCamera, StereoDepth, NeuralNetwork, etc.) connected by links. The camera executes this pipeline on-device and streams results back over USB or PoE.

You build the pipeline in Python, push it to the device, and then read output queues. The `CameraManager` handles this setup internally — from the broker's perspective, you just call `get_frame()` and get a numpy array.

### Redis Streams vs a shared queue

If both the camera broker and the detector ran in the same Python process, you'd use `queue.Queue`. Because they're separate processes (and could be on separate machines), you use Redis Streams instead:

- **Publisher** ([`camera_broker.py`](mydepthai/broker/camera_broker.py#L370)): `redis.xadd(stream, message)`
- **Consumer** (`shared.camera_service` in `tep-shared`): `redis.xrevrange(stream, count=20)` — newest frames first, no consumer group (a consumer that needs to scale out can use a real consumer group instead)

The stream is durable — messages persist until trimmed by `maxlen`. Multiple consumers can read the same stream independently (unlike a queue where each message is consumed once). A consumer that only wants the latest frame takes the newest matching one, so it never works through a backlog.

**Fallback semantics.** While a camera is offline the broker keeps publishing a "CAMERA OFFLINE" placeholder with `is_fallback=1` so the live view shows which camera is down. The wire format lives in one place, `shared.core.camera_stream` (in `tep-shared`) (`encode_frame_message` / `decode_frame_message` / `is_inspectable_frame`); the broker encodes with it and every consumer decodes with it, so a consumer cannot forget to skip placeholders. Only the live `/video_feed` asks for them (`include_fallback=True`).

**Camera recovery.** Cameras reconnect forever with a capped per-camera backoff. `POST /api/cameras/{id}/revive` (the "Revive" button on the status page) skips the wait and reconnects immediately; `state` on `/api/cameras/status` is `online` / `stale` / `connecting` / `offline`.

### BGR vs RGB

OpenCV uses BGR channel order; most everything else (PIL, PyTorch, human expectation) uses RGB. The camera broker doesn't convert — it publishes BGR frames encoded as JPEG (JPEG encoding handles the channel order correctly). The AI service decodes via PIL (which loads JPEG as RGB) and converts to a tensor.

If you ever see inverted colours in a frame, this channel-order mismatch is the first thing to check.

### JPEG quality trade-off

`jpeg_quality=85` is the default. Lower quality = smaller Redis messages = lower latency and memory. Higher quality = better anomaly detection (less compression artifacting). For most manufacturing inspection use cases, 85 is a reasonable default — anomaly detectors trained on 85-quality images will also receive 85-quality images at inference time, so the compression is consistent.
