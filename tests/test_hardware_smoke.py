"""(approved 2026-11-03): smoke test against a physically attached OAK camera.

The rest of the suite mocks ``depthai`` via ``sys.modules`` (conftest.py), so nothing else opens a
real device. This runs ``BasicDAICamera`` end to end -- connect, build, start, read a frame, close --
in a fresh interpreter (``-I``, no conftest mock) against the real SDK.

Never runs in CI (``-m "not hardware"``). It skips, rather than fails, when the wheel is missing or
no device is attached. Run it by hand before merging changes to the camera path::

    pytest my_depthai/tests/test_hardware_smoke.py -m hardware --no-cov
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.hardware

_PKG_ROOT = str(Path(__file__).resolve().parents[1])
_NO_DEVICE_EXIT = 77

_SCRIPT = f"""
import sys, json, warnings
warnings.simplefilter("ignore")
sys.path.insert(0, {_PKG_ROOT!r})
import depthai as dai

try:
    devices = dai.Device.getAllAvailableDevices()
except Exception as exc:  # SDK present but cannot enumerate (no udev rules, no USB access)
    print("enumeration failed: %r" % (exc,), file=sys.stderr)
    sys.exit({_NO_DEVICE_EXIT})
if not devices:
    sys.exit({_NO_DEVICE_EXIT})

from mydepthai.depthai_controllers.basic_dai_camera import BasicDAICamera

cam = BasicDAICamera(0, speed=True)
result = {{"devices": len(devices)}}
try:
    result["connected"] = bool(cam.wait_for_cam(timeout=25.0, reconnect_on_timeout=True, max_retries=2))
    if result["connected"]:
        cam.create_camera("color", outputs=["preview"])
        cam.build()
        cam.start_device()
        frame = None
        import time
        deadline = time.time() + 20
        while frame is None and time.time() < deadline:
            frame = cam.get_frame("color", "preview")
            time.sleep(0.1)
        result["got_frame"] = frame is not None
        if frame is not None:
            result["shape"] = list(frame.shape)
finally:
    cam.close(force=True)
print(json.dumps(result))
"""


def test_attached_camera_connects_and_streams_a_frame() -> None:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", textwrap.dedent(_SCRIPT)], capture_output=True, text=True, timeout=180
    )
    if proc.returncode != 0 and "No module named 'depthai'" in proc.stderr:
        pytest.skip("real depthai wheel not installed")
    if proc.returncode == _NO_DEVICE_EXIT:
        pytest.skip("no OAK camera attached (or the SDK cannot enumerate devices)")
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["connected"], result
    assert result["got_frame"], result
    assert len(result["shape"]) == 3 and result["shape"][2] == 3, result
