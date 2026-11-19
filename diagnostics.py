#!/usr/bin/env python3
"""
Diagnostic tool for OAK cameras and mydepthai library.

Usage:
    python diagnostics.py --find           # Find all available OAK cameras
    python diagnostics.py --info           # Show detailed device information
    python diagnostics.py --test <mxid>    # Test connection to specific device
    python diagnostics.py --all            # Run all diagnostics
"""

import argparse
import logging
import os
import sys
from time import sleep
from typing import Any, List, Optional

import cv2

from mydepthai.depthai_controllers.basic_dai_camera import BasicDAICamera

logger = logging.getLogger(__name__)

try:
    import depthai as dai
except ImportError:
    print("ERROR: depthai module not found. Please install it:")
    print("  pip install depthai")
    sys.exit(1)


def find_devices(ip: Optional[str] = None) -> List[dai.DeviceInfo]:
    """Find all available OAK devices."""
    print("=" * 60)
    print("Searching for OAK devices...")
    print("=" * 60)

    if ip:
        print(f"\nConnecting to PoE camera at {ip}...")
        devices: List[dai.DeviceInfo] = [dai.DeviceInfo(ip)]
    else:
        devices = dai.Device.getAllAvailableDevices()

    if not devices:
        print("\n❌ No OAK devices found!")
        print("\nTroubleshooting tips:")
        print("  1. Check USB connection")
        print("  2. Verify USB permissions (Linux):")
        print('     echo \'SUBSYSTEM=="usb", ATTRS{idVendor}=="03e7", MODE="0666"\' | \\')
        print("       sudo tee /etc/udev/rules.d/80-movidius.rules")
        print("     sudo udevadm control --reload-rules && sudo udevadm trigger")
        print("  3. Try a different USB port or cable")
        print("  4. Check if device appears in lsusb (Linux) or Device Manager (Windows)")
        return []

    print(f"\n✅ Found {len(devices)} OAK device(s):\n")

    for idx, device in enumerate(devices):
        print(f"Device {idx}:")
        print(f"  MXID:       {device.deviceId}")
        print(f"  Name:       {device.name}")
        print(f"  State:      {device.state.name}")
        print(f"  Protocol:   {device.protocol.name}")

    return devices


def _connect_device(device_info: dai.DeviceInfo) -> dai.Device:
    """Attempt to open a dai.Device, falling back to USB 2.0 if SuperSpeed fails."""
    try:
        return dai.Device(device_info)
    except RuntimeError as e:
        print(f"   ⚠️  SuperSpeed failed [{type(e).__name__}], retrying with USB 2.0... ({e})")
        return dai.Device(device_info, maxUsbSpeed=dai.UsbSpeed.HIGH)


def device_info(devices: Optional[List[dai.DeviceInfo]] = None, ip: Optional[str] = None) -> None:
    """Show detailed information about connected devices."""
    if devices is None:
        devices = [dai.DeviceInfo(ip)] if ip else dai.Device.getAllAvailableDevices()

    if not devices:
        print("No devices found to show info for.")
        return

    print("=" * 60)
    print("Detailed Device Information")
    print("=" * 60)

    for idx, device_info in enumerate(devices):
        print(f"\n{'=' * 60}")
        print(f"Device {idx}: {device_info.deviceId}")
        print("=" * 60)

        try:
            with _connect_device(device_info) as device:
                print("\n✅ Successfully connected!\n")

                # Get connected cameras
                cameras = device.getConnectedCameras()
                print(f"Connected Cameras ({len(cameras)}):")
                for cam in cameras:
                    print(f"  - {cam.name}")

                # Get USB speed
                usb_speed = device.getUsbSpeed()
                print(f"\nUSB Speed: {usb_speed.name}")

                # Device info
                print("\nDevice Info:")
                print(f"  MXID:               {device.getDeviceId()}")

                print("\nDevice features:")
                features = device.getConnectedCameraFeatures()
                for feat in features:
                    print(feat)

                # Check available sensors
                print("\nAvailable Sensors:")
                for sensor in cameras:
                    print(f"  Socket {sensor.name}: Available")

        except Exception as e:
            print(f"\n❌ Could not connect to device: {e}")
            print(f"   State: {device_info.state.name}")


def _build_camera_output_matrix(device: dai.Device) -> dict[str, Any]:
    """Map connected camera sockets to their default output types."""
    connected = device.getConnectedCameras()
    matrix = {}
    if dai.CameraBoardSocket.CAM_A in connected:
        matrix["color"] = ["preview"]
    if dai.CameraBoardSocket.CAM_B in connected:
        matrix["left"] = ["video"]
    if dai.CameraBoardSocket.CAM_C in connected:
        matrix["right"] = ["video"]
    return matrix


def _get_device_identifier(mxid: Optional[str] = None, ip: Optional[str] = None) -> "str | int | bool":
    identifier: str | int
    if ip:
        print(f"\nConnecting to PoE camera at {ip}...")
        identifier = ip
    else:
        devices = dai.Device.getAllAvailableDevices()

        if not devices:
            print("\n❌ No devices found!")
            return False

        identifier = mxid if mxid is not None else 0
        if mxid:
            found = any(dev.getDeviceId() == mxid for dev in devices)
            if not found:
                print(f"\n❌ Device with MXID {mxid} not found!")
                return False
    return identifier


def test_device(mxid: Optional[str] = None, ip: Optional[str] = None) -> bool:
    """Test connection to a specific device or the first available one."""
    print("=" * 60)
    print("Testing Device Connection")
    print("=" * 60)

    # IP-based PoE camera
    identifier: int | str
    _get_device_identifier_result = _get_device_identifier(mxid=mxid, ip=ip)
    if not _get_device_identifier_result:
        return False
    identifier = _get_device_identifier_result

    # Test connection
    cam: Optional[BasicDAICamera] = None
    try:
        cam = BasicDAICamera(identifier, speed=True)
        timeout = 10  # seconds
        if ip:
            timeout = 100  # PoE cameras can take longer to initialize
        cam.wait_for_cam(timeout=timeout)
        if not cam.cam_ready():
            print("\n❌ Camera did not become ready within timeout!")
            return False
        camera_output_matrix = _build_camera_output_matrix(cam.device)  # type: ignore[arg-type]

        for cam_name, outputs in camera_output_matrix.items():
            cam.create_camera(cam_name, outputs=outputs)  # type: ignore[arg-type]
        cam.build()
        cam.start_device()
        print("\n✅ Device started successfully!")
        sleep(2)  # Wait a moment for frames to start flowing

        for cam_name, outputs in camera_output_matrix.items():
            for output in outputs:
                print(f"Capturing frame from {cam_name} - {output}...")
                frame = cam.get_frame(cam_name, output)
                cv2.imwrite(f"diagnostics_results/{mxid}_{cam_name}_{output}.png", frame)
        print("\n✅ Device test completed successfully!")
        cam.close()
        return True

    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback

        traceback.print_exc()
        if cam:
            cam.close()
        return False


def run_all_diagnostics(ip: Optional[str] = None) -> None:
    """Run all diagnostic checks."""
    print("\n" + "=" * 60)
    print("  OAK CAMERA DIAGNOSTICS - FULL SUITE")
    print("=" * 60 + "\n")

    # Find devices
    devices = find_devices(ip=ip)
    print("\n")

    # Show device info
    if devices:
        device_info(devices)
        print("\n")
        sleep(1)  # Small delay before testing

        # Test first device
        for device in devices:
            print(f"\n{'=' * 60}")
            if ip:
                print(f"Testing PoE device at {ip}")
            else:
                print(f"Testing device {device.deviceId}")
            print(f"{'=' * 60}\n")
            test_device(None if ip else device.deviceId, ip=ip)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Diagnostic tool for OAK cameras",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python diagnostics.py --find
  python diagnostics.py --info
  python diagnostics.py --test
  python diagnostics.py --test 14442C10D13EABCE00
  python diagnostics.py --all
        """,
    )

    parser.add_argument("--find", action="store_true", help="Find all available OAK cameras")
    parser.add_argument("--info", action="store_true", help="Show detailed device information")
    parser.add_argument(
        "--test",
        nargs="?",
        const=True,
        metavar="MXID",
        help="Test connection (optionally specify MXID)",
    )
    parser.add_argument("--all", action="store_true", help="Run all diagnostics")
    parser.add_argument(
        "--ip",
        metavar="IP_ADDRESS",
        help="IP address of a PoE OAK camera (e.g. 192.168.1.100)",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # If no arguments, show help
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    os.makedirs("diagnostics_results", exist_ok=True)

    try:
        if args.all:
            run_all_diagnostics(ip=args.ip)
        else:
            if args.find:
                find_devices(ip=args.ip)
            if args.info:
                device_info(ip=args.ip)
            if args.test is not None:
                mxid = args.test if isinstance(args.test, str) else None
                test_device(mxid, ip=args.ip)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
