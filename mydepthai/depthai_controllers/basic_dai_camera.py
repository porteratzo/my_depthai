#!/usr/bin/env python3
"""
Basic DepthAI Camera Module

This module provides a simplified interface for controlling DepthAI cameras without
using the OAK SDK. It supports basic camera operations including preview, video,
and still image capture.

Usage:
    echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="03e7", MODE="0666"' |
      sudo tee /etc/udev/rules.d/80-movidius.rules
    sudo udevadm control --reload-rules && sudo udevadm trigger
"""

import logging
import re
import time
from contextlib import ExitStack
from threading import Lock, Thread
from typing import Any, Dict, List, Literal, Optional, Tuple, cast

import depthai as dai
import numpy as np

from .modules.camera_module import CameraModule

STILL_WAIT_TIME = 0.5

logger = logging.getLogger(__name__)


def _is_ip_address(s: str) -> bool:
    """Return True if *s* looks like an IPv4 address."""
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", s))


def reconnect_callback(x: dai.Device.ReconnectionStatus) -> None:
    logger.info("DepthAI SDK reconnection state: %s", x)


class BasicDAICamera:
    """
    Basic DepthAI Camera Controller

    A simplified interface for managing DepthAI cameras with support for multiple
    camera types and output streams. Does not use the OAK SDK wrapper.

    Attributes:
        speed: Whether to use USB SuperSpeed
        device_info: Information about the connected device
        device: DepthAI Device instance
        pipeline: The DepthAI pipeline
        connected: Whether the device is successfully connected
        components: Dictionary of camera modules
    """

    speed: bool
    connected: bool
    connection_error: Optional[Exception]
    device: Optional[dai.Device]
    pipeline: Optional[dai.Pipeline]
    exit_stack: Optional[ExitStack]

    def thread_get_device(self) -> None:
        """
        Thread worker function to connect to the DepthAI device.

        Attempts to connect to the device using the specified USB speed and
        creates the pipeline. Updates the connected status based on success or failure.
        """
        # A previous attempt may have left an open device/pipeline behind (e.g. the pipeline
        # failed after the device opened, or the link dropped). Release it first: a still-open
        # handle can make the next connect to the same device fail with "device in use".
        self._release_handles()
        try:
            try:
                if _is_ip_address(self.device_info.name):
                    self.device = dai.Device(
                        self.device_info,
                    )
                else:
                    self.device = dai.Device(
                        self.device_info,
                        maxUsbSpeed=dai.UsbSpeed.SUPER if self.speed else dai.UsbSpeed.HIGH,
                    )

            except RuntimeError as e:
                logger.warning(
                    "Connection failed [%s] with %s, retrying with USB 2.0... (%s)",
                    type(e).__name__,
                    "SuperSpeed" if self.speed else "HighSpeed",
                    e,
                )
                self.device = dai.Device(
                    self.device_info,
                    maxUsbSpeed=dai.UsbSpeed.HIGH,
                )
            self._device_open = True
            self.device.setMaxReconnectionAttempts(3, reconnect_callback)
            self.pipeline = dai.Pipeline(self.device)
            self.pipeline.__enter__()
            self._pipeline_entered = True
            if self.exit_stack is not None:
                # The caller's stack owns teardown. The callback is idempotent, so a forced
                # close() followed by the stack unwinding never exits the pipeline twice.
                self.exit_stack.callback(self._release_handles)
            self.connected = True
        except RuntimeError as e:
            logger.error("Could not connect to device %s: %s", getattr(self, "device_info", "unknown"), e)
            self.connected = False
            self.connection_error = e

    def __init__(
        self, identifier: int | str = 0, speed: bool = False, exit_stack: Optional[ExitStack] = None
    ) -> None:
        """
        Initialize the BasicDAICamera with device info and connection settings.

        Args:
            identifier: Index of the device to connect to or MXID string (default: 0)
            speed: Whether to use USB SuperSpeed (True) or High Speed (False, default)
            exit_stack: Optional ExitStack for context management (default: None)
        """
        self.speed = speed
        self.connected = False
        self.connection_error = None
        self.device = None
        self.pipeline = None
        self._pipeline_entered = False
        self._device_open = False
        self._release_lock = Lock()
        # set camera and IO nodes and link them
        if isinstance(identifier, int):
            DEVICE_INFOS = dai.Device.getAllAvailableDevices()
            if identifier >= len(DEVICE_INFOS):
                raise IndexError(
                    f"Device index {identifier} out of range — " f"only {len(DEVICE_INFOS)} device(s) found"
                )
            self.device_info: dai.DeviceInfo = DEVICE_INFOS[identifier]
        elif isinstance(identifier, str) and _is_ip_address(identifier):
            # PoE / network camera — connect directly by IP address
            self.device_info = dai.DeviceInfo(identifier)
        elif isinstance(identifier, str):
            all_devs = dai.Device.getAllAvailableDevices()
            for dev in all_devs:
                if dev.getDeviceId() == identifier:
                    self.device_info = dev
                    break
            else:
                raise ValueError(
                    f"Device with MXID {identifier!r} not found. "
                    f"Available devices: {[d.getDeviceId() for d in all_devs]}"
                )

        self.exit_stack = exit_stack

        logger.info("Connecting to %s", self.device_info)
        self.aquire_thread: Thread = Thread(target=self.thread_get_device, args=())
        self.aquire_thread.start()
        self.components: Dict[str, CameraModule] = {}

    def reconnect(self, timeout: float = 10.0) -> None:
        """
        Attempt to reconnect to the DepthAI device.

        Joins any existing connection thread before starting a new one so that
        only one connection thread is ever active at a time.
        Args:
            timeout: Time to wait for existing connection
              thread to finish before starting a new one (default: 10 seconds)
        """
        self.connected = False
        self.connection_error = None
        if self.aquire_thread.is_alive():
            self.aquire_thread.join(timeout=timeout)
            if self.aquire_thread.is_alive():
                logger.warning(
                    "Previous connection thread did not finish within join timeout; "
                    "starting new thread anyway. This may indicate a hung DepthAI call."
                )
        if not self.connected:
            self.aquire_thread = Thread(target=self.thread_get_device, args=())
            self.aquire_thread.start()
        else:
            logger.info("Already connected, no need to reconnect.")

    def cam_ready(self) -> bool:
        """
        Check if the camera device is connected and ready.

        Returns:
            True if the device is connected, False otherwise. A device the SDK reports as
            closed (unplugged, or its link dropped) clears the flag, so this is truthful
            after a disconnect and not just after the first successful connect.
        """
        if self.connected and self.device is not None:
            try:
                if self.device.isClosed() is True:
                    self.connected = False
            except Exception:  # a dead handle may raise; treat as not-closed-info, keep flag
                pass
        return self.connected

    def wait_for_cam(
        self, timeout: float = 10.0, reconnect_on_timeout: bool = True, max_retries: int = 2
    ) -> bool:
        """
        Wait for the camera device to become ready within a specified timeout.

        Args:
            timeout: This is the total time to wait for the device to become ready before giving up
            (default: 10 seconds)
                     retying will occur every timeout/max_retries seconds if reconnect_on_timeout is True
            reconnect_on_timeout: Whether to attempt reconnection if the device is not ready within
              the timeout (default: True)
            max_retries: Maximum number of reconnection attempts before giving up (default: 2)
        Returns:
            True if the device became ready, False if all attempts exhausted.
        """
        start_time = time.time()
        hard_start_time = start_time
        tries = 0
        last_seen_error = None

        while not self.cam_ready():
            now = time.time()

            if (now - hard_start_time) > timeout:
                logger.error("Failed to connect to device within timeout: %s seconds.", timeout)
                return False

            # Error branch — takes priority; use elif so only one reconnect fires per iteration
            current_error = self.connection_error
            if current_error is not None and current_error is not last_seen_error:
                last_seen_error = current_error
                logger.error("Connection error on camera %s: %s", self.device_info, current_error)
                if reconnect_on_timeout:
                    tries += 1
                    if tries >= max_retries:
                        logger.warning(
                            "Max reconnection attempts (%d) reached after error. Giving up.",
                            max_retries,
                        )
                        return False
                    logger.info("Reconnecting after error (attempt %d/%d)...", tries, max_retries)
                    self.reconnect()
                    start_time = time.time()

            # Timeout branch — elif prevents double-reconnect when error also fired above
            elif reconnect_on_timeout and (now - start_time) > (timeout / max_retries):
                tries += 1
                if tries >= max_retries:
                    logger.warning(
                        "Max reconnection attempts (%d) reached after timeout: %.1fs. Giving up.",
                        max_retries,
                        timeout / max_retries,
                    )
                    return False
                logger.warning(
                    "Timeout waiting for camera, reconnecting (attempt %d/%d)...",
                    tries,
                    max_retries,
                )
                self.reconnect()
                start_time = time.time()

            time.sleep(0.1)
        return True

    def create_camera(
        self,
        cam_type: Literal["color", "left", "right"] = "color",
        outputs: Optional[List[Literal["preview", "still", "video", "fullvideo"]]] = None,
        sizes: Optional[List[Optional[Tuple[int, int]]]] = None,
        fpss: Optional[List[Optional[int]]] = None,
    ) -> None:
        """
        Create a camera component with specified outputs.

        Args:
            cam_type: Type of camera to create ("color", "left", or "right", default: "color")
            outputs: List of output types to enable (default: ["preview", "video"])
            sizes: List of output resolutions as (width, height) tuples. None uses defaults.
            fpss: List of FPS values for each output. None uses defaults.

        Raises:
            AssertionError: If a camera of this type already exists
        """
        # create cameracomponent
        self.aquire_thread.join()
        assert cam_type not in self.components.keys(), "node already created"
        self.components[cam_type] = CameraModule(
            cam_type=cam_type,
        )

        if outputs is None:
            outputs = ["preview", "video"]
        if sizes is None:
            sizes = [None] * len(outputs)
        if fpss is None:
            fpss = [None] * len(outputs)
        for output, size, fps in zip(outputs, sizes, fpss):
            self.components[cam_type].add_output(output, size=size, fps=fps)

    def create_output(
        self,
        cam_type: str,
        output_type: Literal["preview", "still", "video", "fullvideo"],
        size: Optional[Tuple[int, int]] = None,
        fps: Optional[int] = None,
    ) -> None:
        """
        Create an additional output for an existing camera component.

        Args:
            cam_type: Camera identifier ("color", "left", or "right")
            output_type: Type of output to create ("preview", "video", or "still")
            size: Resolution as (width, height). None uses defaults.
            fps: Frames per second. None uses defaults.
        """
        self.components[cam_type].add_output(output_type, size=size, fps=fps)

    def build(self) -> None:
        """
        Build the DepthAI pipeline with all configured camera components.

        Calls the build method on all registered camera components.
        """
        for node_type in self.components.keys():
            if (node_type in ["color", "left", "right", "depth"]) or node_type.find("detector_") != -1:
                assert self.pipeline is not None, "pipeline must be initialized before build"
                self.components[node_type].build(self.pipeline)

    def start_device(self) -> None:
        """
        Start the device pipeline and all configured components.

        Initializes all camera components and prints device information including
        MXID, connected cameras, and USB speed.
        """
        if self.connected:
            assert self.pipeline is not None
            assert self.device is not None
            self.pipeline.start()
            for node_type in self.components.keys():
                if (node_type in ["color", "left", "right", "depth"]) or node_type.find("detector_") != -1:
                    self.components[node_type].start_device()

            mxid = self.device.getMxId()
            cameras = self.device.getConnectedCameras()
            usb_speed = self.device.getUsbSpeed()
            logger.info("MXID: %s", mxid)
            logger.info("Cameras: %s", ", ".join(c.name for c in cameras))
            logger.info("USB speed: %s", usb_speed.name)

    def get_frame(
        self, cam: str = "color", out_type: str = "preview", timeout: Optional[float] = None
    ) -> np.ndarray:
        """
        Get a frame from the specified camera and output type.

        Args:
            cam: Camera identifier ("color", "left", or "right", default: "color")
            out_type: Output type (e.g., "preview", "video", default: "preview")
            timeout: Seconds to block waiting for a frame. If ``None`` (default),
                auto-computed as ``4 / fps`` for the requested output.

        Returns:
            Frame as a numpy array (OpenCV format)
        """
        # get either fast preview or slower video frame
        if cam in ["color", "left", "right"]:
            if timeout is None:
                timeout = 4 / self.components[cam].outputs_config[out_type]["fps"]
            frame = self.components[cam].get_frame(out_type, timeout=timeout)
            assert frame is not None, f"get_frame returned None for cam={cam}, out_type={out_type}"
            return frame
        raise ValueError(f"Unknown cam: {cam}")

    def get_frame_with_metadata(
        self, cam: str = "color", out_type: str = "preview", timeout: Optional[float] = None
    ) -> Optional[Tuple[np.ndarray, Dict[str, Any]]]:
        """
        Get a frame plus control metadata from the specified camera and output type.

        Args:
            cam: Camera identifier ("color", "left", or "right", default: "color")
            out_type: Output type (e.g., "preview", "video", default: "preview")
            timeout: Seconds to block waiting for a frame. If ``None`` (default),
                auto-computed as ``4 / fps`` for the requested output.

        Returns:
            A ``(frame, metadata)`` tuple, or ``None`` if unavailable. Unlike
            :meth:`get_frame`, this does not assert on a miss — callers use the
            ``None`` return to distinguish "no frame yet" without an exception.
        """
        if cam in ["color", "left", "right"]:
            if timeout is None:
                timeout = 4 / self.components[cam].outputs_config[out_type]["fps"]
            return self.components[cam].get_frame_with_metadata(out_type, timeout=timeout)
        raise ValueError(f"Unknown cam: {cam}")

    def get_data(self, cam: str = "color", out_type: str = "preview") -> Any:
        """
        Get raw data from the specified camera and output type.

        Args:
            cam: Camera identifier ("color", "left", or "right", default: "color")
            out_type: Output type (e.g., "preview", "video", default: "preview")

        Returns:
            Raw ImgFrame data object from the queue
        """
        # get either fast preview or slower video frame
        if cam in ["color", "left", "right"]:
            return self.components[cam].get_data(out_type, timeout=True)
        raise ValueError(f"Unknown cam: {cam}")

    def get_still(self) -> Optional[np.ndarray]:
        """
        Get a still image from the color camera (blocking).

        Polls for a still image up to ``STILL_WAIT_TIME * 6`` seconds total.
        Returns ``None`` if no image arrives within the timeout — callers that
        need to show something to an operator should use
        :func:`mydepthai.fallback_frames.create_camera_error_frame` explicitly.

        Returns:
            Still image as a numpy array, or ``None`` on timeout.

        Raises:
            AssertionError: If color camera or still output is not configured.
        """
        assert "color" in self.components.keys(), "create a color camera with still output"
        assert (
            "still" in self.components["color"].outputs_config.keys()
        ), "create a color camera with still output"

        for _ in range(6):
            if self.components["color"].has_data("still"):
                frame: dai.ImgFrame = self.components["color"].get_data("still", timeout=2)
                return cast(np.ndarray, frame.getCvFrame())
            time.sleep(STILL_WAIT_TIME)

        logger.warning("Still image capture timed out")
        return None

    def get_still_no_block(self) -> Optional[np.ndarray]:
        """
        Get a still image from the color camera (non-blocking).

        Returns the image immediately if one is queued, otherwise ``None``.
        Callers that need to display something to operators should use
        :func:`mydepthai.fallback_frames.create_camera_error_frame` explicitly.

        Returns:
            Still image as a numpy array, or ``None`` if no image is queued.

        Raises:
            AssertionError: If color camera or still output is not configured.
        """
        assert "color" in self.components.keys(), "create a color camera with still output"
        assert (
            "still" in self.components["color"].outputs_config.keys()
        ), "create a color camera with still output"

        if self.components["color"].has_data("still"):
            frame: dai.ImgFrame = self.components["color"].get_data("still")
            return cast(np.ndarray, frame.getCvFrame())
        return None

    def trigger_still(self) -> None:
        """
        Trigger a still image capture on the color camera.

        Raises:
            AssertionError: If color camera or still output is not configured
        """
        assert "color" in self.components.keys(), "create a color camera with still output"
        assert (
            "still" in self.components["color"].outputs_config.keys()
        ), "create a color camera with still output"
        self.components["color"].trigger_still()

    def trigger_and_get_still(self) -> Optional[np.ndarray]:
        """
        Trigger a still capture and return the captured image.

        Convenience method that triggers a still capture, waits briefly,
        and then retrieves the still image.

        Returns:
            Still image as a numpy array, or None on timeout
        """
        # trigger still shot and return the still
        self.trigger_still()
        time.sleep(0.1)
        return self.get_still()

    def _release_handles(self) -> None:
        """Exit the pipeline and close the device, each at most once (idempotent, thread-safe).

        Order matters: the pipeline is stopped before the device it runs on is closed. Errors
        are logged and swallowed so one failing handle cannot leak the other.
        """
        with self._release_lock:
            if self._pipeline_entered and self.pipeline is not None:
                self._pipeline_entered = False
                try:
                    self.pipeline.__exit__(None, None, None)
                except Exception as e:
                    logger.warning("Error exiting pipeline for %s: %s", self.device_info, e)
            if self._device_open and self.device is not None:
                self._device_open = False
                try:
                    self.device.close()
                except Exception as e:
                    logger.warning("Error closing device %s: %s", self.device_info, e)

    def close(self, force: bool = False) -> None:
        """
        Close the pipeline and the device. Safe to call more than once.

        When an ``exit_stack`` was supplied it owns teardown and a plain ``close()`` is a
        no-op; ``force=True`` tears down now and the stack's later unwind does nothing.
        """
        if self.exit_stack is not None and not force:
            return
        self._release_handles()
