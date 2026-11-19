"""Review 2026-10-21 §1.9: ``maxlen: 0`` means unlimited, so it must not reach ``XADD`` as 0."""

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

pytestmark = pytest.mark.unit

_BROKER_PATH = Path(__file__).parent.parent / "mydepthai" / "broker" / "camera_broker.py"


@pytest.fixture(scope="module")
def broker_mod() -> Any:
    mod_name = "my_depthai_camera_broker_maxlen_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _BROKER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("configured, expected", [(100, 100), (0, None), (-1, None)])
def test_xadd_maxlen_zero_means_untrimmed(
    broker_mod: Any, monkeypatch: pytest.MonkeyPatch, configured: int, expected: Any
) -> None:
    client = MagicMock()
    client.xadd.return_value = b"1-0"
    monkeypatch.setattr(broker_mod.CameraBroker, "_create_redis_client", lambda self: client)
    broker = broker_mod.CameraBroker(maxlen=configured, fps=10, jpeg_quality=85)
    assert broker._xadd_maxlen() == expected

    assert broker.publish_frame("cam1", "color", "preview", np.zeros((48, 64, 3), dtype=np.uint8))
    assert client.xadd.call_args.kwargs["maxlen"] == expected
