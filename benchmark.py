#!/usr/bin/env python3
"""
Throughput/latency benchmark for OAK cameras across resolutions.

Runs each resolution/output config as its own **subprocess**, each opening exactly one
fresh connection, measuring for a fixed duration, and exiting. A parent process launches
these children sequentially (with a cooldown between them) and assembles the results.

Why subprocesses instead of one long-lived connection: testing showed a *second* XLink
connection attempt within the same process reliably fails (even 10s+ after the first is
closed), and reusing one dai.Device across multiple freshly-built dai.Pipeline objects
segfaults inside depthai's C++ core. A fresh OS process reconnects cleanly after a short
cooldown, so process-per-config isolation is the straightforward, non-hacky way to get an
independent measurement per resolution rather than fighting either of those two failure
modes or measuring several resolutions simultaneously (which just saturates the link and
produces degenerate numbers for all of them at once).

Usage:
    python benchmark.py --ip 169.254.1.222
    python benchmark.py --mxid 14442C10D13EABCE00
    python benchmark.py --duration 8 --cooldown 12
    python benchmark.py --out camera_resolution_benchmark.md

Internal (used by the parent to run one measurement in a child process):
    python benchmark.py --child stream --output-type video --width 1920 --height 1080 \\
        --fps 30 --duration 8 --ip 169.254.1.222
    python benchmark.py --child still --bg-width 1920 --bg-height 1080 --bg-fps 20 \\
        --trigger-count 5 --ip 169.254.1.222
"""

import argparse
import json
import logging
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Literal, Optional, Tuple

import depthai as dai

from mydepthai.depthai_controllers.basic_dai_camera import BasicDAICamera

logger = logging.getLogger(__name__)

StreamOutputType = Literal["preview", "video", "fullvideo"]

# (label, output_type, size, requested_fps) — each run in its own subprocess.
STREAM_MATRIX: List[Tuple[str, StreamOutputType, Tuple[int, int], int]] = [
    ("Preview 300x300", "preview", (300, 300), 30),
    ("Preview 640x480", "preview", (640, 480), 30),
    ("Video 1280x720 (HD)", "video", (1280, 720), 30),
    ("Video 1920x1080 (Full HD)", "video", (1920, 1080), 30),
    ("Video 1920x1080 (Full HD, prod fps)", "video", (1920, 1080), 3),
    ("Full 3840x2160 (4K)", "fullvideo", (3840, 2160), 5),
]
STILL_BACKGROUND_SIZE = (1920, 1080)
STILL_BACKGROUND_FPS = 20
STILL_TRIGGER_COUNT = 5
STILL_TRIGGER_INTERVAL = 1.5
DEFAULT_COOLDOWN = 12.0


def clock_now() -> timedelta:
    now: timedelta = dai.Clock.now()  # type: ignore[call-arg]
    return now


def get_connection_info(cam: BasicDAICamera) -> dict:
    assert cam.device is not None
    device = cam.device
    info: dict = {
        "device_id": device.getDeviceId(),
        "connected_cameras": [c.name for c in device.getConnectedCameras()],
        "usb_speed": None,
        "protocol": cam.device_info.protocol.name,
        "name": cam.device_info.name,
    }
    try:
        info["usb_speed"] = device.getUsbSpeed().name
    except Exception as e:  # PoE devices don't expose a USB speed
        logger.debug("No USB speed available (likely PoE device): %s", e)
    return info


# --- Child process bodies: each opens exactly one connection, measures, prints one JSON line ---


def child_run_stream(
    identifier: "int | str",
    output_type: StreamOutputType,
    size: Tuple[int, int],
    requested_fps: int,
    duration: float,
    with_conn_info: bool,
) -> dict:
    cam: Optional[BasicDAICamera] = None
    try:
        cam = BasicDAICamera(identifier, speed=True)
        if not cam.wait_for_cam(timeout=60):
            return {"error": "Camera did not become ready within timeout"}

        conn_info = get_connection_info(cam) if with_conn_info else None

        cam.create_camera("color", outputs=[output_type], sizes=[size], fpss=[requested_fps])
        cam.build()
        cam.start_device()
        time.sleep(1.5)  # let auto-exposure/focus settle before measuring

        frame_count = 0
        total_bytes = 0
        latencies_ms: List[float] = []
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if not cam.components["color"].has_data(output_type):
                continue
            frame_data = cam.get_data("color", output_type)
            if frame_data is None:
                continue
            frame_count += 1
            latencies_ms.append((clock_now() - frame_data.getTimestamp()).total_seconds() * 1000)
            total_bytes += frame_data.getData().size

        result: dict = {"frame_count": frame_count, "total_bytes": total_bytes, "latencies_ms": latencies_ms}
        if conn_info is not None:
            result["conn_info"] = conn_info
        return result
    except Exception as e:
        logger.exception("Stream child failed")
        return {"error": str(e)}
    finally:
        if cam is not None:
            cam.close(force=True)


def child_run_still(
    identifier: "int | str",
    background_size: Tuple[int, int],
    background_fps: int,
    trigger_count: int,
    trigger_interval: float,
) -> dict:
    cam: Optional[BasicDAICamera] = None
    try:
        cam = BasicDAICamera(identifier, speed=True)
        if not cam.wait_for_cam(timeout=60):
            return {"error": "Camera did not become ready within timeout"}

        cam.create_camera(
            "color",
            outputs=["video", "still"],
            sizes=[background_size, None],
            fpss=[background_fps, None],
        )
        cam.build()
        cam.start_device()
        time.sleep(1.5)

        bg_count = 0
        bg_deadline = time.monotonic() + 2.0
        while time.monotonic() < bg_deadline:
            if cam.components["color"].has_data("video"):
                cam.get_data("color", "video")
                bg_count += 1
        background_stream_fps = bg_count / 2.0

        trigger_round_trip_ms: List[float] = []
        capture_latencies_ms: List[float] = []
        for _ in range(trigger_count):
            trigger_time = time.monotonic()
            cam.trigger_still()
            still_frame = None
            poll_deadline = trigger_time + 5.0
            while time.monotonic() < poll_deadline:
                if cam.components["color"].has_data("video"):
                    cam.get_data("color", "video")
                if cam.components["color"].has_data("still"):
                    still_frame = cam.get_data("color", "still")
                    break
            received_time = time.monotonic()
            if still_frame is None:
                logger.warning("Still capture timed out waiting for frame")
                continue
            trigger_round_trip_ms.append((received_time - trigger_time) * 1000)
            capture_latencies_ms.append((clock_now() - still_frame.getTimestamp()).total_seconds() * 1000)
            time.sleep(trigger_interval)

        return {
            "background_stream_fps": background_stream_fps,
            "trigger_round_trip_ms": trigger_round_trip_ms,
            "capture_latencies_ms": capture_latencies_ms,
        }
    except Exception as e:
        logger.exception("Still child failed")
        return {"error": str(e)}
    finally:
        if cam is not None:
            cam.close(force=True)


# --- Parent process: launches children, aggregates, reports ---


@dataclass
class StreamResult:
    label: str
    output_type: StreamOutputType
    size: Tuple[int, int]
    requested_fps: int
    duration: float
    error: Optional[str] = None
    frame_count: int = 0
    total_bytes: int = 0
    latencies_ms: List[float] = field(default_factory=list)

    @property
    def achieved_fps(self) -> float:
        return self.frame_count / self.duration if self.duration > 0 else 0.0

    @property
    def avg_frame_bytes(self) -> int:
        return self.total_bytes // self.frame_count if self.frame_count else 0

    @property
    def mbps(self) -> float:
        return (self.avg_frame_bytes * 8 * self.achieved_fps) / 1_000_000

    @property
    def avg_latency_ms(self) -> float:
        return statistics.mean(self.latencies_ms) if self.latencies_ms else 0.0

    @property
    def p95_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        data = sorted(self.latencies_ms)
        idx = min(int(len(data) * 0.95), len(data) - 1)
        return data[idx]

    @property
    def max_latency_ms(self) -> float:
        return max(self.latencies_ms) if self.latencies_ms else 0.0


@dataclass
class StillResult:
    error: Optional[str] = None
    background_stream_fps: float = 0.0
    trigger_round_trip_ms: List[float] = field(default_factory=list)
    capture_latencies_ms: List[float] = field(default_factory=list)

    @property
    def avg_round_trip_ms(self) -> float:
        return statistics.mean(self.trigger_round_trip_ms) if self.trigger_round_trip_ms else 0.0

    @property
    def max_round_trip_ms(self) -> float:
        return max(self.trigger_round_trip_ms) if self.trigger_round_trip_ms else 0.0

    @property
    def avg_capture_latency_ms(self) -> float:
        return statistics.mean(self.capture_latencies_ms) if self.capture_latencies_ms else 0.0


def run_child(args: List[str], timeout: float) -> dict:
    proc = subprocess.run(
        [sys.executable, __file__] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        return {"error": f"child process exited {proc.returncode}: {proc.stderr[-2000:]}"}
    # The child logs to stderr and prints exactly one JSON line to stdout.
    last_line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    try:
        return dict(json.loads(last_line))
    except (json.JSONDecodeError, IndexError):
        return {
            "error": f"could not parse child output: stdout={proc.stdout!r} stderr={proc.stderr[-2000:]!r}"
        }


def run_all(
    identifier: str, duration: float, cooldown: float
) -> Tuple[dict, List[StreamResult], StillResult]:
    conn_info: dict = {}
    stream_results: List[StreamResult] = []

    for i, (label, output_type, size, fps) in enumerate(STREAM_MATRIX):
        print(f"\nRunning: {label} ({output_type}, {size}, {fps} fps)...")
        child_args = [
            "--child",
            "stream",
            "--ip",
            identifier,
            "--output-type",
            output_type,
            "--width",
            str(size[0]),
            "--height",
            str(size[1]),
            "--fps",
            str(fps),
            "--duration",
            str(duration),
        ]
        if i == 0:
            child_args.append("--with-conn-info")
        data = run_child(child_args, timeout=duration + 90)
        result = StreamResult(
            label=label, output_type=output_type, size=size, requested_fps=fps, duration=duration
        )
        if "error" in data:
            result.error = data["error"]
            print(f"  FAILED: {result.error}")
        else:
            result.frame_count = data["frame_count"]
            result.total_bytes = data["total_bytes"]
            result.latencies_ms = data["latencies_ms"]
            if "conn_info" in data:
                conn_info = data["conn_info"]
            print(f"  achieved {result.achieved_fps:.2f} fps, avg latency {result.avg_latency_ms:.1f} ms")
        stream_results.append(result)
        if i < len(STREAM_MATRIX) - 1:
            print(f"  cooling down {cooldown}s before next connection...")
            time.sleep(cooldown)

    print(f"\nRunning: still capture latency with {STILL_BACKGROUND_SIZE} background stream...")
    time.sleep(cooldown)
    still_data = run_child(
        [
            "--child",
            "still",
            "--ip",
            identifier,
            "--bg-width",
            str(STILL_BACKGROUND_SIZE[0]),
            "--bg-height",
            str(STILL_BACKGROUND_SIZE[1]),
            "--bg-fps",
            str(STILL_BACKGROUND_FPS),
            "--trigger-count",
            str(STILL_TRIGGER_COUNT),
            "--trigger-interval",
            str(STILL_TRIGGER_INTERVAL),
        ],
        timeout=STILL_TRIGGER_COUNT * (STILL_TRIGGER_INTERVAL + 5) + 90,
    )
    still_result = StillResult()
    if "error" in still_data:
        still_result.error = still_data["error"]
        print(f"  FAILED: {still_result.error}")
    else:
        still_result.background_stream_fps = still_data["background_stream_fps"]
        still_result.trigger_round_trip_ms = still_data["trigger_round_trip_ms"]
        still_result.capture_latencies_ms = still_data["capture_latencies_ms"]
        print(f"  avg round trip {still_result.avg_round_trip_ms:.1f} ms")

    return conn_info, stream_results, still_result


def print_report(conn_info: dict, stream_results: List[StreamResult], still_result: StillResult) -> None:
    print("\n" + "=" * 78)
    print("Connection info")
    print("=" * 78)
    for k, v in conn_info.items():
        print(f"  {k}: {v}")

    print("\n" + "=" * 78)
    print("Results (each row measured with its own isolated connection)")
    print("=" * 78)
    header = (
        f"{'Config':<38}{'Req FPS':>8}{'Achv FPS':>10}"
        f"{'AvgLat(ms)':>12}{'P95Lat(ms)':>12}{'Frame(KB)':>11}{'Mbps':>8}"
    )
    print(header)
    print("-" * len(header))
    for r in stream_results:
        if r.error:
            print(f"{r.label:<38}FAILED: {r.error}")
            continue
        print(
            f"{r.label:<38}{r.requested_fps:>8}{r.achieved_fps:>10.2f}"
            f"{r.avg_latency_ms:>12.1f}{r.p95_latency_ms:>12.1f}"
            f"{r.avg_frame_bytes / 1024:>11.1f}{r.mbps:>8.2f}"
        )

    print("\n" + "=" * 78)
    print(f"Still capture latency (background video {STILL_BACKGROUND_SIZE} @ {STILL_BACKGROUND_FPS} fps)")
    print("=" * 78)
    if still_result.error:
        print(f"  FAILED: {still_result.error}")
    else:
        print(f"  background stream achieved fps: {still_result.background_stream_fps:.2f}")
        print(f"  still triggers: {len(still_result.trigger_round_trip_ms)}/{STILL_TRIGGER_COUNT}")
        print(f"  avg trigger-to-receipt round trip: {still_result.avg_round_trip_ms:.1f} ms")
        print(f"  max trigger-to-receipt round trip: {still_result.max_round_trip_ms:.1f} ms")
        print(f"  avg pipeline capture latency: {still_result.avg_capture_latency_ms:.1f} ms")


def write_markdown_report(
    path: str, conn_info: dict, stream_results: List[StreamResult], still_result: StillResult
) -> None:
    lines = []
    lines.append("# OAK Camera Resolution Benchmark")
    lines.append("")
    lines.append("Generated by `my_depthai/benchmark.py`. Each resolution below was measured with its own")
    lines.append(
        "isolated connection (a separate subprocess per row) — see the module docstring in "
        "`benchmark.py` for why: a second connection attempt within the same process to this PoE "
        "camera reliably failed, and reusing one `dai.Device` across multiple `dai.Pipeline` objects "
        "segfaulted inside depthai's C++ core. Running every resolution concurrently in one pipeline "
        "was also tried and simply saturated the PoE link (combined requested throughput exceeded "
        "1 Gbps), producing degenerate near-zero fps for all streams at once — so it is not used here."
    )
    lines.append("")
    lines.append("## Connection info")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    for k, v in conn_info.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append(
        "| Config | Output type | Size | Requested FPS | Achieved FPS | "
        "Avg latency (ms) | P95 latency (ms) | Max latency (ms) | Avg frame size (KB) | "
        "Effective throughput (Mbps) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in stream_results:
        if r.error:
            lines.append(
                f"| {r.label} | {r.output_type} | {r.size[0]}x{r.size[1]} | {r.requested_fps} | "
                f"FAILED: {r.error} | | | | | |"
            )
            continue
        lines.append(
            f"| {r.label} | {r.output_type} | {r.size[0]}x{r.size[1]} | {r.requested_fps} | "
            f"{r.achieved_fps:.2f} | {r.avg_latency_ms:.1f} | {r.p95_latency_ms:.1f} | "
            f"{r.max_latency_ms:.1f} | {r.avg_frame_bytes / 1024:.1f} | {r.mbps:.2f} |"
        )
    lines.append("")
    lines.append("## Still capture latency under a concurrent lower-res stream")
    lines.append("")
    lines.append(
        f"Background video stream: {STILL_BACKGROUND_SIZE[0]}x{STILL_BACKGROUND_SIZE[1]} @ "
        f"{STILL_BACKGROUND_FPS} fps requested, while triggering still captures on demand (simulates "
        "a live inspection stream plus an on-demand full-res capture)."
    )
    lines.append("")
    if still_result.error:
        lines.append(f"**FAILED**: {still_result.error}")
    else:
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        lines.append(f"| Background stream achieved FPS | {still_result.background_stream_fps:.2f} |")
        lines.append(
            f"| Still triggers completed | {len(still_result.trigger_round_trip_ms)}/{STILL_TRIGGER_COUNT} |"
        )
        lines.append(f"| Avg trigger-to-receipt round trip | {still_result.avg_round_trip_ms:.1f} ms |")
        lines.append(f"| Max trigger-to-receipt round trip | {still_result.max_round_trip_ms:.1f} ms |")
        lines.append(
            f"| Avg pipeline capture latency (Clock.now() - frame timestamp) | "
            f"{still_result.avg_capture_latency_ms:.1f} ms |"
        )
    lines.append("")

    with open(path, "w") as f:
        f.write("\n".join(lines))
    print(f"\nMarkdown report written to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark OAK camera FPS/latency across resolutions")
    parser.add_argument("--mxid", default=None, help="MXID of a specific USB device")
    parser.add_argument("--ip", default=None, help="IP address of a PoE camera")
    parser.add_argument(
        "--duration", type=float, default=8.0, help="Seconds to stream per config (default: 8)"
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        default=DEFAULT_COOLDOWN,
        help=f"Seconds to wait between connections (default: {DEFAULT_COOLDOWN})",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Path to write a markdown report (e.g. camera_resolution_benchmark.md)",
    )
    # Internal: run a single measurement in a child process, print JSON to stdout.
    parser.add_argument("--child", choices=["stream", "still"], default=None, help=argparse.SUPPRESS)
    parser.add_argument("--output-type", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--width", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--height", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--fps", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--with-conn-info", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--bg-width", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--bg-height", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--bg-fps", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--trigger-count", type=int, default=STILL_TRIGGER_COUNT, help=argparse.SUPPRESS)
    parser.add_argument(
        "--trigger-interval", type=float, default=STILL_TRIGGER_INTERVAL, help=argparse.SUPPRESS
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    identifier: "int | str" = args.ip or args.mxid or 0

    if args.child == "stream":
        result = child_run_stream(
            identifier,
            args.output_type,
            (args.width, args.height),
            args.fps,
            args.duration,
            args.with_conn_info,
        )
        print(json.dumps(result))
        return
    if args.child == "still":
        result = child_run_still(
            identifier,
            (args.bg_width, args.bg_height),
            args.bg_fps,
            args.trigger_count,
            args.trigger_interval,
        )
        print(json.dumps(result))
        return

    conn_info, stream_results, still_result = run_all(str(identifier), args.duration, args.cooldown)
    if not conn_info:
        print("Benchmark failed: could not establish connection info (first config failed).")
        sys.exit(1)

    print_report(conn_info, stream_results, still_result)

    if args.out:
        write_markdown_report(args.out, conn_info, stream_results, still_result)


if __name__ == "__main__":
    main()
