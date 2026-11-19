"""(approved 2026-11-03): one camera's slow reconnect must not stall the others.

``CameraManager._monitor_health`` is a single thread and reconnects inline, and
``_reconnect_camera`` force-closes the old camera while holding ``_cameras_lock``. A PoE camera
whose reconnect takes tens of seconds (``wait_timeout`` x retries + ``warmup_time``) can therefore
(1) hold up the health check / reconnect of every other camera, and (2) block ``get_frame`` for
healthy cameras while the dead one is closing.

The tests drive the real monitor loop with a controllable "slow" camera instead of poking private
state, in the style of ``tests/robustness/test_circuit_breaker.py::TestRecoveryTiming``.
They were ``xfail(strict=True)`` while the bugs were open; the fix (reconnect dispatched off the
monitor thread, old camera closed outside ``_cameras_lock``) landed, so they are plain regression
tests now.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from mydepthai.camera_manager.camera_manager import CameraManager

pytestmark = pytest.mark.unit

WAIT_S = 2.0


def _manager(**kw: Any) -> CameraManager:
    cfg = {
        "slow": {"id": 0, "camera_type": "color", "outputs": ["preview"]},
        "fast": {"id": 1, "camera_type": "color", "outputs": ["preview"]},
    }
    return CameraManager(
        config_dict=cfg, threaded_init=False, auto_reconnect=True, health_check_interval=0.05, **kw
    )


def test_slow_reconnect_does_not_delay_another_cameras_reconnect() -> None:
    manager = _manager()
    release = threading.Event()
    started: Dict[str, float] = {}
    t0 = time.monotonic()

    def fake_init(cam_id: str, _cfg: Dict[str, Any], _ss: bool) -> bool:
        started[cam_id] = time.monotonic() - t0
        if cam_id == "slow":
            release.wait(WAIT_S + 3)  # a PoE camera stuck in wait_for_cam / warmup
        return False

    manager._initialize_single_camera = fake_init  # type: ignore[method-assign]
    manager.start_monitoring()
    try:
        deadline = time.monotonic() + WAIT_S
        while "fast" not in started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert "slow" in started, "the slow camera's reconnect never started; test is not exercising it"
        assert "fast" in started, f"'fast' reconnect never attempted while 'slow' was reconnecting: {started}"
    finally:
        release.set()
        manager.stop_monitoring()


def test_closing_a_dead_camera_does_not_block_frames_from_healthy_cameras() -> None:
    manager = _manager()
    release = threading.Event()
    closing = threading.Event()

    dead = MagicMock()
    dead.cam_ready.return_value = False

    def slow_close(force: bool = False) -> None:
        closing.set()
        release.wait(WAIT_S + 3)  # force-closing a wedged PoE device can take a long time

    dead.close.side_effect = slow_close
    healthy = MagicMock()
    healthy.cam_ready.return_value = True
    healthy.get_frame.return_value = object()
    manager.cameras = {"slow": dead, "fast": healthy}
    manager._last_frame_time = {"slow": 0.0, "fast": time.time()}
    manager._initialize_single_camera = lambda *a, **k: False  # type: ignore[method-assign]

    t = threading.Thread(target=manager._reconnect_camera, args=("slow",), daemon=True)
    t.start()
    results: List[Any] = []
    try:
        assert closing.wait(WAIT_S), "close() of the dead camera was never called"
        reader = threading.Thread(target=lambda: results.append(manager.get_frame("fast")), daemon=True)
        reader.start()
        reader.join(WAIT_S)
        assert not reader.is_alive(), "get_frame('fast') blocked while 'slow' was being closed"
        assert results and results[0] is not None
    finally:
        release.set()
        t.join(WAIT_S)
