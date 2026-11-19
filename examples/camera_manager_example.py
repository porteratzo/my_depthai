#!/usr/bin/env python3
"""
Camera Manager Example

This example demonstrates how to use the CameraManager to manage multiple
BasicDAICamera instances with configuration file support.
"""

import logging
import time
from pathlib import Path

import cv2

from mydepthai.camera_manager.camera_manager import CameraManager
from shared.core.log_setup import setup_logging

setup_logging("camera_manager_example")
logger = logging.getLogger(__name__)


def example_with_config_file() -> None:
    """Example using a configuration file with automatic cleanup via ExitStack."""
    logger.info("=== Example 1: Using Configuration File ===")

    # Path to configuration file
    config_path = Path(__file__).parent.parent / "configs" / "defaults" / "camera_manager_config.json"

    # Create camera manager with config file
    # Using context manager ensures proper cleanup via ExitStack
    manager = CameraManager(
        config_path=config_path,
        health_check_interval=5.0,  # Check health every 5 seconds
        data_timeout=10.0,  # Warn if no data for 10 seconds
    )

    # Initialize all cameras
    manager.initialize_cameras(use_superspeed=True)

    # Start health monitoring
    manager.start_monitoring()

    logger.info("Active cameras: %d", len(manager))

    # Capture frames for 30 seconds
    try:
        start_time = time.time()
        frame_count = 0

        while time.time() - start_time < 30:
            # Get frames from all cameras
            frames = manager.get_frames_all()

            # Display frames
            for cam_id, cam_frames in frames.items():
                for cam_type, output_frames in cam_frames.items():
                    for output_type, frame in output_frames.items():
                        if frame is not None:
                            cv2.imshow(f"Camera {cam_id} - {cam_type} - {output_type}", frame)
                    frame_count += 1

            # Press 'q' to quit early
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        logger.info("Captured %d frames total", frame_count)

    finally:
        cv2.destroyAllWindows()
        manager.close_all()
        logger.info("Camera manager closed")


def example_with_config_dict() -> None:
    """Example using a configuration dictionary."""
    logger.info("=== Example 2: Using Configuration Dictionary ===")

    # Define configuration as a dictionary
    config = {
        "front_camera": {
            "id": 0,  # Use first available camera by index
            "camera_type": "color",
            "outputs": ["preview", "video"],
            "sizes": [[300, 300], [1280, 720]],
            "fpss": [10, 10],
            # "outputs": ["video"],
            # "sizes": [[1280, 720]],
            # "fpss": [10],
            "settings": {"iso": 100, "exposure": 20000, "focus": 125},
        }
    }

    # Create camera manager with config dictionary
    with CameraManager(config_dict=config) as manager:
        # Initialize cameras
        manager.initialize_cameras(use_superspeed=True)

        # Start monitoring
        manager.start_monitoring()

        # Get specific camera
        if "front_camera" in manager:
            camera = manager.get_camera("front_camera")
            if camera is not None and camera.device is not None:
                logger.info("Front camera MXID: %s", camera.device.getMxId())

        # Capture frames
        try:
            for i in range(100):
                frame = manager.get_frame("front_camera", cam_type="color", output_type="video")
                if frame is not None:
                    cv2.imshow("Front Camera video", frame)
                else:
                    logger.warning("No video frame received from front camera")

                frame = manager.get_frame("front_camera", cam_type="color", output_type="preview")
                if frame is not None:
                    cv2.imshow("Front Camera preview", frame)
                else:
                    logger.warning("No preview frame received from front camera")

                if cv2.waitKey(30) & 0xFF == ord("q"):
                    break

        finally:
            cv2.destroyAllWindows()


def example_reconnecting() -> None:
    """Example using a configuration file with automatic cleanup via ExitStack."""
    logger.info("=== Example 1: Using Configuration File ===")

    # Path to configuration file
    config_path = Path(__file__).parent.parent / "configs" / "defaults" / "camera_manager_config.json"

    # Create camera manager with config file
    # Using context manager ensures proper cleanup via ExitStack
    manager = CameraManager(
        config_path=config_path,
        health_check_interval=5.0,  # Check health every 5 seconds
        data_timeout=10.0,  # Warn if no data for 10 seconds
    )

    # Initialize all cameras
    manager.initialize_cameras(use_superspeed=True)

    # Start health monitoring
    manager.start_monitoring()

    logger.info("Active cameras: %d", len(manager))

    # Capture frames for 30 seconds
    disconnected = False
    try:
        start_time = time.time()
        frame_count = 0

        while time.time() - start_time < 200:
            # Get frames from all cameras
            frames = manager.get_frames_all()

            # Display frames
            for cam_id, cam_frames in frames.items():
                for cam_type, output_frames in cam_frames.items():
                    for output_type, frame in output_frames.items():
                        if frame is not None:
                            cv2.imshow(f"Camera {cam_id} - {cam_type} - {output_type}", frame)
                    frame_count += 1

            if not disconnected and frame_count > 30:
                logger.info("Will disconnect the first camera now")
                disconnected = True
                print(manager.cameras.keys())
                camera = manager.get_camera("camera_1")
                if camera is not None:
                    camera.close(force=True)

            # Press 'q' to quit early
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        logger.info("Captured %d frames total", frame_count)

    finally:
        cv2.destroyAllWindows()
        manager.close_all()
        logger.info("Camera manager closed")


def example_still_capture() -> None:
    """Example capturing still images from multiple cameras."""
    logger.info("=== Example 4: Still Image Capture ===")

    config = {
        "cam_1": {
            "id": 0,
            "camera_type": "color",
            "outputs": ["preview", "still"],
            "settings": {"iso": 100, "exposure": 20000},
        }
    }

    manager = CameraManager(config_dict=config)
    manager.initialize_cameras()

    try:
        camera = manager.get_camera("cam_1")

        if camera:
            logger.info("Press SPACE to capture, 'q' to quit")

            still_count = 0
            while True:
                # Show preview
                frame = manager.get_frame("cam_1", output_type="preview")
                if frame is not None:
                    cv2.imshow("Preview", frame)

                key = cv2.waitKey(1) & 0xFF

                # Capture still on SPACE
                if key == ord(" "):
                    logger.info("Capturing still image...")
                    still = camera.trigger_and_get_still()

                    if still is not None:
                        filename = f"still_{still_count:03d}.jpg"
                        cv2.imwrite(filename, still)
                        logger.info("Saved %s", filename)
                        still_count += 1

                        # Show thumbnail
                        thumb = cv2.resize(still, (400, 300))
                        cv2.imshow("Last Still", thumb)

                elif key == ord("q"):
                    break

    finally:
        cv2.destroyAllWindows()
        manager.close_all()


if __name__ == "__main__":
    # Choose which example to run
    import sys

    if len(sys.argv) > 1:
        example_num = sys.argv[1]
    else:
        print("Usage: python camera_manager_example.py [1|2|3|4]")
        print("  1: Configuration file example")
        print("  2: Configuration dictionary example")
        print("  3: Single camera example")
        print("  4: Still capture example")
        print("\nRunning example 3 by default...")
        example_num = "3"

    if example_num == "1":
        example_with_config_file()
    elif example_num == "2":
        example_with_config_dict()
    elif example_num == "3":
        example_reconnecting()
    elif example_num == "4":
        example_still_capture()
    else:
        print(f"Unknown example: {example_num}")
        sys.exit(1)
