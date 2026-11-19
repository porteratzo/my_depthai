#!/usr/bin/env python3
"""
Camera module for DepthAI.

This module provides a comprehensive interface for configuring and managing camera
outputs on DepthAI devices. It supports multiple output types (preview, video, still)
and camera types (color, left, right).
"""

import logging
from typing import Any, Dict, Literal, Optional, Tuple

import depthai as dai

from .camera_config_module import CameraConfigModule
from .camera_frame_module import CameraFrameModule

logger = logging.getLogger(__name__)

# Default queue depth. Non-blocking queues drop the oldest frame when full,
# which is correct for real-time inspection — we want the *latest* frame,
# not a growing backlog that adds latency.
_DEFAULT_QUEUE_SIZE = 4


class CameraModule(CameraFrameModule, CameraConfigModule):
    """
    Camera module combining frame retrieval and configuration capabilities.

    Inherits from CameraFrameModule for frame operations and CameraConfigModule
    for camera control settings.

    Attributes:
        cam_type: Type of camera ("color", "left", or "right").
        outputs: Dictionary mapping output types to their configurations.
        pipeline: The DepthAI pipeline this camera belongs to.
        node: The DepthAI Camera node instance.
        controlQueue: Input queue for sending camera control commands.
    """

    def __init__(
        self,
        cam_type: Literal["color", "left", "right"] = "color",
    ) -> None:
        """
        Initialize the camera module.

        Args:
            cam_type: Type of camera to create ("color", "left", or "right", default: "color").

        Raises:
            AssertionError: If cam_type is not one of the supported camera types.
        """
        CameraFrameModule.__init__(self)
        CameraConfigModule.__init__(self)
        assert cam_type in ["color", "left", "right"], "unsupported camera type"
        self.cam_type: str = cam_type
        self.outputs_config: Dict[str, Dict[str, Any]] = {}
        self.pipeline: Optional[dai.Pipeline] = None
        self.node: Optional[dai.node.Camera] = None

    def add_output(
        self,
        output_type: Literal["preview", "still", "video", "fullvideo"],
        size: Optional[Tuple[int, int]] = None,
        fps: Optional[int] = None,
    ) -> None:
        """
        Add an output stream to the camera.

        Args:
            output_type: Type of output ("preview", "still", or "video").
            size: Resolution of the output as (width, height). Uses defaults if None.
            fps: Frames per second for the output. Uses defaults if None.

        Raises:
            AssertionError: If the output type has already been added.
            RuntimeError: If the output type is not supported.
        """
        # preview, still, video
        assert output_type not in self.outputs_config.keys(), "output already added"
        if output_type not in ["preview", "still", "video", "fullvideo"]:
            logger.error("Unsupported output type: %s", output_type)
            raise RuntimeError(f"Unsupported output type: {output_type}")
        self.outputs_config[output_type] = {"size": size, "fps": fps}

    def build(self, pipeline: Optional[dai.Pipeline] = None) -> None:
        """
        Build the camera node and configure outputs within the pipeline.

        Creates the camera node, configures all added outputs with their respective
        settings, and sets up the control queue for camera commands.

        Args:
            pipeline: The DepthAI pipeline to add this camera to.

        Raises:
            RuntimeError: If an unsupported camera type or output type is encountered.
        """
        self.pipeline = pipeline
        assert self.pipeline is not None, "pipeline must be provided to camera_module.build"
        if self.cam_type == "color":
            socket = dai.CameraBoardSocket.CAM_A
        elif self.cam_type == "left":
            socket = dai.CameraBoardSocket.CAM_B
        elif self.cam_type == "right":
            socket = dai.CameraBoardSocket.CAM_C
        else:
            logger.error("Unsupported camera type: %s", self.cam_type)
            raise RuntimeError(f"Unsupported camera type: {self.cam_type}")
        self.node = self.pipeline.create(dai.node.Camera).build(socket)
        self.controlQueue: dai.InputQueue = self.node.inputControl.createInputQueue()
        for output_type, kargs in self.outputs_config.items():
            size = kargs.get("size", None)
            fps = kargs.get("fps", None)
            if output_type == "preview":
                size = size if size is not None else (300, 300)
                fps = fps if fps is not None else 20
                out = self.node.requestOutput(
                    size=size, type=dai.ImgFrame.Type.NV12, fps=fps
                ).createOutputQueue()
            elif output_type == "video":
                size = size if size is not None else (1920, 1080)
                fps = fps if fps is not None else 20
                out = self.node.requestOutput(
                    size=size, type=dai.ImgFrame.Type.BGR888p, fps=fps
                ).createOutputQueue()
            elif output_type == "fullvideo":
                size = size if size is not None else (3840, 2160)
                fps = fps if fps is not None else 2
                out = self.node.requestOutput(
                    # size=size, type=dai.ImgFrame.Type.BGR888p, fps=fps,
                    size=size,
                    type=dai.ImgFrame.Type.NV12,
                    fps=fps,
                ).createOutputQueue()
            elif output_type == "still":
                # depthai v3's unified Camera node has no gated still-capture output
                # (the v2 ColorCamera.still + CameraControl.setCaptureStill mechanism
                # was dropped; setCaptureStill is a no-op here). Luxonis's own v3
                # replacement is a Script node that buffers the full-res stream and
                # forwards a frame only when it receives a host-sent trigger message
                # — see docs.luxonis.com "Camera still image at max resolution".
                if size is not None:
                    logger.warning(
                        "size is ignored for 'still' output; full resolution is "
                        "determined by the sensor via requestFullResolutionOutput()"
                    )
                size = None
                fps = fps if fps is not None else 2
                full_res = self.node.requestFullResolutionOutput(fps=fps)
                still_script = self.pipeline.create(dai.node.Script)
                full_res.link(still_script.inputs["in"])
                still_script.setScript("""
                    while True:
                        frame = node.inputs["in"].get()
                        if node.inputs["trigger"].tryGet() is not None:
                            node.io["still"].send(frame)
                    """)
                out = still_script.outputs["still"].createOutputQueue()
                self.in_queues["still"] = still_script.inputs["trigger"].createInputQueue()
            else:
                logger.error("Unsupported output type: %s", output_type)
                raise RuntimeError(f"Unsupported output type: {output_type}")
            # Non-blocking queue: drops the *oldest* frame when full so the
            # pipeline never stalls waiting for a slow consumer. This keeps
            # frame latency minimal, which is critical in a real-time
            # manufacturing inspection context.
            out.setMaxSize(_DEFAULT_QUEUE_SIZE)
            out.setBlocking(False)
            self.out_queues[output_type] = out

            self.outputs_config[output_type]["size"] = size
            self.outputs_config[output_type]["fps"] = fps
