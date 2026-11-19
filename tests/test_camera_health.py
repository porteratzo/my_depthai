"""classify_health: the single health rule behind get_status and the monitor thread."""

from typing import Any

import pytest

from mydepthai.camera_manager.health import CameraHealth, classify_health

pytestmark = pytest.mark.unit


def _h(**kw: Any) -> CameraHealth:
    args: dict = dict(
        reconnecting=False, camera_present=True, camera_ready=True, last_frame_age_s=1.0, data_timeout=10.0
    )
    args.update(kw)
    return classify_health(**args)


@pytest.mark.parametrize(
    "kw,expected",
    [
        ({}, CameraHealth.ONLINE),
        ({"last_frame_age_s": 10.0}, CameraHealth.ONLINE),  # boundary: not yet stale
        ({"last_frame_age_s": 10.1}, CameraHealth.STALE),
        ({"last_frame_age_s": None}, CameraHealth.STALE),
        ({"camera_ready": False}, CameraHealth.OFFLINE),
        ({"camera_present": False, "camera_ready": False, "last_frame_age_s": None}, CameraHealth.OFFLINE),
        ({"reconnecting": True}, CameraHealth.CONNECTING),
        ({"reconnecting": True, "camera_present": False, "camera_ready": False}, CameraHealth.CONNECTING),
        (
            {"camera_ready": False, "last_frame_age_s": 0.0},
            CameraHealth.OFFLINE,
        ),  # fresh frame can't save a dead link
    ],
)
def test_classification(kw: dict, expected: CameraHealth) -> None:
    assert _h(**kw) is expected


def test_values_are_the_public_state_strings() -> None:
    assert [h.value for h in CameraHealth] == ["online", "stale", "connecting", "offline"]
    assert CameraHealth.ONLINE == "online"  # str-enum: JSON-serialisable as-is
