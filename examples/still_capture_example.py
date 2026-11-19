#!/usr/bin/env python3
"""
Quick manual smoke test for still-image capture.

Requires a physical OAK camera connected (USB or PoE). Not run in CI — depthai
needs real hardware, so this is meant to be run by hand after touching
still-capture code (camera_module.py, camera_config_module.py, basic_dai_camera.py).

Usage:
    python examples/still_capture_example.py [camera_index]

Controls:
    SPACE - trigger a still capture and save it to disk
    q     - quit
"""

import sys
import time

import cv2

from mydepthai.depthai_controllers.basic_dai_camera import BasicDAICamera

camera_number: int | str
try:
    camera_number = int(sys.argv[1])
except (IndexError, ValueError):
    camera_number = str(sys.argv[1]) if len(sys.argv) > 1 else 0

print(f"Using camera number: {camera_number}")
cam = BasicDAICamera(camera_number, speed=True)

print("Waiting for camera to connect...")
if not cam.wait_for_cam(10):
    print("Camera not connected!")
    sys.exit(1)

cam.create_camera("color", outputs=["preview", "still"])
cam.build()
cam.start_device()
print("Camera connected and streaming. Press SPACE to capture a still, 'q' to quit.")

still_count = 0
try:
    while True:
        frame = cam.get_frame("color", "preview")
        if frame is not None:
            cv2.imshow("Preview", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break

        if key == ord(" "):
            print("Triggering still capture...")
            start = time.time()
            still = cam.trigger_and_get_still()
            elapsed = time.time() - start

            if still is None:
                print(f"Still capture timed out after {elapsed:.2f}s")
                continue

            filename = f"still_{still_count:03d}.jpg"
            cv2.imwrite(filename, still)
            print(f"Saved {filename} shape={still.shape} in {elapsed:.2f}s")
            still_count += 1
            cv2.imshow("Last Still", cv2.resize(still, (640, 480)))
finally:
    cv2.destroyAllWindows()
    cam.close()
