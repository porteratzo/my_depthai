"""Edge branches of the broker and its web config routes that no other file pins.

Hermetic: fake Redis client, MagicMock camera manager, no SDK, no network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

_HERE = Path(__file__).resolve().parent
_SERVER_PATH = _HERE.parent / "mydepthai" / "web" / "server.py"
_BROKER_PATH = _HERE.parent / "mydepthai" / "broker" / "camera_broker.py"


def _load(name: str, path: Path) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def server_mod() -> Any:
    return _load("my_depthai_web_server_edges_test", _SERVER_PATH)


@pytest.fixture()
def client(server_mod: Any):
    from fastapi.testclient import TestClient

    server_mod._broker = None
    server_mod._nav_links = []
    server_mod._token = "t"
    server_mod._bind_host = "127.0.0.1"
    with TestClient(
        server_mod.app,
        raise_server_exceptions=False,
        base_url="http://localhost",
        headers={"X-Broker-Token": "t"},
    ) as c:
        yield c, server_mod


class TestSaveCameraConfigValidation:
    def test_non_object_body_is_400_and_nothing_written(self, client, tmp_path):
        c, srv = client
        cfg = tmp_path / "cfg.json"
        srv._broker = MagicMock(config_path=str(cfg))
        resp = c.post("/api/camera_config", json=[1, 2])
        assert resp.status_code == 400
        assert not cfg.exists()

    def test_entry_that_is_not_an_object_is_400_and_nothing_written(self, client, tmp_path):
        c, srv = client
        cfg = tmp_path / "cfg.json"
        srv._broker = MagicMock(config_path=str(cfg))
        resp = c.post("/api/camera_config", json={"cam1": "oops"})
        assert resp.status_code == 400
        assert "cam1" in resp.json()["detail"]
        assert not cfg.exists()

    def test_broker_without_config_path_is_400(self, client):
        c, srv = client
        srv._broker = MagicMock(config_path="")
        assert c.post("/api/camera_config", json={"cam1": {"id": 0}}).status_code == 400


class TestReloadBodyValidation:
    def test_malformed_json_body_is_400_and_does_not_reload(self, client):
        c, srv = client
        srv._broker = MagicMock(config_path="/nonexistent")
        resp = c.post(
            "/api/cameras/reload", content=b"{not json", headers={"content-type": "application/json"}
        )
        assert resp.status_code == 400
        srv._broker.reload_cameras.assert_not_called()

    def test_invalid_config_body_is_400_never_written_never_reloaded(self, client, tmp_path):
        c, srv = client
        cfg = tmp_path / "cfg.json"
        srv._broker = MagicMock(config_path=str(cfg))
        # structurally invalid camera entry (validator rejects an unknown camera_type)
        resp = c.post("/api/cameras/reload", json={"cam1": {"id": 0, "camera_type": "bogus"}})
        assert resp.status_code == 400
        assert not cfg.exists()
        srv._broker.reload_cameras.assert_not_called()

    def test_reload_value_error_is_400_and_other_errors_are_500(self, client):
        c, srv = client
        srv._broker = MagicMock(config_path="")
        srv._broker.reload_cameras.side_effect = ValueError("bad")
        assert c.post("/api/cameras/reload").status_code == 400
        srv._broker.reload_cameras.side_effect = RuntimeError("boom")
        assert c.post("/api/cameras/reload").status_code == 500


class TestRevive503:
    def test_no_broker_is_503(self, client):
        c, _ = client
        assert c.post("/api/cameras/cam0/revive").status_code == 503


@pytest.fixture(scope="module")
def broker_mod() -> Any:
    return _load("my_depthai_broker_edges_test", _BROKER_PATH)


@pytest.fixture()
def broker(broker_mod: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    client = MagicMock()
    monkeypatch.setattr(broker_mod.CameraBroker, "_create_redis_client", lambda self: client)
    b = broker_mod.CameraBroker(fps=1000, jpeg_quality=85)
    b._fake_client = client
    return b


class TestGetOutputSize:
    @pytest.mark.parametrize(
        "cfg, output, expected",
        [
            ({"outputs": ["preview", "video"], "sizes": [[320, 240], [1280, 720]]}, "video", (1280, 720)),
            ({"outputs": ["preview"], "sizes": [[320, 240]]}, "video", (640, 480)),  # unknown output
            ({"outputs": ["preview", "video"], "sizes": [[320, 240]]}, "video", (640, 480)),  # short sizes
            ({"outputs": ["preview"], "sizes": [None]}, "preview", (640, 480)),
            ({"outputs": ["preview"], "sizes": ["bad"]}, "preview", (640, 480)),
            ({}, "preview", (640, 480)),
        ],
    )
    def test_configured_size_or_default(self, broker: Any, cfg: dict, output: str, expected: tuple) -> None:
        broker.camera_manager = MagicMock()
        broker.camera_manager.config = {"cam1": cfg}
        assert broker._get_output_size("cam1", output) == expected

    def test_no_manager_or_unknown_camera_gives_default(self, broker: Any) -> None:
        broker.camera_manager = None
        assert broker._get_output_size("cam1", "preview") == (640, 480)
        broker.camera_manager = MagicMock()
        broker.camera_manager.config = {}
        assert broker._get_output_size("camX", "preview") == (640, 480)


class TestStopAndRunFailure:
    def test_stop_closes_redis_even_when_closing_cameras_fails(self, broker: Any) -> None:
        broker.camera_manager = MagicMock()
        broker.camera_manager.close_all.side_effect = RuntimeError("usb gone")
        broker.stop()  # must not raise
        assert broker.running is False
        broker._fake_client.close.assert_called_once()

    def test_stop_survives_redis_close_failure(self, broker: Any) -> None:
        broker.camera_manager = MagicMock()
        broker._fake_client.close.side_effect = RuntimeError("socket")
        broker.stop()
        broker.camera_manager.close_all.assert_called_once()

    def test_unexpected_loop_error_propagates_but_resources_are_released(self, broker: Any) -> None:
        cam_mgr = MagicMock()
        cam_mgr.config = {"cam1": {"camera_type": "color", "outputs": ["preview"]}}
        cam_mgr.get_frame_with_metadata.side_effect = RuntimeError("sdk exploded")
        broker.camera_manager = cam_mgr
        broker.publish_frame = MagicMock(return_value=True)
        # An error in a single camera read must not kill the loop silently; whichever way the
        # implementation chooses (propagate or skip), cleanup must always run.
        calls = {"n": 0}

        def _boom(*_a: Any, **_k: Any) -> Any:
            calls["n"] += 1
            broker.running = False
            raise RuntimeError("sdk exploded")

        cam_mgr.get_frame_with_metadata.side_effect = _boom
        try:
            broker.run()
        except RuntimeError:
            pass
        assert calls["n"] >= 1
        cam_mgr.close_all.assert_called_once()
        broker._fake_client.close.assert_called_once()


def test_publish_frame_grayscale_uses_one_channel(broker: Any) -> None:
    ok = broker.publish_frame("cam1", "color", "preview", np.zeros((4, 6), dtype=np.uint8))
    assert ok is True
    msg = broker._fake_client.xadd.call_args[0][1]
    assert msg["shape"] == b"4,6,1"
