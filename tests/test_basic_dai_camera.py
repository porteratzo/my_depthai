"""Unit tests for mydepthai.depthai_controllers.basic_dai_camera.BasicDAICamera.

depthai is stubbed at import time by conftest.py. Per-test control over connection behaviour is applied via
``monkeypatch.setattr`` on the module's own ``dai`` binding and its ``CameraModule``
import, both reverted automatically after each test.
"""

from contextlib import ExitStack
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

import mydepthai.depthai_controllers.basic_dai_camera as bdc_mod
from mydepthai.depthai_controllers.basic_dai_camera import (
    BasicDAICamera,
    _is_ip_address,
    reconnect_callback,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _device_info(mxid: str = "test-mxid", name: str = "Test Device") -> MagicMock:
    info = MagicMock()
    info.mxid = mxid
    info.name = name
    info.getDeviceId.return_value = mxid
    return info


@pytest.fixture()
def fake_dai(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A fresh, fully-controllable ``dai`` stand-in bound onto the module under test."""
    fake = MagicMock()
    fake.UsbSpeed.SUPER = "SUPER"
    fake.UsbSpeed.HIGH = "HIGH"
    fake.Device.getAllAvailableDevices.return_value = [_device_info()]
    fake.Device.return_value = MagicMock()
    fake.Pipeline.return_value = MagicMock()
    fake.DeviceInfo.side_effect = lambda name: _device_info(name=name)
    monkeypatch.setattr(bdc_mod, "dai", fake)
    return fake


@pytest.fixture()
def fake_camera_module(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace CameraModule with a MagicMock class; each instantiation returns a fresh mock."""
    instances: list[MagicMock] = []

    def _factory(cam_type: str, **_: Any) -> MagicMock:
        inst = MagicMock()
        inst.cam_type = cam_type
        inst.outputs_config = {}

        def _add_output(output_type: str, size: Any = None, fps: Any = None) -> None:
            inst.outputs_config[output_type] = {"size": size, "fps": fps}

        inst.add_output.side_effect = _add_output
        instances.append(inst)
        return inst

    factory_mock = MagicMock(side_effect=_factory)
    factory_mock.instances = instances
    monkeypatch.setattr(bdc_mod, "CameraModule", factory_mock)
    return factory_mock


def _make_camera(
    fake_dai: MagicMock, identifier: Any = 0, speed: bool = False, exit_stack: Any = None
) -> BasicDAICamera:
    """Construct a BasicDAICamera and wait for its connection thread to settle."""
    cam = BasicDAICamera(identifier=identifier, speed=speed, exit_stack=exit_stack)
    cam.aquire_thread.join(timeout=5)
    return cam


# ---------------------------------------------------------------------------
# _is_ip_address
# ---------------------------------------------------------------------------


class TestIsIpAddress:
    @pytest.mark.parametrize(
        "value",
        ["192.168.1.1", "0.0.0.0", "255.255.255.255", "10.0.0.1"],
    )
    def test_valid_ipv4_returns_true(self, value: str) -> None:
        assert _is_ip_address(value) is True

    @pytest.mark.parametrize(
        "value",
        ["not-an-ip", "Test Device", "", "192.168.1", "192.168.1.1.1", "abc.def.ghi.jkl"],
    )
    def test_non_ipv4_returns_false(self, value: str) -> None:
        assert _is_ip_address(value) is False


class TestReconnectCallback:
    def test_does_not_raise(self) -> None:
        reconnect_callback(MagicMock())


# ---------------------------------------------------------------------------
# __init__ — device resolution
# ---------------------------------------------------------------------------


class TestInitDeviceResolution:
    def test_default_identifier_uses_first_available_device(self, fake_dai: MagicMock) -> None:
        info = _device_info(mxid="abc")
        fake_dai.Device.getAllAvailableDevices.return_value = [info]
        cam = _make_camera(fake_dai)
        assert cam.device_info is info

    def test_int_identifier_selects_indexed_device(self, fake_dai: MagicMock) -> None:
        info0, info1 = _device_info(mxid="d0"), _device_info(mxid="d1")
        fake_dai.Device.getAllAvailableDevices.return_value = [info0, info1]
        cam = _make_camera(fake_dai, identifier=1)
        assert cam.device_info is info1

    def test_int_identifier_out_of_range_raises_index_error(self, fake_dai: MagicMock) -> None:
        fake_dai.Device.getAllAvailableDevices.return_value = []
        with pytest.raises(IndexError):
            BasicDAICamera(identifier=0)

    def test_string_ip_address_builds_device_info_directly(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai, identifier="192.168.1.50")
        fake_dai.DeviceInfo.assert_called_once_with("192.168.1.50")
        assert cam.device_info.name == "192.168.1.50"

    def test_string_mxid_matches_available_device(self, fake_dai: MagicMock) -> None:
        info0, info1 = _device_info(mxid="aaa"), _device_info(mxid="bbb")
        fake_dai.Device.getAllAvailableDevices.return_value = [info0, info1]
        cam = _make_camera(fake_dai, identifier="bbb")
        assert cam.device_info is info1

    def test_string_mxid_not_found_raises_value_error(self, fake_dai: MagicMock) -> None:
        fake_dai.Device.getAllAvailableDevices.return_value = [_device_info(mxid="aaa")]
        with pytest.raises(ValueError):
            BasicDAICamera(identifier="does-not-exist")

    def test_initial_state_before_connection_completes(self, fake_dai: MagicMock) -> None:
        # Don't join — inspect state as set synchronously inside __init__.
        cam = BasicDAICamera(identifier=0)
        assert cam.connection_error is None
        assert cam.components == {}
        cam.aquire_thread.join(timeout=5)


# ---------------------------------------------------------------------------
# thread_get_device — connection outcomes
# ---------------------------------------------------------------------------


class TestThreadGetDevice:
    def test_successful_connection_sets_connected_true(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        assert cam.connected is True
        assert cam.device is not None
        assert cam.pipeline is not None

    def test_speed_true_uses_super_speed(self, fake_dai: MagicMock) -> None:
        _make_camera(fake_dai, speed=True)
        _, kwargs = fake_dai.Device.call_args
        assert kwargs["maxUsbSpeed"] == "SUPER"

    def test_speed_false_uses_high_speed(self, fake_dai: MagicMock) -> None:
        _make_camera(fake_dai, speed=False)
        _, kwargs = fake_dai.Device.call_args
        assert kwargs["maxUsbSpeed"] == "HIGH"

    def test_ip_device_connects_without_usb_speed_kwarg(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai, identifier="10.0.0.5")
        args, kwargs = fake_dai.Device.call_args
        assert "maxUsbSpeed" not in kwargs
        assert cam.connected is True

    def test_first_attempt_runtime_error_falls_back_to_high_speed(self, fake_dai: MagicMock) -> None:
        fake_dai.Device.side_effect = [RuntimeError("boom"), MagicMock()]
        cam = _make_camera(fake_dai, speed=True)
        assert cam.connected is True
        assert fake_dai.Device.call_count == 2
        # second (fallback) call must use HIGH speed regardless of the requested speed
        _, kwargs = fake_dai.Device.call_args
        assert kwargs["maxUsbSpeed"] == "HIGH"

    def test_both_attempts_failing_marks_disconnected(self, fake_dai: MagicMock) -> None:
        err = RuntimeError("still broken")
        fake_dai.Device.side_effect = [RuntimeError("first"), err]
        cam = _make_camera(fake_dai)
        assert cam.connected is False
        assert cam.connection_error is err

    def test_exit_stack_used_when_provided(self, fake_dai: MagicMock) -> None:
        stack = ExitStack()
        cam = _make_camera(fake_dai, exit_stack=stack)
        assert cam.connected is True
        # ExitStack.enter_context() calls __enter__ on our behalf, so the
        # pipeline is still entered — but its __exit__ is now owned by the
        # stack rather than by BasicDAICamera.close() (see TestClose).
        cam.pipeline.__enter__.assert_called_once()

    def test_no_exit_stack_enters_pipeline_directly(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai, exit_stack=None)
        cam.pipeline.__enter__.assert_called_once()

    def test_max_reconnection_attempts_configured(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        cam.device.setMaxReconnectionAttempts.assert_called_once_with(3, reconnect_callback)


# ---------------------------------------------------------------------------
# reconnect()
# ---------------------------------------------------------------------------


class TestReconnect:
    def test_reconnect_starts_new_thread_and_reconnects(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        assert cam.connected is True
        old_thread = cam.aquire_thread
        cam.reconnect(timeout=5)
        assert cam.aquire_thread is not old_thread
        assert cam.connected is True

    def test_reconnect_after_failed_connection_retries(self, fake_dai: MagicMock) -> None:
        fake_dai.Device.side_effect = RuntimeError("down")
        cam = _make_camera(fake_dai)
        assert cam.connected is False

        fake_dai.Device.side_effect = None
        fake_dai.Device.return_value = MagicMock()
        cam.reconnect(timeout=5)
        assert cam.connected is True


# ---------------------------------------------------------------------------
# cam_ready() / wait_for_cam()
# ---------------------------------------------------------------------------


class TestCamReady:
    def test_true_when_connected(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        assert cam.cam_ready() is True

    def test_false_when_not_connected(self, fake_dai: MagicMock) -> None:
        fake_dai.Device.side_effect = RuntimeError("down")
        cam = _make_camera(fake_dai)
        assert cam.cam_ready() is False

    def test_true_while_device_open(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        cam.device.isClosed.return_value = False
        assert cam.cam_ready() is True

    def test_cleared_once_device_reports_closed(self, fake_dai: MagicMock) -> None:
        """`connected` was set once and never cleared when the device went away."""
        cam = _make_camera(fake_dai)
        cam.device.isClosed.return_value = True
        assert cam.cam_ready() is False
        assert cam.connected is False


class TestWaitForCam:
    def test_returns_true_immediately_when_already_connected(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        assert cam.wait_for_cam(timeout=1.0) is True

    def test_returns_false_when_never_connects_and_reconnect_disabled(self, fake_dai: MagicMock) -> None:
        fake_dai.Device.side_effect = RuntimeError("down")
        cam = _make_camera(fake_dai)
        assert cam.wait_for_cam(timeout=0.3, reconnect_on_timeout=False) is False

    def test_gives_up_after_max_retries_on_persistent_error(
        self, fake_dai: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_dai.Device.side_effect = RuntimeError("down")
        cam = _make_camera(fake_dai)
        # reconnect() would spin up real threads that reconnect via the same
        # broken mock; keep it fast and deterministic by stubbing it out.
        monkeypatch.setattr(cam, "reconnect", MagicMock())
        result = cam.wait_for_cam(timeout=0.3, reconnect_on_timeout=True, max_retries=2)
        assert result is False
        assert cam.reconnect.call_count >= 1


# ---------------------------------------------------------------------------
# create_camera() / create_output()
# ---------------------------------------------------------------------------


class TestCreateCamera:
    def test_creates_component_with_default_outputs(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock
    ) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color")
        assert "color" in cam.components
        fake_camera_module.assert_called_once_with(cam_type="color")
        component = cam.components["color"]
        assert component.add_output.call_count == 2  # default ["preview", "video"]

    def test_creates_component_with_custom_outputs(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock
    ) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color", outputs=["still"], sizes=[(640, 480)], fpss=[10])
        component = cam.components["color"]
        component.add_output.assert_called_once_with("still", size=(640, 480), fps=10)

    def test_duplicate_cam_type_raises_assertion_error(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock
    ) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color")
        with pytest.raises(AssertionError):
            cam.create_camera("color")

    def test_joins_connection_thread_before_creating_component(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock
    ) -> None:
        cam = BasicDAICamera(identifier=0)
        cam.create_camera("color")  # should join internally, not race
        assert cam.connected is True


class TestCreateOutput:
    def test_delegates_to_existing_component(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock
    ) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color")
        cam.create_output("color", "still", size=(100, 100), fps=5)
        cam.components["color"].add_output.assert_called_with("still", size=(100, 100), fps=5)


# ---------------------------------------------------------------------------
# build()
# ---------------------------------------------------------------------------


class TestBuild:
    def test_builds_recognized_camera_types(self, fake_dai: MagicMock, fake_camera_module: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color")
        cam.build()
        cam.components["color"].build.assert_called_once_with(cam.pipeline)

    def test_skips_unrecognized_component_types(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock
    ) -> None:
        cam = _make_camera(fake_dai)
        cam.components["unknown_widget"] = MagicMock()
        cam.build()
        cam.components["unknown_widget"].build.assert_not_called()

    def test_builds_detector_prefixed_components(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock
    ) -> None:
        cam = _make_camera(fake_dai)
        cam.components["detector_yolo"] = MagicMock()
        cam.build()
        cam.components["detector_yolo"].build.assert_called_once_with(cam.pipeline)


# ---------------------------------------------------------------------------
# start_device()
# ---------------------------------------------------------------------------


class TestStartDevice:
    def test_starts_pipeline_and_components_when_connected(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock
    ) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color")
        cam.device.getMxId.return_value = "mx123"
        cam.device.getConnectedCameras.return_value = []
        cam.device.getUsbSpeed.return_value = MagicMock(name="SUPER")

        cam.start_device()

        cam.pipeline.start.assert_called_once()
        cam.components["color"].start_device.assert_called_once()

    def test_does_nothing_when_not_connected(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock
    ) -> None:
        fake_dai.Device.side_effect = RuntimeError("down")
        cam = _make_camera(fake_dai)
        cam.components["color"] = MagicMock()
        cam.start_device()  # must not raise despite pipeline/device being None
        cam.components["color"].start_device.assert_not_called()


# ---------------------------------------------------------------------------
# get_frame() / get_data()
# ---------------------------------------------------------------------------


class TestGetFrame:
    def test_returns_frame_from_component(self, fake_dai: MagicMock, fake_camera_module: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color")
        frame = np.zeros((10, 10, 3), dtype=np.uint8)
        cam.components["color"].outputs_config = {"preview": {"fps": 30}}
        cam.components["color"].get_frame.return_value = frame

        result = cam.get_frame("color", "preview")
        assert result is frame

    def test_auto_computes_timeout_from_fps(self, fake_dai: MagicMock, fake_camera_module: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color")
        cam.components["color"].outputs_config = {"preview": {"fps": 20}}
        cam.components["color"].get_frame.return_value = np.zeros((2, 2, 3))

        cam.get_frame("color", "preview")
        cam.components["color"].get_frame.assert_called_once_with("preview", timeout=4 / 20)

    def test_explicit_timeout_is_respected(self, fake_dai: MagicMock, fake_camera_module: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color")
        cam.components["color"].outputs_config = {"preview": {"fps": 20}}
        cam.components["color"].get_frame.return_value = np.zeros((2, 2, 3))

        cam.get_frame("color", "preview", timeout=1.5)
        cam.components["color"].get_frame.assert_called_once_with("preview", timeout=1.5)

    def test_none_frame_raises_assertion_error(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock
    ) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color")
        cam.components["color"].outputs_config = {"preview": {"fps": 10}}
        cam.components["color"].get_frame.return_value = None

        with pytest.raises(AssertionError):
            cam.get_frame("color", "preview")

    def test_unknown_cam_raises_value_error(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        with pytest.raises(ValueError):
            cam.get_frame("thermal", "preview")


class TestGetFrameWithMetadata:
    def test_delegates_to_component(self, fake_dai: MagicMock, fake_camera_module: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color")
        frame = np.zeros((10, 10, 3), dtype=np.uint8)
        metadata = {"lens_position": 1, "exposure_time_us": 8000.0, "iso": 400}
        cam.components["color"].outputs_config = {"preview": {"fps": 30}}
        cam.components["color"].get_frame_with_metadata.return_value = (frame, metadata)

        result = cam.get_frame_with_metadata("color", "preview")
        assert result == (frame, metadata)

    def test_auto_computes_timeout_from_fps(self, fake_dai: MagicMock, fake_camera_module: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color")
        cam.components["color"].outputs_config = {"preview": {"fps": 20}}
        cam.components["color"].get_frame_with_metadata.return_value = (np.zeros((2, 2, 3)), {})

        cam.get_frame_with_metadata("color", "preview")
        cam.components["color"].get_frame_with_metadata.assert_called_once_with("preview", timeout=4 / 20)

    def test_returns_none_without_raising_when_component_returns_none(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock
    ) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color")
        cam.components["color"].outputs_config = {"preview": {"fps": 10}}
        cam.components["color"].get_frame_with_metadata.return_value = None

        assert cam.get_frame_with_metadata("color", "preview") is None

    def test_unknown_cam_raises_value_error(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        with pytest.raises(ValueError):
            cam.get_frame_with_metadata("thermal", "preview")


class TestGetData:
    def test_returns_data_from_component(self, fake_dai: MagicMock, fake_camera_module: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color")
        sentinel = MagicMock()
        cam.components["color"].get_data.return_value = sentinel

        assert cam.get_data("color", "preview") is sentinel
        cam.components["color"].get_data.assert_called_once_with("preview", timeout=True)

    def test_unknown_cam_raises_value_error(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        with pytest.raises(ValueError):
            cam.get_data("thermal")


# ---------------------------------------------------------------------------
# get_still() / get_still_no_block() / trigger_still() / trigger_and_get_still()
# ---------------------------------------------------------------------------


@pytest.fixture()
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Still-image polling sleeps in real time; make tests instant."""
    monkeypatch.setattr(bdc_mod.time, "sleep", lambda *_a, **_k: None)


class TestGetStill:
    def test_requires_color_camera_configured(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        with pytest.raises(AssertionError):
            cam.get_still()

    def test_requires_still_output_configured(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock
    ) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color", outputs=["preview"])
        with pytest.raises(AssertionError):
            cam.get_still()

    def test_returns_frame_when_data_available(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock, no_sleep: None
    ) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color", outputs=["still"])
        cv_frame = np.zeros((5, 5, 3))
        raw = MagicMock()
        raw.getCvFrame.return_value = cv_frame
        cam.components["color"].has_data.return_value = True
        cam.components["color"].get_data.return_value = raw

        result = cam.get_still()
        assert result is cv_frame

    def test_returns_none_on_timeout(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock, no_sleep: None
    ) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color", outputs=["still"])
        cam.components["color"].has_data.return_value = False

        assert cam.get_still() is None


class TestGetStillNoBlock:
    def test_returns_none_when_no_data_queued(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock
    ) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color", outputs=["still"])
        cam.components["color"].has_data.return_value = False
        assert cam.get_still_no_block() is None

    def test_returns_frame_when_data_queued(self, fake_dai: MagicMock, fake_camera_module: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color", outputs=["still"])
        cv_frame = np.ones((3, 3))
        raw = MagicMock()
        raw.getCvFrame.return_value = cv_frame
        cam.components["color"].has_data.return_value = True
        cam.components["color"].get_data.return_value = raw

        assert cam.get_still_no_block() is cv_frame


class TestTriggerStill:
    def test_requires_color_camera(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        with pytest.raises(AssertionError):
            cam.trigger_still()

    def test_requires_still_output(self, fake_dai: MagicMock, fake_camera_module: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color", outputs=["preview"])
        with pytest.raises(AssertionError):
            cam.trigger_still()

    def test_triggers_still_on_component(self, fake_dai: MagicMock, fake_camera_module: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color", outputs=["still"])
        cam.trigger_still()
        cam.components["color"].trigger_still.assert_called_once()


class TestTriggerAndGetStill:
    def test_triggers_then_returns_still(
        self, fake_dai: MagicMock, fake_camera_module: MagicMock, no_sleep: None
    ) -> None:
        cam = _make_camera(fake_dai)
        cam.create_camera("color", outputs=["still"])
        cv_frame = np.ones((4, 4))
        raw = MagicMock()
        raw.getCvFrame.return_value = cv_frame
        cam.components["color"].has_data.return_value = True
        cam.components["color"].get_data.return_value = raw

        result = cam.trigger_and_get_still()
        cam.components["color"].trigger_still.assert_called_once()
        assert result is cv_frame


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


class TestClose:
    def test_closes_pipeline_when_no_exit_stack(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        cam.close()
        cam.pipeline.__exit__.assert_called_once_with(None, None, None)

    def test_does_not_double_close_when_exit_stack_owns_pipeline(self, fake_dai: MagicMock) -> None:
        stack = ExitStack()
        cam = _make_camera(fake_dai, exit_stack=stack)
        cam.close()
        cam.pipeline.__exit__.assert_not_called()

    def test_force_closes_even_with_exit_stack(self, fake_dai: MagicMock) -> None:
        stack = ExitStack()
        cam = _make_camera(fake_dai, exit_stack=stack)
        cam.close(force=True)
        cam.pipeline.__exit__.assert_called_once_with(None, None, None)

    def test_close_is_noop_when_pipeline_never_created(self, fake_dai: MagicMock) -> None:
        fake_dai.Device.side_effect = RuntimeError("down")
        cam = _make_camera(fake_dai)
        cam.close()  # must not raise

    # --- device handles are released exactly once -------------------------------

    def test_close_exits_pipeline_then_closes_device(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        order = MagicMock()
        order.attach_mock(cam.pipeline.__exit__, "pipeline_exit")
        order.attach_mock(cam.device.close, "device_close")
        cam.close()
        names = [c[0] for c in order.mock_calls]
        assert names == ["pipeline_exit", "device_close"]

    def test_close_is_idempotent(self, fake_dai: MagicMock) -> None:
        cam = _make_camera(fake_dai)
        cam.close()
        cam.close(force=True)
        cam.pipeline.__exit__.assert_called_once()
        cam.device.close.assert_called_once()

    def test_forced_close_then_stack_unwind_does_not_double_exit(self, fake_dai: MagicMock) -> None:
        stack = ExitStack()
        cam = _make_camera(fake_dai, exit_stack=stack)
        cam.close(force=True)
        stack.close()
        cam.pipeline.__exit__.assert_called_once()
        cam.device.close.assert_called_once()

    def test_stack_unwind_releases_pipeline_and_device(self, fake_dai: MagicMock) -> None:
        stack = ExitStack()
        cam = _make_camera(fake_dai, exit_stack=stack)
        cam.close()  # no-op: the stack owns teardown
        cam.device.close.assert_not_called()
        stack.close()
        cam.pipeline.__exit__.assert_called_once()
        cam.device.close.assert_called_once()

    def test_device_closed_when_pipeline_creation_fails(self, fake_dai: MagicMock) -> None:
        fake_dai.Pipeline.side_effect = RuntimeError("pipeline boom")
        cam = _make_camera(fake_dai)
        assert cam.connected is False
        cam.close()
        cam.device.close.assert_called_once()

    def test_retry_releases_device_from_failed_attempt(self, fake_dai: MagicMock) -> None:
        first, second = MagicMock(name="dev1"), MagicMock(name="dev2")
        fake_dai.Device.side_effect = [first, second]
        fake_dai.Pipeline.side_effect = [RuntimeError("pipeline boom"), MagicMock()]
        cam = _make_camera(fake_dai)
        assert cam.connected is False
        cam.reconnect()
        cam.aquire_thread.join(timeout=5)
        assert cam.connected is True
        first.close.assert_called_once()
        second.close.assert_not_called()
