"""CameraConfigModule against the *real* ``dai.CameraControl`` (TEST_REVIEW B8).

The rest of the suite mocks ``depthai``, and a MagicMock accepts any argument, so a float handed to
an SDK setter that only takes ints went unnoticed: the real ``setManualFocus`` /
``setManualExposure`` / ``setAutoFocusRegion`` / ``setAutoExposureRegion`` raise ``TypeError`` on
floats. ``_apply_camera_settings`` swallows that, so a settings save returned 200 with nothing
applied, and a float lens position / ISO from the web API gave a 500.

Like test_depthai_sdk_surface.py this runs in a fresh interpreter (``-I``) so the conftest mock
never leaks in, and skips when the wheel isn't installed. A real CameraControl can be built
without a device.
"""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = """
import warnings
warnings.simplefilter("ignore")
import depthai as dai
from mydepthai.depthai_controllers.modules.camera_config_module import CameraConfigModule

seen = []
m = CameraConfigModule()
m.try_set_control_queue = lambda ctrl: seen.append(ctrl)  # keep the REAL CameraControl, skip the queue

m.set_focus(120.5)
m.set_exposure(1000.5, 250.7)
m.set_Focus_Region(10.4, 20.5, 100.6, 100.2)
m.set_Exposure_Region(10.4, 20.5, 100.6, 100.2)
assert all(isinstance(c, dai.CameraControl) for c in seen) and len(seen) == 4, seen
print("OK")
"""


def test_float_inputs_reach_the_real_sdk_as_ints() -> None:
    proc = subprocess.run([sys.executable, "-I", "-c", _SCRIPT], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 and (
        "No module named 'depthai'" in proc.stderr or "No module named 'mydepthai'" in proc.stderr
    ):
        pytest.skip("real depthai wheel / mydepthai not installed")
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert proc.stdout.strip().endswith("OK")
