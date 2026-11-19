"""Tests for my_depthai/mydepthai/fallback_frames.py.

These tests cover the synthetic offline-camera frame helpers.  All code is
pure Python / OpenCV — no DepthAI hardware required.
"""

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.unit

_FF_PATH = Path(__file__).parent.parent / "mydepthai" / "fallback_frames.py"


@pytest.fixture(scope="module")
def ff():
    """Load fallback_frames once for this test module."""
    mod_name = "mydepthai._fallback_frames_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _FF_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# create_camera_error_frame
# ---------------------------------------------------------------------------


class TestCreateCameraErrorFrame:
    def test_returns_numpy_array(self, ff):
        frame = ff.create_camera_error_frame("cam1")
        assert isinstance(frame, np.ndarray)

    def test_default_size_640x480(self, ff):
        ff.clear_fallback_cache()
        frame = ff.create_camera_error_frame("cam_default")
        assert frame.shape == (480, 640, 3)

    def test_custom_size_320x240(self, ff):
        frame = ff.create_camera_error_frame("cam_small", size=(320, 240))
        assert frame.shape == (240, 320, 3)

    def test_custom_size_1280x720(self, ff):
        frame = ff.create_camera_error_frame("cam_hd", size=(1280, 720))
        assert frame.shape == (720, 1280, 3)

    def test_dtype_is_uint8(self, ff):
        frame = ff.create_camera_error_frame("cam_dtype")
        assert frame.dtype == np.uint8

    def test_frame_has_non_zero_pixels(self, ff):
        # Red border and text make some pixels non-zero
        frame = ff.create_camera_error_frame("cam_nonzero")
        assert frame.any()

    def test_custom_reason_does_not_raise(self, ff):
        frame = ff.create_camera_error_frame("cam1", reason="Connection timeout")
        assert frame.shape == (480, 640, 3)

    def test_long_reason_word_wraps_without_error(self, ff):
        long_reason = (
            "This is a very long reason message that should wrap onto multiple "
            "lines when rendered inside the frame"
        )
        frame = ff.create_camera_error_frame("cam1", reason=long_reason)
        assert frame.shape == (480, 640, 3)

    def test_single_word_reason(self, ff):
        frame = ff.create_camera_error_frame("cam1", reason="Disconnected")
        assert isinstance(frame, np.ndarray)

    def test_empty_reason_string(self, ff):
        frame = ff.create_camera_error_frame("cam1", reason="")
        assert isinstance(frame, np.ndarray)

    def test_template_cached_after_first_call(self, ff):
        ff.clear_fallback_cache()
        ff.create_camera_error_frame("cam_cache_key", reason="offline", size=(640, 480))
        assert ("cam_cache_key", "offline", 640, 480) in ff._template_cache

    def test_second_call_does_not_grow_cache(self, ff):
        ff.clear_fallback_cache()
        ff.create_camera_error_frame("cam_reuse", reason="test", size=(640, 480))
        size_before = len(ff._template_cache)
        ff.create_camera_error_frame("cam_reuse", reason="test", size=(640, 480))
        assert len(ff._template_cache) == size_before

    def test_different_cam_ids_create_separate_entries(self, ff):
        ff.clear_fallback_cache()
        ff.create_camera_error_frame("cam_A")
        ff.create_camera_error_frame("cam_B")
        assert len(ff._template_cache) == 2

    def test_different_sizes_create_separate_entries(self, ff):
        ff.clear_fallback_cache()
        ff.create_camera_error_frame("cam1", size=(640, 480))
        ff.create_camera_error_frame("cam1", size=(320, 240))
        assert len(ff._template_cache) == 2

    def test_different_reasons_create_separate_entries(self, ff):
        ff.clear_fallback_cache()
        ff.create_camera_error_frame("cam1", reason="offline")
        ff.create_camera_error_frame("cam1", reason="timeout")
        assert len(ff._template_cache) == 2

    def test_returned_frame_is_a_copy_not_cache_ref(self, ff):
        ff.clear_fallback_cache()
        frame1 = ff.create_camera_error_frame("cam_copy")
        frame2 = ff.create_camera_error_frame("cam_copy")
        # Modifying one should not affect the other
        frame1[0, 0] = [99, 99, 99]
        assert not np.array_equal(frame1[0, 0], frame2[0, 0])


# ---------------------------------------------------------------------------
# clear_fallback_cache
# ---------------------------------------------------------------------------


class TestClearFallbackCache:
    def test_empties_cache(self, ff):
        ff.create_camera_error_frame("cam_before_clear")
        ff.clear_fallback_cache()
        assert len(ff._template_cache) == 0

    def test_clearing_empty_cache_does_not_raise(self, ff):
        ff.clear_fallback_cache()
        ff.clear_fallback_cache()

    def test_cache_repopulates_after_clear(self, ff):
        ff.clear_fallback_cache()
        ff.create_camera_error_frame("cam_repop")
        assert len(ff._template_cache) == 1


# ---------------------------------------------------------------------------
# _build_template
# ---------------------------------------------------------------------------


class TestBuildTemplate:
    def test_returns_numpy_array(self, ff):
        t = ff._build_template("cam1", "offline", 640, 480)
        assert isinstance(t, np.ndarray)

    def test_correct_shape_640x480(self, ff):
        t = ff._build_template("cam1", "test reason", 640, 480)
        assert t.shape == (480, 640, 3)

    def test_correct_shape_small(self, ff):
        t = ff._build_template("cam1", "offline", 160, 120)
        assert t.shape == (120, 160, 3)

    def test_dtype_uint8(self, ff):
        t = ff._build_template("cam1", "offline", 640, 480)
        assert t.dtype == np.uint8

    def test_multiword_reason_wraps(self, ff):
        reason = "Camera connection lost due to network failure please check cables"
        t = ff._build_template("cam_long", reason, 320, 240)
        assert t.shape == (240, 320, 3)

    def test_single_word_reason(self, ff):
        t = ff._build_template("cam1", "timeout", 640, 480)
        assert t.shape == (480, 640, 3)

    def test_result_has_non_zero_pixels(self, ff):
        t = ff._build_template("cam1", "offline", 640, 480)
        assert t.any()
