"""Real-SDK host-side tests: my_depthai modules driven against genuine ``depthai`` objects.

The rest of the suite mocks ``depthai`` through ``sys.modules`` (conftest.py), so a MagicMock
``CameraControl`` accepts anything. The real pybind11 SDK does not: it rejects floats for ``int``
parameters. ``dai.CameraControl``, ``dai.MessageQueue`` and ``dai.ImgFrame`` all construct
host-side with no device, so each scenario here runs in a fresh interpreter (``-I``, no conftest
mock) that imports the real wheel and the real module under test. Skips when the wheel is absent.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PKG_ROOT = str(Path(__file__).resolve().parents[1])

_PRELUDE = f"""
import sys, json, warnings
warnings.simplefilter("ignore")
sys.path.insert(0, {_PKG_ROOT!r})
import depthai as dai
import numpy as np
"""


def _run_real(body: str) -> Any:
    """Run ``body`` against the real SDK; it must ``print(json.dumps(result))`` last."""
    code = _PRELUDE + textwrap.dedent(body)
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0 and "No module named 'depthai'" in proc.stderr:
        pytest.skip("real depthai wheel not installed")
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


_SETUP = """
from mydepthai.depthai_controllers.modules.camera_config_module import CameraConfigModule

class Q:
    def __init__(self):
        self.sent = []

    def send(self, c):
        self.sent.append(c)

m = CameraConfigModule()
q = Q()
m.set_control_queue(q)
"""

# (label, python expression run on the module). Every input is one the config validator or the
# control endpoint can let through today: floats, np.float64 and numpy ints.
_SETTER_CALLS = [
    ("focus_float", "m.set_focus(120.5)"),
    ("focus_int", "m.set_focus(120)"),
    ("focus_distance", "m.set_focus(1.5, distance=True)"),
    ("focus_clipped_high", "m.set_focus(9999.9)"),
    ("exposure_float_iso", "m.set_exposure(1000, 400.0)"),
    ("exposure_float_time", "m.set_exposure(1000.7, 400)"),
    ("exposure_numpy", "m.set_exposure(np.float64(500.5), np.float64(800.0))"),
    ("af_region_float", "m.set_Focus_Region(1.0, 2.0, 30.5, 40.5)"),
    ("ae_region_float", "m.set_Exposure_Region(10.2, 20.7, 300.0, 400.4)"),
    ("af_region_int", "m.set_Focus_Region(1, 2, 3, 4)"),
    ("lens_range", "m.set_auto_focus_lens_range(10.0, 200.0)"),
    ("exposure_limit_float", "m.set_auto_exposure_limit(100.5)"),
    ("exposure_comp_float", "m.set_exposure_compensation(2.0)"),
    ("ae_lock", "m.set_auto_exposure_lock(True)"),
    ("auto_exposure", "m.set_auto_exposure()"),
    ("continuous_af", "m.continuous_auto_focus()"),
    ("continuous_pic_af", "m.continuous_pic_auto_focus()"),
    ("trigger_af", "m.trigger_auto_focus()"),
    ("trigger_af_macro", "m.trigger_auto_focus_macro()"),
    ("trigger_af_edof", "m.trigger_auto_focus_EDOF()"),
    ("antibanding_0", "m.set_antibanding(0)"),
    ("antibanding_1", "m.set_antibanding(1)"),
]


@pytest.mark.parametrize("label,call", _SETTER_CALLS, ids=[c[0] for c in _SETTER_CALLS])
def test_config_module_setter_accepted_by_real_camera_control(label: str, call: str) -> None:
    """Each setter must build a real CameraControl without a pybind TypeError and send it once.

    Regression for the float-argument bug: set_focus / set_exposure / the region setters passed
    np.float64 or float values straight to depthai, which raises TypeError. The caller
    (``_apply_camera_settings``) swallows that, so the setting was silently dropped.
    """
    out = _run_real(_SETUP + f"""
{call}
print(json.dumps({{"sent": len(q.sent), "types": [type(c).__name__ for c in q.sent]}}))
""")
    assert out["sent"] == 1
    assert out["types"] == ["CameraControl"]


def test_config_module_state_holds_plain_ints_after_float_input() -> None:
    out = _run_real(_SETUP + """
m.set_focus(120.5)
m.set_exposure(1000.7, 400.0)
m.set_Focus_Region(1.4, 2.6, 30.0, 40.0)
s = m.get_control_state()
print(json.dumps({"lens": s["manual_lens_position"], "exp": s["manual_exposure_us"], "iso": s["manual_iso"],
                  "af": list(s["af_region"]), "types": [type(x).__name__ for x in s["af_region"]]}))
""")
    assert out["lens"] == 120
    assert out["exp"] == 1000
    assert out["iso"] == 400
    assert out["af"] == [1, 3, 30, 40]  # rounded, not truncated
    assert set(out["types"]) == {"int"}


def test_base_module_get_data_against_real_message_queue() -> None:
    out = _run_real("""
from mydepthai.depthai_controllers.modules.base_module import BaseModule

class M(BaseModule):
    pass

m = M()
m.out_queues["x"] = dai.MessageQueue()
res = {}
res["empty_poll"] = m.get_data("x", timeout=0) is None
res["empty_timeout"] = m.get_data("x", timeout=0.01) is None
res["empty_default"] = m.get_data("x") is None
res["has_empty"] = m.has_data("x")
f = dai.ImgFrame()
f.setCvFrame(np.zeros((2, 2, 3), np.uint8), dai.ImgFrame.Type.BGR888p)
m.out_queues["x"].send(f)
res["has_full"] = m.has_data("x")
res["got_type"] = type(m.get_data("x", timeout=0.5)).__name__
res["drained"] = m.get_data("x", timeout=0) is None
print(json.dumps(res))
""")
    assert out == {
        "empty_poll": True,
        "empty_timeout": True,
        "empty_default": True,
        "has_empty": False,
        "has_full": True,
        "got_type": "ImgFrame",
        "drained": True,
    }


def test_camera_frame_module_metadata_against_real_img_frame() -> None:
    out = _run_real("""
from mydepthai.depthai_controllers.modules.camera_frame_module import CameraFrameModule

m = CameraFrameModule()
m.out_queues["preview"] = dai.MessageQueue()
img = np.full((6, 8, 3), 7, np.uint8)
f = dai.ImgFrame()
f.setCvFrame(img, dai.ImgFrame.Type.BGR888p)
m.out_queues["preview"].send(f)
frame, meta = m.get_frame_with_metadata("preview", timeout=0.5)
res = {"shape": list(frame.shape), "mean": float(frame.mean()), "meta": meta,
       "meta_types": {k: type(v).__name__ for k, v in meta.items()},
       "none_when_empty": m.get_frame_with_metadata("preview", timeout=0) is None,
       "frame_none_when_empty": m.get_frame("preview", timeout=0) is None}
print(json.dumps(res))
""")
    assert out["shape"] == [6, 8, 3]
    assert out["mean"] == 7.0
    assert out["meta"]["frame_width"] == 8
    assert out["meta"]["frame_height"] == 6
    assert set(out["meta"]) == {"lens_position", "exposure_time_us", "iso", "frame_width", "frame_height"}
    assert out["meta_types"]["exposure_time_us"] == "float"
    assert out["meta_types"]["lens_position"] == "int"
    assert out["meta_types"]["iso"] == "int"
    assert out["none_when_empty"] and out["frame_none_when_empty"]
