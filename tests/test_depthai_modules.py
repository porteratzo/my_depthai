"""Tests for low-level DepthAI module classes.

Covers:
- BaseModule   (abstract base with queue helpers)
- CameraFrameModule  (frame retrieval)
- CameraConfigModule (camera settings / control)
- CameraModule  (combined camera node)

`depthai` is stubbed by conftest.py's import-time mock so no hardware is required. Because that
mock accepts any argument, real-SDK behavior (type rejection, queue semantics) is covered
separately in test_depthai_real_host.py.
"""

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# BaseModule
# ---------------------------------------------------------------------------


class TestBaseModule:
    """BaseModule is abstract; we test it via a minimal concrete subclass."""

    def _make_base(self):
        from mydepthai.depthai_controllers.modules.base_module import BaseModule

        class _Concrete(BaseModule):
            pass

        return _Concrete()

    def test_init_creates_empty_dicts(self):
        obj = self._make_base()
        assert obj.outputs == {}
        assert obj.inputs == {}
        assert obj.out_queues == {}
        assert obj.in_queues == {}

    def test_build_is_noop(self):
        obj = self._make_base()
        obj.build()  # should not raise

    def test_start_device_is_noop(self):
        obj = self._make_base()
        obj.start_device()  # should not raise

    def test_send_data_delegates_to_in_queue(self):
        obj = self._make_base()
        mock_queue = MagicMock()
        obj.in_queues["preview"] = mock_queue
        obj.send_data("preview", b"frame_data")
        mock_queue.send.assert_called_once_with(b"frame_data")

    def test_get_data_with_none_timeout_uses_default(self):
        obj = self._make_base()
        mock_queue = MagicMock()
        mock_queue.get.return_value = b"frame"
        obj.out_queues["preview"] = mock_queue
        result = obj.get_data("preview", timeout=None)
        assert result == b"frame"
        # Called with the default timeout (a timedelta)
        mock_queue.get.assert_called_once()

    def test_get_data_with_float_timeout(self):
        obj = self._make_base()
        mock_queue = MagicMock()
        mock_queue.get.return_value = b"frame"
        obj.out_queues["preview"] = mock_queue
        result = obj.get_data("preview", timeout=0.1)
        assert result == b"frame"
        mock_queue.get.assert_called_once()

    @pytest.mark.parametrize("timeout", [0, 0.0, -1])
    def test_get_data_with_zero_timeout_polls_without_blocking(self, timeout):
        """timeout<=0 must use tryGet(), never a blocking get()."""
        obj = self._make_base()
        mock_queue = MagicMock()
        mock_queue.tryGet.return_value = None
        obj.out_queues["preview"] = mock_queue
        assert obj.get_data("preview", timeout=timeout) is None
        mock_queue.tryGet.assert_called_once_with()
        mock_queue.get.assert_not_called()

    def test_has_data_delegates_to_out_queue(self):
        obj = self._make_base()
        mock_queue = MagicMock()
        mock_queue.has.return_value = True
        obj.out_queues["preview"] = mock_queue
        assert obj.has_data("preview") is True

    def test_has_data_returns_false_when_empty(self):
        obj = self._make_base()
        mock_queue = MagicMock()
        mock_queue.has.return_value = False
        obj.out_queues["preview"] = mock_queue
        assert obj.has_data("preview") is False


# ---------------------------------------------------------------------------
# CameraFrameModule
# ---------------------------------------------------------------------------


class TestCameraFrameModule:
    def _make_cfm(self):
        from mydepthai.depthai_controllers.modules.camera_frame_module import (
            CameraFrameModule,
        )

        class _Concrete(CameraFrameModule):
            pass

        return _Concrete()

    def test_init_inherits_base_dicts(self):
        obj = self._make_cfm()
        assert obj.out_queues == {}

    def test_get_frame_returns_none_when_queue_returns_none(self):
        obj = self._make_cfm()
        mock_queue = MagicMock()
        # Simulate the inner get_data returning None via mocked queue
        mock_queue.get.return_value = None
        obj.out_queues["preview"] = mock_queue
        result = obj.get_frame("preview")
        assert result is None

    def test_get_frame_calls_get_cv_frame(self):
        obj = self._make_cfm()
        mock_queue = MagicMock()
        mock_img_frame = MagicMock()
        mock_img_frame.getCvFrame.return_value = b"np_array_placeholder"
        mock_queue.get.return_value = mock_img_frame
        obj.out_queues["preview"] = mock_queue
        result = obj.get_frame("preview")
        assert result == b"np_array_placeholder"
        mock_img_frame.getCvFrame.assert_called_once()

    def test_get_frame_with_metadata_returns_none_when_no_frame(self):
        obj = self._make_cfm()
        mock_queue = MagicMock()
        mock_queue.get.return_value = None
        obj.out_queues["preview"] = mock_queue
        assert obj.get_frame_with_metadata("preview") is None

    def test_get_frame_with_metadata_returns_frame_and_metadata(self):
        from datetime import timedelta

        import numpy as np

        obj = self._make_cfm()
        mock_queue = MagicMock()
        mock_img_frame = MagicMock()
        cv_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        mock_img_frame.getCvFrame.return_value = cv_frame
        mock_img_frame.getLensPosition.return_value = 132
        mock_img_frame.getExposureTime.return_value = timedelta(microseconds=8000)
        mock_img_frame.getSensitivity.return_value = 400
        mock_queue.get.return_value = mock_img_frame
        obj.out_queues["preview"] = mock_queue

        result = obj.get_frame_with_metadata("preview")

        assert result is not None
        frame, metadata = result
        assert frame is cv_frame
        assert metadata == {
            "lens_position": 132,
            "exposure_time_us": 8000.0,
            "iso": 400,
            "frame_width": 640,
            "frame_height": 480,
        }

    def test_get_frame_with_metadata_reads_queue_exactly_once(self):
        # Regression test: queues are consumed, not peekable — a second read
        # would block or silently return a different frame.
        from datetime import timedelta

        import numpy as np

        obj = self._make_cfm()
        mock_queue = MagicMock()
        mock_img_frame = MagicMock()
        mock_img_frame.getCvFrame.return_value = np.zeros((2, 2, 3), dtype="uint8")
        mock_img_frame.getLensPosition.return_value = 1
        mock_img_frame.getExposureTime.return_value = timedelta(microseconds=1)
        mock_img_frame.getSensitivity.return_value = 100
        mock_queue.get.return_value = mock_img_frame
        obj.out_queues["preview"] = mock_queue

        obj.get_frame_with_metadata("preview")

        mock_queue.get.assert_called_once()


# ---------------------------------------------------------------------------
# CameraConfigModule
# ---------------------------------------------------------------------------


class TestCameraConfigModule:
    def _make_ccm(self):
        from mydepthai.depthai_controllers.modules.camera_config_module import (
            CameraConfigModule,
        )

        return CameraConfigModule()

    def test_init_sets_control_queue_to_none(self):
        obj = self._make_ccm()
        assert obj.controlQueue is None

    def test_set_control_queue_stores_queue(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        assert obj.controlQueue is mock_q

    def test_try_set_control_queue_sends_when_queue_set(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        mock_ctrl = MagicMock()
        obj.set_control_queue(mock_q)
        obj.try_set_control_queue(mock_ctrl)
        mock_q.send.assert_called_once_with(mock_ctrl)

    def test_try_set_control_queue_warns_when_no_queue(self, caplog):
        import logging

        obj = self._make_ccm()
        with caplog.at_level(logging.WARNING):
            obj.try_set_control_queue(MagicMock())
        assert "Control queue not set" in caplog.text

    def test_trigger_still_sends_to_still_trigger_queue(self):
        # Regression test: trigger_still must NOT rely on CameraControl /
        # controlQueue (a no-op under depthai v3's Camera node) — it must send
        # to the Script-node trigger queue stored at in_queues["still"].
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.in_queues = {"still": mock_q}
        obj.trigger_still()
        mock_q.send.assert_called_once()

    def test_trigger_still_does_not_use_control_queue(self):
        obj = self._make_ccm()
        control_mock_q = MagicMock()
        obj.set_control_queue(control_mock_q)
        obj.in_queues = {"still": MagicMock()}
        obj.trigger_still()
        control_mock_q.send.assert_not_called()

    def test_trigger_still_warns_when_no_trigger_queue(self, caplog):
        import logging

        obj = self._make_ccm()
        with caplog.at_level(logging.WARNING):
            obj.trigger_still()
        assert "Still trigger queue not set" in caplog.text

    def test_set_exposure_sends_control(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.set_exposure(20000, 400)
        mock_q.send.assert_called_once()

    def test_set_exposure_clips_high_value(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        # Should not raise, clips internally to 33333
        obj.set_exposure(999999, 100)
        mock_q.send.assert_called_once()

    def test_set_auto_exposure_sends_control(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.set_auto_exposure()
        mock_q.send.assert_called_once()

    def test_set_focus_sends_control(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.set_focus(125)
        mock_q.send.assert_called_once()

    def test_set_focus_with_distance_mode(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.set_focus(1.0, distance=True)
        mock_q.send.assert_called_once()

    def test_set_focus_region_sends_control(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.set_Focus_Region(10, 20, 100, 80)
        mock_q.send.assert_called_once()

    def test_set_exposure_region_sends_control(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.set_Exposure_Region(0, 0, 200, 150)
        mock_q.send.assert_called_once()

    def test_trigger_auto_focus_sends_control(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.trigger_auto_focus()
        mock_q.send.assert_called_once()

    def test_trigger_auto_focus_macro_sends_control(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.trigger_auto_focus_macro()
        mock_q.send.assert_called_once()

    def test_trigger_auto_focus_edof_sends_control(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.trigger_auto_focus_EDOF()
        mock_q.send.assert_called_once()

    def test_continuous_auto_focus_sends_control(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.continuous_auto_focus()
        mock_q.send.assert_called_once()

    def test_continuous_pic_auto_focus_sends_control(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.continuous_pic_auto_focus()
        mock_q.send.assert_called_once()

    def test_set_antibanding_with_uppercase_enum_mock(self):
        import mydepthai.depthai_controllers.modules.camera_config_module as ccm_mod

        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)

        # Assign the class itself (not an instance) on the *same* dai reference
        # that camera_config_module holds, so vars() returns the mappingproxy
        # with uppercase class attributes that the list-comprehension filter sees.
        class _Abm:
            MODE_OFF = 0
            HZ50 = 1
            HZ60 = 2
            AUTO = 3

        original_abm = ccm_mod.dai.CameraControl.AntiBandingMode
        try:
            ccm_mod.dai.CameraControl.AntiBandingMode = _Abm
            obj.set_antibanding(option=0)
        finally:
            ccm_mod.dai.CameraControl.AntiBandingMode = original_abm

        mock_q.send.assert_called_once()

    # -----------------------------------------------------------------------
    # Control-state tracking (software-commanded state, no hardware readback)
    # -----------------------------------------------------------------------

    def test_init_sets_default_state(self):
        obj = self._make_ccm()
        state = obj.get_control_state()
        assert state["auto_exposure_enabled"] is True
        assert state["auto_focus_enabled"] is True
        assert state["af_mode"] == "CONTINUOUS_VIDEO"
        assert state["ae_lock"] is False
        assert state["exposure_compensation"] == 0
        assert state["exposure_limit_us"] == 33333
        assert state["af_lens_range"] is None
        assert state["af_region"] is None
        assert state["ae_region"] is None

    def test_get_control_state_returns_copy_not_reference(self):
        obj = self._make_ccm()
        state = obj.get_control_state()
        state["auto_exposure_enabled"] = "mutated"
        assert obj.get_control_state()["auto_exposure_enabled"] is True

    def test_set_exposure_updates_state(self):
        obj = self._make_ccm()
        obj.set_control_queue(MagicMock())
        obj.set_exposure(5000, 200)
        state = obj.get_control_state()
        assert state["auto_exposure_enabled"] is False
        assert state["manual_exposure_us"] == 5000
        assert state["manual_iso"] == 200

    def test_set_auto_exposure_updates_state(self):
        obj = self._make_ccm()
        obj.set_control_queue(MagicMock())
        obj.set_exposure(5000, 200)
        obj.set_auto_exposure()
        state = obj.get_control_state()
        assert state["auto_exposure_enabled"] is True
        assert state["manual_exposure_us"] is None
        assert state["manual_iso"] is None

    def test_set_focus_updates_state(self):
        obj = self._make_ccm()
        obj.set_control_queue(MagicMock())
        obj.set_focus(125)
        state = obj.get_control_state()
        assert state["auto_focus_enabled"] is False
        assert state["af_mode"] == "MANUAL"
        assert state["manual_lens_position"] == 125

    def test_trigger_auto_focus_sets_af_mode_state(self):
        obj = self._make_ccm()
        obj.set_control_queue(MagicMock())
        obj.trigger_auto_focus()
        state = obj.get_control_state()
        assert state["auto_focus_enabled"] is True
        assert state["af_mode"] == "AUTO"

    def test_trigger_auto_focus_macro_sets_af_mode_state(self):
        obj = self._make_ccm()
        obj.set_control_queue(MagicMock())
        obj.trigger_auto_focus_macro()
        assert obj.get_control_state()["af_mode"] == "MACRO"

    def test_trigger_auto_focus_edof_sets_af_mode_state(self):
        obj = self._make_ccm()
        obj.set_control_queue(MagicMock())
        obj.trigger_auto_focus_EDOF()
        assert obj.get_control_state()["af_mode"] == "EDOF"

    def test_continuous_auto_focus_sets_af_mode_state(self):
        obj = self._make_ccm()
        obj.set_control_queue(MagicMock())
        obj.continuous_auto_focus()
        assert obj.get_control_state()["af_mode"] == "CONTINUOUS_VIDEO"

    def test_continuous_pic_auto_focus_sets_af_mode_state(self):
        obj = self._make_ccm()
        obj.set_control_queue(MagicMock())
        obj.continuous_pic_auto_focus()
        assert obj.get_control_state()["af_mode"] == "CONTINUOUS_PICTURE"

    def test_set_focus_region_updates_state(self):
        obj = self._make_ccm()
        obj.set_control_queue(MagicMock())
        obj.set_Focus_Region(10, 20, 100, 80)
        assert obj.get_control_state()["af_region"] == (10, 20, 100, 80)

    def test_set_exposure_region_updates_state(self):
        obj = self._make_ccm()
        obj.set_control_queue(MagicMock())
        obj.set_Exposure_Region(0, 0, 200, 150)
        assert obj.get_control_state()["ae_region"] == (0, 0, 200, 150)

    # -----------------------------------------------------------------------
    # New setters: AE lock, exposure compensation, exposure limit, lens range
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize("locked", [True, False])
    def test_set_auto_exposure_lock_sends_control_and_updates_state(self, locked):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.set_auto_exposure_lock(locked)
        mock_q.send.assert_called_once()
        assert obj.get_control_state()["ae_lock"] is locked

    def test_set_exposure_compensation_sends_control_and_updates_state(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.set_exposure_compensation(5)
        mock_q.send.assert_called_once()
        assert obj.get_control_state()["exposure_compensation"] == 5

    def test_set_exposure_compensation_clips_out_of_range(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.set_exposure_compensation(15)
        assert obj.get_control_state()["exposure_compensation"] == 9
        obj.set_exposure_compensation(-15)
        assert obj.get_control_state()["exposure_compensation"] == -9

    def test_set_auto_exposure_limit_sends_control_and_updates_state(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.set_auto_exposure_limit(10000)
        mock_q.send.assert_called_once()
        assert obj.get_control_state()["exposure_limit_us"] == 10000

    def test_set_auto_exposure_limit_clips_to_range(self):
        obj = self._make_ccm()
        obj.set_control_queue(MagicMock())
        obj.set_auto_exposure_limit(0)
        assert obj.get_control_state()["exposure_limit_us"] == 1
        obj.set_auto_exposure_limit(99999)
        assert obj.get_control_state()["exposure_limit_us"] == 33333

    def test_set_auto_focus_lens_range_valid_sets_state(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.set_auto_focus_lens_range(10, 200)
        mock_q.send.assert_called_once()
        assert obj.get_control_state()["af_lens_range"] == (10, 200)

    def test_set_auto_focus_lens_range_rejects_infinity_ge_macro(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        with pytest.raises(ValueError):
            obj.set_auto_focus_lens_range(200, 10)
        with pytest.raises(ValueError):
            obj.set_auto_focus_lens_range(50, 50)
        mock_q.send.assert_not_called()

    def test_set_auto_focus_lens_range_clips_out_of_bounds_before_ordering_check(self):
        obj = self._make_ccm()
        mock_q = MagicMock()
        obj.set_control_queue(mock_q)
        obj.set_auto_focus_lens_range(-5, 300)
        mock_q.send.assert_called_once()
        assert obj.get_control_state()["af_lens_range"] == (0, 255)


# ---------------------------------------------------------------------------
# CameraModule
# ---------------------------------------------------------------------------


class TestCameraModuleInit:
    def test_color_cam_type(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("color")
        assert mod.cam_type == "color"

    def test_left_cam_type(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("left")
        assert mod.cam_type == "left"

    def test_right_cam_type(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("right")
        assert mod.cam_type == "right"

    def test_default_cam_type_is_color(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule()
        assert mod.cam_type == "color"

    def test_invalid_cam_type_raises_assertion_error(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        with pytest.raises(AssertionError):
            CameraModule("depth")

    def test_outputs_config_starts_empty(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule()
        assert mod.outputs_config == {}

    def test_pipeline_starts_none(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule()
        assert mod.pipeline is None

    def test_node_starts_none(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule()
        assert mod.node is None


class TestCameraModuleAddOutput:
    def test_add_preview(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule()
        mod.add_output("preview")
        assert "preview" in mod.outputs_config

    def test_add_video(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule()
        mod.add_output("video")
        assert "video" in mod.outputs_config

    def test_add_still(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule()
        mod.add_output("still")
        assert "still" in mod.outputs_config

    def test_add_fullvideo(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule()
        mod.add_output("fullvideo")
        assert "fullvideo" in mod.outputs_config

    def test_add_output_stores_size_and_fps(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule()
        mod.add_output("preview", size=(640, 480), fps=30)
        assert mod.outputs_config["preview"]["size"] == (640, 480)
        assert mod.outputs_config["preview"]["fps"] == 30

    def test_add_output_stores_none_defaults(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule()
        mod.add_output("video")
        assert mod.outputs_config["video"]["size"] is None
        assert mod.outputs_config["video"]["fps"] is None

    def test_duplicate_output_raises_assertion_error(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule()
        mod.add_output("preview")
        with pytest.raises(AssertionError):
            mod.add_output("preview")

    def test_invalid_output_type_raises_runtime_error(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule()
        with pytest.raises(RuntimeError, match="Unsupported output type"):
            mod.add_output("raw")

    def test_multiple_distinct_outputs(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule()
        mod.add_output("preview")
        mod.add_output("video")
        assert len(mod.outputs_config) == 2


class TestCameraModuleBuild:
    def _mock_pipeline(self):
        return MagicMock()

    def test_build_color_assigns_pipeline(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("color")
        pipeline = self._mock_pipeline()
        mod.build(pipeline=pipeline)
        assert mod.pipeline is pipeline

    def test_build_left_assigns_pipeline(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("left")
        mod.build(pipeline=self._mock_pipeline())
        assert mod.pipeline is not None

    def test_build_right_assigns_pipeline(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("right")
        mod.build(pipeline=self._mock_pipeline())
        assert mod.pipeline is not None

    def test_build_assigns_node(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("color")
        mod.build(pipeline=self._mock_pipeline())
        assert mod.node is not None

    def test_build_preview_populates_out_queues(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("color")
        mod.add_output("preview")
        mod.build(pipeline=self._mock_pipeline())
        assert "preview" in mod.out_queues

    def test_build_video_populates_out_queues(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("color")
        mod.add_output("video")
        mod.build(pipeline=self._mock_pipeline())
        assert "video" in mod.out_queues

    def test_build_still_populates_out_queues(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("color")
        mod.add_output("still")
        mod.build(pipeline=self._mock_pipeline())
        assert "still" in mod.out_queues

    def test_build_still_populates_trigger_in_queue(self):
        # Regression test: "still" must set up a Script-node trigger queue
        # (in_queues["still"]) since depthai v3's Camera node cannot gate a
        # still output on CameraControl.setCaptureStill.
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("color")
        mod.add_output("still")
        mod.build(pipeline=self._mock_pipeline())
        assert "still" in mod.in_queues

    def test_build_still_uses_full_resolution_output(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("color")
        mod.add_output("still")
        mod.build(pipeline=self._mock_pipeline())
        assert mod.node.requestFullResolutionOutput.called
        assert not mod.node.requestOutput.called

    def test_build_fullvideo_populates_out_queues(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("color")
        mod.add_output("fullvideo")
        mod.build(pipeline=self._mock_pipeline())
        assert "fullvideo" in mod.out_queues

    def test_build_sets_default_preview_size(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("color")
        mod.add_output("preview")
        mod.build(pipeline=self._mock_pipeline())
        assert mod.outputs_config["preview"]["size"] == (300, 300)

    def test_build_sets_default_video_size(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("color")
        mod.add_output("video")
        mod.build(pipeline=self._mock_pipeline())
        assert mod.outputs_config["video"]["size"] == (1920, 1080)

    def test_build_multiple_outputs(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("color")
        mod.add_output("preview")
        mod.add_output("video")
        mod.build(pipeline=self._mock_pipeline())
        assert "preview" in mod.out_queues
        assert "video" in mod.out_queues

    def test_build_with_custom_size_overrides_default(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        mod = CameraModule("color")
        mod.add_output("preview", size=(640, 480), fps=30)
        mod.build(pipeline=self._mock_pipeline())
        assert mod.outputs_config["preview"]["size"] == (640, 480)
        assert mod.outputs_config["preview"]["fps"] == 30


class TestCameraModuleBuildWiring:
    """Pin what build() asks the SDK for (the tests above only check bookkeeping).

    ``pipeline`` is a MagicMock, so the assertions target the exact arguments forwarded to it:
    socket per camera type, pixel type per output, and the non-blocking bounded queue (a blocking
    queue stalls the camera pipeline when the consumer is slow).
    """

    @staticmethod
    def _build(cam_type, outputs):
        import mydepthai.depthai_controllers.modules.camera_module as cm_mod

        mod = cm_mod.CameraModule(cam_type)
        for name, kwargs in outputs:
            mod.add_output(name, **kwargs)
        pipeline = MagicMock()
        mod.build(pipeline=pipeline)
        return cm_mod, mod, pipeline

    @pytest.mark.parametrize(
        "cam_type,socket_name", [("color", "CAM_A"), ("left", "CAM_B"), ("right", "CAM_C")]
    )
    def test_socket_mapping(self, cam_type, socket_name):
        cm_mod, _mod, pipeline = self._build(cam_type, [("preview", {})])
        camera_node_factory = pipeline.create.return_value
        camera_node_factory.build.assert_called_once_with(getattr(cm_mod.dai.CameraBoardSocket, socket_name))

    def test_camera_sockets_are_distinct(self):
        # Guards the mapping test above against a mock where all three attributes compare equal.
        cm_mod, _, _ = self._build("color", [])
        sockets = {cm_mod.dai.CameraBoardSocket.CAM_A, cm_mod.dai.CameraBoardSocket.CAM_B}
        assert len(sockets) == 2

    @pytest.mark.parametrize(
        "output,type_name,size,fps",
        [
            ("preview", "NV12", (300, 300), 20),
            ("video", "BGR888p", (1920, 1080), 20),
            ("fullvideo", "NV12", (3840, 2160), 2),
        ],
    )
    def test_default_request_output_args(self, output, type_name, size, fps):
        cm_mod, mod, _ = self._build("color", [(output, {})])
        mod.node.requestOutput.assert_called_once_with(
            size=size, type=getattr(cm_mod.dai.ImgFrame.Type, type_name), fps=fps
        )

    def test_custom_size_and_fps_are_forwarded(self):
        cm_mod, mod, _ = self._build("color", [("video", {"size": (640, 480), "fps": 7})])
        mod.node.requestOutput.assert_called_once_with(
            size=(640, 480), type=cm_mod.dai.ImgFrame.Type.BGR888p, fps=7
        )

    def test_pixel_types_differ_between_preview_and_video(self):
        cm_mod, _, _ = self._build("color", [])
        assert cm_mod.dai.ImgFrame.Type.NV12 is not cm_mod.dai.ImgFrame.Type.BGR888p

    @pytest.mark.parametrize("output", ["preview", "video", "fullvideo", "still"])
    def test_output_queue_is_nonblocking_and_bounded(self, output):
        import mydepthai.depthai_controllers.modules.camera_module as cm_mod

        _cm, mod, _ = self._build("color", [(output, {})])
        q = mod.out_queues[output]
        q.setBlocking.assert_called_once_with(False)
        q.setMaxSize.assert_called_once_with(cm_mod._DEFAULT_QUEUE_SIZE)

    def test_still_wires_script_trigger_and_full_res_fps(self):
        _cm, mod, pipeline = self._build("color", [("still", {"fps": 3})])
        mod.node.requestFullResolutionOutput.assert_called_once_with(fps=3)
        script = pipeline.create.return_value
        script.setScript.assert_called_once()
        assert "tryGet" in script.setScript.call_args.args[0]
        assert mod.in_queues["still"] is script.inputs["trigger"].createInputQueue.return_value
        # the stored config reflects what was actually requested
        assert mod.outputs_config["still"] == {"size": None, "fps": 3}

    def test_control_queue_is_created_from_node_input_control(self):
        _cm, mod, _ = self._build("color", [("preview", {})])
        assert mod.controlQueue is mod.node.inputControl.createInputQueue.return_value

    def test_build_without_pipeline_asserts(self):
        from mydepthai.depthai_controllers.modules.camera_module import CameraModule

        with pytest.raises(AssertionError):
            CameraModule("color").build(pipeline=None)


class TestGetDataTimeoutArguments:
    """Pin the exact timeout handed to the SDK: a missing timeout means a blocking get() forever."""

    def _module_with_queue(self):
        from mydepthai.depthai_controllers.modules.base_module import BaseModule

        class _M(BaseModule):
            pass

        mod = _M()
        mod.out_queues["q"] = MagicMock()
        return mod

    def test_float_timeout_becomes_timedelta(self):
        from datetime import timedelta

        mod = self._module_with_queue()
        mod.get_data("q", timeout=0.25)
        mod.out_queues["q"].get.assert_called_once_with(timeout=timedelta(seconds=0.25))
        mod.out_queues["q"].tryGet.assert_not_called()

    def test_none_timeout_uses_default_timedelta(self):
        from mydepthai.depthai_controllers.modules.base_module import DEFAULT_TIMEOUT

        mod = self._module_with_queue()
        mod.get_data("q")
        mod.out_queues["q"].get.assert_called_once_with(timeout=DEFAULT_TIMEOUT)
        assert DEFAULT_TIMEOUT.total_seconds() > 0
