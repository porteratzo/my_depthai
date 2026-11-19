"""
Redis-based camera frame publisher for OAK cameras.

Wraps CameraManager to publish frames from multiple cameras to Redis Streams for
 consumption by downstream services.
"""

# ── Standard library ─────────────────────────────────────────────────────────
import asyncio
import copy
import dataclasses
import json
import logging
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from shared.core.camera_stream import encode_frame_message
from shared.core.config_loader import ConfigLoader
from shared.core.log_setup import setup_logging
from shared.core.reconnect import ReconnectBackoff

logger = logging.getLogger("my_depthai.camera_broker")

# ── Third-party ──────────────────────────────────────────────────────────────
import cv2  # noqa: E402
import numpy as np  # noqa: E402
from redis.typing import EncodableT, FieldT  # noqa: E402

try:
    import redis  # noqa: E402
except ImportError:
    logger.error("redis package not installed. Run: pip install redis")
    sys.exit(1)

from mydepthai.broker.auth import log_bind_and_token, resolve_web_token  # noqa: E402

# ── First-party ──────────────────────────────────────────────────────────────
from mydepthai.broker.cli import build_parser  # noqa: E402
from mydepthai.broker.settings import BrokerSettings  # noqa: E402
from mydepthai.camera_manager.camera_manager import CameraManager  # noqa: E402
from mydepthai.camera_manager.config_utils import validate_camera_config, write_config  # noqa: E402
from mydepthai.fallback_frames import create_camera_error_frame  # noqa: E402

# Per-output fps used by CameraModule.build() when the config gives none.
_DEFAULT_OUTPUT_FPS = {"fullvideo": 2}


class CameraBroker:
    """
    Multi-camera frame publisher to Redis Streams.

    Manages multiple OAK cameras via CameraManager and publishes frames to Redis for distributed consumption.
    Supports health monitoring, auto-reconnect, and threaded initialization.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        config_dict: Optional[Dict[str, Any]] = None,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_db: int = 0,
        redis_password: Optional[str] = None,
        stream_name: str = "camera_frames",
        maxlen: int = 100,
        fps: int = 5,
        jpeg_quality: int = 85,
        health_check_interval: float = 5.0,
        data_timeout: float = 10.0,
        auto_reconnect: bool = True,
        threaded_init: bool = True,
        redis_max_retries: int = 5,
        redis_socket_connect_timeout: float = 5.0,
        redis_socket_timeout: float = 5.0,
    ):
        """
        Initialize camera broker with CameraManager.

        Args:
            config_path: Path to camera configuration JSON file
            config_dict: Camera configuration dictionary (used if config_path is None)
            redis_host: Redis server host
            redis_port: Redis server port
            redis_db: Redis database number
            redis_password: Redis password (if required)
            stream_name: Redis stream name
            maxlen: Maximum stream length (older entries trimmed)
            fps: Target frame capture rate
            jpeg_quality: JPEG compression quality (1-100)
            health_check_interval: Time in seconds between health checks (default: 5.0)
            data_timeout: Maximum time in seconds without new data before warning (default: 10.0)
            auto_reconnect: Whether to automatically reconnect inactive cameras (default: True)
            threaded_init: Whether to initialize cameras in parallel (default: True)
            redis_max_retries: Caps reconnect backoff at 2**(n-1) s (default 5 → 16 s); never gives up
            redis_socket_connect_timeout: Redis connect timeout in seconds (default: 5.0)
            redis_socket_timeout: Redis socket read/write timeout in seconds (default: 5.0)
        """
        self.stream_name = stream_name
        self.maxlen = maxlen
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self.frame_interval = 1.0 / fps

        # Redis connection parameters stored for reconnect
        self._redis_host = redis_host
        self._redis_port = redis_port
        self._redis_db = redis_db
        self._redis_password = redis_password
        self._redis_max_retries = redis_max_retries
        self._redis_socket_connect_timeout = redis_socket_connect_timeout
        self._redis_socket_timeout = redis_socket_timeout
        self._redis_connected = False
        # 1, 2, 4, ... s between attempts, capped at 2 ** (max_retries - 1) s; never gives up.
        # The clock is looked up per call so tests can patch ``time.monotonic``.
        self._redis_backoff = ReconnectBackoff(
            initial=1.0,
            maximum=float(2 ** max(redis_max_retries - 1, 0)),
            clock=lambda: time.monotonic(),
        )

        # Initialize Redis connection
        self.redis_client = self._create_redis_client()

        # Test Redis connection
        try:
            self.redis_client.ping()
            self._redis_connected = True
            logger.info("Connected to Redis at %s:%d", redis_host, redis_port)
            logger.info(
                "Redis Stream: %s  Max Length: %d  FPS: %d  JPEG Quality: %d",
                stream_name,
                maxlen,
                fps,
                jpeg_quality,
            )
        except redis.RedisError as e:
            # Start anyway (decision MD-T1): cameras open and frames are cached, and publishing
            # retries on the same 1, 2, 4, ... s backoff as a mid-run outage. get_status()
            # reports redis_connected=False meanwhile.
            self._redis_connected = False
            self._redis_failures = 1
            self._redis_backoff.record_failure()
            logger.error(
                "Redis unavailable at %s:%d — %s; starting anyway and retrying with backoff",
                redis_host,
                redis_port,
                e,
            )

        # Initialize CameraManager
        self.camera_manager: Optional[CameraManager] = None
        self.config_path = config_path
        self.config_dict = config_dict
        self.health_check_interval = health_check_interval
        self.data_timeout = data_timeout
        self.auto_reconnect = auto_reconnect
        self.threaded_init = threaded_init
        self.running = False
        self._use_superspeed = True
        self._reload_lock = threading.Lock()
        self._last_jpeg: Dict[Tuple[str, str], bytes] = {}
        self._last_real_frame: Dict[Tuple[str, str], float] = {}

    def _create_redis_client(self) -> "redis.Redis":
        """Create a new Redis client using stored connection parameters."""
        return redis.Redis(
            host=self._redis_host,
            port=self._redis_port,
            db=self._redis_db,
            password=self._redis_password,
            decode_responses=False,
            socket_connect_timeout=self._redis_socket_connect_timeout,
            socket_timeout=self._redis_socket_timeout,
        )

    _redis_failures: int = 0

    def _xadd_maxlen(self) -> Optional[int]:
        """``maxlen`` for ``XADD``: ``None`` (no trimming) when configured as 0 = unlimited.

        Passing 0 straight through would make Redis trim the stream to zero entries on every add.
        """
        return self.maxlen if self.maxlen and self.maxlen > 0 else None

    def _reconnect_redis(self) -> bool:
        """
        Make at most one non-blocking Redis reconnect attempt, on a backoff schedule.

        Never sleeps (the capture loop must not stall for 31 s per failed publish).
        Returns False immediately while the backoff window is open. After each failed
        attempt the next one is scheduled 1, 2, 4, ... seconds out, capped at
        ``2 ** (redis_max_retries - 1)`` s (16 s by default). It never gives up: the broker
        keeps retrying at the capped interval until Redis returns.

        Returns:
            True if reconnected on this call, False otherwise.
        """
        if not self._redis_backoff.ready():
            return False
        try:
            self.redis_client = self._create_redis_client()
            self.redis_client.ping()
        except redis.RedisError as e:
            self._redis_failures += 1
            self._redis_backoff.record_failure()
            self._redis_connected = False
            logger.warning(
                "Redis reconnect failed (%d consecutive): %s — backing off before the next attempt",
                self._redis_failures,
                e,
            )
            return False
        self._redis_failures = 0
        self._redis_backoff.reset()
        self._redis_connected = True
        logger.info("Redis reconnected successfully")
        return True

    def get_status(self) -> List[Dict[str, Any]]:
        """Return per-camera health info for the web UI."""
        if self.camera_manager is None:
            return []
        now = time.time()
        result = []
        with self.camera_manager._cameras_lock:
            for cam_id, cam_cfg in self.camera_manager.config.items():
                last_t = self.camera_manager._last_frame_time.get(cam_id)
                state = self.camera_manager.get_camera_state(cam_id)
                result.append(
                    {
                        "name": cam_id,
                        "id": cam_cfg.get("id"),
                        "camera_type": cam_cfg.get("camera_type", "color"),
                        "outputs": cam_cfg.get("outputs") or [],
                        "fps": (cam_cfg.get("fpss") or [None])[0],
                        "online": state == "online",
                        # False = frames are captured but NOT reaching Redis (consumers see nothing)
                        "redis_connected": self._redis_connected,
                        "state": state,
                        "last_frame_age_s": round(now - last_t, 1) if last_t else None,
                    }
                )
        return result

    _STRUCTURAL_KEYS = {"id", "camera_type", "outputs", "sizes", "fpss"}

    def _is_settings_only_change(self, new_config: Dict[str, Any]) -> bool:
        """Return True if the new config differs from current only in 'settings' values."""
        if self.camera_manager is None:
            return False
        old = self.camera_manager.config
        if set(old.keys()) != set(new_config.keys()):
            return False
        for cam_id in old:
            for key in self._STRUCTURAL_KEYS:
                if old[cam_id].get(key) != new_config[cam_id].get(key):
                    return False
        return True

    def _apply_settings(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Apply settings (exposure, ISO, focus) to live cameras without disconnecting.

        Cameras whose config has no ``settings`` key are treated as fully-auto —
        auto exposure and continuous auto focus are restored for them too.
        """
        if self.camera_manager is None:
            return {"status": "error", "reason": "camera_manager not initialized"}
        updated = []
        skipped = []
        with self.camera_manager._cameras_lock:
            for cam_id, cam_cfg in config.items():
                camera = self.camera_manager.cameras.get(cam_id)
                if camera is None:
                    skipped.append(cam_id)
                    continue
                cam_type = cam_cfg.get("camera_type", "color")
                settings = cam_cfg.get("settings", {})
                self.camera_manager._apply_camera_settings(camera, cam_type, settings, cam_id)
                # Keep in-memory config in sync
                if settings:
                    self.camera_manager.config[cam_id]["settings"] = settings
                else:
                    self.camera_manager.config[cam_id].pop("settings", None)
                updated.append(cam_id)
        logger.info("Applied settings to cameras: %s (skipped: %s)", updated, skipped)
        return {
            "status": "ok",
            "action": "settings_applied",
            "updated": updated,
            "skipped": skipped,
        }

    def reload_cameras(self) -> Dict[str, Any]:
        """
        Update cameras from the saved config file.

        If only settings (exposure, ISO, focus) changed, applies them in-place without
        disconnecting. Otherwise does a full hot-reload (close + reinitialize).
        """
        if not self._reload_lock.acquire(blocking=False):
            return {"status": "busy", "message": "Reload already in progress"}

        try:
            # Load the new config to compare
            new_config: Optional[Dict[str, Any]] = None
            if self.config_path:
                with open(self.config_path) as f:
                    new_config = json.load(f)
            else:
                new_config = self.config_dict

            if new_config:
                validate_camera_config(new_config)  # ValueError -> caller reports 400

            if new_config and self._is_settings_only_change(new_config):
                logger.info("Settings-only change detected — skipping camera reconnect")
                return self._apply_settings(new_config)

            logger.info("Structural change detected — hot-reloading cameras…")
            # Build (and thereby validate) the new manager BEFORE tearing down the old one:
            # a bad config must leave the running cameras untouched, not closed.
            new_manager = CameraManager(
                config_path=self.config_path,
                config_dict=self.config_dict if not self.config_path else None,
                health_check_interval=self.health_check_interval,
                data_timeout=self.data_timeout,
                threaded_init=self.threaded_init,
                auto_reconnect=self.auto_reconnect,
            )
            old_manager = self.camera_manager
            previous_config = copy.deepcopy(old_manager.config) if old_manager is not None else None
            if old_manager is not None:
                old_manager.close_all()
            self.camera_manager = new_manager
            try:
                self.camera_manager.initialize_cameras(use_superspeed=self._use_superspeed)
                self.camera_manager.start_monitoring()
            except Exception:
                # init failed after the swap. Don't leave a half-initialised manager running
                # (and the capture loop publishing fallback frames): put the previous config back.
                logger.error("Camera init failed after reload — rolling back to the previous config")
                self._roll_back_reload(new_manager, previous_config)
                raise
            cameras = list(self.camera_manager.config.keys())
            logger.info("Hot-reload complete — cameras: %s", cameras)
            return {"status": "ok", "action": "reloaded", "cameras": cameras}
        except Exception as e:
            logger.error("Camera reload failed: %s", e)
            raise
        finally:
            self._reload_lock.release()

    def _roll_back_reload(
        self, failed_manager: CameraManager, previous_config: Optional[Dict[str, Any]]
    ) -> None:
        """Replace ``failed_manager`` with a manager built from ``previous_config``.

        Best effort: a rollback that also fails is logged, never raised over the original error, and
        leaves ``camera_manager`` as ``None`` so the capture loop publishes fallback frames rather than
        reading from a torn-down manager.
        """
        try:
            failed_manager.close_all()
        except Exception as e:  # noqa: BLE001
            logger.warning("Closing the failed manager during rollback raised: %s", e)
        self.camera_manager = None
        if previous_config is None:
            return
        try:
            use_path = None
            if self.config_path:
                write_config(self.config_path, previous_config)  # disk must match what is running
                use_path = self.config_path
            restored = CameraManager(
                config_path=use_path,
                config_dict=None if use_path else previous_config,
                health_check_interval=self.health_check_interval,
                data_timeout=self.data_timeout,
                threaded_init=self.threaded_init,
                auto_reconnect=self.auto_reconnect,
            )
            restored.initialize_cameras(use_superspeed=self._use_superspeed)
            restored.start_monitoring()
            self.camera_manager = restored
            logger.info("Rolled back to previous camera config — cameras: %s", list(restored.config.keys()))
        except Exception as e:  # noqa: BLE001
            logger.error("Rollback to the previous camera config failed: %s", e)

    def start_cameras(self, use_superspeed: bool = True) -> None:
        """
        Start all cameras via CameraManager.

        Args:
            use_superspeed: Use USB SuperSpeed for all cameras (default: True)
        """
        logger.info("Initializing CameraManager")
        self._use_superspeed = use_superspeed

        try:
            self.camera_manager = CameraManager(
                config_path=self.config_path,
                config_dict=self.config_dict,
                health_check_interval=self.health_check_interval,
                data_timeout=self.data_timeout,
                threaded_init=self.threaded_init,
                auto_reconnect=self.auto_reconnect,
            )

            # Initialize all cameras
            self.camera_manager.initialize_cameras(use_superspeed=use_superspeed)

            # Start health monitoring
            self.camera_manager.start_monitoring()

            n = len(self.camera_manager)
            logger.info("Started %d camera(s) successfully", n)
            logger.info(
                "Broker config — stream: %s  fps: %d  jpeg_quality: %d  maxlen: %d",
                self.stream_name,
                self.fps,
                self.jpeg_quality,
                self.maxlen,
            )
            for cam_id, cam_cfg in self.camera_manager.config.items():
                logger.info(
                    "  Camera '%s': type=%s  outputs=%s  sizes=%s  fpss=%s",
                    cam_id,
                    cam_cfg.get("camera_type", "color"),
                    cam_cfg.get("outputs"),
                    cam_cfg.get("sizes"),
                    cam_cfg.get("fpss"),
                )
        except Exception as e:
            logger.error("Failed to start cameras: %s", e)
            raise

    @staticmethod
    def _miss_grace_s(cam_config: Dict[str, Any], output_type: str) -> float:
        """How long an output may go without a frame before it is reported as down (4 / fps)."""
        outputs = cam_config.get("outputs") or []
        fpss = cam_config.get("fpss") or []
        fps: Optional[float] = None
        if output_type in outputs:
            idx = outputs.index(output_type)
            if idx < len(fpss):
                fps = fpss[idx]
        if not fps:
            fps = _DEFAULT_OUTPUT_FPS.get(output_type, 20)
        return 4.0 / max(float(fps), 1.0)

    def _get_output_size(self, cam_id: str, output_type: str) -> Tuple[int, int]:
        """Return the configured (width, height) for a camera output, defaulting to 640×480."""
        default: Tuple[int, int] = (640, 480)
        if self.camera_manager is None:
            return default
        cam_config = self.camera_manager.config.get(cam_id, {})
        outputs = cam_config.get("outputs") or []
        sizes = cam_config.get("sizes") or []
        try:
            idx = outputs.index(output_type)
            if idx < len(sizes) and sizes[idx] is not None:
                w, h = sizes[idx]
                return (int(w), int(h))
        except (ValueError, TypeError, IndexError):
            pass
        return default

    def publish_frame(
        self,
        camera_id: str,
        cam_type: str,
        output_type: str,
        frame: np.ndarray,
        is_fallback: bool = False,
    ) -> bool:
        """
        Publish a single frame to Redis.

        Args:
            camera_id: Identifier of the camera that captured this frame.
            cam_type: Camera component type (e.g. 'color').
            output_type: Output stream type (e.g. 'preview', 'video').
            frame: Frame as numpy array (BGR format from OpenCV).
            is_fallback: ``True`` when the frame was generated by
                :func:`mydepthai.fallback_frames.create_camera_error_frame`
                rather than captured from a real camera.  Consumers such as
                the model-detector must skip fallback frames.

        Returns:
            True if published successfully.
        """
        try:
            # Encode frame as JPEG
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
            success, buffer = cv2.imencode(".jpg", frame, encode_param)

            if not success:
                logger.error("Failed to encode frame as JPEG for camera '%s'", camera_id)
                return False

            # Prepare message
            frame_bytes = buffer.tobytes()
            # Cache for the camera-controls UI's "fetch latest frame to draw an ROI" flow.
            # Cached unconditionally (including fallback frames) — a placeholder is better
            # than a 404 if the camera is briefly offline.
            self._last_jpeg[(camera_id, output_type)] = frame_bytes
            message: dict[FieldT, EncodableT] = dict(
                encode_frame_message(
                    camera_id=camera_id,
                    cam_type=cam_type,
                    output_type=output_type,
                    frame=frame_bytes,
                    # Grayscale frames are 2-D: report a single channel instead of IndexError-ing
                    # (which the outer except would turn into a silently dropped frame).
                    shape=(frame.shape[0], frame.shape[1], frame.shape[2] if frame.ndim == 3 else 1),
                    is_fallback=is_fallback,
                )
            )

            # Redis is known-down and we're inside the backoff window: don't spend a socket
            # timeout per frame on a dead connection.
            if not self._redis_connected and not self._redis_backoff.ready():
                return False

            # Publish to Redis Stream with automatic trimming
            try:
                stream_id = self.redis_client.xadd(
                    self.stream_name, message, maxlen=self._xadd_maxlen(), approximate=True
                )
            except redis.RedisError as redis_err:
                logger.warning(
                    "Redis publish failed for camera '%s': %s — attempting reconnect",
                    camera_id,
                    redis_err,
                )
                if self._reconnect_redis():
                    stream_id = self.redis_client.xadd(
                        self.stream_name, message, maxlen=self._xadd_maxlen(), approximate=True
                    )
                else:
                    return False

            if not self._redis_connected:
                # An initial-ping failure leaves the flag False; the first successful publish proves
                # Redis is back, so health must stop reporting it as unavailable.
                self._redis_connected = True
                self._redis_failures = 0
                self._redis_backoff.reset()
                logger.info("Redis publishing resumed")
            logger.debug("Published frame %s from camera '%s' to %s", stream_id, camera_id, self.stream_name)
            return True

        except Exception as e:
            logger.error("Failed to publish frame from camera '%s': %s", camera_id, e)
            return False

    def get_last_jpeg(self, cam_id: str, output_type: Optional[str] = None) -> Optional[bytes]:
        """
        Return the last-published JPEG bytes for a camera/output, or None.

        Backs the camera-controls UI's ROI drag-box flow — serves whatever was
        last put on the wire, with no extra device round-trip.

        Args:
            cam_id: Camera identifier from configuration.
            output_type: Output stream type; defaults to the camera's first
                configured output when omitted.
        """
        if output_type is None:
            if self.camera_manager is None:
                return None
            cam_config = self.camera_manager.config.get(cam_id, {})
            outputs = cam_config.get("outputs") or []
            if not outputs:
                return None
            output_type = outputs[0]
        return self._last_jpeg.get((cam_id, output_type))

    def run(self) -> None:
        """
        Main loop: capture frames from all cameras and publish to Redis.

        Iterates every configured camera output on each tick.  When a camera
        returns ``None`` (offline, disconnected, or errored), a fallback frame
        is generated via :func:`mydepthai.fallback_frames.create_camera_error_frame`
        and published with ``is_fallback=True`` so consumers such as the
        model-detector can filter it out while operators can still see which
        cameras are down.

        Runs until interrupted (Ctrl+C) or stopped via :meth:`stop`.
        """
        if self.camera_manager is None:
            raise RuntimeError("Cameras not started. Call start_cameras() first.")

        self.running = True
        frame_counts: Dict[str, int] = {}
        last_frame_time = time.time()

        logger.info("Starting frame capture at %d FPS", self.fps)
        logger.info("Publishing to Redis stream: %s", self.stream_name)
        logger.info("Monitoring %d configured cameras", len(self.camera_manager.config))

        try:
            while self.running:
                # Rate limiting
                current_time = time.time()
                elapsed = current_time - last_frame_time

                if elapsed < self.frame_interval:
                    time.sleep(self.frame_interval - elapsed)
                    continue

                # Iterate all configured camera outputs so the operator always
                # receives a frame — real or a clearly-marked fallback.
                for cam_id, cam_config in self.camera_manager.config.items():
                    cam_type = cam_config.get("camera_type", "color")
                    outputs = cam_config.get("outputs") or ["preview"]

                    for output_type in outputs:
                        # ``still`` is trigger-driven; the broker never triggers it, so polling
                        # would block a full timeout per tick and publish a fallback each time.
                        if output_type == "still":
                            continue
                        # Non-blocking read: a stalled camera must not hold up the
                        # others for its 4/fps timeout. "Nothing queued yet" is not an outage,
                        # so a miss only becomes a fallback frame once it outlasts the grace
                        # window the old blocking read used (4/fps), or if no real frame has
                        # ever arrived for this output.
                        key = (cam_id, output_type)
                        result = self.camera_manager.get_frame_with_metadata(
                            cam_id, cam_type, output_type, update_health=True, timeout=0
                        )
                        frame = result[0] if result is not None else None

                        now = time.monotonic()
                        if frame is not None:
                            self._last_real_frame[key] = now
                        else:
                            last_real = self._last_real_frame.get(key)
                            if last_real is not None and now - last_real < self._miss_grace_s(
                                cam_config, output_type
                            ):
                                continue

                        is_fallback = frame is None
                        if is_fallback:
                            size = self._get_output_size(cam_id, output_type)
                            frame = create_camera_error_frame(
                                cam_id, f"No frame from camera '{cam_id}'", size
                            )
                            logger.debug("Publishing fallback frame for camera '%s' %s", cam_id, output_type)

                        if frame is not None and self.publish_frame(
                            cam_id, cam_type, output_type, frame, is_fallback=is_fallback
                        ):
                            count = frame_counts.get(cam_id, 0) + 1
                            frame_counts[cam_id] = count
                            if count % 100 == 0:
                                logger.info("Published %d frames from camera '%s'", count, cam_id)

                last_frame_time = time.time()

        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception as e:
            logger.error("Unexpected error in capture loop: %s", e)
            raise
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop all cameras and close connections."""
        logger.info("Stopping camera broker...")
        self.running = False

        if self.camera_manager is not None:
            try:
                self.camera_manager.close_all()
                logger.info("All cameras stopped")
            except Exception as e:
                logger.error("Error stopping cameras: %s", e)

        if self.redis_client is not None:
            try:
                self.redis_client.close()
                logger.info("Redis connection closed")
            except Exception as e:
                logger.error("Error closing Redis: %s", e)


def main() -> None:
    """Main entry point for standalone execution (also the ``mydepthai-broker`` console script)."""
    # Configure logging here, not at import: importing this module must not need a project root.
    setup_logging("camera_broker")
    config_loader = ConfigLoader()  # the process's single loader; handed to the web UI too
    settings = BrokerSettings.from_config(config_loader)
    args = build_parser(settings).parse_args()
    settings = settings.with_overrides(
        **{f.name: getattr(args, f.name, None) for f in dataclasses.fields(BrokerSettings)}
    )

    # Create simple config if no config file provided
    config_dict = None
    if args.config_path is None:
        logger.warning("No config file provided, creating single camera configuration")
        config_dict = {
            "camera_0": {
                "id": 0,
                "camera_type": args.cam_type,
                "outputs": [args.output_type],
                "sizes": None,
                "fpss": None,
            }
        }

    # Create and start broker
    broker = CameraBroker(
        config_path=args.config_path,
        config_dict=config_dict,
        auto_reconnect=not args.no_auto_reconnect,
        threaded_init=not args.no_threaded_init,
        **settings.broker_kwargs(),
    )

    broker.start_cameras(use_superspeed=args.use_superspeed)

    if not args.no_web:
        try:
            import uvicorn

            from mydepthai.web.server import app as web_app
            from mydepthai.web.server import init_app

            web_token, token_generated = resolve_web_token(settings.web_token)
            log_bind_and_token(settings.web_host, token_generated)
            init_app(broker, config_loader, token=web_token, bind_host=settings.web_host)

            def _run_web() -> None:
                # uvicorn.run() installs signal handlers which only work on the main thread.
                # Using uvicorn.Server directly avoids that restriction.
                config = uvicorn.Config(
                    app=web_app,
                    host=settings.web_host,
                    port=settings.web_port,
                    log_level="warning",
                )
                server = uvicorn.Server(config)
                asyncio.run(server.serve())

            web_thread = threading.Thread(target=_run_web, daemon=True)
            web_thread.start()
            logger.info("Web UI started at http://%s:%d", settings.web_host, settings.web_port)
        except ImportError:
            logger.warning(
                "fastapi/uvicorn not installed — web UI disabled. "
                "Run: pip install fastapi uvicorn[standard] jinja2"
            )

    broker.run()


if __name__ == "__main__":
    main()
