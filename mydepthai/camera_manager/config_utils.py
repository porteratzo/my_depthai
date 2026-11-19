"""
Camera configuration validation utilities.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Union

logger = logging.getLogger(__name__)

VALID_CAMERA_TYPES = {"color", "left", "right"}
VALID_OUTPUTS = {"preview", "video", "still", "fullvideo"}


def default_config_path(name: str = "camera_manager_config.json") -> Path:
    """Path of a camera config shipped inside the package (``mydepthai/configs/defaults``).

    Resolved from this file, so it works from an installed wheel and from a checkout. Copy it into your
    project to seed a camera config; ``sampleconfig_camera.json`` is the annotated sample.
    """
    return Path(__file__).resolve().parents[1] / "configs" / "defaults" / name


def _validate_outputs(cam_id: str, cam_config: Dict[str, Any]) -> None:
    """Validate ``camera_type``, ``outputs`` and the per-output ``sizes`` / ``fpss`` lists."""
    cam_type = cam_config.get("camera_type", "color")
    if cam_type not in VALID_CAMERA_TYPES:
        raise ValueError(
            f"Camera '{cam_id}': invalid camera_type '{cam_type}', " f"must be one of {VALID_CAMERA_TYPES}"
        )

    outputs = cam_config.get("outputs", ["preview", "video"])
    if not isinstance(outputs, list) or not outputs:
        raise ValueError(f"Camera '{cam_id}': 'outputs' must be a non-empty list")
    for output in outputs:
        if output not in VALID_OUTPUTS:
            raise ValueError(
                f"Camera '{cam_id}': invalid output '{output}', " f"must be one of {VALID_OUTPUTS}"
            )

    sizes = cam_config.get("sizes")
    if sizes is not None and len(sizes) != len(outputs):
        raise ValueError(
            f"Camera '{cam_id}': 'sizes' length {len(sizes)} does not match "
            f"'outputs' length {len(outputs)}"
        )

    fpss = cam_config.get("fpss")
    if fpss is not None and len(fpss) != len(outputs):
        raise ValueError(
            f"Camera '{cam_id}': 'fpss' length {len(fpss)} does not match " f"'outputs' length {len(outputs)}"
        )


def _validate_connection_params(cam_id: str, cam_config: Dict[str, Any]) -> None:
    """Validate ``wait_timeout`` / ``reconnect_on_timeout`` / ``max_retries``."""
    if "wait_timeout" in cam_config:
        wt = cam_config["wait_timeout"]
        if not isinstance(wt, (int, float)) or wt <= 0:
            raise ValueError(f"Camera '{cam_id}': wait_timeout must be a positive number, got {wt}")

    if "reconnect_on_timeout" in cam_config:
        rot = cam_config["reconnect_on_timeout"]
        if not isinstance(rot, bool):
            raise ValueError(f"Camera '{cam_id}': reconnect_on_timeout must be a boolean, got {rot!r}")

    if "max_retries" in cam_config:
        mr = cam_config["max_retries"]
        if not isinstance(mr, int) or mr < 0:
            raise ValueError(f"Camera '{cam_id}': max_retries must be a non-negative integer, got {mr!r}")


def _validate_exposure_focus_settings(cam_id: str, settings: Dict[str, Any]) -> None:
    """Validate the manual exposure / ISO / focus settings."""
    if "exposure" in settings:
        exp = settings["exposure"]
        if not isinstance(exp, (int, float)) or not (1 <= exp <= 33333):
            raise ValueError(f"Camera '{cam_id}': exposure {exp} out of range [1, 33333] µs")
    if "iso" in settings:
        iso = settings["iso"]
        if not isinstance(iso, int) or not (100 <= iso <= 1600):
            raise ValueError(f"Camera '{cam_id}': ISO {iso} out of range [100, 1600]")
    if "focus" in settings:
        focus = settings["focus"]
        if not isinstance(focus, int) or not (1 <= focus <= 254):
            raise ValueError(f"Camera '{cam_id}': focus {focus} out of range [1, 254]")


def _validate_ae_settings(cam_id: str, settings: Dict[str, Any]) -> None:
    """Validate the auto-exposure lock / compensation / limit settings."""
    if "ae_lock" in settings:
        ae_lock = settings["ae_lock"]
        if not isinstance(ae_lock, bool):
            raise ValueError(f"Camera '{cam_id}': ae_lock must be a boolean, got {ae_lock!r}")
    if "exposure_compensation" in settings:
        comp = settings["exposure_compensation"]
        if not isinstance(comp, int) or isinstance(comp, bool) or not (-9 <= comp <= 9):
            raise ValueError(f"Camera '{cam_id}': exposure_compensation {comp} out of range [-9, 9]")
    if "exposure_limit_us" in settings:
        lim = settings["exposure_limit_us"]
        if not isinstance(lim, (int, float)) or isinstance(lim, bool) or not (1 <= lim <= 33333):
            raise ValueError(f"Camera '{cam_id}': exposure_limit_us {lim} out of range [1, 33333]")


def _validate_af_lens_range(cam_id: str, rng: Any) -> None:
    if (
        not isinstance(rng, list)
        or len(rng) != 2
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in rng)
        or not (0 <= rng[0] <= 255 and 0 <= rng[1] <= 255)
        or not (rng[0] < rng[1])
    ):
        raise ValueError(
            f"Camera '{cam_id}': af_lens_range {rng} must be [infinity, macro] "
            "with 0 <= infinity < macro <= 255"
        )


def _validate_region(cam_id: str, region_key: str, region: Any) -> None:
    if (
        not isinstance(region, list)
        or len(region) != 4
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in region)
        or region[0] < 0
        or region[1] < 0
        or region[2] <= 0
        or region[3] <= 0
    ):
        raise ValueError(
            f"Camera '{cam_id}': {region_key} {region} must be [x, y, w, h] " "with x,y >= 0 and w,h > 0"
        )


def _validate_single_camera(cam_id: str, cam_config: Dict[str, Any]) -> None:
    """Validate one camera's config entry. Raises ValueError on bad values."""
    if not isinstance(cam_config, dict):
        raise ValueError(f"Camera '{cam_id}': config must be a dict, got {type(cam_config)}")

    _validate_outputs(cam_id, cam_config)
    _validate_connection_params(cam_id, cam_config)

    settings = cam_config.get("settings", {})
    _validate_exposure_focus_settings(cam_id, settings)
    _validate_ae_settings(cam_id, settings)
    if "af_lens_range" in settings:
        _validate_af_lens_range(cam_id, settings["af_lens_range"])
    for region_key in ("af_region", "ae_region"):
        if region_key in settings:
            _validate_region(cam_id, region_key, settings[region_key])


def validate_camera_config(config: Dict[str, Any]) -> None:
    """
    Validate camera configuration schema and value ranges.

    Args:
        config: Configuration dictionary to validate

    Raises:
        ValueError: If any configuration field is invalid
    """
    if not isinstance(config, dict) or not config:
        raise ValueError("Configuration must be a non-empty dictionary")

    for cam_id, cam_config in config.items():
        _validate_single_camera(cam_id, cam_config)

    logger.info("Configuration validated successfully (%d cameras)", len(config))


def write_config(path: Union[str, Path], config: Dict[str, Any]) -> None:
    """Validate ``config`` and atomically write it to ``path`` (temp file + ``os.replace``).

    An invalid config raises ``ValueError`` *before* anything is written, and a crash
    mid-write can never leave a truncated file that stops the service from restarting.
    """
    validate_camera_config(config)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
