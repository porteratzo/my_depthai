#!/usr/bin/env python3
"""
Camera configuration module for DepthAI.

This module provides an interface for configuring camera settings such as exposure,
focus, and anti-banding modes using the DepthAI SDK.
"""

import logging
from typing import Any, Dict, Optional

import depthai as dai
import numpy as np

logger = logging.getLogger(__name__)


class CameraConfigModule:
    """
    Configuration class for DepthAI cameras.

    Attributes:
        controlQueue: Input queue for sending camera control commands.
        _state: Last-commanded control state. depthai does not report control
            mode back from hardware, so "is AE/AF currently on" and the other
            indicators exposed to the UI are tracked purely as last-commanded
            software state here, updated by every setter in this class.
    """

    def __init__(self) -> None:
        """Initialize the config_cam with an empty control queue."""
        self.controlQueue: Optional[dai.InputQueue] = None
        self._state: Dict[str, Any] = {
            "auto_exposure_enabled": True,
            "auto_focus_enabled": True,
            "af_mode": "CONTINUOUS_VIDEO",
            "ae_lock": False,
            "exposure_compensation": 0,
            "exposure_limit_us": 33333,  # max == "no cap"; depthai has no "clear limit" API
            "manual_exposure_us": None,
            "manual_iso": None,
            "af_lens_range": None,  # (infinity_pos, macro_pos) or None = "not set"
            "af_region": None,  # (x, y, w, h) or None = "not set"
            "ae_region": None,  # (x, y, w, h) or None = "not set"
            "manual_lens_position": None,
        }

    def _update_state(self, **kwargs: Any) -> None:
        """Merge kwargs into the last-commanded control state."""
        self._state.update(kwargs)

    def get_control_state(self) -> Dict[str, Any]:
        """Return a shallow copy of the last-commanded control state."""
        return dict(self._state)

    def set_control_queue(self, queue: dai.InputQueue) -> None:
        """Set the control queue for sending camera control commands.

        Args:
            queue: A DepthAI InputQueue to send control commands to.
        """
        self.controlQueue = queue

    def try_set_control_queue(self, ctrl: dai.CameraControl) -> None:
        """Set the control queue for sending camera control commands.

        Args:
            ctrl: A DepthAI CameraControl object to send control commands.
        """
        if self.controlQueue is not None:
            self.controlQueue.send(ctrl)
        else:
            logger.warning("Control queue not set. Cannot send control signal.")

    def trigger_still(self) -> None:
        """
        Trigger a still image capture.

        depthai v3's Camera node has no gated still-capture output, so
        CameraControl.setCaptureStill is a no-op here. Instead this sends a
        trigger message to the Script node set up by camera_module's "still"
        output, which forwards the next full-resolution frame on receipt.
        """
        trigger_queue = getattr(self, "in_queues", {}).get("still")
        if trigger_queue is not None:
            trigger_queue.send(dai.Buffer())
        else:
            logger.warning("Still trigger queue not set. Cannot trigger still capture.")

    def set_antibanding(self, option: int = 0) -> None:
        """
        Set the anti-banding mode for the camera.

        Args:
            option: Index of the anti-banding mode to set (default: 0).
        """
        anti_banding_mode = [
            item for name, item in vars(dai.CameraControl.AntiBandingMode).items() if name.isupper()
        ]
        abm = anti_banding_mode[option]
        logger.debug("Anti-banding mode: %s", abm)
        ctrl = dai.CameraControl()
        ctrl.setAntiBandingMode(abm)
        self.try_set_control_queue(ctrl)

    def set_exposure(self, expTime_us: float, sensIso: int = 100) -> None:
        """
        Set manual exposure time and ISO sensitivity.

        Args:
            expTime_us: Exposure time in microseconds.
            sensIso: ISO sensitivity (default: 100).
        """
        # microsecond 10e-6
        expTime_us = int(np.clip(expTime_us, 1, 33333))
        # depthai's pybind signature takes ints and rejects floats / np.float64.
        sensIso = int(np.clip(sensIso, 100, 1600))
        logger.debug("Setting manual exposure: time=%d µs, iso=%d", expTime_us, sensIso)
        ctrl = dai.CameraControl()
        ctrl.setManualExposure(expTime_us, sensIso)
        self.try_set_control_queue(ctrl)
        self._update_state(auto_exposure_enabled=False, manual_exposure_us=expTime_us, manual_iso=sensIso)

    def set_auto_exposure(self) -> None:
        """Set the camera to auto exposure mode."""
        ctrl = dai.CameraControl()
        ctrl.setAutoExposureEnable()
        self.try_set_control_queue(ctrl)
        self._update_state(auto_exposure_enabled=True, manual_exposure_us=None, manual_iso=None)

    def set_focus(self, lens_step: int, distance: bool = False) -> None:
        """
        Set the focus length of the camera.

        Args:
            lens_step: Focus position or distance to focus on.
            distance: If True, interpret lens_step as a distance in meters (default: False).
        """
        # set focus length
        lensMin = 1
        lensMax = 254
        if distance:

            def get_lens_position(dist: float) -> int:
                dist = dist * 1000
                return int(150 - dist * 0.0242 + 0.00000412 * dist**2)

            lensPos = get_lens_position(lens_step)
        else:
            lensPos = lens_step
        # int(): depthai rejects floats / np.float64 for the lens position.
        lensPos = int(np.clip(lensPos, lensMin, lensMax))
        ctrl = dai.CameraControl()
        ctrl.setManualFocus(lensPos)
        self.try_set_control_queue(ctrl)
        self._update_state(auto_focus_enabled=False, af_mode="MANUAL", manual_lens_position=lensPos)

    def set_Focus_Region(self, x: int, y: int, w: int, h: int) -> None:
        """
        Set the region for auto-focus.

        Args:
            x: X-coordinate of the region.
            y: Y-coordinate of the region.
            w: Width of the region.
            h: Height of the region.
        """
        x, y, w, h = (int(round(v)) for v in (x, y, w, h))  # depthai rejects floats
        ctrl = dai.CameraControl()
        ctrl.setAutoFocusRegion(x, y, w, h)
        self.try_set_control_queue(ctrl)
        self._update_state(af_region=(x, y, w, h))

    def set_Exposure_Region(self, x: int, y: int, w: int, h: int) -> None:
        """
        Set the region for auto-exposure.

        Args:
            x: X-coordinate of the region.
            y: Y-coordinate of the region.
            w: Width of the region.
            h: Height of the region.
        """
        x, y, w, h = (int(round(v)) for v in (x, y, w, h))  # depthai rejects floats
        ctrl = dai.CameraControl()
        ctrl.setAutoExposureRegion(x, y, w, h)
        self.try_set_control_queue(ctrl)
        self._update_state(ae_region=(x, y, w, h))

    # focus mode functions
    def trigger_auto_focus(self) -> None:
        """Trigger auto-focus mode."""
        ctrl = dai.CameraControl()
        ctrl.setAutoFocusMode(dai.CameraControl.AutoFocusMode.AUTO)
        ctrl.setAutoFocusTrigger()
        self.try_set_control_queue(ctrl)
        self._update_state(auto_focus_enabled=True, af_mode="AUTO", manual_lens_position=None)

    def trigger_auto_focus_macro(self) -> None:
        """Trigger macro auto-focus mode."""
        ctrl = dai.CameraControl()
        ctrl.setAutoFocusMode(dai.CameraControl.AutoFocusMode.MACRO)
        self.try_set_control_queue(ctrl)
        self._update_state(auto_focus_enabled=True, af_mode="MACRO", manual_lens_position=None)

    def trigger_auto_focus_EDOF(self) -> None:
        """Trigger extended depth of field (EDOF) auto-focus mode."""
        ctrl = dai.CameraControl()
        ctrl.setAutoFocusMode(dai.CameraControl.AutoFocusMode.EDOF)
        self.try_set_control_queue(ctrl)
        self._update_state(auto_focus_enabled=True, af_mode="EDOF", manual_lens_position=None)

    def continuous_auto_focus(self) -> None:
        """Set the camera to continuous video auto-focus mode."""
        ctrl = dai.CameraControl()
        ctrl.setAutoFocusMode(dai.CameraControl.AutoFocusMode.CONTINUOUS_VIDEO)
        self.try_set_control_queue(ctrl)
        self._update_state(auto_focus_enabled=True, af_mode="CONTINUOUS_VIDEO", manual_lens_position=None)

    def continuous_pic_auto_focus(self) -> None:
        """Set the camera to continuous picture auto-focus mode."""
        ctrl = dai.CameraControl()
        ctrl.setAutoFocusMode(dai.CameraControl.AutoFocusMode.CONTINUOUS_PICTURE)
        self.try_set_control_queue(ctrl)
        self._update_state(auto_focus_enabled=True, af_mode="CONTINUOUS_PICTURE", manual_lens_position=None)

    def set_auto_exposure_lock(self, locked: bool) -> None:
        """
        Lock or unlock the auto-exposure algorithm, keeping its latest sensor config.

        Args:
            locked: True to stop the AE algorithm (keeping current exposure/ISO),
                False to let it continue adjusting.
        """
        locked = bool(locked)
        ctrl = dai.CameraControl()
        ctrl.setAutoExposureLock(locked)
        self.try_set_control_queue(ctrl)
        self._update_state(ae_lock=locked)

    def set_exposure_compensation(self, value: int) -> None:
        """
        Set auto-exposure compensation, biasing the AE brightness target.

        Args:
            value: Compensation in [-9, 9]; positive brightens, negative darkens.
        """
        value = int(np.clip(value, -9, 9))
        ctrl = dai.CameraControl()
        ctrl.setAutoExposureCompensation(value)
        self.try_set_control_queue(ctrl)
        self._update_state(exposure_compensation=value)

    def set_auto_exposure_limit(self, max_us: float) -> None:
        """
        Cap the maximum exposure time the AE algorithm may use.

        Args:
            max_us: Maximum exposure time in microseconds, clipped to [1, 33333].
                There is no depthai API to clear this limit; set it back to the
                max (33333) to effectively remove the cap.
        """
        max_us = int(np.clip(max_us, 1, 33333))
        ctrl = dai.CameraControl()
        ctrl.setAutoExposureLimit(max_us)
        self.try_set_control_queue(ctrl)
        self._update_state(exposure_limit_us=max_us)

    def set_auto_focus_lens_range(self, infinity_position: int, macro_position: int) -> None:
        """
        Constrain the autofocus lens travel range.

        Args:
            infinity_position: Lens position for the infinity end, in [0, 255].
            macro_position: Lens position for the macro end, in [0, 255].

        Raises:
            ValueError: If infinity_position is not strictly less than macro_position,
                as required by the DepthAI CameraControl API.
        """
        infinity_position = int(np.clip(infinity_position, 0, 255))
        macro_position = int(np.clip(macro_position, 0, 255))
        if infinity_position >= macro_position:
            raise ValueError(
                f"infinity_position ({infinity_position}) must be < macro_position " f"({macro_position})"
            )
        ctrl = dai.CameraControl()
        ctrl.setAutoFocusLensRange(infinity_position, macro_position)
        self.try_set_control_queue(ctrl)
        self._update_state(af_lens_range=(infinity_position, macro_position))
