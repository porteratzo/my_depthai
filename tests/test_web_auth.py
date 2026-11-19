"""token on the :5002 web UI's state-changing routes, loopback default, Host/Origin guards."""

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator, Tuple
from unittest.mock import MagicMock

import pytest

from mydepthai.broker.auth import (
    host_header_allowed,
    is_loopback_host,
    log_bind_and_token,
    origin_allowed,
    resolve_web_token,
    token_matches,
)
from mydepthai.broker.settings import BrokerSettings

pytestmark = pytest.mark.unit

_SERVER_PATH = Path(__file__).parent.parent / "mydepthai" / "web" / "server.py"
TOKEN = "s3cret-token"
POSTS = [
    ("/api/camera_config", {"cam": {"id": 0}}),
    ("/api/cameras/reload", None),
    ("/api/cameras/cam0/control", {"action": "lock"}),
    ("/api/cameras/cam0/revive", None),
]


@pytest.fixture(scope="module")
def server_mod() -> Iterator[Any]:
    mod_name = "my_depthai_web_server_auth_test"
    spec = importlib.util.spec_from_file_location(mod_name, _SERVER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.modules.pop(mod_name, None)


@pytest.fixture()
def client(server_mod: Any, tmp_path: Path) -> Iterator[Tuple[Any, Any, MagicMock]]:
    """Unauthenticated client against a server initialised with TOKEN and a loopback bind."""
    from fastapi.testclient import TestClient

    broker = MagicMock()
    broker.config_path = str(tmp_path / "cfg.json")
    broker.config_dict = {}
    broker.reload_cameras.return_value = {"cameras": []}
    broker.camera_manager.send_camera_action.return_value = {"ok": True}
    server_mod.init_app(broker, MagicMock(get_service_url=lambda n: f"http://{n}"), TOKEN, "127.0.0.1")
    with TestClient(server_mod.app, raise_server_exceptions=False, base_url="http://localhost") as c:
        yield c, server_mod, broker
    server_mod._broker = None
    server_mod._token = ""
    server_mod._bind_host = "127.0.0.1"


def _post(c: Any, path: str, body: Any, headers: Any = None) -> Any:
    return c.post(path, content=json.dumps(body) if body is not None else b"", headers=headers or {})


@pytest.mark.parametrize("path,body", POSTS)
class TestPostAuth:
    def test_missing_token_is_401_envelope(self, client, path, body):
        c, _, broker = client
        resp = _post(c, path, body)
        assert resp.status_code == 401
        assert resp.json() == {"success": False, "data": None, "error": "Unauthorized"}
        broker.reload_cameras.assert_not_called()
        broker.camera_manager.send_camera_action.assert_not_called()
        broker.camera_manager.revive_camera.assert_not_called()

    def test_wrong_token_is_401_same_body(self, client, path, body):
        c, _, _ = client
        missing = _post(c, path, body)
        wrong = _post(c, path, body, {"X-Broker-Token": "nope"})
        assert wrong.status_code == 401
        assert wrong.json() == missing.json()

    def test_right_token_passes_the_guard(self, client, path, body):
        c, _, _ = client
        resp = _post(c, path, body, {"X-Broker-Token": TOKEN, "Content-Type": "application/json"})
        assert resp.status_code not in (401, 403)

    def test_server_without_token_rejects_everything_without_saying_so(self, client, path, body):
        c, srv, _ = client
        srv._token = ""
        for hdr in ({}, {"X-Broker-Token": ""}, {"X-Broker-Token": "anything"}):
            resp = _post(c, path, body, hdr)
            assert resp.status_code == 401
            assert resp.json()["error"] == "Unauthorized"

    def test_cross_origin_post_is_refused_even_with_token(self, client, path, body):
        c, _, _ = client
        resp = _post(c, path, body, {"X-Broker-Token": TOKEN, "Origin": "http://evil.example"})
        assert resp.status_code == 403

    def test_same_origin_post_is_allowed(self, client, path, body):
        c, _, _ = client
        resp = _post(c, path, body, {"X-Broker-Token": TOKEN, "Origin": "http://localhost"})
        assert resp.status_code not in (401, 403)


def test_get_routes_stay_unauthenticated(client):
    c, _, broker = client
    broker.get_status.return_value = []
    assert c.get("/api/cameras/status").status_code == 200
    assert c.get("/api/camera_config").status_code == 200


def test_reload_with_token_reaches_the_broker(client):
    c, _, broker = client
    resp = _post(c, "/api/cameras/reload", None, {"X-Broker-Token": TOKEN})
    assert resp.status_code == 200
    broker.reload_cameras.assert_called_once()


class TestPagesCarryTheToken:
    @pytest.mark.parametrize("path", ["/", "/status", "/controls"])
    def test_page_embeds_token_and_sends_header(self, client, path):
        c, _, _ = client
        resp = c.get(path)
        assert resp.status_code == 200
        assert json.dumps(TOKEN) in resp.text  # const _brokerToken = "..." injection
        assert "X-Broker-Token" in resp.text
        assert resp.headers["cache-control"] == "no-store"

    def test_controls_post_goes_through_auth_headers(self, client):
        c, _, _ = client
        html = c.get("/controls").text
        assert re.search(r"/control`, \{\s*method: 'POST',\s*headers: authHeaders\(", html)

    def test_cameras_posts_go_through_auth_headers(self, client):
        c, _, _ = client
        assert c.get("/").text.count("headers: authHeaders(") == 2


class TestHostGuard:
    def test_rebinding_host_is_refused_on_loopback_bind(self, client):
        c, _, _ = client
        resp = c.get("/controls", headers={"Host": "evil.example:5002"})
        assert resp.status_code == 403
        assert TOKEN not in resp.text

    def test_loopback_names_are_served(self, client):
        c, _, _ = client
        for host in ("localhost:5002", "127.0.0.1:5002", "[::1]:5002"):
            assert c.get("/controls", headers={"Host": host}).status_code == 200

    def test_non_loopback_bind_accepts_any_host(self, client):
        c, srv, _ = client
        srv._bind_host = "0.0.0.0"
        assert c.get("/controls", headers={"Host": "cam-box.lan:5002"}).status_code == 200


class TestHelpers:
    def test_loopback_detection(self):
        assert all(is_loopback_host(h) for h in ("127.0.0.1", "localhost", "::1", "[::1]", "127.1.2.3"))
        assert not any(is_loopback_host(h) for h in ("0.0.0.0", "192.168.1.5", "cam.lan", "", "::"))

    def test_token_matches_fails_closed(self):
        assert token_matches("abc", "abc")
        assert not token_matches("abd", "abc")
        assert not token_matches(None, "abc")
        assert not token_matches("", "")
        assert not token_matches("abc", "")
        assert token_matches("pässword", "pässword")  # non-ASCII must not raise

    def test_generated_token_when_unset(self):
        for unset in (None, "", "   "):
            tok, generated = resolve_web_token(unset)
            assert generated and len(tok) >= 32
        a, _ = resolve_web_token(None)
        b, _ = resolve_web_token(None)
        assert a != b

    def test_configured_token_is_used_as_is(self):
        assert resolve_web_token(" abc ") == ("abc", False)

    def test_generated_token_logged_without_its_value(self, caplog):
        tok, generated = resolve_web_token(None)
        with caplog.at_level("INFO", logger="my_depthai.broker.auth"):
            log_bind_and_token("127.0.0.1", generated)
        assert "generated" in caplog.text
        assert tok not in caplog.text
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

    def test_non_loopback_bind_warns(self, caplog):
        with caplog.at_level("INFO", logger="my_depthai.broker.auth"):
            log_bind_and_token("0.0.0.0", False)
        assert [r for r in caplog.records if r.levelname == "WARNING" and "non-loopback" in r.message]

    def test_host_and_origin_helpers(self):
        assert host_header_allowed("localhost:5002", "127.0.0.1")
        assert not host_header_allowed("evil.example", "127.0.0.1")
        assert not host_header_allowed(None, "127.0.0.1")
        assert host_header_allowed("anything", "0.0.0.0")
        assert origin_allowed(None, "localhost:5002")
        assert origin_allowed("http://localhost:5002", "localhost:5002")
        assert not origin_allowed("http://evil.example", "localhost:5002")


class TestSettings:
    def test_defaults_are_loopback_and_no_token(self):
        s = BrokerSettings()
        assert s.web_host == "127.0.0.1"
        assert s.web_token is None
        assert "web_token" not in s.broker_kwargs()

    def test_token_is_not_in_repr(self):
        assert "hunter2" not in repr(BrokerSettings(web_token="hunter2"))

    def test_config_defaults_to_loopback_and_reads_token(self, monkeypatch):
        from shared.core.config_loader import ConfigLoader

        monkeypatch.delenv("CAMERA_BROKER_HOST", raising=False)
        monkeypatch.delenv("CAMERA_BROKER_TOKEN", raising=False)
        s = BrokerSettings.from_config(ConfigLoader())
        assert s.web_host == "127.0.0.1"
        assert s.web_token is None

        monkeypatch.setenv("CAMERA_BROKER_TOKEN", "12345")  # all digits: YAML would type it int
        monkeypatch.setenv("CAMERA_BROKER_HOST", "0.0.0.0")
        s = BrokerSettings.from_config(ConfigLoader())
        assert s.web_token == "12345"
        assert s.web_host == "0.0.0.0"


class TestReviveRoute:
    """the status page's Revive button posts here; it forces an immediate reconnect."""

    def _setup(self, broker: MagicMock, revived: bool) -> None:
        broker.camera_manager.config = {"cam0": {"id": 0}}
        broker.camera_manager.revive_camera.return_value = revived
        broker.camera_manager.get_camera_state.return_value = "online" if revived else "offline"

    def test_revives_a_known_camera_and_reports_state(self, client):
        c, _, broker = client
        self._setup(broker, True)
        resp = _post(c, "/api/cameras/cam0/revive", None, {"X-Broker-Token": TOKEN})
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "state": "online"}
        broker.camera_manager.revive_camera.assert_called_once_with("cam0")

    def test_failed_reconnect_is_reported_not_raised(self, client):
        c, _, broker = client
        self._setup(broker, False)
        resp = _post(c, "/api/cameras/cam0/revive", None, {"X-Broker-Token": TOKEN})
        assert resp.status_code == 200
        assert resp.json() == {"success": False, "state": "offline"}

    def test_unknown_camera_is_404(self, client):
        c, _, broker = client
        self._setup(broker, True)
        resp = _post(c, "/api/cameras/nope/revive", None, {"X-Broker-Token": TOKEN})
        assert resp.status_code == 404
        broker.camera_manager.revive_camera.assert_not_called()

    def test_status_page_shows_state_and_wires_revive_through_auth_headers(self, client):
        c, _, _ = client
        html = c.get("/status").text
        assert "cam.state" in html
        assert re.search(r"/revive`, \{\s*method: 'POST',\s*headers: authHeaders\(", html)
