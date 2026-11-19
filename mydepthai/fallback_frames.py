"""
Fallback frame helpers for operator display.

These helpers create synthetic frames for UI display when a camera is offline
or not producing data. They are intentionally separate from frame-acquisition
so that None propagates cleanly to AI/processing code.

Call these explicitly only when you need to show something to an operator.
Never pass the result to anomaly detection or model inference.

Example::

    frame = camera_manager.get_frame(cam_id, cam_type, output_type)
    if frame is None:
        display = create_camera_error_frame(cam_id, "Camera offline")
        # Use display for the operator feed, not for AI processing

Performance note
----------------
The static parts of the frame (border, title, camera ID, reason text) are
pre-rendered once per ``(cam_id, reason, width, height)`` combination and
cached.  Only the timestamp is re-drawn on each call, keeping CPU usage low
even when many cameras are offline and frames are published at high frequency.
"""

import time
from typing import Dict, Tuple

import cv2
import numpy as np

# Cache key: (cam_id, reason, width, height)
_TemplateKey = Tuple[str, str, int, int]
_template_cache: Dict[_TemplateKey, np.ndarray] = {}


def _build_template(cam_id: str, reason: str, width: int, height: int) -> np.ndarray:
    """
    Render the static portions of a fallback frame (everything except the
    timestamp) and return it as a BGR numpy array.

    This is called once per unique ``(cam_id, reason, width, height)``
    combination; the result is stored in ``_template_cache``.
    """
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    font = cv2.FONT_HERSHEY_SIMPLEX

    # Red border
    border = 4
    cv2.rectangle(
        frame,
        (border, border),
        (width - border - 1, height - border - 1),
        (0, 0, 200),
        border,
    )

    # Large "CAMERA OFFLINE" title
    title = "CAMERA OFFLINE"
    title_scale = max(0.6, min(width / 480.0, 1.2))
    (tw, th), _ = cv2.getTextSize(title, font, title_scale, 2)
    cv2.putText(
        frame,
        title,
        ((width - tw) // 2, height // 2 - th // 2),
        font,
        title_scale,
        (0, 0, 255),
        2,
        cv2.LINE_AA,
    )

    # Camera ID line
    id_text = f"ID: {cam_id}"
    id_scale = max(0.4, min(width / 640.0, 0.7))
    (iw, ih), _ = cv2.getTextSize(id_text, font, id_scale, 1)
    id_y = height // 2 + th // 2 + ih + 8
    cv2.putText(
        frame,
        id_text,
        ((width - iw) // 2, id_y),
        font,
        id_scale,
        (160, 160, 160),
        1,
        cv2.LINE_AA,
    )

    # Reason text, word-wrapped
    reason_scale = max(0.35, min(width / 800.0, 0.55))
    max_line_width = width - 40
    words = reason.split()
    lines = []
    current: list = []
    for word in words:
        test = " ".join(current + [word])
        (lw, _), _ = cv2.getTextSize(test, font, reason_scale, 1)
        if lw <= max_line_width:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))

    y = id_y + 20
    for line in lines:
        (lw, lh), _ = cv2.getTextSize(line, font, reason_scale, 1)
        cv2.putText(
            frame,
            line,
            ((width - lw) // 2, y),
            font,
            reason_scale,
            (80, 80, 200),
            1,
            cv2.LINE_AA,
        )
        y += lh + 6

    return frame


def create_camera_error_frame(
    cam_id: str,
    reason: str = "Camera offline",
    size: Tuple[int, int] = (640, 480),
) -> np.ndarray:
    """
    Create a visually distinct fallback frame for operator display.

    The frame has a black background, red border, large "CAMERA OFFLINE"
    title, the camera ID, a reason string, and a live timestamp.  It is
    intentionally styled so operators immediately recognise it as synthetic.

    Static content is generated once per unique ``(cam_id, reason, size)``
    combination and cached; only the timestamp is re-rendered on each call.

    This function must only be used for display purposes.  The returned array
    must never be passed to AI/anomaly-detection code.

    Args:
        cam_id: Camera identifier shown on the frame.
        reason: Human-readable reason string (e.g. "Connection timeout").
        size: Output frame size as ``(width, height)``.  Defaults to 640×480.

    Returns:
        BGR numpy array suitable for ``cv2.imshow`` or JPEG encoding.
    """
    width, height = size
    cache_key: _TemplateKey = (cam_id, reason, width, height)

    if cache_key not in _template_cache:
        _template_cache[cache_key] = _build_template(cam_id, reason, width, height)

    # Copy the cached template so we can safely add the mutable timestamp
    frame = _template_cache[cache_key].copy()

    # Timestamp bottom-left (the only element that changes each call)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    cv2.putText(
        frame,
        timestamp,
        (10, height - 10),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.35,
        (60, 60, 60),
        1,
    )

    return frame


def clear_fallback_cache() -> None:
    """
    Clear the pre-rendered template cache.

    Call this if camera IDs or frame sizes change at runtime to avoid serving
    stale templates.
    """
    _template_cache.clear()
