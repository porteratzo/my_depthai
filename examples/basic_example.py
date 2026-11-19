import sys
import time
from time import sleep
from typing import List, Literal

import cv2
from porter_bench import CountDownClock, TimedCounter

from mydepthai.depthai_controllers.basic_dai_camera import BasicDAICamera

camera_number: int | str
try:
    camera_number = int(sys.argv[1])
except (IndexError, ValueError):
    camera_number = str(sys.argv[1])
print(f"Using camera number: {camera_number}")
cam: BasicDAICamera = BasicDAICamera(camera_number, speed=True)
print("Waiting for camera to connect...")
if not cam.wait_for_cam(100):
    print("Camera not connected!")
    exit(1)
# streams_to_use = ["preview", "video"]
# streams_to_use = ["still"]
# streams_to_use = ["preview"]
# streams_to_use = ["video"]
streams_to_use: List[Literal["preview", "still", "video", "fullvideo"]] = ["fullvideo"]
print(f"Using streams: {streams_to_use}")
cam.create_camera("color", outputs=streams_to_use, fpss=[5])
# cam.create_camera('color', outputs=['still'])
cam.build()
cam.start_device()
print("Camera connected and streaming!")

sleep(2)

# cam.components["color"].set_focus(60)
cam.components["color"].trigger_auto_focus()

clock = CountDownClock(2)
counters: dict[str, TimedCounter] = {}
window_name = ""
while True:
    frames = {}
    # getting info from oak
    # source = cam.detectors['main']._input.stream.name
    for out in streams_to_use:
        if out == "still":
            cam.trigger_still()
        if cam.components["color"].has_data(out):
            if counters.get(out) is None:
                counters[out] = TimedCounter()
            counters[out].count()
            frame = cam.get_frame("color", out)
            frames[out] = frame

    k = cv2.waitKey(50)
    if k != -1:
        print(k)

    if k == 27:
        break

    if k == ord("a"):
        print("Triggering auto focus")
        cam.components["color"].trigger_auto_focus()

    if k == ord("s"):
        print("Setting manual focus to 130")
        cam.components["color"].set_focus(130)

    if k == ord("r"):
        print("Setting auto focus")
        cam.components["color"].set_focus(0)

    if k == ord("e"):
        cam.components["color"].set_focus(255)

    if k == ord("d"):
        cam.components["color"].set_auto_exposure()

    if k == ord("t"):
        print("setting manual exposure to 20000us, iso 800")
        cam.components["color"].set_exposure(20000, 800)

    if k == ord("y"):
        print("setting auto exposure")
        cam.components["color"].set_exposure(10000, 100)

    frames_to_use = [frames[i] for i in streams_to_use if frames.get(i) is not None]

    if len(frames_to_use) > 0:

        if clock.completed():
            for i in range(len(frames_to_use)):
                h, w, _ = frames_to_use[i].shape
                print(f"frame {streams_to_use[i]} shape:", frames_to_use[i].shape)
                print("fps:", counters[streams_to_use[i]].get_frequency())
            cv2.imwrite(f"datatosave2/ {time.time()}.png", frame)
            clock.set_count_down(2)
        for n, frame in enumerate(frames_to_use):
            window_name = f"Camera {camera_number} - {streams_to_use[n]}"
            display_frame = cv2.resize(frame, (1920, 1080))
            cv2.imshow(window_name, display_frame)

cv2.destroyAllWindows()
time.sleep(1)
cam.close()
