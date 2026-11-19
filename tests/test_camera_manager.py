#!/usr/bin/env python3
"""
Simple test script for CameraManager

This script tests basic functionality without requiring actual cameras.
For full testing with hardware, use the examples in examples/camera_manager_example.py
"""

import json
import logging
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

logger = logging.getLogger(__name__)


# Prod default and sample; the dev copy is seeded from the default at startup.
_SHIPPED_CONFIGS = [
    Path(__file__).resolve().parents[1] / "mydepthai" / "configs" / "defaults" / "camera_manager_config.json",
    Path(__file__).resolve().parents[1] / "mydepthai" / "configs" / "defaults" / "sampleconfig_camera.json",
]  # config/camera_manager_config.json is git-ignored and seeded from the default by the start scripts


@pytest.mark.parametrize("config_path", _SHIPPED_CONFIGS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_shipped_camera_configs_exist_validate_and_load(config_path: Path) -> None:
    """Every camera config the repo ships must exist, pass validation and load into a manager.

    A deleted/renamed/invalid shipped config is a startup outage, so a missing file fails here
    rather than being skipped.
    """
    from mydepthai.camera_manager.camera_manager import CameraManager
    from mydepthai.camera_manager.config_utils import validate_camera_config

    assert config_path.is_file(), f"shipped camera config missing: {config_path}"
    raw = json.loads(config_path.read_text())
    validate_camera_config(raw)  # raises ValueError on a bad shipped config

    manager = CameraManager(config_path=config_path)
    assert set(manager.config) == set(raw)
    assert len(manager.config) >= 1
    assert len(manager) == 0  # configured, but nothing connected before initialize_cameras()


def test_manager_loads_config_dict() -> None:
    from mydepthai.camera_manager.camera_manager import CameraManager

    config_dict = {
        "test_cam": {
            "id": 0,
            "camera_type": "color",
            "outputs": ["preview"],
            "sizes": [[640, 480]],
            "fpss": [30],
        }
    }
    manager = CameraManager(config_dict=config_dict)
    assert list(manager.config) == ["test_cam"]
    assert manager.config["test_cam"]["outputs"] == ["preview"]


def test_manager_len_contains_and_context_manager_track_active_cameras_only() -> None:
    """``len``/``in`` reflect connected cameras, not configured ones; ``with`` closes on exit."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    config = {
        "cam1": {"id": 0, "camera_type": "color", "outputs": ["preview"]},
        "cam2": {"id": 1, "camera_type": "color", "outputs": ["preview"]},
    }
    with CameraManager(config_dict=config) as manager:
        assert len(manager) == 0
        assert "cam1" not in manager  # configured but not connected
        manager.cameras["cam1"] = MagicMock()
        assert len(manager) == 1
        assert "cam1" in manager and "cam2" not in manager
    assert len(manager) == 0  # __exit__ -> close_all cleared the active set


def test_error_handling() -> None:
    """Test error handling."""
    logger.info("=== Testing Error Handling ===")

    try:
        from mydepthai.camera_manager.camera_manager import CameraManager

        # Test missing config
        try:
            CameraManager()
            raise AssertionError("Should have raised ValueError for missing config")
        except ValueError as e:
            logger.info("✓ Correctly raises ValueError for missing config: %s", e)

        # Test missing file
        try:
            CameraManager(config_path="nonexistent_file.json")
            raise AssertionError("Should have raised FileNotFoundError")
        except FileNotFoundError as e:
            logger.info("✓ Correctly raises FileNotFoundError: %s", e)

        logger.info("✓ All error handling tests passed")

    except Exception as e:
        logger.exception("✗ Error handling tests failed: %s", e)
        raise


# ---------------------------------------------------------------------------
# P2.5 — Additional camera manager unit tests
# ---------------------------------------------------------------------------


def test_camera_manager_from_dict_connects_nothing_until_initialized() -> None:
    """Building from a config dict only records config; connecting is initialize_cameras()' job.

    The connect/close path itself is driven through a stateful fake SDK in test_camera_lifecycle.py.
    """
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(
        config_dict={
            "cam1": {
                "id": 0,
                "camera_type": "color",
                "outputs": ["preview"],
                "sizes": [[640, 480]],
                "fpss": [30],
            }
        }
    )
    assert list(manager.config) == ["cam1"]
    assert manager.cameras == {}


def test_get_frame_returns_none_for_unknown_camera() -> None:
    """get_frame returns None when the camera ID is not in the manager."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color", "outputs": ["preview"]}})
    result = manager.get_frame("nonexistent_cam")
    assert result is None


def test_camera_manager_context_manager_closes_cleanly() -> None:
    """``with CameraManager(...)`` does not raise on exit."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    with CameraManager(
        config_dict={"cam1": {"id": 0, "camera_type": "color", "outputs": ["preview"]}}
    ) as manager:
        assert manager is not None
    # After exit, cameras should be cleared
    assert len(manager) == 0


def test_camera_manager_invalid_config_path_raises_file_not_found() -> None:
    """FileNotFoundError is raised with a helpful message for non-existent config."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    with pytest.raises(FileNotFoundError, match="not found"):
        CameraManager(config_path="/does/not/exist.json")


# ---------------------------------------------------------------------------
# Additional coverage: monitoring, frame helpers, and direct access methods
# ---------------------------------------------------------------------------


def test_start_and_stop_monitoring() -> None:
    """start_monitoring flips _monitoring; stop_monitoring reverses it."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(
        config_dict={"cam1": {"id": 0, "camera_type": "color", "outputs": ["preview"]}},
        health_check_interval=999.0,  # prevent actual health checks during the test
    )
    assert manager._monitoring is False
    manager.start_monitoring()
    assert manager._monitoring is True
    manager.stop_monitoring()
    assert manager._monitoring is False


def test_start_monitoring_is_idempotent() -> None:
    """Calling start_monitoring twice does not raise or create two threads."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(
        config_dict={"cam1": {"id": 0, "camera_type": "color"}},
        health_check_interval=999.0,
    )
    manager.start_monitoring()
    thread_before = manager._monitor_thread
    manager.start_monitoring()  # second call should be a no-op
    assert manager._monitor_thread is thread_before
    manager.stop_monitoring()


def test_stop_monitoring_when_not_started_does_not_raise() -> None:
    """stop_monitoring is a no-op when monitoring was never started."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    manager.stop_monitoring()  # should not raise


def test_get_all_frames_type_output_empty_cameras() -> None:
    """get_all_frames_type_output returns an empty dict when no cameras are active."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    result = manager.get_all_frames_type_output()
    assert result == {}


def test_get_frames_all_empty_cameras() -> None:
    """get_frames_all returns an empty dict when no cameras are active."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    result = manager.get_frames_all()
    assert result == {}


def test_get_camera_returns_none_for_unknown() -> None:
    """get_camera returns None when the ID is not in the manager."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    assert manager.get_camera("nonexistent") is None


def test_contains_returns_false_for_unconfigured_camera() -> None:
    """__contains__ checks cameras dict, not config — so returns False before init."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    assert "cam1" not in manager  # in cameras dict, not config


def test_reset_reconnect_attempts_single_camera() -> None:
    """reset_reconnect_attempts clears the backoff for a specific camera."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    manager._backoff_for("cam1").record_failure()
    assert not manager._backoff_for("cam1").ready()
    manager.reset_reconnect_attempts("cam1")
    assert manager._backoff_for("cam1").ready()


def test_reset_reconnect_attempts_all_cameras() -> None:
    """reset_reconnect_attempts with no argument clears all counters."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(
        config_dict={
            "cam1": {"id": 0, "camera_type": "color"},
            "cam2": {"id": 1, "camera_type": "color"},
        }
    )
    manager._backoff_for("cam1").record_failure()
    manager._backoff_for("cam2").record_failure()
    manager.reset_reconnect_attempts()
    assert manager._backoffs == {}


def test_reset_reconnect_attempts_unknown_camera_noop() -> None:
    """reset_reconnect_attempts for an unknown camera ID is a no-op."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    manager.reset_reconnect_attempts("nonexistent")  # should not raise


def test_close_all_stops_monitoring_and_clears_cameras() -> None:
    """close_all stops monitoring and empties the cameras dict."""
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(
        config_dict={"cam1": {"id": 0, "camera_type": "color"}},
        health_check_interval=999.0,
    )
    manager.start_monitoring()
    assert manager._monitoring is True
    manager.close_all()
    assert manager._monitoring is False
    assert len(manager.cameras) == 0


def test_get_frame_returns_none_on_exception() -> None:
    """get_frame returns None when the camera's get_frame raises."""
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_camera.get_frame.side_effect = RuntimeError("camera fault")
    manager.cameras["cam1"] = mock_camera

    result = manager.get_frame("cam1")
    assert result is None


def test_get_frame_updates_last_frame_time_on_success() -> None:
    """get_frame updates _last_frame_time when a frame is returned."""
    from unittest.mock import MagicMock

    import numpy as np

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    mock_camera.get_frame.return_value = dummy_frame
    manager.cameras["cam1"] = mock_camera

    result = manager.get_frame("cam1")
    assert result is not None
    assert "cam1" in manager._last_frame_time


def test_apply_camera_settings_exposure_and_iso() -> None:
    """_apply_camera_settings calls set_exposure; focus absent → continuous_auto_focus."""
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}

    manager._apply_camera_settings(mock_camera, "color", {"exposure": 10000, "iso": 200}, "cam1")
    mock_component.set_exposure.assert_called_once_with(10000, 200)
    mock_component.set_auto_exposure.assert_not_called()
    mock_component.continuous_auto_focus.assert_called_once()


def test_apply_camera_settings_focus() -> None:
    """_apply_camera_settings calls set_focus; exposure absent → set_auto_exposure."""
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}

    manager._apply_camera_settings(mock_camera, "color", {"focus": 125}, "cam1")
    mock_component.set_focus.assert_called_once_with(125)
    mock_component.set_auto_exposure.assert_called_once()
    mock_component.continuous_auto_focus.assert_not_called()


def test_apply_camera_settings_empty_restores_auto_modes() -> None:
    """Empty settings dict restores both auto exposure and continuous auto focus."""
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}

    manager._apply_camera_settings(mock_camera, "color", {}, "cam1")
    mock_component.set_exposure.assert_not_called()
    mock_component.set_focus.assert_not_called()
    mock_component.set_auto_exposure.assert_called_once()
    mock_component.continuous_auto_focus.assert_called_once()


def test_apply_camera_settings_all_explicit_no_auto_calls() -> None:
    """When all three keys are set, no auto-mode methods are called."""
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}

    manager._apply_camera_settings(
        mock_camera, "color", {"exposure": 10000, "iso": 200, "focus": 130}, "cam1"
    )
    mock_component.set_exposure.assert_called_once_with(10000, 200)
    mock_component.set_focus.assert_called_once_with(130)
    mock_component.set_auto_exposure.assert_not_called()
    mock_component.continuous_auto_focus.assert_not_called()


def test_apply_camera_settings_unknown_component_does_not_raise() -> None:
    """_apply_camera_settings is a no-op when the cam_type is absent."""
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_camera.components = {}  # cam_type not present

    manager._apply_camera_settings(mock_camera, "color", {"exposure": 10000, "iso": 200}, "cam1")
    # Should not raise


def test_apply_camera_settings_ae_lock_present_and_absent() -> None:
    """ae_lock present -> set_auto_exposure_lock(value); absent -> restored to False."""
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}

    manager._apply_camera_settings(mock_camera, "color", {"ae_lock": True}, "cam1")
    mock_component.set_auto_exposure_lock.assert_called_once_with(True)

    mock_component.reset_mock()
    manager._apply_camera_settings(mock_camera, "color", {}, "cam1")
    mock_component.set_auto_exposure_lock.assert_called_once_with(False)


def test_apply_camera_settings_exposure_compensation_present_and_absent() -> None:
    """exposure_compensation present -> set_exposure_compensation(value); absent -> restored to 0."""
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}

    manager._apply_camera_settings(mock_camera, "color", {"exposure_compensation": -4}, "cam1")
    mock_component.set_exposure_compensation.assert_called_once_with(-4)

    mock_component.reset_mock()
    manager._apply_camera_settings(mock_camera, "color", {}, "cam1")
    mock_component.set_exposure_compensation.assert_called_once_with(0)


def test_apply_camera_settings_exposure_limit_present_and_absent() -> None:
    """exposure_limit_us present -> set_auto_exposure_limit(value); absent -> restored to max (33333)."""
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}

    manager._apply_camera_settings(mock_camera, "color", {"exposure_limit_us": 15000}, "cam1")
    mock_component.set_auto_exposure_limit.assert_called_once_with(15000)

    mock_component.reset_mock()
    manager._apply_camera_settings(mock_camera, "color", {}, "cam1")
    mock_component.set_auto_exposure_limit.assert_called_once_with(33333)


def test_apply_camera_settings_af_lens_range_present_skips_when_absent() -> None:
    """af_lens_range present -> set_auto_focus_lens_range(...); absent -> skipped (no safe default)."""
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}

    manager._apply_camera_settings(mock_camera, "color", {"af_lens_range": [10, 200]}, "cam1")
    mock_component.set_auto_focus_lens_range.assert_called_once_with(10, 200)

    mock_component.reset_mock()
    manager._apply_camera_settings(mock_camera, "color", {}, "cam1")
    mock_component.set_auto_focus_lens_range.assert_not_called()


def _region_manager(sensor=(3000, 3000), cached_frame=None, requested_size=None):
    """Manager + mock camera on a 3000x3000 sensor, for persisted-region scaling tests."""
    from unittest.mock import MagicMock

    import mydepthai.camera_manager.camera_manager as cm_mod
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color", "outputs": ["preview"]}})
    camera = MagicMock()
    component = MagicMock()
    component.outputs_config = {"preview": {"size": requested_size, "fps": 20}}
    camera.components = {"color": component}
    manager.cameras["cam1"] = camera
    feat = MagicMock()
    feat.socket = cm_mod.dai.CameraBoardSocket.CAM_A
    feat.width, feat.height = sensor
    camera.device.getConnectedCameraFeatures.return_value = [feat]
    if cached_frame:
        manager._last_metadata["cam1"] = {"frame_width": cached_frame[0], "frame_height": cached_frame[1]}
    return manager, camera, component


def test_apply_camera_settings_regions_scaled_from_cached_frame_and_absent_skipped() -> None:
    """MD-T2: persisted output-space regions are scaled to sensor space on (re)apply."""
    manager, camera, component = _region_manager(cached_frame=(300, 300))

    manager._apply_camera_settings(
        camera, "color", {"af_region": [0, 0, 50, 50], "ae_region": [10, 10, 60, 60]}, "cam1"
    )
    component.set_Focus_Region.assert_called_once_with(0, 0, 500, 500)  # 10x
    component.set_Exposure_Region.assert_called_once_with(100, 100, 600, 600)

    component.reset_mock()
    manager._apply_camera_settings(camera, "color", {}, "cam1")  # absent -> sticky, not re-sent
    component.set_Focus_Region.assert_not_called()
    component.set_Exposure_Region.assert_not_called()


def test_restart_reapply_scales_using_requested_output_size_when_no_frame_cached() -> None:
    """Regression: after a restart nothing is cached; a 1080p-output region on a 4K sensor
    used to be applied unscaled (top-left quarter). It must land at the same sensor spot as the
    live drag-box did."""
    manager, camera, component = _region_manager(sensor=(3840, 2160), requested_size=(1920, 1080))
    live = {"x": 960, "y": 540, "w": 480, "h": 270}  # what the drag-box sends (output space)

    # live path (frame cached) -> sensor coords
    manager._last_metadata["cam1"] = {"frame_width": 1920, "frame_height": 1080}
    manager.send_camera_action("cam1", "color", "set_focus_region", live)
    live_call = component.set_Focus_Region.call_args
    assert live_call.args == (1920, 1080, 960, 540)

    # restart: cache empty, persisted config re-applied -> identical sensor coords
    manager._last_metadata.clear()
    component.reset_mock()
    persisted = manager.config["cam1"]["settings"]
    manager._apply_camera_settings(camera, "color", persisted, "cam1")
    component.set_Focus_Region.assert_called_once_with(1920, 1080, 960, 540)


def test_reapply_skips_region_when_output_size_unknown_instead_of_misplacing_it() -> None:
    manager, camera, component = _region_manager(requested_size=None)
    manager._apply_camera_settings(camera, "color", {"af_region": [0, 0, 50, 50], "focus": 100}, "cam1")
    component.set_Focus_Region.assert_not_called()
    component.set_focus.assert_called_once_with(100)  # other settings still applied


def test_get_frame_with_metadata_caches_metadata() -> None:
    """get_frame_with_metadata caches the metadata dict, readable via get_cached_metadata."""
    from unittest.mock import MagicMock

    import numpy as np

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    metadata = {
        "lens_position": 1,
        "exposure_time_us": 8000.0,
        "iso": 400,
        "frame_width": 2,
        "frame_height": 2,
    }
    mock_camera.get_frame_with_metadata.return_value = (frame, metadata)
    manager.cameras["cam1"] = mock_camera

    result = manager.get_frame_with_metadata("cam1")
    assert result == (frame, metadata)
    assert manager.get_cached_metadata("cam1") == metadata


def test_get_frame_with_metadata_returns_none_does_not_cache() -> None:
    """A None result (offline camera) must not overwrite the cache with stale data."""
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_camera.get_frame_with_metadata.return_value = None
    manager.cameras["cam1"] = mock_camera
    manager._last_metadata["cam1"] = {"lens_position": 99}

    result = manager.get_frame_with_metadata("cam1")
    assert result is None
    assert manager.get_cached_metadata("cam1") == {"lens_position": 99}


def test_get_frame_with_metadata_returns_none_for_unknown_camera() -> None:
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    assert manager.get_frame_with_metadata("unknown") is None


def test_get_cached_metadata_unknown_camera_returns_empty_dict() -> None:
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    assert manager.get_cached_metadata("unknown") == {}


# ---------------------------------------------------------------------------
# send_camera_action
# ---------------------------------------------------------------------------


def test_send_camera_action_unknown_camera_raises() -> None:
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    with pytest.raises(ValueError, match="Unknown camera"):
        manager.send_camera_action("nope", "color", "trigger_auto_focus")


def test_send_camera_action_unknown_component_raises() -> None:
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_camera.components = {}
    manager.cameras["cam1"] = mock_camera
    with pytest.raises(ValueError, match="component"):
        manager.send_camera_action("cam1", "color", "trigger_auto_focus")


def test_send_camera_action_unknown_action_raises() -> None:
    """Proves the allow-list rejects an arbitrary string rather than calling it via getattr."""
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}
    manager.cameras["cam1"] = mock_camera

    with pytest.raises(ValueError, match="Unknown action"):
        manager.send_camera_action("cam1", "color", "set_control_queue")
    mock_component.set_control_queue.assert_not_called()


def test_send_camera_action_trigger_auto_focus_dispatches() -> None:
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}
    manager.cameras["cam1"] = mock_camera

    result = manager.send_camera_action("cam1", "color", "trigger_auto_focus")
    mock_component.trigger_auto_focus.assert_called_once()
    assert result == {"status": "ok", "action": "trigger_auto_focus"}


def test_send_camera_action_set_exposure_dispatches_and_updates_config() -> None:
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}
    manager.cameras["cam1"] = mock_camera

    manager.send_camera_action("cam1", "color", "set_exposure", {"exposure_us": 12000, "iso": 300})
    mock_component.set_exposure.assert_called_once_with(12000, 300)
    assert manager.config["cam1"]["settings"]["exposure"] == 12000
    assert manager.config["cam1"]["settings"]["iso"] == 300


def test_send_camera_action_set_ae_lock_dispatches_and_updates_config() -> None:
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}
    manager.cameras["cam1"] = mock_camera

    manager.send_camera_action("cam1", "color", "set_ae_lock", {"locked": True})
    mock_component.set_auto_exposure_lock.assert_called_once_with(True)
    assert manager.config["cam1"]["settings"]["ae_lock"] is True


def test_send_camera_action_set_exposure_compensation_out_of_range_raises_before_calling_component() -> None:
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}
    manager.cameras["cam1"] = mock_camera

    with pytest.raises(ValueError, match="exposure_compensation"):
        manager.send_camera_action("cam1", "color", "set_exposure_compensation", {"value": 20})
    mock_component.set_exposure_compensation.assert_not_called()


def test_send_camera_action_set_af_lens_range_propagates_value_error_from_component() -> None:
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_component.set_auto_focus_lens_range.side_effect = ValueError("infinity >= macro")
    mock_camera.components = {"color": mock_component}
    manager.cameras["cam1"] = mock_camera

    with pytest.raises(ValueError, match="infinity"):
        manager.send_camera_action(
            "cam1", "color", "set_af_lens_range", {"infinity_pos": 200, "macro_pos": 10}
        )


def test_send_camera_action_set_focus_region_scales_to_sensor_and_dispatches() -> None:
    from unittest.mock import MagicMock

    import mydepthai.camera_manager.camera_manager as cm_mod
    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}
    manager.cameras["cam1"] = mock_camera
    manager._last_metadata["cam1"] = {"frame_width": 300, "frame_height": 300}

    # Use the exact same (mocked) dai reference camera_manager.py holds, so the
    # mocked CameraBoardSocket.CAM_A compares equal to _SOCKET_BY_CAM_TYPE["color"].
    feat = MagicMock()
    feat.socket = cm_mod.dai.CameraBoardSocket.CAM_A
    feat.width = 3000
    feat.height = 3000
    mock_camera.device.getConnectedCameraFeatures.return_value = [feat]

    manager.send_camera_action("cam1", "color", "set_focus_region", {"x": 30, "y": 30, "w": 30, "h": 30})

    # 10x scale factor (3000/300)
    mock_component.set_Focus_Region.assert_called_once_with(300, 300, 300, 300)
    # Config stores output-space values, not sensor-space.
    assert manager.config["cam1"]["settings"]["af_region"] == [30, 30, 30, 30]


def test_send_camera_action_set_focus_region_raises_when_no_cached_frame() -> None:
    from unittest.mock import MagicMock

    from mydepthai.camera_manager.camera_manager import CameraManager

    manager = CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}})
    mock_camera = MagicMock()
    mock_component = MagicMock()
    mock_camera.components = {"color": mock_component}
    manager.cameras["cam1"] = mock_camera

    with pytest.raises(ValueError, match="No cached frame"):
        manager.send_camera_action("cam1", "color", "set_focus_region", {"x": 0, "y": 0, "w": 10, "h": 10})


def create_sample_config() -> bool:
    """Create a sample configuration file for testing."""
    logger.info("=== Creating Sample Configuration ===")

    config = {
        "camera_1": {
            "id": "14442C10D13597D000",
            "camera_type": "color",
            "outputs": ["preview", "video"],
            "sizes": [[300, 300], [1280, 720]],
            "fpss": [20, 20],
            "settings": {"iso": 100, "exposure": 20000, "focus": 125},
        },
        "camera_2": {
            "id": 0,
            "camera_type": "color",
            "outputs": ["preview"],
            "sizes": [[640, 480]],
            "fpss": [30],
        },
    }

    output_path = Path("test_camera_config.json")

    try:
        with open(output_path, "w") as f:
            json.dump(config, f, indent=4)

        logger.info("✓ Sample config created: %s", output_path)
        return True

    except Exception as e:
        logger.exception("✗ Failed to create sample config: %s", e)
        return False


if __name__ == "__main__":
    logger.info("Starting CameraManager tests...\n")

    results = []

    # Run tests
    results.append(("Error Handling", test_error_handling()))  # type: ignore[func-returns-value]
    results.append(("Sample Config Creation", create_sample_config()))  # type: ignore[arg-type]

    # Summary
    logger.info("\n" + "=" * 50)
    logger.info("TEST SUMMARY")
    logger.info("=" * 50)

    passed = 0
    failed = 0

    for test_name, result in results:
        status = "PASS" if result else "FAIL"
        logger.info("%s: %s", test_name, status)
        if result:
            passed += 1
        else:
            failed += 1

    logger.info("=" * 50)
    logger.info("Total: %d passed, %d failed", passed, failed)

    if failed == 0:
        logger.info("✓ All tests passed!")
    else:
        logger.warning("✗ Some tests failed")

    logger.info("\nNote: These tests don't require cameras.")
    logger.info("For full hardware testing, run: python examples/camera_manager_example.py")


# ---------------------------------------------------------------------------
# truthful state, retry forever with capped backoff
# ---------------------------------------------------------------------------


def _manager(**kw: Any) -> Any:
    from mydepthai.camera_manager.camera_manager import CameraManager

    return CameraManager(config_dict={"cam1": {"id": 0, "camera_type": "color"}}, **kw)


def _fake_clock_backoff(manager: Any, now: list) -> None:
    backoff = manager._backoff_for("cam1")
    backoff.clock = lambda: now[0]


def test_reconnect_never_gives_up_and_backs_off_to_the_cap() -> None:
    """the old budget abandoned USB cameras after 3 tries (~15 s) for good."""
    manager = _manager(health_check_interval=5.0, reconnect_max_delay=20.0)
    now = [1000.0]
    _fake_clock_backoff(manager, now)
    attempts: list[float] = []

    def _fail(_cam_id: str) -> bool:
        attempts.append(now[0])
        return False

    manager._reconnect_camera = _fail  # type: ignore[method-assign]
    for _ in range(200):  # 200 health ticks of 5 s
        manager._attempt_camera_reconnect("cam1", "absent")
        now[0] += 5.0

    gaps = [b - a for a, b in zip(attempts, attempts[1:])]
    assert len(attempts) > 10  # still trying long after "3 attempts"
    assert gaps[:3] == [5.0, 10.0, 20.0]
    assert max(gaps) == 20.0  # capped


def test_successful_reconnect_resets_backoff() -> None:
    manager = _manager(health_check_interval=5.0)
    now = [1000.0]
    _fake_clock_backoff(manager, now)
    manager._backoff_for("cam1").record_failure()
    now[0] += 5.0
    manager._reconnect_camera = lambda _c: True  # type: ignore[method-assign]

    manager._attempt_camera_reconnect("cam1", "absent")

    assert manager._backoff_for("cam1").ready()


def test_failed_connect_is_not_added_to_cameras(monkeypatch: Any) -> None:
    """a camera whose connect timed out used to be registered anyway."""
    from mydepthai.camera_manager import camera_manager as cm_mod

    fake_cam = MagicMock()
    fake_cam.wait_for_cam.return_value = False
    monkeypatch.setattr(cm_mod, "BasicDAICamera", lambda **_k: fake_cam)
    manager = _manager()

    assert manager._initialize_single_camera("cam1", {"id": 0}, False) is False
    assert "cam1" not in manager.cameras
    fake_cam.close.assert_called_once_with(force=True)


def test_constructor_failure_is_not_added_to_cameras(monkeypatch: Any) -> None:
    from mydepthai.camera_manager import camera_manager as cm_mod

    def _boom(**_k: Any) -> Any:
        raise RuntimeError("device not enumerated")

    monkeypatch.setattr(cm_mod, "BasicDAICamera", _boom)
    manager = _manager()
    assert manager._initialize_single_camera("cam1", {"id": 0}, False) is False
    assert manager.cameras == {}


def test_camera_state_is_truthful() -> None:
    manager = _manager(data_timeout=10.0)
    assert manager.get_camera_state("cam1") == "offline"  # never connected

    cam = MagicMock()
    cam.cam_ready.return_value = True
    manager.cameras["cam1"] = cam
    manager._last_frame_time["cam1"] = time.time()
    assert manager.get_camera_state("cam1") == "online"

    manager._last_frame_time["cam1"] = time.time() - 60
    assert manager.get_camera_state("cam1") == "stale"

    cam.cam_ready.return_value = False  # unplugged
    assert manager.get_camera_state("cam1") == "offline"

    manager._reconnecting.add("cam1")
    assert manager.get_camera_state("cam1") == "connecting"


def test_get_frame_with_metadata_forwards_timeout_only_when_given() -> None:
    """the broker polls with timeout=0; other callers keep the per-output default."""
    manager = _manager()
    cam = MagicMock()
    cam.get_frame_with_metadata.return_value = None
    manager.cameras["cam1"] = cam

    manager.get_frame_with_metadata("cam1", "color", "preview")
    cam.get_frame_with_metadata.assert_called_with(cam="color", out_type="preview")

    manager.get_frame_with_metadata("cam1", "color", "preview", timeout=0)
    cam.get_frame_with_metadata.assert_called_with(cam="color", out_type="preview", timeout=0)


def _monitor_once(manager: Any) -> list:
    """Run exactly one pass of the health-monitor loop and return the reconnect reasons it raised."""
    reasons: list = []

    def _record(cam_id: str, reason: str) -> None:
        reasons.append((cam_id, reason))

    manager._attempt_camera_reconnect = _record
    manager._monitoring = True
    original_wait = manager._stop_event.wait

    def _wait_then_stop(_t: float) -> bool:
        manager._monitoring = False
        return original_wait(0)

    manager._stop_event.wait = _wait_then_stop
    manager._monitor_health()
    return reasons


def test_monitor_reconnects_with_reason_matching_the_status_state() -> None:
    """The monitor and get_camera_state share one health rule (MD Phase 3 health.py)."""
    m = _manager(data_timeout=10.0)
    assert m.get_camera_state("cam1") == "offline"
    assert _monitor_once(m) == [("cam1", "absent")]

    m = _manager(data_timeout=10.0)
    cam = MagicMock()
    cam.cam_ready.return_value = False
    m.cameras["cam1"] = cam
    m._last_frame_time["cam1"] = time.time()
    assert m.get_camera_state("cam1") == "offline"
    assert _monitor_once(m) == [("cam1", "not connected")]

    m = _manager(data_timeout=10.0)
    cam = MagicMock()
    cam.cam_ready.return_value = True
    m.cameras["cam1"] = cam
    m._last_frame_time["cam1"] = time.time() - 60
    assert m.get_camera_state("cam1") == "stale"
    assert _monitor_once(m) == [("cam1", "inactive")]


def test_monitor_leaves_online_and_connecting_cameras_alone() -> None:
    m = _manager(data_timeout=10.0)
    cam = MagicMock()
    cam.cam_ready.return_value = True
    m.cameras["cam1"] = cam
    m._last_frame_time["cam1"] = time.time()
    m._backoffs["cam1"] = m._backoff_for("cam1")
    assert _monitor_once(m) == []
    assert "cam1" not in m._backoffs  # healthy camera clears its stale backoff

    m._reconnecting.add("cam1")
    assert m.get_camera_state("cam1") == "connecting"
    assert _monitor_once(m) == []
