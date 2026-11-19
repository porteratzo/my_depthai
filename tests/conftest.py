"""Pytest configuration and fixtures for my_depthai tests."""

import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Import-time depthai stub
# ---------------------------------------------------------------------------
# Installed once, at conftest import time (before test modules are collected), so test files
# that import from ``mydepthai`` at module top level never trigger a real ``import depthai``
# (which needs USB access and prints driver warnings). It must be import-time rather than a
# fixture: ``mydepthai.*`` binds ``dai`` when it is first imported during collection.
#
# Nothing here is torn down: the stub has to outlive collection, and tests that need a different
# ``dai`` patch the module attribute (``monkeypatch.setattr(bdc_mod, "dai", fake)``). Tests that
# need the *real* SDK run it in a fresh interpreter (test_depthai_sdk_surface.py,
# test_depthai_real_host.py) so this stub can never hide an SDK signature change from them.
if "depthai" not in sys.modules:
    _mock_dai = MagicMock()
    _mock_dai.UsbSpeed = MagicMock()
    _mock_dai.UsbSpeed.SUPER = "SUPER"
    _mock_dai.UsbSpeed.HIGH = "HIGH"
    sys.modules["depthai"] = _mock_dai


# The only project config the broker reads. A consuming project supplies the same keys in its own
# ``config/`` (``${ENV:default}`` lets an environment variable override each one).
_REDIS_YAML = """\
redis:
  host: ${REDIS_HOST:localhost}
  port: ${REDIS_PORT:6379}
  db: ${REDIS_DB:0}
  password: ${REDIS_PASSWORD:}
  streams:
    camera_frames:
      name: camera_frames
      maxlen: 100
"""
_SERVICES_YAML = """\
services:
  camera_broker:
    host: ${CAMERA_BROKER_HOST:127.0.0.1}
    port: ${CAMERA_BROKER_PORT:5002}
    token: ${CAMERA_BROKER_TOKEN:}
    fps: ${CAMERA_FPS:5}
    jpeg_quality: ${JPEG_QUALITY:85}
"""


@pytest.fixture(autouse=True)
def _isolated_project_root(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``ConfigLoader`` at a tiny project root, so this suite needs no ``full_tep`` checkout.

    ``ConfigLoader`` has no cwd fallback: it needs ``root=`` or ``TEP_PROJECT_ROOT``. The root written
    here holds only the redis/broker keys above, so the tests also pass in this module's own repo.
    """
    root = tmp_path_factory.mktemp("project_root")
    (root / "config").mkdir()
    (root / "config" / "redis.yaml").write_text(_REDIS_YAML, encoding="utf-8")
    (root / "config" / "services.yaml").write_text(_SERVICES_YAML, encoding="utf-8")
    monkeypatch.setenv("TEP_PROJECT_ROOT", str(root))


@pytest.fixture
def sample_frame() -> np.ndarray:
    """Generate a sample frame for testing."""
    return np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)


@pytest.fixture
def sample_disparity() -> np.ndarray:
    """Generate a sample disparity map for testing."""
    return np.random.randint(0, 95, (400, 640), dtype=np.uint16)
