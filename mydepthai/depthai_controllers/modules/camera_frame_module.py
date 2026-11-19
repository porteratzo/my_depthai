#!/usr/bin/env python3
"""
Camera frame module for DepthAI.

This module extends BaseModule to provide functionality for retrieving
image frames from the camera.
"""

from typing import Any, Dict, Optional, Tuple, cast

import depthai as dai
import numpy as np

from .base_module import BaseModule


class CameraFrameModule(BaseModule):
    """
    Camera frame module class.

    Inherits from BaseModule and provides methods to retrieve image frames
    from the camera's output.
    """

    def get_frame(self, out_type: str = "preview", timeout: Optional[float] = None) -> Optional[np.ndarray]:
        """
        Retrieve a frame from the camera.

        Args:             o
            out_type: The type of output to retrieve (default: "preview").
            timeout: Seconds to block waiting for data; if ``None``, uses the
                queue's default timeout (default: None).

        Returns:
            The retrieved image frame as an ImgFrame object.
        """
        frame: dai.ImgFrame = self.get_data(out_type, timeout=timeout)
        if frame is None:
            return None
        return cast(np.ndarray, frame.getCvFrame())

    def get_frame_with_metadata(
        self, out_type: str = "preview", timeout: Optional[float] = None
    ) -> Optional[Tuple[np.ndarray, Dict[str, Any]]]:
        """
        Retrieve a frame together with its hardware-reported control metadata.

        Performs exactly one ``get_data()`` call — output queues are consumed,
        not peekable, so a second read would block or return a different
        frame than the one whose metadata is needed.

        Args:
            out_type: The type of output to retrieve (default: "preview").
            timeout: Seconds to block waiting for data; if ``None``, uses the
                queue's default timeout (default: None).

        Returns:
            A ``(frame, metadata)`` tuple, or ``None`` if no frame is available.
            ``metadata`` keys: ``lens_position`` (int), ``exposure_time_us``
            (float), ``iso`` (int), ``frame_width`` (int), ``frame_height`` (int).
        """
        frame: dai.ImgFrame = self.get_data(out_type, timeout=timeout)
        if frame is None:
            return None
        cv_frame = cast(np.ndarray, frame.getCvFrame())
        metadata: Dict[str, Any] = {
            "lens_position": frame.getLensPosition(),
            "exposure_time_us": frame.getExposureTime().total_seconds() * 1e6,
            "iso": frame.getSensitivity(),
            "frame_width": int(cv_frame.shape[1]),
            "frame_height": int(cv_frame.shape[0]),
        }
        return cv_frame, metadata
