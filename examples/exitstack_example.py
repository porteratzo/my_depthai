#!/usr/bin/env python3
"""
ExitStack Integration Example

This example demonstrates how ExitStack is used to manage multiple camera
contexts, ensuring proper cleanup even when dealing with multiple cameras.
"""

import logging
import time
from contextlib import ExitStack

import cv2

from mydepthai.depthai_controllers.basic_dai_camera import BasicDAICamera
from shared.core.log_setup import setup_logging

setup_logging("exitstack_example")
logger = logging.getLogger(__name__)


def example_single_camera_with_exitstack() -> None:
    """Example using ExitStack with a single camera."""
    logger.info("=== Example 1: Single Camera with ExitStack ===")

    with ExitStack() as stack:
        # Create camera with ExitStack for automatic cleanup
        camera = BasicDAICamera(identifier=0, exit_stack=stack)

        if camera.wait_for_cam(timeout=10.0):
            logger.info("Camera connected")

            # Configure camera
            camera.create_camera(cam_type="color", outputs=["preview"], sizes=[(640, 480)], fpss=[30])
            camera.build()
            camera.start_device()

            # Capture some frames
            logger.info("Capturing frames for 5 seconds...")
            start_time = time.time()
            frame_count = 0

            while time.time() - start_time < 5:
                frame = camera.get_frame(cam="color", out_type="preview")
                if frame is not None:
                    cv2.imshow("Camera", frame)
                    frame_count += 1

                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            cv2.destroyAllWindows()
            logger.info("Captured %d frames", frame_count)
        else:
            logger.error("Camera failed to connect")

    logger.info("Camera pipeline automatically closed via ExitStack")


def example_multiple_cameras_with_exitstack() -> None:
    """Example using ExitStack with multiple cameras."""
    logger.info("=== Example 2: Multiple Cameras with ExitStack ===")

    with ExitStack() as stack:
        cameras = []

        # Initialize multiple cameras
        for i in range(2):  # Attempt to initialize 2 cameras
            try:
                logger.info("Initializing camera %d", i)
                camera = BasicDAICamera(identifier=i, exit_stack=stack)

                if camera.wait_for_cam(timeout=10.0):
                    camera.create_camera(cam_type="color", outputs=["preview"], sizes=[(300, 300)], fpss=[20])
                    camera.build()
                    camera.start_device()
                    cameras.append(camera)
                    logger.info("Camera %d initialized successfully", i)
                else:
                    logger.warning("Camera %d failed to connect", i)

            except Exception as e:
                logger.exception("Error initializing camera %d: %s", i, e)

        logger.info("Successfully initialized %d cameras", len(cameras))

        if cameras:
            # Capture frames from all cameras
            logger.info("Capturing frames for 10 seconds...")
            start_time = time.time()

            while time.time() - start_time < 10:
                for idx, camera in enumerate(cameras):
                    frame = camera.get_frame(cam="color", out_type="preview")
                    if frame is not None:
                        cv2.imshow(f"Camera {idx}", frame)

                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            cv2.destroyAllWindows()

    logger.info("All %d camera pipelines automatically closed via ExitStack", len(cameras))


def example_without_exitstack() -> None:
    """Example showing manual management without ExitStack (old way)."""
    logger.info("=== Example 3: Manual Management (Without ExitStack) ===")

    camera = None
    try:
        # Create camera without ExitStack
        camera = BasicDAICamera(identifier=0)

        if camera.wait_for_cam(timeout=10.0):
            logger.info("Camera connected")

            camera.create_camera(cam_type="color", outputs=["preview"], sizes=[(640, 480)], fpss=[30])
            camera.build()
            camera.start_device()

            # Capture frames
            logger.info("Capturing frames for 5 seconds...")
            start_time = time.time()
            frame_count = 0

            while time.time() - start_time < 5:
                frame = camera.get_frame(cam="color", out_type="preview")
                if frame is not None:
                    cv2.imshow("Camera", frame)
                    frame_count += 1

                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            cv2.destroyAllWindows()
            logger.info("Captured %d frames", frame_count)
        else:
            logger.error("Camera failed to connect")

    finally:
        # Must manually close
        if camera is not None:
            logger.info("Manually closing camera")
            camera.close()


def example_error_handling_with_exitstack() -> None:
    """Example showing ExitStack still cleans up even with errors."""
    logger.info("=== Example 4: Error Handling with ExitStack ===")

    try:
        with ExitStack() as stack:
            camera = BasicDAICamera(identifier=0, exit_stack=stack)

            if camera.wait_for_cam(timeout=10.0):
                camera.create_camera(cam_type="color", outputs=["preview"], sizes=[(640, 480)], fpss=[30])
                camera.build()
                camera.start_device()

                # Simulate an error
                logger.info("Simulating an error...")
                raise RuntimeError("Simulated error during operation")

    except RuntimeError as e:
        logger.error("Caught error: %s", e)

    logger.info("Camera was still properly closed despite the error!")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        example_num = sys.argv[1]
    else:
        print("Usage: python exitstack_example.py [1|2|3|4]")
        print("  1: Single camera with ExitStack")
        print("  2: Multiple cameras with ExitStack")
        print("  3: Manual management (without ExitStack)")
        print("  4: Error handling with ExitStack")
        print("\nRunning example 1 by default...")
        example_num = "1"

    if example_num == "1":
        example_single_camera_with_exitstack()
    elif example_num == "2":
        example_multiple_cameras_with_exitstack()
    elif example_num == "3":
        example_without_exitstack()
    elif example_num == "4":
        example_error_handling_with_exitstack()
    else:
        print(f"Unknown example: {example_num}")
        sys.exit(1)
