"""SDK-surface guard: every ``depthai`` name the live code uses must exist in the installed wheel.

The rest of the suite mocks ``depthai`` via ``sys.modules`` (conftest.py), so a renamed or
removed SDK symbol — exactly what a ``depthai`` major bump does — would pass CI and fail on a
camera. This test imports the *real* wheel in a fresh interpreter (``-I``, so no mock leaks in)
and checks each attribute path. It skips when the wheel isn't installed; CI installs it.
"""

import json
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

# Keep in sync with `grep -rho "dai\.[A-Za-z_.]*" my_depthai/mydepthai` (excluding not_ready_*).
USED_SDK_NAMES = [
    "Buffer",
    "CameraBoardSocket.CAM_A",
    "CameraBoardSocket.CAM_B",
    "CameraBoardSocket.CAM_C",
    "CameraControl",
    "CameraControl.AntiBandingMode",
    "CameraControl.AutoFocusMode.AUTO",
    "CameraControl.AutoFocusMode.CONTINUOUS_PICTURE",
    "CameraControl.AutoFocusMode.CONTINUOUS_VIDEO",
    "CameraControl.AutoFocusMode.EDOF",
    "CameraControl.AutoFocusMode.MACRO",
    "Device",
    "Device.getAllAvailableDevices",
    "Device.ReconnectionStatus",
    "DeviceInfo",
    "ImageManipConfig",
    "ImgFrame",
    "ImgFrame.Type.BGR888p",
    "ImgFrame.Type.NV12",
    "InputQueue",
    "MessageQueue",
    "MessageQueue.tryGet",
    "Node.Output",
    "Pipeline",
    "UsbSpeed.HIGH",
    "UsbSpeed.SUPER",
    "node.Camera",
    "node.ImageManip",
    "node.Script",
]
# Methods called on a Device instance (checked on the class).
USED_DEVICE_METHODS = [
    "isClosed",
    "setMaxReconnectionAttempts",
    "getMxId",
    "getUsbSpeed",
    "getConnectedCameras",
    "getConnectedCameraFeatures",
    "close",
]
# Instance methods the code calls on SDK objects other than Device (checked on the class).
# These are not `dai.`-prefixed in the source, so the grep above cannot find them; keep in
# sync with calls on queues, frames, nodes, pipelines and CameraControl in mydepthai/.
USED_CLASS_METHODS = {
    "DeviceInfo": ["getDeviceId"],
    "ImgFrame": ["getCvFrame", "getLensPosition", "getExposureTime", "getSensitivity"],
    "MessageQueue": ["get", "has", "tryGet", "send", "setMaxSize", "setBlocking"],
    "InputQueue": ["send"],
    "Pipeline": ["create", "start"],
    "node.Camera": ["build", "requestOutput", "requestFullResolutionOutput", "inputControl"],
    "node.Script": ["setScript"],
    "Node.Output": ["createOutputQueue", "link"],
    "Node.Input": ["createInputQueue"],
    "CameraControl": [
        "setManualFocus",
        "setAutoFocusRegion",
        "setAutoExposureRegion",
        "setManualExposure",
        "setAutoExposureEnable",
        "setAntiBandingMode",
        "setAutoFocusMode",
        "setAutoFocusTrigger",
        "setAutoExposureLock",
        "setAutoExposureCompensation",
        "setAutoExposureLimit",
        "setAutoFocusLensRange",
    ],
}

_CHECK = """
import sys, warnings
warnings.simplefilter("ignore")
import depthai as dai
from functools import reduce
missing = []
for name in sys.argv[1].split(","):
    try:
        reduce(getattr, name.split("."), dai)
    except AttributeError:
        missing.append(name)
for m in sys.argv[2].split(","):
    if not hasattr(dai.Device, m):
        missing.append("Device." + m)
import json
for cls, methods in json.loads(sys.argv[3]).items():
    obj = reduce(getattr, cls.split("."), dai)
    for m in methods:
        if not hasattr(obj, m):
            missing.append(cls + "." + m)
print("MISSING:" + ",".join(missing))
"""


def test_used_sdk_names_exist_in_installed_depthai() -> None:
    # A fresh interpreter (-I) never sees conftest's sys.modules mock, only a real install.
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            _CHECK,
            ",".join(USED_SDK_NAMES),
            ",".join(USED_DEVICE_METHODS),
            json.dumps(USED_CLASS_METHODS),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0 and "No module named 'depthai'" in proc.stderr:
        pytest.skip("real depthai wheel not installed")
    assert proc.returncode == 0, proc.stderr
    missing = proc.stdout.strip().rsplit("MISSING:", 1)[-1]
    assert missing == "", f"depthai SDK no longer provides: {missing}"
