"""Tests for camera_broker.py::main()'s Redis CLI-default wiring.

The ``--redis-host``/``--redis-port``/``--redis-db``/``--redis-password``
argparse defaults must come from ``ConfigLoader``/``config/redis.yaml``
(so multi-host deployments work without CLI flags), not a hardcoded
``"localhost"``. An explicit CLI flag must still take priority.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_BROKER_PATH = Path(__file__).parent.parent / "mydepthai" / "broker" / "camera_broker.py"


@pytest.fixture(scope="module")
def broker_mod() -> Any:
    """Load camera_broker.py once for this test module."""
    mod_name = "my_depthai_camera_broker_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _BROKER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def fake_camera_broker_cls(broker_mod: Any, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace CameraBroker with a mock so main() never touches real hardware."""
    fake_cls = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(broker_mod, "CameraBroker", fake_cls)
    return fake_cls


class TestMainRedisDefaults:
    def test_redis_host_defaults_from_config_loader(
        self,
        broker_mod: Any,
        fake_camera_broker_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("REDIS_HOST", "config-redis-host")
        monkeypatch.setattr(sys, "argv", ["camera_broker.py", "--no-web"])

        broker_mod.main()

        _, kwargs = fake_camera_broker_cls.call_args
        assert kwargs["redis_host"] == "config-redis-host"

    def test_redis_port_defaults_from_config_loader(
        self,
        broker_mod: Any,
        fake_camera_broker_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("REDIS_PORT", "16379")
        monkeypatch.setattr(sys, "argv", ["camera_broker.py", "--no-web"])

        broker_mod.main()

        _, kwargs = fake_camera_broker_cls.call_args
        assert kwargs["redis_port"] == 16379

    def test_explicit_cli_redis_host_overrides_config(
        self,
        broker_mod: Any,
        fake_camera_broker_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("REDIS_HOST", "config-redis-host")
        monkeypatch.setattr(sys, "argv", ["camera_broker.py", "--no-web", "--redis-host", "cli-redis-host"])

        broker_mod.main()

        _, kwargs = fake_camera_broker_cls.call_args
        assert kwargs["redis_host"] == "cli-redis-host"

    def test_no_config_falls_back_to_localhost(
        self,
        broker_mod: Any,
        fake_camera_broker_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("REDIS_HOST", raising=False)
        monkeypatch.setattr(sys, "argv", ["camera_broker.py", "--no-web"])

        broker_mod.main()

        _, kwargs = fake_camera_broker_cls.call_args
        assert kwargs["redis_host"] == "localhost"


class TestMainMaxlen:
    def test_maxlen_defaults_from_config(
        self,
        broker_mod: Any,
        fake_camera_broker_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """maxlen comes from redis.streams.camera_frames.maxlen, not a literal 10000."""
        monkeypatch.setattr(sys, "argv", ["camera_broker.py", "--no-web"])
        broker_mod.main()
        _, kwargs = fake_camera_broker_cls.call_args
        assert kwargs["maxlen"] == 100

    def test_explicit_cli_maxlen_overrides_config(
        self,
        broker_mod: Any,
        fake_camera_broker_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["camera_broker.py", "--no-web", "--maxlen", "7"])
        broker_mod.main()
        _, kwargs = fake_camera_broker_cls.call_args
        assert kwargs["maxlen"] == 7


class TestMainBrokerKnobs:
    """services.camera_broker.* and the CLI flags actually take effect."""

    def _run(self, broker_mod: Any, monkeypatch: pytest.MonkeyPatch, *argv: str) -> Any:
        monkeypatch.setattr(sys, "argv", ["camera_broker.py", "--no-web", *argv])
        broker_mod.main()

    def test_fps_and_jpeg_quality_default_from_config(
        self, broker_mod: Any, fake_camera_broker_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CAMERA_FPS", "7")
        monkeypatch.setenv("JPEG_QUALITY", "60")
        self._run(broker_mod, monkeypatch)
        _, kwargs = fake_camera_broker_cls.call_args
        assert kwargs["fps"] == 7
        assert kwargs["jpeg_quality"] == 60

    def test_cli_fps_overrides_config(
        self, broker_mod: Any, fake_camera_broker_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CAMERA_FPS", "7")
        self._run(broker_mod, monkeypatch, "--fps", "3")
        assert fake_camera_broker_cls.call_args[1]["fps"] == 3

    def test_superspeed_can_be_disabled(
        self, broker_mod: Any, fake_camera_broker_cls: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._run(broker_mod, monkeypatch)
        fake_camera_broker_cls.return_value.start_cameras.assert_called_with(use_superspeed=True)
        self._run(broker_mod, monkeypatch, "--no-use-superspeed")
        fake_camera_broker_cls.return_value.start_cameras.assert_called_with(use_superspeed=False)

    def test_constructor_fps_default_matches_config_default(self, broker_mod: Any) -> None:
        import inspect

        default = inspect.signature(broker_mod.CameraBroker.__init__).parameters["fps"].default
        assert default == 5


class TestMainWebUI:
    """main() without --no-web: the uvicorn-import, init_app, and thread-start branch.

    Previously 0% covered — every other main() test passes --no-web, so this path (and its
    ImportError fallback when fastapi/uvicorn aren't installed) never ran.
    """

    def test_starts_web_thread_when_uvicorn_available(
        self,
        broker_mod: Any,
        fake_camera_broker_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_uvicorn = MagicMock()
        fake_server = MagicMock()
        fake_uvicorn.Server.return_value = fake_server
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

        fake_web_server = MagicMock()
        fake_web_server.app = MagicMock()
        fake_web_server.init_app = MagicMock()
        monkeypatch.setitem(sys.modules, "mydepthai.web", MagicMock())
        monkeypatch.setitem(sys.modules, "mydepthai.web.server", fake_web_server)

        started_threads = []
        real_thread = broker_mod.threading.Thread

        class _ImmediateThread:
            def __init__(self, target: Any = None, daemon: bool = False) -> None:
                self._target = target
                self.daemon = daemon

            def start(self) -> None:
                # Don't actually run the target (it calls asyncio.run(server.serve()));
                # just record that start_web would have launched it.
                started_threads.append(self)

        monkeypatch.setattr(broker_mod.threading, "Thread", _ImmediateThread)
        monkeypatch.setattr(sys, "argv", ["camera_broker.py"])  # no --no-web

        broker_mod.main()

        fake_web_server.init_app.assert_called_once()
        assert len(started_threads) == 1
        monkeypatch.setattr(broker_mod.threading, "Thread", real_thread)

    def test_import_error_disables_web_ui_without_raising(
        self,
        broker_mod: Any,
        fake_camera_broker_cls: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setitem(sys.modules, "uvicorn", None)  # forces ImportError on `import uvicorn`
        monkeypatch.setattr(sys, "argv", ["camera_broker.py"])

        broker_mod.main()  # must not raise

        fake_camera_broker_cls.return_value.run.assert_called_once()
