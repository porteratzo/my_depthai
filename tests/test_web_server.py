"""Tests for mydepthai/web/server.py FastAPI routes.

All routes are exercised via FastAPI's synchronous TestClient.  The global
broker reference is reset before each test so tests are fully independent.
"""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_SERVER_PATH = Path(__file__).parent.parent / "mydepthai" / "web" / "server.py"


@pytest.fixture(scope="module")
def server_mod() -> Any:
    """Load web/server.py once for this test module."""
    mod_name = "my_depthai_web_server_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _SERVER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def client(server_mod: Any):
    """TestClient with broker and URL globals reset before every test."""
    from fastapi.testclient import TestClient

    server_mod._broker = None
    server_mod._nav_links = []
    server_mod._token = "test-token"
    server_mod._bind_host = "127.0.0.1"
    # Authenticated client: the route tests below are about route behaviour, not auth (see test_web_auth.py).
    with TestClient(
        server_mod.app,
        raise_server_exceptions=False,
        base_url="http://localhost",
        headers={"X-Broker-Token": "test-token"},
    ) as c:
        yield c, server_mod


# ---------------------------------------------------------------------------
# GET /api/camera_config
# ---------------------------------------------------------------------------


class TestGetCameraConfig:
    def test_no_broker_returns_empty_dict(self, client):
        c, _ = client
        resp = c.get("/api/camera_config")
        assert resp.status_code == 200
        assert resp.json() == {}

    def test_broker_reads_config_file(self, client, tmp_path):
        c, srv = client
        data = {"cam1": {"id": 0, "camera_type": "color"}}
        config_file = tmp_path / "cam_config.json"
        config_file.write_text(json.dumps(data))
        mock_broker = MagicMock()
        mock_broker.config_path = str(config_file)
        srv._broker = mock_broker

        resp = c.get("/api/camera_config")
        assert resp.status_code == 200
        assert resp.json() == data

    def test_broker_falls_back_to_config_dict_when_file_missing(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.config_path = "/does/not/exist.json"
        mock_broker.config_dict = {"cam2": {"id": 1}}
        srv._broker = mock_broker

        resp = c.get("/api/camera_config")
        assert resp.status_code == 200
        assert resp.json() == {"cam2": {"id": 1}}

    def test_broker_with_no_path_and_no_dict_returns_empty(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.config_path = None
        mock_broker.config_dict = None
        srv._broker = mock_broker

        resp = c.get("/api/camera_config")
        assert resp.status_code == 200
        assert resp.json() == {}


# ---------------------------------------------------------------------------
# POST /api/camera_config
# ---------------------------------------------------------------------------


class TestPostCameraConfig:
    def test_no_broker_returns_400(self, client):
        c, _ = client
        resp = c.post("/api/camera_config", json={"cam1": {"id": 0}})
        assert resp.status_code == 400

    def test_saves_valid_config_to_file(self, client, tmp_path):
        c, srv = client
        config_file = tmp_path / "saved_config.json"
        mock_broker = MagicMock()
        mock_broker.config_path = str(config_file)
        srv._broker = mock_broker

        data = {"cam1": {"id": 0, "camera_type": "color"}}
        resp = c.post("/api/camera_config", json=data)
        assert resp.status_code == 200
        assert resp.json()["message"] == "Config saved"
        assert json.loads(config_file.read_text()) == data

    def test_entry_not_a_dict_returns_400(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.config_path = "/tmp/test_cam_cfg.json"
        srv._broker = mock_broker

        resp = c.post("/api/camera_config", json={"cam1": "not_a_dict"})
        assert resp.status_code == 400

    def test_invalid_config_rejected_and_file_untouched(self, client, tmp_path):
        """a config the CameraManager would reject must never reach disk."""
        c, srv = client
        config_file = tmp_path / "keep.json"
        good = {"cam1": {"id": 0, "camera_type": "color"}}
        config_file.write_text(json.dumps(good))
        mock_broker = MagicMock()
        mock_broker.config_path = str(config_file)
        srv._broker = mock_broker

        for bad in ({"cam1": {"outputs": []}}, {"cam1": {"settings": {"iso": 50}}}):
            resp = c.post("/api/camera_config", json=bad)
            assert resp.status_code == 400
            assert json.loads(config_file.read_text()) == good
        assert list(tmp_path.iterdir()) == [config_file]  # no stray temp files

    def test_broker_without_config_path_returns_400(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.config_path = None
        srv._broker = mock_broker

        resp = c.post("/api/camera_config", json={"cam1": {"id": 0}})
        assert resp.status_code == 400

    def test_saves_multiple_cameras(self, client, tmp_path):
        c, srv = client
        config_file = tmp_path / "multi_cam.json"
        mock_broker = MagicMock()
        mock_broker.config_path = str(config_file)
        srv._broker = mock_broker

        data = {
            "cam1": {"id": 0, "camera_type": "color"},
            "cam2": {"id": 1, "camera_type": "color"},
        }
        resp = c.post("/api/camera_config", json=data)
        assert resp.status_code == 200
        saved = json.loads(config_file.read_text())
        assert len(saved) == 2


# ---------------------------------------------------------------------------
# GET /api/cameras/status
# ---------------------------------------------------------------------------


class TestCamerasStatus:
    def test_no_broker_returns_empty_list(self, client):
        c, _ = client
        resp = c.get("/api/cameras/status")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_broker_status(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.get_status.return_value = [{"id": "cam1", "connected": True}]
        srv._broker = mock_broker

        resp = c.get("/api/cameras/status")
        assert resp.status_code == 200
        assert resp.json() == [{"id": "cam1", "connected": True}]

    def test_empty_status_list_from_broker(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.get_status.return_value = []
        srv._broker = mock_broker

        resp = c.get("/api/cameras/status")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# POST /api/cameras/reload
# ---------------------------------------------------------------------------


class TestCamerasReload:
    def test_no_broker_returns_503(self, client):
        c, _ = client
        resp = c.post("/api/cameras/reload")
        assert resp.status_code == 503

    def test_reload_with_broker_succeeds(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.reload_cameras.return_value = {"status": "ok"}
        srv._broker = mock_broker

        resp = c.post("/api/cameras/reload")
        assert resp.status_code == 200

    def test_reload_with_config_body_saves_config_first(self, client, tmp_path):
        c, srv = client
        config_file = tmp_path / "reload_config.json"
        mock_broker = MagicMock()
        mock_broker.config_path = str(config_file)
        mock_broker.reload_cameras.return_value = {"status": "reloaded"}
        srv._broker = mock_broker

        data = {"cam1": {"id": 0}}
        resp = c.post("/api/cameras/reload", json=data)
        assert resp.status_code == 200
        # Config should have been written before reload was called
        assert config_file.exists()

    def test_reload_invalid_body_rejected_without_reloading(self, client, tmp_path):
        """invalid posted config -> 400, file untouched, cameras not reloaded."""
        c, srv = client
        config_file = tmp_path / "reload_config.json"
        good = {"cam1": {"id": 0}}
        config_file.write_text(json.dumps(good))
        mock_broker = MagicMock()
        mock_broker.config_path = str(config_file)
        srv._broker = mock_broker

        resp = c.post("/api/cameras/reload", json={"cam1": {"outputs": []}})
        assert resp.status_code == 400
        mock_broker.reload_cameras.assert_not_called()
        assert json.loads(config_file.read_text()) == good

    def test_reload_value_error_from_broker_returns_400(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.reload_cameras.side_effect = ValueError("bad edit on disk")
        srv._broker = mock_broker
        assert c.post("/api/cameras/reload").status_code == 400

    def test_reload_exception_returns_500(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.reload_cameras.side_effect = RuntimeError("device error")
        srv._broker = mock_broker

        resp = c.post("/api/cameras/reload")
        assert resp.status_code == 500

    def test_reload_empty_body_still_reloads(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.reload_cameras.return_value = {}
        srv._broker = mock_broker

        resp = c.post("/api/cameras/reload", content=b"")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/cameras/{cam_id}/frame
# ---------------------------------------------------------------------------


class TestGetCameraFrame:
    def test_no_broker_returns_503(self, client):
        c, _ = client
        resp = c.get("/api/cameras/cam1/frame")
        assert resp.status_code == 503

    def test_returns_404_when_no_cached_frame(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.get_last_jpeg.return_value = None
        srv._broker = mock_broker

        resp = c.get("/api/cameras/cam1/frame")
        assert resp.status_code == 404

    def test_returns_jpeg_bytes_with_correct_media_type(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.get_last_jpeg.return_value = b"\xff\xd8\xff\xd9"
        srv._broker = mock_broker

        resp = c.get("/api/cameras/cam1/frame")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content == b"\xff\xd8\xff\xd9"

    def test_output_type_query_param_is_forwarded(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.get_last_jpeg.return_value = b"\xff\xd8"
        srv._broker = mock_broker

        resp = c.get("/api/cameras/cam1/frame?output_type=video")
        assert resp.status_code == 200
        mock_broker.get_last_jpeg.assert_called_once_with("cam1", "video")


# ---------------------------------------------------------------------------
# GET /api/cameras/{cam_id}/control_state
# ---------------------------------------------------------------------------


class TestGetControlState:
    def test_no_broker_returns_503(self, client):
        c, _ = client
        resp = c.get("/api/cameras/cam1/control_state")
        assert resp.status_code == 503

    def test_no_camera_manager_returns_503(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.camera_manager = None
        srv._broker = mock_broker

        resp = c.get("/api/cameras/cam1/control_state")
        assert resp.status_code == 503

    def test_unknown_camera_returns_404(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.camera_manager.cameras = {}
        srv._broker = mock_broker

        resp = c.get("/api/cameras/cam1/control_state")
        assert resp.status_code == 404

    def test_unknown_component_returns_404(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_camera = MagicMock()
        mock_camera.components = {}
        mock_broker.camera_manager.cameras = {"cam1": mock_camera}
        srv._broker = mock_broker

        resp = c.get("/api/cameras/cam1/control_state?cam_type=color")
        assert resp.status_code == 404

    def test_returns_combined_live_and_commanded_state(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_camera = MagicMock()
        mock_component = MagicMock()
        mock_component.get_control_state.return_value = {
            "auto_exposure_enabled": True,
            "auto_focus_enabled": True,
            "af_mode": "CONTINUOUS_VIDEO",
            "ae_lock": False,
        }
        mock_camera.components = {"color": mock_component}
        mock_broker.camera_manager.cameras = {"cam1": mock_camera}
        mock_broker.camera_manager.get_cached_metadata.return_value = {
            "lens_position": 132,
            "exposure_time_us": 8000.0,
            "iso": 400,
            "frame_width": 1920,
            "frame_height": 1080,
        }
        srv._broker = mock_broker

        resp = c.get("/api/cameras/cam1/control_state?cam_type=color")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cam_id"] == "cam1"
        assert body["cam_type"] == "color"
        assert body["live"]["lens_position"] == 132
        assert body["live"]["exposure_time_us"] == 8000.0
        assert body["live"]["iso"] == 400
        assert body["commanded"]["auto_exposure_enabled"] is True


# ---------------------------------------------------------------------------
# POST /api/cameras/{cam_id}/control
# ---------------------------------------------------------------------------


class TestPostCameraControl:
    def test_no_broker_returns_503(self, client):
        c, _ = client
        resp = c.post("/api/cameras/cam1/control", json={"action": "trigger_auto_focus"})
        assert resp.status_code == 503

    def test_missing_action_returns_400(self, client):
        c, srv = client
        mock_broker = MagicMock()
        srv._broker = mock_broker

        resp = c.post("/api/cameras/cam1/control", json={"cam_type": "color"})
        assert resp.status_code == 400

    def test_value_error_from_manager_returns_400(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.camera_manager.send_camera_action.side_effect = ValueError("Unknown action: foo")
        srv._broker = mock_broker

        resp = c.post("/api/cameras/cam1/control", json={"action": "foo"})
        assert resp.status_code == 400

    def test_unexpected_exception_returns_500(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.camera_manager.send_camera_action.side_effect = RuntimeError("device error")
        srv._broker = mock_broker

        resp = c.post("/api/cameras/cam1/control", json={"action": "trigger_auto_focus"})
        assert resp.status_code == 500

    def test_success_returns_manager_result(self, client):
        c, srv = client
        mock_broker = MagicMock()
        mock_broker.camera_manager.send_camera_action.return_value = {
            "status": "ok",
            "action": "trigger_auto_focus",
        }
        srv._broker = mock_broker

        resp = c.post(
            "/api/cameras/cam1/control",
            json={"cam_type": "color", "action": "trigger_auto_focus", "params": {}},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "action": "trigger_auto_focus"}
        mock_broker.camera_manager.send_camera_action.assert_called_once_with(
            "cam1", "color", "trigger_auto_focus", {}
        )


# ---------------------------------------------------------------------------
# HTML page routes (render the real Jinja templates)
# ---------------------------------------------------------------------------


class TestPageRoutes:
    @pytest.mark.parametrize("path", ["/", "/status", "/controls"])
    def test_page_renders_html(self, client, path):
        c, _ = client
        resp = c.get(path)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "<html" in resp.text.lower()

    def test_controls_page_exposes_manual_modes_and_live_viewer(self, client):
        """Manual focus/exposure, AF trigger/continuous, and the zoomable live view must stay on the page."""
        c, _ = client
        body = c.get("/controls").text
        for needle in (
            "setFocusMode('manual')",
            "setFocusMode('trigger')",
            "setFocusMode('continuous')",
            "setExposureMode('auto')",
            "setExposureMode('manual')",
            "'set_focus'",
            "'set_exposure'",
            "'set_auto_exposure'",
            'id="viewerCanvas"',
        ):
            assert needle in body

    def test_nav_links_render_the_configured_links(self, client):
        c, srv = client
        srv._nav_links = [
            {"label": "Main", "url": "http://main.example:5000"},
            {"label": "Trainer", "url": "http://train.example:5001"},
        ]
        body = c.get("/").text
        assert 'href="http://main.example:5000"' in body and "Main" in body
        assert 'href="http://train.example:5001"' in body and "Trainer" in body

    def test_no_nav_links_by_default(self, client):
        c, _ = client
        body = c.get("/").text
        assert "InspectAI" not in body and "Dataset Service" not in body
        assert 'target="_blank"' not in body

    def test_unknown_page_is_404(self, client):
        c, _ = client
        assert c.get("/nope").status_code == 404


class TestInitApp:
    def test_failure_to_resolve_urls_leaves_blank_nav_and_keeps_broker(self, client):
        _, srv = client
        loader = MagicMock()
        loader.get.side_effect = RuntimeError("unreadable config")
        broker = MagicMock()
        srv._nav_links = [{"label": "stale", "url": "http://x"}]
        srv.init_app(broker, config_loader=loader)
        assert srv._broker is broker
        assert srv._nav_links == []

    def test_init_app_reads_nav_links_from_config(self, client):
        _, srv = client
        loader = MagicMock()
        loader.get.return_value = [{"label": "Main", "url": "https://main.example"}]
        srv.init_app(MagicMock(), config_loader=loader)
        loader.get.assert_called_once_with("services.camera_broker.nav_links", [])
        assert srv._nav_links == [{"label": "Main", "url": "https://main.example"}]


class TestParseNavLinks:
    def test_unset_or_empty_means_no_links(self, client):
        _, srv = client
        assert srv.parse_nav_links(None) == []
        assert srv.parse_nav_links([]) == []

    def test_rejects_non_http_schemes_and_incomplete_entries(self, client):
        _, srv = client
        raw = [
            {"label": "ok", "url": "https://ok.example"},
            {"label": "bad", "url": "javascript:alert(1)"},
            {"label": "", "url": "http://blank-label.example"},
            {"label": "no url"},
            "not a mapping",
        ]
        assert srv.parse_nav_links(raw) == [{"label": "ok", "url": "https://ok.example"}]

    def test_a_non_list_value_is_ignored(self, client):
        _, srv = client
        assert srv.parse_nav_links(5) == []


# ---------------------------------------------------------------------------
# Control endpoint: malformed input is a 400, never a 500
# ---------------------------------------------------------------------------


class TestPostCameraControlMalformedInput:
    @pytest.fixture()
    def live(self, client):
        """Real CameraManager (config only) behind the endpoint, with a mock camera injected."""
        from mydepthai.camera_manager.camera_manager import CameraManager

        c, srv = client
        broker = MagicMock()
        manager = CameraManager(
            config_dict={"cam1": {"id": 0, "camera_type": "color", "outputs": ["preview"]}}
        )
        component = MagicMock()
        camera = MagicMock()
        camera.components = {"color": component}
        manager.cameras["cam1"] = camera
        broker.camera_manager = manager
        srv._broker = broker
        return c, component

    def test_invalid_json_body_returns_400(self, live):
        c, _ = live
        resp = c.post(
            "/api/cameras/cam1/control", content=b"{not json", headers={"content-type": "application/json"}
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("body", [[1, 2], "text", 5])
    def test_non_object_body_returns_400(self, live, body):
        c, _ = live
        assert c.post("/api/cameras/cam1/control", json=body).status_code == 400

    def test_non_object_params_returns_400(self, live):
        c, _ = live
        resp = c.post("/api/cameras/cam1/control", json={"action": "set_focus", "params": [1]})
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "action",
        ["set_focus", "set_exposure", "set_ae_lock", "set_exposure_compensation", "set_focus_region"],
    )
    def test_missing_required_param_returns_400(self, live, action):
        c, component = live
        resp = c.post("/api/cameras/cam1/control", json={"action": action, "params": {}})
        assert resp.status_code == 400
        assert "missing" in resp.json()["detail"].lower()

    def test_wrong_typed_param_returns_400(self, live):
        c, component = live
        component.set_focus.side_effect = TypeError("bad")
        resp = c.post(
            "/api/cameras/cam1/control", json={"action": "set_focus", "params": {"lens_position": "abc"}}
        )
        assert resp.status_code == 400

    def test_unknown_camera_and_component_and_action_return_400(self, live):
        c, _ = live
        assert c.post("/api/cameras/nope/control", json={"action": "set_auto_exposure"}).status_code == 400
        assert (
            c.post(
                "/api/cameras/cam1/control", json={"cam_type": "left", "action": "set_auto_exposure"}
            ).status_code
            == 400
        )
        assert c.post("/api/cameras/cam1/control", json={"action": "__class__"}).status_code == 400

    def test_valid_request_still_reaches_the_component(self, live):
        c, component = live
        resp = c.post(
            "/api/cameras/cam1/control", json={"action": "set_focus", "params": {"lens_position": 120}}
        )
        assert resp.status_code == 200
        component.set_focus.assert_called_once_with(120)
