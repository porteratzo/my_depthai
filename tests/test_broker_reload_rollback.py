"""a reload whose camera init fails after the manager swap rolls back to the previous config."""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_BROKER_PATH = Path(__file__).parent.parent / "mydepthai" / "broker" / "camera_broker.py"


@pytest.fixture(scope="module")
def broker_mod() -> Any:
    mod_name = "my_depthai_camera_broker_rollback_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _BROKER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _manager(config: dict) -> MagicMock:
    m = MagicMock()
    m.config = config
    return m


@pytest.fixture()
def broker(broker_mod: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(broker_mod.CameraBroker, "_create_redis_client", lambda self: MagicMock())
    cfg = tmp_path / "cams.json"
    cfg.write_text(json.dumps({"new_cam": {"camera_type": "color"}}))  # what the UI just saved
    b = broker_mod.CameraBroker(config_path=str(cfg), fps=10, jpeg_quality=85)
    monkeypatch.setattr(b, "_is_settings_only_change", lambda c: False)
    return b


def test_init_failure_restores_previous_config_and_cameras(
    broker: Any, broker_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = {"old_cam": {"camera_type": "color", "outputs": ["preview"]}}
    old_manager = _manager(previous)
    broker.camera_manager = old_manager

    failed = _manager({"new_cam": {}})
    failed.initialize_cameras.side_effect = RuntimeError("device in use")
    restored = _manager(previous)
    factory = MagicMock(side_effect=[failed, restored])
    monkeypatch.setattr(broker_mod, "CameraManager", factory)
    written: list = []
    monkeypatch.setattr(broker_mod, "write_config", lambda path, cfg: written.append((str(path), cfg)))

    with pytest.raises(RuntimeError, match="device in use"):
        broker.reload_cameras()

    old_manager.close_all.assert_called_once()
    failed.close_all.assert_called_once()
    assert written == [(broker.config_path, previous)]  # disk matches what is running again
    restored.initialize_cameras.assert_called_once()
    restored.start_monitoring.assert_called_once()
    assert broker.camera_manager is restored
    # The reload lock was released, so the next reload is not reported as busy.
    assert broker._reload_lock.acquire(blocking=False)
    broker._reload_lock.release()


def test_failed_rollback_leaves_no_half_initialised_manager(
    broker: Any, broker_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker.camera_manager = _manager({"old_cam": {"camera_type": "color", "outputs": ["preview"]}})
    failed = _manager({"new_cam": {}})
    failed.initialize_cameras.side_effect = RuntimeError("device in use")
    factory = MagicMock(side_effect=[failed, RuntimeError("old config no longer comes back")])
    monkeypatch.setattr(broker_mod, "CameraManager", factory)
    monkeypatch.setattr(broker_mod, "write_config", lambda path, cfg: None)

    with pytest.raises(RuntimeError, match="device in use"):  # the original error, not the rollback's
        broker.reload_cameras()
    assert broker.camera_manager is None
