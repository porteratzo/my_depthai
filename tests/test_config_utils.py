"""Unit tests for mydepthai.camera_manager.config_utils.

Covers each validation branch in ``_validate_single_camera`` and the
top-level ``validate_camera_config`` entry point.
"""

from typing import Any

import pytest

from mydepthai.camera_manager.config_utils import validate_camera_config

pytestmark = pytest.mark.unit


def _minimal() -> dict[str, Any]:
    """Return a single-camera config that passes validation."""
    return {
        "cam0": {
            "camera_type": "color",
            "outputs": ["preview"],
            "sizes": [[640, 480]],
            "fpss": [30],
        }
    }


# ---------------------------------------------------------------------------
# Top-level shape
# ---------------------------------------------------------------------------


def test_empty_config_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        validate_camera_config({})


def test_non_dict_config_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        validate_camera_config([])  # type: ignore[arg-type]


def test_camera_entry_must_be_dict() -> None:
    with pytest.raises(ValueError, match="must be a dict"):
        validate_camera_config({"cam0": "not-a-dict"})  # type: ignore[dict-item]


def test_minimal_valid_config_accepted() -> None:
    validate_camera_config(_minimal())


# ---------------------------------------------------------------------------
# camera_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cam_type", ["color", "left", "right"])
def test_valid_camera_types_accepted(cam_type: str) -> None:
    cfg = _minimal()
    cfg["cam0"]["camera_type"] = cam_type
    validate_camera_config(cfg)


def test_invalid_camera_type_rejected() -> None:
    cfg = _minimal()
    cfg["cam0"]["camera_type"] = "thermal"
    with pytest.raises(ValueError, match="invalid camera_type"):
        validate_camera_config(cfg)


# ---------------------------------------------------------------------------
# outputs
# ---------------------------------------------------------------------------


def test_outputs_must_be_non_empty_list() -> None:
    cfg = _minimal()
    cfg["cam0"]["outputs"] = []
    with pytest.raises(ValueError, match="non-empty list"):
        validate_camera_config(cfg)


def test_outputs_unknown_value_rejected() -> None:
    cfg = _minimal()
    cfg["cam0"]["outputs"] = ["bogus"]
    cfg["cam0"]["sizes"] = [[640, 480]]
    cfg["cam0"]["fpss"] = [30]
    with pytest.raises(ValueError, match="invalid output"):
        validate_camera_config(cfg)


# ---------------------------------------------------------------------------
# sizes / fpss length must match outputs
# ---------------------------------------------------------------------------


def test_sizes_length_mismatch_rejected() -> None:
    cfg = _minimal()
    cfg["cam0"]["sizes"] = [[640, 480], [1280, 720]]
    with pytest.raises(ValueError, match="'sizes' length"):
        validate_camera_config(cfg)


def test_fpss_length_mismatch_rejected() -> None:
    cfg = _minimal()
    cfg["cam0"]["fpss"] = [30, 60]
    with pytest.raises(ValueError, match="'fpss' length"):
        validate_camera_config(cfg)


# ---------------------------------------------------------------------------
# wait_timeout / reconnect_on_timeout / max_retries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, "five"])
def test_wait_timeout_invalid_rejected(bad: object) -> None:
    cfg = _minimal()
    cfg["cam0"]["wait_timeout"] = bad
    with pytest.raises(ValueError, match="wait_timeout"):
        validate_camera_config(cfg)


def test_reconnect_on_timeout_must_be_bool() -> None:
    cfg = _minimal()
    cfg["cam0"]["reconnect_on_timeout"] = "yes"
    with pytest.raises(ValueError, match="reconnect_on_timeout"):
        validate_camera_config(cfg)


@pytest.mark.parametrize("bad", [-1, 1.5, "many"])
def test_max_retries_invalid_rejected(bad: object) -> None:
    cfg = _minimal()
    cfg["cam0"]["max_retries"] = bad
    with pytest.raises(ValueError, match="max_retries"):
        validate_camera_config(cfg)


# ---------------------------------------------------------------------------
# settings: exposure / iso / focus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, 33334, "auto"])
def test_exposure_out_of_range_rejected(bad: object) -> None:
    cfg = _minimal()
    cfg["cam0"]["settings"] = {"exposure": bad}
    with pytest.raises(ValueError, match="exposure"):
        validate_camera_config(cfg)


@pytest.mark.parametrize("bad", [50, 1700, 100.5])
def test_iso_out_of_range_rejected(bad: object) -> None:
    cfg = _minimal()
    cfg["cam0"]["settings"] = {"iso": bad}
    with pytest.raises(ValueError, match="ISO"):
        validate_camera_config(cfg)


@pytest.mark.parametrize("bad", [0, 255, 5.5])
def test_focus_out_of_range_rejected(bad: object) -> None:
    cfg = _minimal()
    cfg["cam0"]["settings"] = {"focus": bad}
    with pytest.raises(ValueError, match="focus"):
        validate_camera_config(cfg)


@pytest.mark.parametrize("bad", ["yes", 1, 0])
def test_ae_lock_must_be_bool_rejected(bad: object) -> None:
    cfg = _minimal()
    cfg["cam0"]["settings"] = {"ae_lock": bad}
    with pytest.raises(ValueError, match="ae_lock"):
        validate_camera_config(cfg)


@pytest.mark.parametrize("bad", [-10, 10, 9.5])
def test_exposure_compensation_out_of_range_rejected(bad: object) -> None:
    cfg = _minimal()
    cfg["cam0"]["settings"] = {"exposure_compensation": bad}
    with pytest.raises(ValueError, match="exposure_compensation"):
        validate_camera_config(cfg)


@pytest.mark.parametrize("bad", [0, 33334])
def test_exposure_limit_us_out_of_range_rejected(bad: object) -> None:
    cfg = _minimal()
    cfg["cam0"]["settings"] = {"exposure_limit_us": bad}
    with pytest.raises(ValueError, match="exposure_limit_us"):
        validate_camera_config(cfg)


@pytest.mark.parametrize(
    "bad",
    [
        [10],  # wrong length
        [300, 10],  # out of bounds + wrong order
        [10, 300],  # out of bounds
        [50, 50],  # infinity == macro
        [100, 50],  # infinity > macro
    ],
)
def test_af_lens_range_invalid_rejected(bad: object) -> None:
    cfg = _minimal()
    cfg["cam0"]["settings"] = {"af_lens_range": bad}
    with pytest.raises(ValueError, match="af_lens_range"):
        validate_camera_config(cfg)


@pytest.mark.parametrize("region_key", ["af_region", "ae_region"])
@pytest.mark.parametrize(
    "bad",
    [
        [1, 2, 3],  # wrong length
        [-1, 0, 10, 10],  # negative x
        [0, 0, 0, 10],  # zero width
        [0, 0, 10, 0],  # zero height
    ],
)
def test_region_invalid_rejected(region_key: str, bad: object) -> None:
    cfg = _minimal()
    cfg["cam0"]["settings"] = {region_key: bad}
    with pytest.raises(ValueError, match=region_key):
        validate_camera_config(cfg)


def test_in_range_settings_accepted() -> None:
    cfg = _minimal()
    cfg["cam0"]["settings"] = {
        "exposure": 20000,
        "iso": 400,
        "focus": 125,
        "ae_lock": True,
        "exposure_compensation": -3,
        "exposure_limit_us": 15000,
        "af_lens_range": [10, 200],
        "af_region": [0, 0, 100, 100],
        "ae_region": [50, 50, 200, 150],
    }
    cfg["cam0"]["wait_timeout"] = 5.0
    cfg["cam0"]["reconnect_on_timeout"] = True
    cfg["cam0"]["max_retries"] = 3
    validate_camera_config(cfg)


# ---------------------------------------------------------------------------
# Multiple cameras
# ---------------------------------------------------------------------------


def test_multiple_cameras_all_validated() -> None:
    cfg = {
        "cam0": {"camera_type": "color", "outputs": ["preview"]},
        "cam1": {"camera_type": "left", "outputs": ["video"]},
    }
    validate_camera_config(cfg)


def test_second_camera_failure_surfaces_id() -> None:
    cfg = {
        "cam0": {"camera_type": "color", "outputs": ["preview"]},
        "cam1": {"camera_type": "infrared", "outputs": ["preview"]},
    }
    with pytest.raises(ValueError, match="cam1"):
        validate_camera_config(cfg)


class TestWriteConfig:
    def test_writes_valid_config_atomically(self, tmp_path):
        import json

        from mydepthai.camera_manager.config_utils import write_config

        target = tmp_path / "sub" / "cfg.json"
        write_config(target, {"cam1": {"camera_type": "color"}})
        assert json.loads(target.read_text()) == {"cam1": {"camera_type": "color"}}
        assert [p.name for p in target.parent.iterdir()] == ["cfg.json"]

    def test_invalid_config_raises_and_leaves_existing_file(self, tmp_path):
        import pytest

        from mydepthai.camera_manager.config_utils import write_config

        target = tmp_path / "cfg.json"
        target.write_text("original")
        with pytest.raises(ValueError):
            write_config(target, {"cam1": {"outputs": []}})
        assert target.read_text() == "original"
        assert [p.name for p in tmp_path.iterdir()] == ["cfg.json"]
