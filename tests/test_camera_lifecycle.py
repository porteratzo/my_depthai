"""Lifecycle tests: repeated reconnects must not leak pipelines or device handles.

Uses a small stateful fake of the DepthAI layer (every ``Device()``/``Pipeline()`` call yields a
fresh object that records how often it was exited/closed) driven through the real
``CameraManager`` and ``BasicDAICamera``. The mock-everything unit tests cannot see a handle that
is opened and never closed; this can.
"""

from typing import Any, List
from unittest.mock import MagicMock

import pytest

import mydepthai.depthai_controllers.basic_dai_camera as bdc_mod
from mydepthai.camera_manager.camera_manager import CameraManager

pytestmark = pytest.mark.unit


class _FakeDai:
    """Stand-in for the ``dai`` module that remembers every device/pipeline it handed out."""

    def __init__(self) -> None:
        self.devices: List[MagicMock] = []
        self.pipelines: List[MagicMock] = []
        self.UsbSpeed = MagicMock()
        info = MagicMock()
        info.name = "Test Device"
        info.getDeviceId.return_value = "mxid"
        self.Device = MagicMock(side_effect=self._device)
        self.Device.getAllAvailableDevices.return_value = [info]
        self.Pipeline = MagicMock(side_effect=self._pipeline)
        self.DeviceInfo = MagicMock()

    def _device(self, *_a: Any, **_k: Any) -> MagicMock:
        dev = MagicMock()
        dev.isClosed.return_value = False
        self.devices.append(dev)
        return dev

    def _pipeline(self, *_a: Any, **_k: Any) -> MagicMock:
        pipe = MagicMock()
        self.pipelines.append(pipe)
        return pipe


@pytest.fixture()
def fake_dai(monkeypatch: pytest.MonkeyPatch) -> _FakeDai:
    fake = _FakeDai()
    monkeypatch.setattr(bdc_mod, "dai", fake)
    monkeypatch.setattr(bdc_mod, "CameraModule", MagicMock())
    return fake


def _manager() -> CameraManager:
    cfg = {"cam1": {"id": 0, "camera_type": "color", "outputs": ["preview"], "wait_timeout": 5.0}}
    return CameraManager(config_dict=cfg, threaded_init=False, auto_reconnect=False)


def test_reconnect_cycles_release_every_old_pipeline_and_device(fake_dai: _FakeDai) -> None:
    manager = _manager()
    assert manager._initialize_single_camera("cam1", manager.config["cam1"], False) is True

    for _ in range(3):
        assert manager._reconnect_camera("cam1") is True

    assert len(fake_dai.pipelines) == 4 and len(fake_dai.devices) == 4
    # The three superseded cameras were torn down exactly once each ...
    for pipe, dev in zip(fake_dai.pipelines[:3], fake_dai.devices[:3]):
        pipe.__exit__.assert_called_once()
        dev.close.assert_called_once()
    # ... and the live one is still running.
    fake_dai.pipelines[3].__exit__.assert_not_called()
    fake_dai.devices[3].close.assert_not_called()

    manager.close_all()
    for pipe, dev in zip(fake_dai.pipelines, fake_dai.devices):
        pipe.__exit__.assert_called_once()  # not re-exited by close_all
        dev.close.assert_called_once()


def test_close_all_twice_is_safe(fake_dai: _FakeDai) -> None:
    manager = _manager()
    manager._initialize_single_camera("cam1", manager.config["cam1"], False)
    manager.close_all()
    manager.close_all()
    fake_dai.pipelines[0].__exit__.assert_called_once()
    fake_dai.devices[0].close.assert_called_once()


def test_failed_connect_does_not_leak_device(fake_dai: _FakeDai) -> None:
    fake_dai.Pipeline.side_effect = RuntimeError("pipeline boom")
    manager = CameraManager(
        config_dict={"cam1": {"id": 0, "outputs": ["preview"], "wait_timeout": 0.3, "max_retries": 1}},
        threaded_init=False,
        auto_reconnect=False,
    )
    assert manager._initialize_single_camera("cam1", manager.config["cam1"], False) is False
    assert fake_dai.devices, "a device was opened"
    for dev in fake_dai.devices:
        dev.close.assert_called()


def test_initialize_registers_camera_and_close_all_empties_manager(fake_dai: _FakeDai) -> None:
    """Replaces the old no-op "starts with mocked device" test: drive a real connect + close."""
    manager = _manager()
    assert len(manager) == 0 and "cam1" not in manager
    assert manager._initialize_single_camera("cam1", manager.config["cam1"], False) is True
    assert len(manager) == 1 and "cam1" in manager
    manager.close_all()
    assert len(manager) == 0 and "cam1" not in manager
