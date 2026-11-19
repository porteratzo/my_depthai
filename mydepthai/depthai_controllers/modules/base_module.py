#!/usr/bin/env python3
"""
Base module for DepthAI pipeline components.

Provides a minimal interface for building modules that interact with the DepthAI pipeline,
including input/output queues and basic data exchange methods.
"""

from abc import ABC
from datetime import timedelta
from typing import Any, Dict, Optional

import depthai as dai

DEFAULT_TIMEOUT = timedelta(seconds=1 / 20)  # 20 fps


class BaseModule(ABC):  # noqa: B024
    """
    Abstract base for pipeline modules.

    Attributes:
        outputs: Mapping of output stream names to stream descriptors/metadata.
        inputs: Mapping of input stream names to stream descriptors/metadata.
        out_queues: Mapping of output stream names to DepthAI output queues.
        in_queues: Mapping of input stream names to DepthAI input queues.
    """

    def __init__(self) -> None:
        """Initialize empty I/O mappings and queues."""
        self.outputs: Dict[str, Any] = {}
        self.inputs: Dict[str, Any] = {}
        self.out_queues: Dict[str, dai.MessageQueue] = {}
        self.in_queues: Dict[str, dai.InputQueue] = {}

    def build(self, pipeline: Optional[dai.Pipeline] = None) -> None:  # noqa: B027
        """
        Create nodes and link I/O queues.

        Override in subclasses to create nodes, link streams, and populate:
        - self.out_queues for outputs
        - self.in_queues for inputs
        """
        pass

    def start_device(self) -> None:  # noqa: B027
        """
        Start any device-side processing for this module.

        Override in subclasses to perform device start logic (e.g., start encoders).
        """
        pass

    def send_data(self, output_type: str, data: Any) -> Any:
        """
        Send data to an input queue.

        Args:
            output_type: Name of the input stream to send to.
            data: DepthAI message or payload to send.

        Returns:
            Implementation-dependent value from DepthAI InputQueue.send (commonly bool/None).
        """
        return self.in_queues[output_type].send(data)

    def get_data(self, output_type: str, timeout: Optional[float] = None) -> Any:
        """
        Retrieve data from an output queue.

        Args:
            output_type: Name of the output stream to read from.
            timeout: Optional float for blocking time; if None, defaults to DEFAULT_TIMEOUT.
                ``0`` (or less) polls without blocking and returns ``None`` if nothing is queued.

        Returns:
            A DepthAI message read from the output queue.
        """
        if timeout is not None and timeout <= 0:
            # Non-blocking poll: returns None immediately when nothing is queued.
            return self.out_queues[output_type].tryGet()
        if timeout is not None:
            # convert float to datetime.timedelta
            timeout_delta = timedelta(seconds=timeout)
            return self.out_queues[output_type].get(timeout=timeout_delta)
        return self.out_queues[output_type].get(timeout=DEFAULT_TIMEOUT)

    def has_data(self, output_type: str) -> bool:
        """
        Check if an output queue currently has data available.

        Args:
            output_type: Name of the output stream.

        Returns:
            True if data is available, False otherwise.
        """
        return bool(self.out_queues[output_type].has())
