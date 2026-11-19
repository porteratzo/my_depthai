"""BrokerSettings: one resolved view of the broker's config (precedence: CLI > config > default)."""

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest

from mydepthai.broker.settings import BrokerSettings
from shared.core.config_loader import ConfigLoader

pytestmark = pytest.mark.unit


def test_from_config_reads_redis_stream_and_broker_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_HOST", "redis.example")
    monkeypatch.setenv("REDIS_PORT", "16379")
    monkeypatch.setenv("CAMERA_FPS", "7")
    monkeypatch.setenv("JPEG_QUALITY", "60")
    monkeypatch.setenv("CAMERA_BROKER_HOST", "127.0.0.1")
    monkeypatch.setenv("CAMERA_BROKER_PORT", "15002")

    s = BrokerSettings.from_config(ConfigLoader())

    assert (s.redis_host, s.redis_port) == ("redis.example", 16379)
    assert (s.fps, s.jpeg_quality) == (7, 60)
    assert (s.web_host, s.web_port) == ("127.0.0.1", 15002)
    assert s.stream_name == "camera_frames"
    assert s.maxlen == 100  # redis.streams.camera_frames.maxlen


def test_overrides_win_and_none_means_unset() -> None:
    base = BrokerSettings(fps=5, redis_host="a")
    out = base.with_overrides(fps=3, redis_host=None, maxlen=None)
    assert (out.fps, out.redis_host, out.maxlen) == (3, "a", 100)
    assert base.fps == 5  # frozen: the original is untouched


def test_unknown_override_is_rejected() -> None:
    with pytest.raises(TypeError):
        BrokerSettings().with_overrides(fsp=3)


def test_broker_kwargs_match_camera_broker_constructor() -> None:
    """Guards drift: every setting (except the web bind) must be a CameraBroker parameter."""
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).parent.parent / "mydepthai" / "broker" / "camera_broker.py"
    spec = importlib.util.spec_from_file_location("camera_broker_settings_drift", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    params = set(inspect.signature(mod.CameraBroker.__init__).parameters) - {"self"}
    assert set(BrokerSettings().broker_kwargs()) <= params


def test_init_app_reuses_the_passed_config_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hygiene: one ConfigLoader per process; the web UI must not build a second one."""
    pytest.importorskip("fastapi")
    from mydepthai.web import server

    loader: Any = MagicMock()
    loader.get.return_value = [{"label": "Main", "url": "http://main.example"}]
    created = MagicMock()
    monkeypatch.setattr("shared.core.config_loader.ConfigLoader", created)

    server.init_app(MagicMock(), loader)

    created.assert_not_called()
    loader.load.assert_not_called()
    assert server._nav_links == [{"label": "Main", "url": "http://main.example"}]
