"""One definition of camera health, used by the status API and the monitor thread.

"Connected", "online" and "stale" used to be computed in three places with three meanings.
``classify_health`` is the single pure rule; ``CameraManager`` feeds it the live inputs
and both ``get_status`` and ``_monitor_health`` read the result.
"""

from enum import Enum
from typing import Optional


class CameraHealth(str, Enum):
    """Per-camera state. The string values are the ``state`` field on ``/api/cameras/status``."""

    ONLINE = "online"  # connected and a frame arrived within ``data_timeout``
    STALE = "stale"  # connected but no recent frames
    CONNECTING = "connecting"  # a reconnect is in progress
    OFFLINE = "offline"  # never connected, unplugged, or between reconnect attempts


def classify_health(
    *,
    reconnecting: bool,
    camera_present: bool,
    camera_ready: bool,
    last_frame_age_s: Optional[float],
    data_timeout: float,
) -> CameraHealth:
    """Classify a camera. Precedence: reconnecting > not present/ready > frame age."""
    if reconnecting:
        return CameraHealth.CONNECTING
    if not camera_present or not camera_ready:
        return CameraHealth.OFFLINE
    if last_frame_age_s is None or last_frame_age_s > data_timeout:
        return CameraHealth.STALE
    return CameraHealth.ONLINE
