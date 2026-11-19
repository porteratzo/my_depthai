#!/usr/bin/env python3
# %%
import cv2
import depthai as dai

# Create pipeline
print("Creating pipeline...")
device = dai.Device("169.254.1.222")

print("Pipeline created, connecting to device...")
with dai.Pipeline(device) as pipeline:
    outputQueues = {}
    print("Pipeline started, getting connected cameras...")
    sockets = device.getConnectedCameras()
    for socket in sockets:
        print(f"Creating camera for socket: {socket}")
        cam = pipeline.create(dai.node.Camera).build(socket)
        outputQueues[str(socket)] = cam.requestFullResolutionOutput().createOutputQueue()

    print("Starting pipeline...")
    pipeline.start()
    while pipeline.isRunning():
        from time import sleep

        sleep(10)
        print("Pipeline running, getting frames...")
        for name in outputQueues.keys():
            queue = outputQueues[name]
            videoIn = queue.get()
            assert isinstance(videoIn, dai.ImgFrame)
            # Visualizing the frame on slower hosts might have overhead
            cv2.imshow(name, videoIn.getCvFrame())

        if cv2.waitKey(1) == ord("q"):
            break
