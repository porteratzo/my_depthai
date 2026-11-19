#!/usr/bin/env python3
"""
Camera Manager Module

This module provides a manager for multiple BasicDAICamera instances. It handles:
- Configuration file loading
- Multiple camera initialization
- Health monitoring and logging
- Graceful shutdown
"""

import json
import logging
import time
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any, Callable, Dict, Optional, Tuple, Union

import depthai as dai
import numpy as np

from shared.core.reconnect import ReconnectBackoff

from ..depthai_controllers.basic_dai_camera import BasicDAICamera
from ..depthai_controllers.modules.camera_config_module import CameraConfigModule
from .config_utils import validate_camera_config
from .health import CameraHealth, classify_health

logger = logging.getLogger(__name__)


class CameraManager:
    """
    Manager for multiple BasicDAICamera instances.

    Handles initialization, monitoring, and management of multiple cameras
    based on configuration files. Monitors camera health and logs connection
    status and data flow issues.

    Attributes:
        cameras: Dictionary mapping camera IDs to BasicDAICamera instances
        config: Configuration dictionary for all cameras
        health_check_interval: Time in seconds between health checks
        data_timeout: Maximum time in seconds without new data before warning
        threaded_init: Whether to initialize cameras in parallel threads
        auto_reconnect: Whether to automatically reconnect inactive cameras
        reconnect_max_delay: Cap in seconds on the per-camera reconnect backoff
        _monitoring: Flag to control monitoring thread
        _monitor_thread: Thread running health monitoring
        _cameras_lock: Lock for thread-safe camera dictionary updates
        _backoffs: Per-camera reconnect rate limiter (retries forever, capped delay)
    """

    def __init__(
        self,
        config_path: Optional[Union[str, Path]] = None,
        config_dict: Optional[Dict[str, Any]] = None,
        health_check_interval: float = 5.0,
        data_timeout: float = 10.0,
        threaded_init: bool = True,
        auto_reconnect: bool = True,
        reconnect_max_delay: float = 60.0,
    ) -> None:
        """
        Initialize the CameraManager.

        Args:
            config_path: Path to JSON configuration file
            config_dict: Configuration dictionary (used if config_path is None)
            health_check_interval: Time in seconds between health checks (default: 5.0)
            data_timeout: Maximum time in seconds without new data before warning (default: 10.0)
            threaded_init: Whether to initialize cameras in parallel (default: True)
            auto_reconnect: Whether to automatically reconnect inactive cameras (default: True)
            reconnect_max_delay: Cap in seconds on the reconnect backoff (default: 60.0). Reconnects
                never stop: the delay between attempts starts at ``health_check_interval``,
                doubles after each failure, and is capped here.

        Raises:
            ValueError: If neither config_path nor config_dict is provided
            FileNotFoundError: If config_path does not exist
        """
        self.cameras: Dict[str, BasicDAICamera] = {}
        self.config: Dict[str, Any] = {}
        self.health_check_interval: float = health_check_interval
        self.data_timeout: float = data_timeout
        self.threaded_init: bool = threaded_init
        self.auto_reconnect: bool = auto_reconnect
        self.reconnect_max_delay: float = reconnect_max_delay
        self._monitoring: bool = False
        self._monitor_thread: Optional[Thread] = None
        self._stop_event: Event = Event()
        self._last_frame_time: Dict[str, float] = {}
        self._last_metadata: Dict[str, Dict[str, Any]] = {}
        # Cameras already reported as "not found" (log once, not on every polling tick).
        self._missing_logged: set[str] = set()
        self._cameras_lock: RLock = RLock()  # reentrant so get_frames_all can call get_frame safely
        self._backoffs: Dict[str, ReconnectBackoff] = {}
        self._reconnecting: set = set()  # cameras with a reconnect currently in progress
        # Worker threads running monitor-dispatched reconnects, so a slow camera never blocks the
        # monitor loop. Guarded by ``_cameras_lock``.
        self._reconnect_threads: Dict[str, Thread] = {}
        self._use_superspeed: bool = False

        # Load configuration
        if config_path is not None:
            self._load_config_from_file(config_path)
        elif config_dict is not None:
            self.config = config_dict
            validate_camera_config(self.config)
        else:
            raise ValueError("Either config_path or config_dict must be provided")

        logger.info("CameraManager initialized with %d camera configurations", len(self.config))

    def _load_config_from_file(self, config_path: Union[str, Path]) -> None:
        """
        Load camera configuration from a JSON file.

        Args:
            config_path: Path to the configuration file

        Raises:
            FileNotFoundError: If the configuration file does not exist
            json.JSONDecodeError: If the file is not valid JSON
            ValueError: If the configuration schema is invalid
        """
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_path}")

        logger.info("Loading configuration from: %s", config_path)
        with open(path, "r") as f:
            self.config = json.load(f)
        validate_camera_config(self.config)
        logger.info("Configuration loaded successfully")

    def _initialize_single_camera(
        self, cam_id: str, cam_config: Dict[str, Any], use_superspeed: bool
    ) -> bool:
        """
        Initialize a single camera.

        The camera is added to ``self.cameras`` only if it connected and started; a camera that
        failed to connect is closed and left out, so ``cam_id in self.cameras`` means "usable".

        Args:
            cam_id: Camera identifier from configuration
            cam_config: Configuration dictionary for this camera
            use_superspeed: Whether to use USB SuperSpeed

        Returns:
            True if the camera connected and started, False otherwise.
        """
        camera: Optional[BasicDAICamera] = None
        try:
            # Get the identifier - can be an index or MXID string
            identifier = cam_config.get("id", cam_id)

            logger.info("Initializing camera '%s' with identifier: %s", cam_id, identifier)

            # Each camera owns its pipeline/device and releases them in close(). A manager-wide
            # ExitStack shared across reconnects only ever grew and re-exited dead pipelines.
            camera = BasicDAICamera(identifier=identifier, speed=use_superspeed)

            # Read connection parameters from config (with sensible defaults)
            wait_timeout = cam_config.get("wait_timeout", 25.0)
            reconnect_on_timeout = cam_config.get("reconnect_on_timeout", True)
            max_retries = cam_config.get("max_retries", 2)

            # Wait for camera to be ready
            if camera.wait_for_cam(
                timeout=wait_timeout,
                reconnect_on_timeout=reconnect_on_timeout,
                max_retries=max_retries,
            ):
                logger.info("Camera '%s' connected successfully", cam_id)

                # Create camera components based on config
                cam_type = cam_config.get("camera_type", "color")
                outputs = cam_config.get("outputs", ["preview", "video"])
                sizes = cam_config.get("sizes", None)
                fpss = cam_config.get("fpss", None)

                camera.create_camera(cam_type=cam_type, outputs=outputs, sizes=sizes, fpss=fpss)

                # Build and start the camera
                camera.build()
                camera.start_device()

                # Apply camera settings if provided
                if "settings" in cam_config:
                    self._apply_camera_settings(camera, cam_type, cam_config["settings"], cam_id)

                # Allow the pipeline to produce its first frame before handing off.
                # PoE cameras need several seconds after pipeline.start() for firmware
                # init and ISP convergence. Without this sleep the first broker polls
                # time out, and the health monitor then triggers a reconnect cycle.
                warmup_time = cam_config.get("warmup_time", 0.0)
                if warmup_time > 0:
                    logger.info("Camera '%s' warming up for %.1f seconds...", cam_id, warmup_time)
                    time.sleep(warmup_time)

                logger.info("Camera '%s' initialized and started successfully", cam_id)
            else:
                logger.error("Camera '%s' failed to connect within timeout", cam_id)
                self._discard_camera(cam_id, camera)
                return False

            # Thread-safe update of cameras dictionary
            with self._cameras_lock:
                self.cameras[cam_id] = camera
                self._last_frame_time[cam_id] = time.time()
            return True

        except Exception as e:
            logger.exception("Failed to initialize camera '%s': %s", cam_id, e)
            self._discard_camera(cam_id, camera)
            return False

    @staticmethod
    def _discard_camera(cam_id: str, camera: Optional[BasicDAICamera]) -> None:
        """Best-effort close of a camera instance that never became usable."""
        if camera is None:
            return
        try:
            camera.close(force=True)
        except Exception as e:
            logger.warning("Error closing unusable camera instance for '%s': %s", cam_id, e)

    def initialize_cameras(self, use_superspeed: bool = False) -> None:
        """
        Initialize all cameras based on the configuration.

        Uses threaded initialization if threaded_init=True (default), otherwise
        initializes cameras sequentially.

        Args:
            use_superspeed: Whether to use USB SuperSpeed for all cameras (default: False)
        """
        logger.info("Starting camera initialization for %d cameras", len(self.config))
        logger.info("Initialization mode: %s", "threaded" if self.threaded_init else "sequential")

        # Store superspeed setting for reconnection
        self._use_superspeed = use_superspeed

        available_devices = dai.Device.getAllAvailableDevices()
        logger.info("Found %d available DepthAI devices", len(available_devices))

        for cam_id, cam_config in self.config.items():
            self._initialize_single_camera(cam_id, cam_config, use_superspeed)

        logger.info(
            "Camera initialization complete. %d/%d cameras ready",
            len(self.cameras),
            len(self.config),
        )

    def _apply_camera_settings(
        self, camera: BasicDAICamera, cam_type: str, settings: Dict[str, Any], cam_id: str
    ) -> None:
        """
        Apply camera settings like ISO, exposure, focus, AE/AF controls, etc.

        Args:
            camera: The BasicDAICamera instance
            cam_type: Type of camera component to configure
            settings: Dictionary of settings to apply. Absent keys restore auto/default
                mode, except ``af_lens_range``, ``af_region`` and ``ae_region`` — depthai
                has no API to clear a lens range or region back to a hardware default,
                so those are simply skipped when absent (regions are sticky until
                explicitly changed again or the camera reconnects). Persisted region values
                are in output-frame pixel space and are scaled to sensor space here, exactly as
                the live drag-box path does. If the output size cannot be determined
                the region is skipped with a warning rather than applied in the wrong place.
            cam_id: Camera identifier from configuration (used to look up frame/output size).
        """
        try:
            if cam_type in camera.components:
                component: CameraConfigModule = camera.components[cam_type]

                if "exposure" in settings or "iso" in settings:
                    component.set_exposure(settings.get("exposure", 20000), settings.get("iso", 100))
                    logger.debug(
                        "Set exposure to %d and ISO to %d for camera component '%s'",
                        settings.get("exposure", 20000),
                        settings.get("iso", 100),
                        cam_type,
                    )
                else:
                    component.set_auto_exposure()
                    logger.debug("Restored auto exposure for camera component '%s'", cam_type)

                if "focus" in settings:
                    component.set_focus(settings["focus"])
                    logger.debug("Set focus to %d for camera component '%s'", settings["focus"], cam_type)
                else:
                    component.continuous_auto_focus()
                    logger.debug("Restored continuous auto focus for camera component '%s'", cam_type)

                if "ae_lock" in settings:
                    component.set_auto_exposure_lock(settings["ae_lock"])
                else:
                    component.set_auto_exposure_lock(False)

                if "exposure_compensation" in settings:
                    component.set_exposure_compensation(settings["exposure_compensation"])
                else:
                    component.set_exposure_compensation(0)

                if "exposure_limit_us" in settings:
                    component.set_auto_exposure_limit(settings["exposure_limit_us"])
                else:
                    component.set_auto_exposure_limit(33333)  # effectively "no cap"

                if "af_lens_range" in settings:
                    infinity_pos, macro_pos = settings["af_lens_range"]
                    component.set_auto_focus_lens_range(infinity_pos, macro_pos)
                # absent -> skip; no safe universal default to restore

                self._apply_regions(camera, cam_id, cam_type, component, settings)

        except Exception as e:
            logger.warning("Failed to apply some camera settings: %s", e)

    _SOCKET_BY_CAM_TYPE = {
        "color": dai.CameraBoardSocket.CAM_A,
        "left": dai.CameraBoardSocket.CAM_B,
        "right": dai.CameraBoardSocket.CAM_C,
    }

    def _apply_regions(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        settings: Dict[str, Any],
    ) -> None:
        """Apply persisted af/ae regions (scaled to sensor space). Absent keys are skipped: depthai
        has no API to clear a region, so it stays sticky until changed or the camera reconnects."""
        for key, setter in (
            ("af_region", component.set_Focus_Region),
            ("ae_region", component.set_Exposure_Region),
        ):
            if key in settings:
                roi = self._scale_persisted_region(camera, cam_id, cam_type, key, settings)
                if roi is not None:
                    setter(*roi)

    def _scale_persisted_region(
        self, camera: BasicDAICamera, cam_id: str, cam_type: str, key: str, settings: Dict[str, Any]
    ) -> Optional[Tuple[int, int, int, int]]:
        """Scale a persisted output-space region to sensor space, or None (logged) if impossible."""
        x, y, w, h = settings[key]
        try:
            return self._scale_roi_to_sensor(camera, cam_id, cam_type, x, y, w, h)
        except ValueError as e:
            logger.warning("Skipping %s for '%s': %s", key, cam_id, e)
            return None

    def _output_frame_size(self, cam_id: str, camera: BasicDAICamera, cam_type: str) -> Tuple[int, int]:
        """Size (w, h) of the output frame the persisted regions were drawn on.

        Prefers the last cached live frame; at startup (nothing cached yet) falls back to the size
        the built pipeline actually requested for the camera's first configured output.
        Returns (0, 0) if unknown.
        """
        metadata = self.get_cached_metadata(cam_id)
        frame_w, frame_h = metadata.get("frame_width"), metadata.get("frame_height")
        if frame_w and frame_h:
            return int(frame_w), int(frame_h)
        component = camera.components.get(cam_type)
        outputs_config = getattr(component, "outputs_config", None)
        if isinstance(outputs_config, dict):
            wanted = self.config.get(cam_id, {}).get("outputs") or list(outputs_config)
            for out in wanted:
                size = (outputs_config.get(out) or {}).get("size")
                if size:
                    return int(size[0]), int(size[1])
        return 0, 0

    def _scale_roi_to_sensor(
        self, camera: BasicDAICamera, cam_id: str, cam_type: str, x: float, y: float, w: float, h: float
    ) -> Tuple[int, int, int, int]:
        """
        Scale an ROI given in output-frame pixel space to sensor-native pixel space.

        ``setAutoFocusRegion``/``setAutoExposureRegion`` expect sensor-native,
        pre-ISP-scaling coordinates, but the UI's drag-box is drawn on the
        displayed output frame. The source-space size is taken from the
        *actual last-cached frame's* dimensions (populated by
        :meth:`get_frame_with_metadata`), not the config file's "sizes" entry,
        since per-output defaults applied by CameraModule.build() can
        legitimately disagree with what was configured. Before any frame is
        cached (startup re-apply) it uses the size the built pipeline requested
        (``CameraModule.outputs_config``), which is that same value.

        Raises:
            ValueError: If no frame has been cached yet for this camera, or
                cam_type is not a recognized socket.
        """
        frame_w, frame_h = self._output_frame_size(cam_id, camera, cam_type)
        if not frame_w or not frame_h:
            raise ValueError(
                f"No cached frame for camera '{cam_id}' yet — fetch a frame before setting a region"
            )

        socket = self._SOCKET_BY_CAM_TYPE.get(cam_type)
        if socket is None:
            raise ValueError(f"Unknown camera type for ROI scaling: {cam_type}")

        sensor_w, sensor_h = frame_w, frame_h  # fallback if features lookup fails
        try:
            if camera.device is None:
                raise RuntimeError("Camera device not connected")
            for feat in camera.device.getConnectedCameraFeatures():
                if feat.socket == socket:
                    sensor_w, sensor_h = feat.width, feat.height
                    break
        except Exception as e:
            logger.warning("Could not read sensor features for '%s', using frame size: %s", cam_id, e)

        scale_x = sensor_w / frame_w
        scale_y = sensor_h / frame_h
        sx = int(x * scale_x)
        sy = int(y * scale_y)
        sw = max(1, int(w * scale_x))
        sh = max(1, int(h * scale_y))
        return sx, sy, sw, sh

    def _action_trigger_auto_focus(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        params: Dict[str, Any],
    ) -> None:
        component.trigger_auto_focus()

    def _action_trigger_auto_focus_macro(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        params: Dict[str, Any],
    ) -> None:
        component.trigger_auto_focus_macro()

    def _action_trigger_auto_focus_edof(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        params: Dict[str, Any],
    ) -> None:
        component.trigger_auto_focus_EDOF()

    def _action_continuous_auto_focus(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        params: Dict[str, Any],
    ) -> None:
        component.continuous_auto_focus()
        self.config[cam_id].setdefault("settings", {}).pop("focus", None)

    def _action_continuous_pic_auto_focus(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        params: Dict[str, Any],
    ) -> None:
        component.continuous_pic_auto_focus()
        self.config[cam_id].setdefault("settings", {}).pop("focus", None)

    def _action_set_exposure(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        params: Dict[str, Any],
    ) -> None:
        exposure_us = params["exposure_us"]
        iso = params.get("iso", 100)
        component.set_exposure(exposure_us, iso)
        settings = self.config[cam_id].setdefault("settings", {})
        settings["exposure"] = exposure_us
        settings["iso"] = iso

    def _action_set_auto_exposure(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        params: Dict[str, Any],
    ) -> None:
        component.set_auto_exposure()
        settings = self.config[cam_id].setdefault("settings", {})
        settings.pop("exposure", None)
        settings.pop("iso", None)

    def _action_set_focus(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        params: Dict[str, Any],
    ) -> None:
        lens_position = params["lens_position"]
        component.set_focus(lens_position)
        self.config[cam_id].setdefault("settings", {})["focus"] = lens_position

    def _action_set_ae_lock(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        params: Dict[str, Any],
    ) -> None:
        locked = bool(params["locked"])
        component.set_auto_exposure_lock(locked)
        self.config[cam_id].setdefault("settings", {})["ae_lock"] = locked

    def _action_set_exposure_compensation(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        params: Dict[str, Any],
    ) -> None:
        value = int(params["value"])
        if not (-9 <= value <= 9):
            raise ValueError(f"exposure_compensation {value} out of range [-9, 9]")
        component.set_exposure_compensation(value)
        self.config[cam_id].setdefault("settings", {})["exposure_compensation"] = value

    def _action_set_exposure_limit(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        params: Dict[str, Any],
    ) -> None:
        max_us = params["max_us"]
        if not (1 <= max_us <= 33333):
            raise ValueError(f"exposure_limit_us {max_us} out of range [1, 33333]")
        component.set_auto_exposure_limit(max_us)
        self.config[cam_id].setdefault("settings", {})["exposure_limit_us"] = max_us

    def _action_set_af_lens_range(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        params: Dict[str, Any],
    ) -> None:
        infinity_pos, macro_pos = params["infinity_pos"], params["macro_pos"]
        component.set_auto_focus_lens_range(infinity_pos, macro_pos)
        self.config[cam_id].setdefault("settings", {})["af_lens_range"] = [infinity_pos, macro_pos]

    def _action_set_focus_region(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        params: Dict[str, Any],
    ) -> None:
        x, y, w, h = params["x"], params["y"], params["w"], params["h"]
        sx, sy, sw, sh = self._scale_roi_to_sensor(camera, cam_id, cam_type, x, y, w, h)
        component.set_Focus_Region(sx, sy, sw, sh)
        self.config[cam_id].setdefault("settings", {})["af_region"] = [x, y, w, h]

    def _action_set_exposure_region(
        self,
        camera: BasicDAICamera,
        cam_id: str,
        cam_type: str,
        component: CameraConfigModule,
        params: Dict[str, Any],
    ) -> None:
        x, y, w, h = params["x"], params["y"], params["w"], params["h"]
        sx, sy, sw, sh = self._scale_roi_to_sensor(camera, cam_id, cam_type, x, y, w, h)
        component.set_Exposure_Region(sx, sy, sw, sh)
        self.config[cam_id].setdefault("settings", {})["ae_region"] = [x, y, w, h]

    _CAMERA_ACTIONS: Dict[
        str,
        Callable[["CameraManager", BasicDAICamera, str, str, CameraConfigModule, Dict[str, Any]], None],
    ] = {
        "trigger_auto_focus": _action_trigger_auto_focus,
        "trigger_auto_focus_macro": _action_trigger_auto_focus_macro,
        "trigger_auto_focus_EDOF": _action_trigger_auto_focus_edof,
        "continuous_auto_focus": _action_continuous_auto_focus,
        "continuous_pic_auto_focus": _action_continuous_pic_auto_focus,
        "set_exposure": _action_set_exposure,
        "set_auto_exposure": _action_set_auto_exposure,
        "set_focus": _action_set_focus,
        "set_ae_lock": _action_set_ae_lock,
        "set_exposure_compensation": _action_set_exposure_compensation,
        "set_exposure_limit": _action_set_exposure_limit,
        "set_af_lens_range": _action_set_af_lens_range,
        "set_focus_region": _action_set_focus_region,
        "set_exposure_region": _action_set_exposure_region,
    }

    def send_camera_action(
        self, cam_id: str, cam_type: str, action: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Dispatch a one-shot action or persistent-setting change to a live camera component.

        Dispatches through an explicit allow-list (``_CAMERA_ACTIONS``) — never
        ``getattr(component, action)`` — so an arbitrary request string can
        never invoke an unintended method on the component.

        Args:
            cam_id: Camera identifier from configuration.
            cam_type: Camera component type (e.g. 'color').
            action: One of the allow-listed action names (see ``_CAMERA_ACTIONS``).
            params: Action-specific parameters dict (default: {}).

        Returns:
            {"status": "ok", "action": action} on success.

        Raises:
            ValueError: Unknown camera, unknown component, unknown action, or
                invalid/out-of-range params.
        """
        params = params or {}
        with self._cameras_lock:
            camera = self.cameras.get(cam_id)
        if camera is None:
            raise ValueError(f"Unknown camera: {cam_id}")
        if cam_type not in camera.components:
            raise ValueError(f"Unknown camera component '{cam_type}' for camera '{cam_id}'")
        component: CameraConfigModule = camera.components[cam_type]

        handler = self._CAMERA_ACTIONS.get(action)
        if handler is None:
            raise ValueError(f"Unknown action: {action}")

        try:
            handler(self, camera, cam_id, cam_type, component, params)
        except KeyError as e:
            # Handlers index required params directly; a missing one is a bad request, not a crash.
            raise ValueError(f"Action '{action}' is missing required param {e}") from e
        except (TypeError, OverflowError) as e:
            raise ValueError(f"Action '{action}' got an invalid param value: {e}") from e
        return {"status": "ok", "action": action}

    def start_monitoring(self) -> None:
        """
        Start the health monitoring thread.

        Monitors camera connections and data flow, logging warnings when
        cameras haven't produced new data within the timeout period.
        """
        if self._monitoring:
            logger.warning("Monitoring already started")
            return

        self._monitoring = True
        self._stop_event.clear()
        self._monitor_thread = Thread(target=self._monitor_health, daemon=True)
        self._monitor_thread.start()
        logger.info(
            "Health monitoring started (interval: %.1fs, timeout: %.1fs)",
            self.health_check_interval,
            self.data_timeout,
        )

    def stop_monitoring(self) -> None:
        """Stop the health monitoring thread."""
        if not self._monitoring:
            return

        self._monitoring = False
        self._stop_event.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5.0)
        logger.info("Health monitoring stopped")

    def _reconnect_camera(self, cam_id: str) -> bool:
        """
        Attempt to reconnect a disconnected camera.

        Args:
            cam_id: Camera identifier to reconnect

        Returns:
            True if reconnection successful, False otherwise
        """
        if cam_id not in self.config:
            logger.error("Cannot reconnect camera '%s': not in configuration", cam_id)
            return False

        with self._cameras_lock:
            if cam_id in self._reconnecting:
                logger.debug("Camera '%s' reconnection already in progress, skipping", cam_id)
                return False
            self._reconnecting.add(cam_id)

        logger.info("Attempting to reconnect camera '%s'", cam_id)

        try:
            # Detach the old camera under the lock (cheap), then force-close it (pipeline exit +
            # device close, which can take long on a wedged PoE device) *outside* the lock so
            # get_frame and the other lock users for healthy cameras are not blocked.
            with self._cameras_lock:
                old_camera = self.cameras.pop(cam_id, None)
            if old_camera is not None:
                try:
                    old_camera.close(force=True)
                except Exception as e:
                    logger.warning("Error closing old camera instance for '%s': %s", cam_id, e)

            # Reinitialize the camera
            cam_config = self.config[cam_id]
            if self._initialize_single_camera(cam_id, cam_config, self._use_superspeed):
                logger.info("Successfully reconnected camera '%s'", cam_id)
                return True

            logger.error("Failed to reconnect camera '%s'", cam_id)
            return False

        except Exception as e:
            logger.exception("Exception during reconnection of camera '%s': %s", cam_id, e)
            return False
        finally:
            with self._cameras_lock:
                self._reconnecting.discard(cam_id)

    def _backoff_for(self, cam_id: str) -> ReconnectBackoff:
        """Per-camera reconnect rate limiter, created on first use."""
        with self._cameras_lock:
            backoff = self._backoffs.get(cam_id)
            if backoff is None:
                backoff = ReconnectBackoff(
                    initial=self.health_check_interval,
                    maximum=max(self.reconnect_max_delay, self.health_check_interval),
                )
                self._backoffs[cam_id] = backoff
            return backoff

    def _attempt_camera_reconnect(self, cam_id: str, reason: str) -> None:
        """Try to reconnect ``cam_id`` if its backoff window allows. Retries never stop.

        The delay between attempts doubles after each failure up to ``reconnect_max_delay``
        and resets on success, so a camera replugged hours later is picked up again.
        """
        backoff = self._backoff_for(cam_id)
        if not backoff.ready():
            return
        logger.info("Camera '%s' %s, attempting reconnect", cam_id, reason)
        if self._reconnect_camera(cam_id):
            backoff.reset()
            logger.info("Camera '%s' successfully reconnected", cam_id)
        else:
            backoff.record_failure()
            logger.error("Failed to reconnect camera '%s'; backing off", cam_id)

    def _dispatch_reconnect(self, cam_id: str, reason: str) -> None:
        """Run :meth:`_attempt_camera_reconnect` on a worker thread, one per camera.

        A PoE reconnect can take ``wait_timeout x retries + warmup_time``; doing it inline would
        stop the monitor from health-checking or reconnecting every other camera meanwhile.
        """
        with self._cameras_lock:
            existing = self._reconnect_threads.get(cam_id)
            if existing is not None and existing.is_alive():
                return  # previous attempt still running
            worker = Thread(
                target=self._attempt_camera_reconnect,
                args=(cam_id, reason),
                name=f"reconnect-{cam_id}",
                daemon=True,
            )
            self._reconnect_threads[cam_id] = worker
        worker.start()

    def _monitor_health(self) -> None:
        """
        Internal monitoring loop that checks camera health.

        Runs in a separate thread and checks:
        - Camera connection status
        - Time since last frame update
        - Auto-reconnect disconnected cameras if enabled
        """
        while self._monitoring and not self._stop_event.is_set():
            try:
                current_time = time.time()
                # Iterate over all configured cameras, not just those currently connected.
                # Cameras can be removed from self.cameras after a failed reconnect, but we
                # still want to keep retrying them.
                cameras_to_check = list(self.config.keys())

                for cam_id in cameras_to_check:
                    health = self.get_camera_health(cam_id)

                    if health is CameraHealth.CONNECTING:
                        continue  # a reconnect (e.g. from revive_camera) is already running

                    if health is CameraHealth.OFFLINE:
                        with self._cameras_lock:
                            absent = cam_id not in self.cameras
                        # Absent: never connected, or removed after a failed reconnect. Either
                        # way it is treated as disconnected and retried.
                        reason = "absent from manager" if absent else "not connected"
                        logger.warning("Camera '%s' is %s", cam_id, reason)
                        if self.auto_reconnect:
                            self._dispatch_reconnect(cam_id, "absent" if absent else reason)
                        continue

                    if health is CameraHealth.STALE:
                        with self._cameras_lock:
                            last_t = self._last_frame_time.get(cam_id)
                        age = "unknown" if last_t is None else f"{current_time - last_t:.1f}"
                        logger.warning("Camera '%s' has not produced new data for %s seconds", cam_id, age)
                        if self.auto_reconnect:
                            self._dispatch_reconnect(cam_id, "inactive")
                    else:
                        # Camera healthy: clear any stale backoff
                        self._backoffs.pop(cam_id, None)

            except Exception as e:
                logger.exception("Error in health monitoring: %s", e)

            # Wait for next check interval
            self._stop_event.wait(self.health_check_interval)

    def get_frame(
        self,
        cam_id: str,
        cam_type: str = "color",
        output_type: str = "preview",
        update_health: bool = True,
    ) -> Optional[np.ndarray]:
        """
        Get a frame from a specific camera.

        Returns ``None`` on any error (camera not found, disconnected, or
        frame-retrieval exception).  Callers that need to display something to
        operators when no frame is available should use
        :func:`mydepthai.fallback_frames.create_camera_error_frame` explicitly.

        Args:
            cam_id: Camera identifier from configuration
            cam_type: Camera component type (default: 'color')
            output_type: Output type to get (default: 'preview')
            update_health: Whether to update last frame time on success (default: True)

        Returns:
            Frame as a numpy array, or ``None`` if the camera is unavailable.
        """
        # Snapshot the camera reference under the lock so the health-monitor
        # thread cannot delete it between our existence check and the access.
        with self._cameras_lock:
            camera = self.cameras.get(cam_id)

        if camera is None:
            logger.error("Camera '%s' not found in manager", cam_id)
            return None

        try:
            frame = camera.get_frame(cam=cam_type, out_type=output_type)

            if update_health and frame is not None:
                with self._cameras_lock:
                    self._last_frame_time[cam_id] = time.time()

            return frame

        except Exception as e:
            logger.error("Error getting frame from camera '%s': %s", cam_id, e)
            return None

    def get_frame_with_metadata(
        self,
        cam_id: str,
        cam_type: str = "color",
        output_type: str = "preview",
        update_health: bool = True,
        timeout: Optional[float] = None,
    ) -> Optional[Tuple[np.ndarray, Dict[str, Any]]]:
        """
        Get a frame plus control metadata from a specific camera.

        On success, caches the metadata (readable later via
        :meth:`get_cached_metadata` without a device round-trip). The cache is
        only ever written on success, so a transient offline/fallback read
        never overwrites it with stale/missing data.

        Returns ``None`` on any error (camera not found, disconnected, or
        frame-retrieval exception) — same error contract as :meth:`get_frame`.

        Args:
            cam_id: Camera identifier from configuration
            cam_type: Camera component type (default: 'color')
            output_type: Output type to get (default: 'preview')
            update_health: Whether to update last frame time on success (default: True)
            timeout: Seconds to block for a frame; ``0`` polls without blocking. ``None`` (default)
                uses the per-output ``4 / fps`` timeout.

        Returns:
            A ``(frame, metadata)`` tuple, or ``None`` if the camera is unavailable.
        """
        with self._cameras_lock:
            camera = self.cameras.get(cam_id)

        if camera is None:
            if cam_id not in self._missing_logged:
                self._missing_logged.add(cam_id)
                logger.error("Camera '%s' not found in manager (further occurrences logged at DEBUG)", cam_id)
            else:
                logger.debug("Camera '%s' not found in manager", cam_id)
            return None
        self._missing_logged.discard(cam_id)

        try:
            if timeout is None:
                result = camera.get_frame_with_metadata(cam=cam_type, out_type=output_type)
            else:
                result = camera.get_frame_with_metadata(cam=cam_type, out_type=output_type, timeout=timeout)

            if result is not None:
                frame, metadata = result
                with self._cameras_lock:
                    if update_health:
                        self._last_frame_time[cam_id] = time.time()
                    self._last_metadata[cam_id] = metadata
                return frame, metadata

            return None

        except Exception as e:
            logger.error("Error getting frame+metadata from camera '%s': %s", cam_id, e)
            return None

    def get_cached_metadata(self, cam_id: str) -> Dict[str, Any]:
        """Return the last-cached frame metadata for a camera, or {} if none yet."""
        with self._cameras_lock:
            return dict(self._last_metadata.get(cam_id, {}))

    def get_all_frames_type_output(
        self,
        cam_type: str = "color",
        output_type: str = "preview",
    ) -> Dict[str, Any]:
        """
        Get frames from all cameras for a specific type and output.

        Returns only cameras that successfully produced a frame.

        Args:
            cam_type: Camera component type (default: 'color')
            output_type: Output type to get (default: 'preview')

        Returns:
            Dictionary mapping camera IDs to their frames (omits cameras that returned None)
        """
        with self._cameras_lock:
            cam_ids = list(self.cameras.keys())
        frames = {}
        for cam_id in cam_ids:
            frame = self.get_frame(cam_id, cam_type, output_type)
            if frame is not None:
                frames[cam_id] = frame
        return frames

    def get_frames_all(self) -> Dict[str, Any]:
        """Get frames from all cameras.

        Returns:
            Dictionary mapping camera IDs to their frames.
        """
        frames: Dict[str, Any] = {}
        with self._cameras_lock:
            for cam_id in self.cameras.keys():
                camera = self.cameras[cam_id]
                frames[cam_id] = {}
                for cam_type in camera.components.keys():
                    frames[cam_id][cam_type] = {}
                    for output_type in camera.components[cam_type].outputs_config.keys():
                        frame = self.get_frame(cam_id, cam_type, output_type)
                        if frame is not None:
                            frames[cam_id][cam_type][output_type] = frame
        return frames

    def get_camera(self, cam_id: str) -> Optional[BasicDAICamera]:
        """
        Get a BasicDAICamera instance by ID.

        Args:
            cam_id: Camera identifier from configuration

        Returns:
            BasicDAICamera instance or None if not found
        """
        return self.cameras.get(cam_id)

    def reset_reconnect_attempts(self, cam_id: Optional[str] = None) -> None:
        """
        Clear the reconnect backoff for one or all cameras so the next health check retries
        immediately instead of waiting out the current delay.

        Args:
            cam_id: Camera identifier to reset, or None to reset all cameras
        """
        with self._cameras_lock:
            if cam_id is not None:
                logger.info("Resetting reconnect backoff for camera '%s'", cam_id)
                self._backoffs.pop(cam_id, None)
            else:
                logger.info("Resetting reconnect backoff for all cameras")
                self._backoffs.clear()

    def get_camera_health(self, cam_id: str) -> CameraHealth:
        """Truthful per-camera health, shared by ``get_status`` and the monitor.

        ``online``: connected and a frame arrived within ``data_timeout``. ``stale``: connected
        but no recent frames. ``connecting``: a reconnect is in progress. ``offline``: not
        connected (never connected, unplugged, or between reconnect attempts).
        """
        with self._cameras_lock:
            reconnecting = cam_id in self._reconnecting
            camera = self.cameras.get(cam_id)
            last_t = self._last_frame_time.get(cam_id)
        return classify_health(
            reconnecting=reconnecting,
            camera_present=camera is not None,
            camera_ready=camera is not None and camera.cam_ready(),
            last_frame_age_s=None if last_t is None else time.time() - last_t,
            data_timeout=self.data_timeout,
        )

    def get_camera_state(self, cam_id: str) -> str:
        """String form of :meth:`get_camera_health` (``online`` / ``connecting`` / ...)."""
        return self.get_camera_health(cam_id).value

    def revive_camera(self, cam_id: str) -> bool:
        """
        Force an immediate reconnection attempt, skipping any backoff wait.

        Resets the camera's backoff then calls :meth:`_reconnect_camera` so
        the caller gets a synchronous result rather than waiting for the next
        health-check cycle.

        Args:
            cam_id: Camera identifier to revive

        Returns:
            True if the camera reconnected successfully, False otherwise
        """
        if cam_id not in self.config:
            logger.error("Cannot revive camera '%s': not in configuration", cam_id)
            return False
        logger.info("Reviving camera '%s' (resetting backoff)", cam_id)
        self.reset_reconnect_attempts(cam_id)
        return self._reconnect_camera(cam_id)

    def close_all(self) -> None:
        """
        Close all cameras and stop monitoring.

        Performs graceful shutdown of all camera instances and monitoring thread.
        """
        logger.info("Shutting down CameraManager")

        # Stop monitoring first
        self.stop_monitoring()

        with self._cameras_lock:
            cameras = list(self.cameras.items())
            self.cameras.clear()
            self._last_frame_time.clear()
            self._backoffs.clear()

        # Each camera exits its pipeline and closes its device (idempotent).
        for cam_id, camera in cameras:
            try:
                logger.debug("Closing camera '%s'", cam_id)
                camera.close(force=True)
            except Exception as e:
                logger.exception("Error closing camera '%s': %s", cam_id, e)

        logger.info("CameraManager shutdown complete")

    def __enter__(self) -> "CameraManager":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        """Context manager exit."""
        self.close_all()

    def __len__(self) -> int:
        """Return number of active cameras."""
        return len(self.cameras)

    def __contains__(self, cam_id: str) -> bool:
        """Check if camera ID exists in manager."""
        return cam_id in self.cameras
