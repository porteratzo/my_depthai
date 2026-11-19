"""Tests for CameraBroker's Redis frame-publishing behavior (my_depthai/camera_broker.py).

``test_camera_broker_main.py`` already covers ``main()``'s CLI/config wiring; this file
exercises the ``CameraBroker`` class itself — ``publish_frame()``'s JPEG-encode + Redis
``XADD`` path (success, encode failure, publish failure with reconnect), the reconnect
backoff loop, ``get_status()``'s per-camera reporting, and one pass of the ``run()``
capture loop (real frame and fallback-frame paths) — none of which had direct coverage
before (see docs/TESTING.md's testing roadmap).
"""

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

pytestmark = pytest.mark.unit

_BROKER_PATH = Path(__file__).parent.parent / "mydepthai" / "broker" / "camera_broker.py"


@pytest.fixture(scope="module")
def broker_mod() -> Any:
    """Load camera_broker.py once for this test module (mirrors test_camera_broker_main.py)."""
    mod_name = "my_depthai_camera_broker_publish_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _BROKER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeRedisClient:
    """Stand-in for redis.Redis — never touches a real Redis server."""

    def __init__(self) -> None:
        self.ping = MagicMock(return_value=True)
        self.xadd = MagicMock(return_value=b"1-0")
        self.close = MagicMock()


@pytest.fixture()
def fake_client() -> _FakeRedisClient:
    return _FakeRedisClient()


@pytest.fixture()
def make_broker(broker_mod: Any, fake_client: _FakeRedisClient, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build a CameraBroker wired to ``fake_client`` instead of a real Redis connection.

    Patches ``_create_redis_client`` (the constructor's only seam into ``redis.Redis``)
    rather than the module-level ``redis`` object, so ``redis.RedisError``/``ConnectionError``
    stay the real exception classes the source's ``except`` clauses expect.
    """
    monkeypatch.setattr(broker_mod.CameraBroker, "_create_redis_client", lambda self: fake_client)

    def _make(**overrides: Any) -> Any:
        kwargs: dict = {"fps": 10, "jpeg_quality": 85}
        kwargs.update(overrides)
        return broker_mod.CameraBroker(**kwargs)

    return _make


@pytest.fixture()
def sample_frame() -> np.ndarray:
    return np.zeros((480, 640, 3), dtype=np.uint8)


# ---------------------------------------------------------------------------
# publish_frame()
# ---------------------------------------------------------------------------


class TestPublishFrame:
    def test_success_xadds_expected_fields(
        self, make_broker: Any, fake_client: _FakeRedisClient, sample_frame: np.ndarray
    ) -> None:
        broker = make_broker()

        result = broker.publish_frame("cam1", "color", "preview", sample_frame)

        assert result is True
        fake_client.xadd.assert_called_once()
        (stream_name, message), kwargs = fake_client.xadd.call_args
        assert stream_name == broker.stream_name
        assert message["camera_id"] == b"cam1"
        assert message["cam_type"] == b"color"
        assert message["output_type"] == b"preview"
        assert message["encoding"] == b"jpeg"
        assert message["is_fallback"] == b"0"
        assert message["shape"] == b"480,640,3"
        assert kwargs["maxlen"] == broker.maxlen

    def test_grayscale_2d_frame_is_published_with_one_channel(
        self, make_broker: Any, fake_client: _FakeRedisClient
    ) -> None:
        """A 2-D (grayscale) frame used to IndexError on ``frame.shape[2]`` and vanish silently."""
        broker = make_broker()
        gray = np.zeros((48, 64), dtype=np.uint8)

        assert broker.publish_frame("cam1", "left", "preview", gray) is True

        _, message = fake_client.xadd.call_args[0]
        assert message["shape"] == b"48,64,1"
        assert broker.get_last_jpeg("cam1", "preview") is not None

    def test_fallback_flag_is_set_when_requested(
        self, make_broker: Any, fake_client: _FakeRedisClient, sample_frame: np.ndarray
    ) -> None:
        broker = make_broker()

        broker.publish_frame("cam1", "color", "preview", sample_frame, is_fallback=True)

        _, message = fake_client.xadd.call_args[0]
        assert message["is_fallback"] == b"1"

    def test_jpeg_encode_failure_returns_false_without_publishing(
        self,
        broker_mod: Any,
        make_broker: Any,
        fake_client: _FakeRedisClient,
        sample_frame: np.ndarray,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(broker_mod.cv2, "imencode", lambda *a, **k: (False, None))
        broker = make_broker()

        result = broker.publish_frame("cam1", "color", "preview", sample_frame)

        assert result is False
        fake_client.xadd.assert_not_called()

    def test_redis_error_triggers_reconnect_and_retries_publish(
        self,
        broker_mod: Any,
        make_broker: Any,
        fake_client: _FakeRedisClient,
        sample_frame: np.ndarray,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(broker_mod.time, "sleep", lambda *_: None)
        broker = make_broker()

        reconnected_client = _FakeRedisClient()
        fake_client.xadd.side_effect = broker_mod.redis.ConnectionError("boom")
        monkeypatch.setattr(
            broker,
            "_reconnect_redis",
            lambda: (setattr(broker, "redis_client", reconnected_client), True)[-1],
        )

        result = broker.publish_frame("cam1", "color", "preview", sample_frame)

        assert result is True
        reconnected_client.xadd.assert_called_once()

    def test_redis_error_with_failed_reconnect_returns_false(
        self,
        make_broker: Any,
        fake_client: _FakeRedisClient,
        sample_frame: np.ndarray,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        broker = make_broker()
        fake_client.xadd.side_effect = ConnectionError("boom")
        monkeypatch.setattr(broker, "_reconnect_redis", lambda: False)

        result = broker.publish_frame("cam1", "color", "preview", sample_frame)

        assert result is False


# ---------------------------------------------------------------------------
# _reconnect_redis()
# ---------------------------------------------------------------------------


class TestReconnectRedis:
    def test_succeeds_and_resets_backoff(self, broker_mod: Any, make_broker: Any, monkeypatch: Any) -> None:
        broker = make_broker(redis_max_retries=3)
        broker._redis_failures = 4
        recovered_client = _FakeRedisClient()
        monkeypatch.setattr(broker_mod.CameraBroker, "_create_redis_client", lambda self: recovered_client)

        assert broker._reconnect_redis() is True
        assert broker._redis_connected is True
        assert broker.redis_client is recovered_client
        assert broker._redis_failures == 0

    def test_never_sleeps(self, broker_mod: Any, make_broker: Any, monkeypatch: Any) -> None:
        """the old loop slept 1+2+4+8+16 s inline on the capture thread."""
        sleeps: list[float] = []
        monkeypatch.setattr(broker_mod.time, "sleep", lambda s: sleeps.append(s))
        broker = make_broker(redis_max_retries=5)
        monkeypatch.setattr(broker_mod.CameraBroker, "_create_redis_client", _failing_client(broker_mod))

        assert broker._reconnect_redis() is False
        assert sleeps == []

    def test_backoff_window_blocks_attempts_then_doubles_up_to_cap(
        self, broker_mod: Any, make_broker: Any, monkeypatch: Any
    ) -> None:
        now = [1000.0]
        monkeypatch.setattr(broker_mod.time, "monotonic", lambda: now[0])
        broker = make_broker(redis_max_retries=3)  # cap = 2**2 = 4 s
        attempts = [0]

        def _client(_self: Any) -> Any:
            attempts[0] += 1
            return _failing_client(broker_mod)(_self)

        monkeypatch.setattr(broker_mod.CameraBroker, "_create_redis_client", _client)

        delays = []
        for _ in range(5):
            assert broker._reconnect_redis() is False
            delays.append(broker._redis_backoff._next_attempt - now[0])
            # Inside the window: no new attempt is made.
            before = attempts[0]
            assert broker._reconnect_redis() is False
            assert attempts[0] == before
            now[0] = broker._redis_backoff._next_attempt
        assert delays == [1, 2, 4, 4, 4]  # capped, never gives up
        assert broker._redis_connected is False

    def test_publish_skips_dead_connection_inside_backoff_window(
        self,
        broker_mod: Any,
        make_broker: Any,
        fake_client: _FakeRedisClient,
        sample_frame: Any,
        monkeypatch: Any,
    ) -> None:
        broker = make_broker()
        broker._redis_connected = False
        broker._redis_backoff._next_attempt = broker_mod.time.monotonic() + 60

        assert broker.publish_frame("cam1", "color", "preview", sample_frame) is False
        fake_client.xadd.assert_not_called()


def _failing_client(broker_mod: Any) -> Any:
    def _make(_self: Any) -> Any:
        client = MagicMock()
        client.ping.side_effect = broker_mod.redis.ConnectionError("still down")
        return client

    return _make


# ---------------------------------------------------------------------------
# start_cameras()
# ---------------------------------------------------------------------------


class TestStartCameras:
    """start_cameras() builds a CameraManager, initializes it, and starts monitoring.

    No prior test constructed a real ``CameraManager`` here (every ``main()`` test replaces
    ``CameraBroker`` itself with a mock), so this method was 0% covered.
    """

    def test_builds_manager_initializes_and_starts_monitoring(
        self, broker_mod: Any, make_broker: Any, monkeypatch: Any
    ) -> None:
        fake_manager = MagicMock()
        fake_manager.config = {"cam1": {"camera_type": "color", "outputs": ["preview"]}}
        fake_manager.__len__ = MagicMock(return_value=1)
        fake_manager_cls = MagicMock(return_value=fake_manager)
        monkeypatch.setattr(broker_mod, "CameraManager", fake_manager_cls)

        broker = make_broker(config_path="/tmp/cfg.json", health_check_interval=2.0, data_timeout=3.0)
        broker.start_cameras(use_superspeed=False)

        _, kwargs = fake_manager_cls.call_args
        assert kwargs["config_path"] == "/tmp/cfg.json"
        assert kwargs["health_check_interval"] == 2.0
        assert kwargs["data_timeout"] == 3.0
        fake_manager.initialize_cameras.assert_called_once_with(use_superspeed=False)
        fake_manager.start_monitoring.assert_called_once()
        assert broker.camera_manager is fake_manager
        assert broker._use_superspeed is False

    def test_initialize_failure_is_logged_and_reraised(
        self, broker_mod: Any, make_broker: Any, monkeypatch: Any
    ) -> None:
        fake_manager = MagicMock()
        fake_manager.initialize_cameras.side_effect = RuntimeError("no device found")
        monkeypatch.setattr(broker_mod, "CameraManager", MagicMock(return_value=fake_manager))

        broker = make_broker()
        with pytest.raises(RuntimeError, match="no device found"):
            broker.start_cameras()


# ---------------------------------------------------------------------------
# get_status()
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_returns_empty_list_before_cameras_started(self, make_broker: Any) -> None:
        broker = make_broker()
        assert broker.get_status() == []

    def test_reports_per_camera_health(self, make_broker: Any) -> None:
        broker = make_broker()
        cam_mgr = MagicMock()
        cam_mgr._cameras_lock = MagicMock()
        cam_mgr.config = {"cam1": {"id": 0, "camera_type": "color", "outputs": ["preview"], "fpss": [30]}}
        cam_mgr.cameras = {"cam1": MagicMock()}
        cam_mgr._last_frame_time = {"cam1": time.time() - 5}
        cam_mgr.get_camera_state.return_value = "online"
        broker.camera_manager = cam_mgr

        status = broker.get_status()

        assert len(status) == 1
        entry = status[0]
        assert entry["name"] == "cam1"
        assert entry["online"] is True
        assert entry["state"] == "online"
        assert entry["fps"] == 30
        assert entry["last_frame_age_s"] is not None


# ---------------------------------------------------------------------------
# run() — one pass of the capture loop
# ---------------------------------------------------------------------------


class TestGetStatusStates:
    def test_dead_camera_is_not_reported_online(self, make_broker: Any) -> None:
        """`online` was True for every configured camera (bool(obj) is always True)."""
        broker = make_broker()
        cam_mgr = MagicMock()
        cam_mgr._cameras_lock = MagicMock()
        cam_mgr.config = {"cam1": {"id": 0}}
        cam_mgr._last_frame_time = {}
        cam_mgr.get_camera_state.return_value = "offline"
        broker.camera_manager = cam_mgr

        entry = broker.get_status()[0]

        assert entry["online"] is False
        assert entry["state"] == "offline"


class TestRunLoop:
    def test_publishes_a_real_frame_then_stops(
        self, make_broker: Any, fake_client: _FakeRedisClient, sample_frame: np.ndarray
    ) -> None:
        broker = make_broker(fps=1000)  # tiny frame_interval so rate limiting never blocks the test
        cam_mgr = MagicMock()
        cam_mgr.config = {"cam1": {"camera_type": "color", "outputs": ["preview"]}}

        def _get_frame(*_a: Any, **_k: Any) -> tuple:
            broker.running = False  # stop after this single frame is processed
            return (sample_frame, {"lens_position": 1, "exposure_time_us": 8000.0, "iso": 100})

        cam_mgr.get_frame_with_metadata.side_effect = _get_frame
        broker.camera_manager = cam_mgr

        broker.run()

        fake_client.xadd.assert_called_once()
        message = fake_client.xadd.call_args[0][1]
        assert message["is_fallback"] == b"0"
        cam_mgr.close_all.assert_called_once()  # run()'s finally calls stop()
        fake_client.close.assert_called_once()

    def test_publishes_a_fallback_frame_when_camera_offline(
        self,
        broker_mod: Any,
        make_broker: Any,
        fake_client: _FakeRedisClient,
        sample_frame: np.ndarray,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(broker_mod, "create_camera_error_frame", lambda *a, **k: sample_frame)
        broker = make_broker(fps=1000)
        cam_mgr = MagicMock()
        cam_mgr.config = {"cam1": {"camera_type": "color", "outputs": ["preview"]}}

        def _get_frame(*_a: Any, **_k: Any) -> None:
            broker.running = False
            return None

        cam_mgr.get_frame_with_metadata.side_effect = _get_frame
        broker.camera_manager = cam_mgr

        broker.run()

        fake_client.xadd.assert_called_once()
        message = fake_client.xadd.call_args[0][1]
        assert message["is_fallback"] == b"1"

    def test_still_outputs_are_not_polled_or_published_as_fallback(
        self, make_broker: Any, fake_client: _FakeRedisClient, sample_frame: np.ndarray
    ) -> None:
        """`still` is trigger-driven; polling it blocks a timeout per tick."""
        broker = make_broker(fps=1000)
        cam_mgr = MagicMock()
        cam_mgr.config = {"cam1": {"camera_type": "color", "outputs": ["still", "preview"]}}

        def _get_frame(_cam: Any, _type: Any, output: Any, **_k: Any) -> tuple:
            broker.running = False
            return (sample_frame, {})

        cam_mgr.get_frame_with_metadata.side_effect = _get_frame
        broker.camera_manager = cam_mgr

        broker.run()

        polled = [c.args[2] for c in cam_mgr.get_frame_with_metadata.call_args_list]
        assert polled == ["preview"]
        fake_client.xadd.assert_called_once()


class TestRunLoopNonBlocking:
    """one slow camera must not stall the others."""

    class _Manager:
        """Fake CameraManager: ``stalled`` cameras block for ``stall_s`` unless polled with timeout=0."""

        def __init__(self, config: dict, frame: np.ndarray, stalled: set, ticks: int, stall_s: float = 1.33):
            self.config = config
            self.frame = frame
            self.stalled = stalled
            self.stall_s = stall_s
            self.blocked_s = 0.0
            self.calls: list = []
            self.ticks = ticks
            self.on_done: Any = None
            self.close_all = MagicMock()

        def get_frame_with_metadata(
            self, cam_id: str, _type: str, output: str, update_health: bool = True, timeout: Any = None
        ) -> Any:
            self.calls.append((cam_id, output, timeout))
            if len([c for c in self.calls if c[0] == cam_id]) >= self.ticks and self.on_done:
                self.on_done()
            if cam_id in self.stalled:
                if timeout is None or timeout > 0:
                    self.blocked_s += self.stall_s
                return None
            return (self.frame, {})

    def test_stalled_camera_does_not_block_or_starve_a_healthy_one(
        self,
        make_broker: Any,
        fake_client: _FakeRedisClient,
        sample_frame: np.ndarray,
        broker_mod: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(broker_mod, "create_camera_error_frame", lambda *a, **k: sample_frame)
        broker = make_broker(fps=1000)
        cfg = {
            "good": {"camera_type": "color", "outputs": ["preview"]},
            "bad": {"camera_type": "color", "outputs": ["preview"]},
        }
        mgr = self._Manager(cfg, sample_frame, stalled={"bad"}, ticks=5)
        mgr.on_done = lambda: setattr(broker, "running", False)
        broker.camera_manager = mgr  # type: ignore[assignment]

        broker.run()

        assert mgr.blocked_s == 0.0, "the loop issued a blocking read against a stalled camera"
        assert all(c[2] == 0 for c in mgr.calls), "every read must be a non-blocking poll"
        by_cam = {"good": 0, "bad": 0}
        for call in fake_client.xadd.call_args_list:
            by_cam[call[0][1]["camera_id"].decode()] += 1
        assert by_cam["good"] == 5  # one real frame per tick, none lost to the stalled camera

    def _run_script(self, broker: Any, frame: np.ndarray, script: list, cfg: dict, hook: Any = None) -> None:
        """Run the loop once per entry in ``script`` (True = frame available, False = miss)."""
        calls = {"n": 0}

        class _M:
            config = cfg
            close_all = MagicMock()

            def get_frame_with_metadata(self, *_a: Any, **_k: Any) -> Any:
                i = calls["n"]
                calls["n"] += 1
                if hook:
                    hook(i)
                if i >= len(script) - 1:
                    broker.running = False
                return (frame, {}) if script[i] else None

        broker.camera_manager = _M()  # type: ignore[assignment]
        broker.run()

    def test_short_miss_after_a_real_frame_is_not_reported_as_an_outage(
        self,
        make_broker: Any,
        fake_client: _FakeRedisClient,
        sample_frame: np.ndarray,
        broker_mod: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(broker_mod, "create_camera_error_frame", lambda *a, **k: sample_frame)
        broker = make_broker(fps=1000)
        cfg = {"cam1": {"camera_type": "color", "outputs": ["preview"], "fpss": [1]}}  # 4 s grace

        self._run_script(broker, sample_frame, [True, False, False], cfg)

        flags = [c[0][1]["is_fallback"] for c in fake_client.xadd.call_args_list]
        assert flags == [b"0"]  # the two quick misses published nothing, in particular no placeholder

    def test_miss_beyond_grace_publishes_fallback(
        self,
        make_broker: Any,
        fake_client: _FakeRedisClient,
        sample_frame: np.ndarray,
        broker_mod: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(broker_mod, "create_camera_error_frame", lambda *a, **k: sample_frame)
        broker = make_broker(fps=1000)
        cfg = {"cam1": {"camera_type": "color", "outputs": ["preview"], "fpss": [1]}}

        def _age_last_frame(i: int) -> None:
            if i == 2:  # before the third read: pretend the last real frame was 10 s ago
                broker._last_real_frame[("cam1", "preview")] -= 10.0

        self._run_script(broker, sample_frame, [True, False, False], cfg, hook=_age_last_frame)

        flags = [c[0][1]["is_fallback"] for c in fake_client.xadd.call_args_list]
        assert flags == [b"0", b"1"]

    def test_miss_grace_is_four_frame_periods(self, broker_mod: Any) -> None:
        grace = broker_mod.CameraBroker._miss_grace_s
        assert grace({"outputs": ["preview"], "fpss": [4]}, "preview") == 1.0
        assert grace({"outputs": ["preview"]}, "preview") == 0.2  # CameraModule's default 20 fps
        assert grace({"outputs": ["fullvideo"]}, "fullvideo") == 2.0  # default 2 fps


# ---------------------------------------------------------------------------
# JPEG caching for the camera-controls UI's "fetch latest frame" flow
# ---------------------------------------------------------------------------


class TestPublishFrameJpegCache:
    def test_caches_jpeg_bytes_on_success(self, make_broker: Any, sample_frame: np.ndarray) -> None:
        broker = make_broker()
        broker.publish_frame("cam1", "color", "preview", sample_frame)
        assert broker._last_jpeg[("cam1", "preview")]
        assert isinstance(broker._last_jpeg[("cam1", "preview")], bytes)

    def test_caches_jpeg_bytes_for_fallback_frame_too(
        self, make_broker: Any, sample_frame: np.ndarray
    ) -> None:
        broker = make_broker()
        broker.publish_frame("cam1", "color", "preview", sample_frame, is_fallback=True)
        assert broker._last_jpeg[("cam1", "preview")]


class TestGetLastJpeg:
    def test_returns_cached_bytes_after_publish(self, make_broker: Any, sample_frame: np.ndarray) -> None:
        broker = make_broker()
        broker.publish_frame("cam1", "color", "preview", sample_frame)
        result = broker.get_last_jpeg("cam1", "preview")
        assert result == broker._last_jpeg[("cam1", "preview")]

    def test_returns_none_when_nothing_published(self, make_broker: Any) -> None:
        broker = make_broker()
        assert broker.get_last_jpeg("cam1", "preview") is None

    def test_defaults_to_first_configured_output(self, make_broker: Any, sample_frame: np.ndarray) -> None:
        broker = make_broker()
        cam_mgr = MagicMock()
        cam_mgr.config = {"cam1": {"outputs": ["preview", "video"]}}
        broker.camera_manager = cam_mgr
        broker.publish_frame("cam1", "color", "preview", sample_frame)

        result = broker.get_last_jpeg("cam1")
        assert result == broker._last_jpeg[("cam1", "preview")]

    def test_returns_none_when_camera_manager_unset_and_no_output_type(self, make_broker: Any) -> None:
        broker = make_broker()
        assert broker.get_last_jpeg("cam1") is None


class TestReloadCameras:
    """a bad config must not tear down the running cameras."""

    def test_invalid_config_on_disk_keeps_old_manager_open(
        self, make_broker: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        cfg = tmp_path / "cams.json"
        cfg.write_text(json.dumps({"cam1": {"outputs": []}}))  # invalid
        broker = make_broker(config_path=str(cfg))
        old_manager = MagicMock()
        broker.camera_manager = old_manager

        with pytest.raises(ValueError):
            broker.reload_cameras()
        old_manager.close_all.assert_not_called()
        assert broker.camera_manager is old_manager

    def test_unvalidated_constructor_failure_keeps_old_manager_open(
        self, make_broker: Any, broker_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        cfg = tmp_path / "cams.json"
        cfg.write_text(json.dumps({"cam1": {"camera_type": "color"}}))
        broker = make_broker(config_path=str(cfg))
        old_manager = MagicMock()
        broker.camera_manager = old_manager
        monkeypatch.setattr(broker, "_is_settings_only_change", lambda c: False)
        monkeypatch.setattr(broker_mod, "CameraManager", MagicMock(side_effect=RuntimeError("boom")))

        with pytest.raises(RuntimeError):
            broker.reload_cameras()
        old_manager.close_all.assert_not_called()
        assert broker.camera_manager is old_manager

    def test_valid_structural_reload_swaps_managers(
        self, make_broker: Any, broker_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        cfg = tmp_path / "cams.json"
        cfg.write_text(json.dumps({"cam1": {"camera_type": "color"}}))
        broker = make_broker(config_path=str(cfg))
        old_manager = MagicMock()
        broker.camera_manager = old_manager
        new_manager = MagicMock()
        new_manager.config = {"cam1": {}}
        monkeypatch.setattr(broker, "_is_settings_only_change", lambda c: False)
        monkeypatch.setattr(broker_mod, "CameraManager", MagicMock(return_value=new_manager))

        result = broker.reload_cameras()
        old_manager.close_all.assert_called_once()
        new_manager.initialize_cameras.assert_called_once()
        assert broker.camera_manager is new_manager
        assert result["action"] == "reloaded"


class TestSettingsOnlyReload:
    """The path the UI exposure/focus save takes: classify the diff, then apply or reconnect.

    Uses a *real* CameraManager (config only, cameras injected) so ``_is_settings_only_change``,
    ``_apply_settings`` and ``_apply_camera_settings`` all run unpatched.
    """

    BASE: dict = {
        "cam1": {
            "id": 0,
            "camera_type": "color",
            "outputs": ["preview"],
            "sizes": [[640, 480]],
            "fpss": [30],
            "settings": {"exposure": 1000, "iso": 100},
        }
    }

    @staticmethod
    def _cfg(**changes: Any) -> dict:
        import copy

        cfg = copy.deepcopy(TestSettingsOnlyReload.BASE)
        for key, value in changes.items():
            cfg["cam1"][key] = value
        return cfg

    @pytest.fixture()
    def broker(self, make_broker: Any, broker_mod: Any, tmp_path: Path) -> Any:
        import copy
        import json

        from mydepthai.camera_manager.camera_manager import CameraManager

        cfg_path = tmp_path / "cams.json"
        cfg_path.write_text(json.dumps(self.BASE))
        b = make_broker(config_path=str(cfg_path))
        b.camera_manager = CameraManager(config_dict=copy.deepcopy(self.BASE))
        self.component = MagicMock()
        camera = MagicMock()
        camera.components = {"color": self.component}
        b.camera_manager.cameras["cam1"] = camera
        b.cfg_path = cfg_path
        return b

    def _write(self, broker: Any, cfg: dict) -> None:
        import json

        broker.cfg_path.write_text(json.dumps(cfg))

    # -- classification -------------------------------------------------------------------

    def test_identical_config_is_settings_only(self, broker: Any) -> None:
        assert broker._is_settings_only_change(self._cfg()) is True

    def test_settings_value_change_is_settings_only(self, broker: Any) -> None:
        assert broker._is_settings_only_change(self._cfg(settings={"exposure": 5000, "iso": 400})) is True

    @pytest.mark.parametrize(
        "key,value",
        [
            ("id", 1),
            ("camera_type", "left"),
            ("outputs", ["preview", "video"]),
            ("sizes", [[1280, 720]]),
            ("fpss", [15]),
        ],
    )
    def test_each_structural_key_forces_reconnect(self, broker: Any, key: str, value: Any) -> None:
        assert broker._is_settings_only_change(self._cfg(**{key: value})) is False

    def test_added_or_removed_camera_forces_reconnect(self, broker: Any) -> None:
        added = self._cfg()
        added["cam2"] = dict(added["cam1"], id=1)
        assert broker._is_settings_only_change(added) is False
        assert broker._is_settings_only_change({}) is False

    def test_no_manager_is_never_settings_only(self, broker: Any) -> None:
        broker.camera_manager = None
        assert broker._is_settings_only_change(self._cfg()) is False

    # -- end to end through reload_cameras ------------------------------------------------

    def test_settings_only_reload_applies_in_place_without_reconnect(
        self, broker: Any, broker_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = broker.camera_manager
        ctor = MagicMock()
        monkeypatch.setattr(broker_mod, "CameraManager", ctor)
        self._write(broker, self._cfg(settings={"exposure": 5000, "iso": 400}))

        result = broker.reload_cameras()

        assert result == {"status": "ok", "action": "settings_applied", "updated": ["cam1"], "skipped": []}
        self.component.set_exposure.assert_called_once_with(5000, 400)
        ctor.assert_not_called()  # no new manager, i.e. no disconnect
        assert broker.camera_manager is manager
        # in-memory config follows the file so a later diff compares against what is applied
        assert manager.config["cam1"]["settings"] == {"exposure": 5000, "iso": 400}

    def test_removing_settings_restores_auto_and_drops_them_from_memory(self, broker: Any) -> None:
        no_settings = self._cfg()
        del no_settings["cam1"]["settings"]
        self._write(broker, no_settings)

        result = broker.reload_cameras()

        assert result["action"] == "settings_applied"
        self.component.set_auto_exposure.assert_called_once()
        self.component.continuous_auto_focus.assert_called_once()
        assert "settings" not in broker.camera_manager.config["cam1"]

    def test_camera_not_connected_is_skipped_not_fatal(self, broker: Any) -> None:
        broker.camera_manager.cameras.clear()
        self._write(broker, self._cfg(settings={"exposure": 5000}))
        result = broker.reload_cameras()
        assert result["updated"] == [] and result["skipped"] == ["cam1"]

    def test_structural_change_takes_the_full_reload_path(
        self, broker: Any, broker_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        new_manager = MagicMock()
        new_manager.config = self._cfg(sizes=[[1280, 720]])
        monkeypatch.setattr(broker_mod, "CameraManager", MagicMock(return_value=new_manager))
        old = broker.camera_manager
        old.close_all = MagicMock()
        self._write(broker, self._cfg(sizes=[[1280, 720]]))

        result = broker.reload_cameras()

        assert result["action"] == "reloaded"
        old.close_all.assert_called_once()
        assert broker.camera_manager is new_manager
        self.component.set_exposure.assert_not_called()

    def test_reload_while_another_reload_runs_reports_busy(self, broker: Any) -> None:
        assert broker._reload_lock.acquire(blocking=False)
        try:
            result = broker.reload_cameras()
        finally:
            broker._reload_lock.release()
        assert result["status"] == "busy"
        self.component.set_exposure.assert_not_called()

    def test_lock_is_released_after_validation_failure(self, broker: Any) -> None:
        self._write(broker, {"cam1": {"outputs": []}})
        with pytest.raises(ValueError):
            broker.reload_cameras()
        assert broker._reload_lock.acquire(blocking=False), "lock leaked after a failed reload"
        broker._reload_lock.release()


class TestRedisDownAtStartup:
    """MD-T1: the broker starts without Redis, reports it, and recovers on the usual backoff."""

    @pytest.fixture()
    def down_broker(self, make_broker: Any, fake_client: _FakeRedisClient, broker_mod: Any) -> Any:
        fake_client.ping.side_effect = broker_mod.redis.ConnectionError("redis down")
        return make_broker()

    def test_constructor_does_not_raise_and_marks_disconnected(self, down_broker: Any) -> None:
        assert down_broker._redis_connected is False
        assert down_broker._redis_failures == 1

    def test_timeout_at_startup_is_also_tolerated(
        self, make_broker: Any, fake_client: _FakeRedisClient, broker_mod: Any
    ) -> None:
        fake_client.ping.side_effect = broker_mod.redis.TimeoutError("slow")
        assert make_broker()._redis_connected is False

    def test_status_reports_redis_unavailable_per_camera(self, down_broker: Any) -> None:
        manager = MagicMock()
        manager._cameras_lock = __import__("threading").Lock()
        manager._last_frame_time = {}
        manager.config = {"cam1": {"id": 0, "camera_type": "color", "outputs": ["preview"]}}
        manager.get_camera_state.return_value = "online"
        down_broker.camera_manager = manager
        assert down_broker.get_status()[0]["redis_connected"] is False

    def test_publish_while_down_returns_false_but_still_caches_frame(
        self, down_broker: Any, fake_client: _FakeRedisClient, sample_frame: np.ndarray
    ) -> None:
        assert down_broker.publish_frame("cam1", "color", "preview", sample_frame) is False
        fake_client.xadd.assert_not_called()  # inside the backoff window: no per-frame socket wait
        assert down_broker.get_last_jpeg("cam1", "preview") is not None  # ROI UI still works

    def test_recovers_and_publishes_once_redis_returns(
        self, down_broker: Any, fake_client: _FakeRedisClient, sample_frame: np.ndarray
    ) -> None:
        fake_client.ping.side_effect = None  # Redis comes back
        down_broker._redis_backoff.reset()  # backoff window elapsed

        assert down_broker.publish_frame("cam1", "color", "preview", sample_frame) is True
        assert down_broker._redis_connected is True
        fake_client.xadd.assert_called_once()
