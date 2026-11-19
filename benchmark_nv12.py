#!/usr/bin/env python3
"""
NV12 vs standard (BGR888p) frame-format benchmark for OAK cameras.

Compares the same set of resolutions requested two ways: NV12 (4:2:0 chroma-subsampled,
1.5 bytes/pixel on the wire) and "standard" BGR888p (full-resolution color, 3 bytes/pixel).
``CameraModule.build()`` (see ``mydepthai/depthai_controllers/modules/camera_module.py``)
already requests NV12 for the ``"preview"`` output type and BGR888p for ``"video"``, and both
accept arbitrary size/fps via ``add_output()`` — so this benchmark reuses that existing
output-type machinery instead of adding a new pixel-format knob to the production camera code.

Like ``benchmark.py``, each resolution/format config runs in its own **subprocess**, each
opening exactly one fresh connection, measuring for a fixed duration, and exiting — see that
module's docstring for why (a second XLink connection within one process reliably fails, and
reusing one ``dai.Device`` across multiple ``dai.Pipeline`` objects segfaults). This script
imports ``clock_now``/``get_connection_info`` from ``benchmark.py`` rather than duplicating
them.

Two things this benchmark measures beyond plain throughput:

- **The cv2 reconversion penalty.** Turning a captured ``dai.ImgFrame`` into a numpy BGR array
  requires calling ``frame.getCvFrame()``. For NV12 that triggers a ``cv2.cvtColor`` YUV->BGR
  pass; for BGR888p it's a much cheaper planar->interleaved reshape. This cost is timed in a
  separate pass from the achieved-fps measurement so it can't skew the throughput numbers, and
  reported per-format so the two are directly comparable.
- **Image quality.** The camera/scene is assumed static for the duration of a run, so a frame
  saved from the NV12 config and a frame saved from the BGR888p config at the same resolution
  are directly comparable even though they were captured in different subprocesses at different
  times — nothing in the scene moved in between. One converted (BGR) frame per config is saved
  to ``--image-dir`` as a lossless PNG for that comparison. Note that both decode to the same
  *shape* array via ``getCvFrame()``, but NV12's chroma planes are subsampled 2x in each
  dimension on the wire, so its color detail is inherently coarser than a native BGR888p
  capture at the same pixel size — the saved images make that visible directly.

Usage:
    python benchmark_nv12.py --ip 169.254.1.222
    python benchmark_nv12.py --mxid 14442C10D13EABCE00
    python benchmark_nv12.py --duration 8 --cooldown 12
    python benchmark_nv12.py --out nv12_vs_standard_benchmark.md

Internal (used by the parent to run one measurement in a child process):
    python benchmark_nv12.py --child format --output-type preview --width 1920 --height 1080 \\
        --fps 30 --duration 8 --ip 169.254.1.222 --save-image /tmp/nv12_1080p.png
"""

import argparse
import json
import logging
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, cast

import cv2
import numpy as np

from benchmark import StreamOutputType, clock_now, get_connection_info
from mydepthai.depthai_controllers.basic_dai_camera import BasicDAICamera

logger = logging.getLogger(__name__)

# (format_label, output_type) — output_type selects which dai.ImgFrame.Type
# CameraModule.build() requests: "preview" -> NV12, "video" -> BGR888p.
FORMAT_VARIANTS: List[Tuple[str, StreamOutputType]] = [
    ("NV12", "preview"),
    ("Standard (BGR888p)", "video"),
]

# (label, size, requested_fps) — each tested in both formats above.
RESOLUTION_MATRIX: List[Tuple[str, Tuple[int, int], int]] = [
    ("640x480 (VGA)", (640, 480), 30),
    ("1280x720 (HD)", (1280, 720), 30),
    ("1920x1080 (Full HD)", (1920, 1080), 30),
    ("1920x1080 (Full HD, prod fps)", (1920, 1080), 3),
    # 5 fps is where benchmark.py found NV12 4K throughput saturates (see
    # the camera_resolution_benchmark.md report); BGR888p at 3840x2160 requests
    # ~2x the bytes/frame of NV12, so this row is expected to show the format gap
    # widen sharply rather than both sides tracking the requested fps.
    ("3840x2160 (4K)", (3840, 2160), 5),
]

CONVERSION_SAMPLE_COUNT = 30
DEFAULT_COOLDOWN = 12.0
IMAGE_DIR_DEFAULT = Path(__file__).parent / "benchmark_images" / "nv12_vs_standard"


def build_matrix() -> List[Tuple[str, str, StreamOutputType, Tuple[int, int], int]]:
    """Return (label, format_label, output_type, size, fps) rows: every resolution x format."""
    matrix = []
    for res_label, size, fps in RESOLUTION_MATRIX:
        for format_label, output_type in FORMAT_VARIANTS:
            matrix.append((f"{res_label} — {format_label}", format_label, output_type, size, fps))
    return matrix


def slugify(label: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in label).strip("_").lower()


# --- Child process body: opens exactly one connection, measures, prints one JSON line ---


def child_run_format(
    identifier: "int | str",
    output_type: StreamOutputType,
    size: Tuple[int, int],
    requested_fps: int,
    duration: float,
    conversion_sample_count: int,
    with_conn_info: bool,
    save_image_path: Optional[str],
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

        # Pass 1: raw wire throughput/latency. Only the ImgFrame's timestamp and byte
        # count are read here — no host-side pixel conversion — so the cv2 reconversion
        # cost measured in pass 2 below can't skew achieved fps or latency.
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

        # Pass 2: cv2 reconversion penalty, timed in isolation over a fixed sample count.
        # The last sampled frame is saved (if requested) for the image-quality comparison.
        conversion_times_ms: List[float] = []
        cv_shape: Optional[List[int]] = None
        image_saved = False
        sample_deadline = time.monotonic() + max(conversion_sample_count / max(requested_fps, 1) * 4, 10.0)
        while len(conversion_times_ms) < conversion_sample_count and time.monotonic() < sample_deadline:
            if not cam.components["color"].has_data(output_type):
                continue
            frame_data = cam.get_data("color", output_type)
            if frame_data is None:
                continue
            t0 = time.perf_counter()
            cv_frame = cast(np.ndarray, frame_data.getCvFrame())
            conversion_times_ms.append((time.perf_counter() - t0) * 1000)
            cv_shape = list(cv_frame.shape)
            if save_image_path and len(conversion_times_ms) == conversion_sample_count:
                cv2.imwrite(save_image_path, cv_frame)
                image_saved = True

        result: dict = {
            "frame_count": frame_count,
            "total_bytes": total_bytes,
            "latencies_ms": latencies_ms,
            "conversion_times_ms": conversion_times_ms,
            "cv_shape": cv_shape,
            "image_path": save_image_path if image_saved else None,
        }
        if conn_info is not None:
            result["conn_info"] = conn_info
        return result
    except Exception as e:
        logger.exception("Format child failed")
        return {"error": str(e)}
    finally:
        if cam is not None:
            cam.close(force=True)


# --- Parent process: launches children, aggregates, reports ---


@dataclass
class FormatResult:
    label: str
    format_label: str
    output_type: StreamOutputType
    size: Tuple[int, int]
    requested_fps: int
    duration: float
    error: Optional[str] = None
    frame_count: int = 0
    total_bytes: int = 0
    latencies_ms: List[float] = field(default_factory=list)
    conversion_times_ms: List[float] = field(default_factory=list)
    cv_shape: Optional[List[int]] = None
    image_path: Optional[str] = None

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
    def avg_conversion_ms(self) -> float:
        return statistics.mean(self.conversion_times_ms) if self.conversion_times_ms else 0.0

    @property
    def p95_conversion_ms(self) -> float:
        if not self.conversion_times_ms:
            return 0.0
        data = sorted(self.conversion_times_ms)
        idx = min(int(len(data) * 0.95), len(data) - 1)
        return data[idx]


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
    identifier: str,
    duration: float,
    cooldown: float,
    conversion_samples: int,
    image_dir: Optional[Path],
) -> Tuple[dict, List[FormatResult]]:
    conn_info: dict = {}
    results: List[FormatResult] = []
    matrix = build_matrix()

    if image_dir is not None:
        image_dir.mkdir(parents=True, exist_ok=True)

    for i, (label, format_label, output_type, size, fps) in enumerate(matrix):
        print(f"\nRunning: {label} ({output_type}, {size}, {fps} fps)...")
        save_image_path = str(image_dir / f"{slugify(label)}.png") if image_dir is not None else None
        child_args = [
            "--child",
            "format",
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
            "--conversion-samples",
            str(conversion_samples),
        ]
        if save_image_path:
            child_args += ["--save-image", save_image_path]
        if i == 0:
            child_args.append("--with-conn-info")
        data = run_child(child_args, timeout=duration + 90)
        result = FormatResult(
            label=label,
            format_label=format_label,
            output_type=output_type,
            size=size,
            requested_fps=fps,
            duration=duration,
        )
        if "error" in data:
            result.error = data["error"]
            print(f"  FAILED: {result.error}")
        else:
            result.frame_count = data["frame_count"]
            result.total_bytes = data["total_bytes"]
            result.latencies_ms = data["latencies_ms"]
            result.conversion_times_ms = data["conversion_times_ms"]
            result.cv_shape = data.get("cv_shape")
            result.image_path = data.get("image_path")
            if "conn_info" in data:
                conn_info = data["conn_info"]
            print(
                f"  achieved {result.achieved_fps:.2f} fps, avg latency {result.avg_latency_ms:.1f} ms, "
                f"avg cv2 conversion {result.avg_conversion_ms:.2f} ms"
            )
        results.append(result)
        if i < len(matrix) - 1:
            print(f"  cooling down {cooldown}s before next connection...")
            time.sleep(cooldown)

    return conn_info, results


def print_report(conn_info: dict, results: List[FormatResult]) -> None:
    print("\n" + "=" * 100)
    print("Connection info")
    print("=" * 100)
    for k, v in conn_info.items():
        print(f"  {k}: {v}")

    print("\n" + "=" * 100)
    print("Results (each row measured with its own isolated connection)")
    print("=" * 100)
    header = (
        f"{'Config':<42}{'Req FPS':>8}{'Achv FPS':>10}"
        f"{'AvgLat(ms)':>12}{'Frame(KB)':>11}{'Mbps':>8}{'AvgConv(ms)':>13}{'P95Conv(ms)':>13}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        if r.error:
            print(f"{r.label:<42}FAILED: {r.error}")
            continue
        print(
            f"{r.label:<42}{r.requested_fps:>8}{r.achieved_fps:>10.2f}"
            f"{r.avg_latency_ms:>12.1f}{r.avg_frame_bytes / 1024:>11.1f}{r.mbps:>8.2f}"
            f"{r.avg_conversion_ms:>13.2f}{r.p95_conversion_ms:>13.2f}"
        )
        if r.image_path:
            print(f"{'':<42}saved comparison frame -> {r.image_path}")


def write_markdown_report(path: str, conn_info: dict, results: List[FormatResult]) -> None:
    lines = []
    lines.append("# OAK Camera NV12 vs Standard (BGR888p) Format Benchmark")
    lines.append("")
    lines.append(
        "Generated by `my_depthai/benchmark_nv12.py`. Each row was measured with its own isolated "
        "connection (a separate subprocess), for the same reasons documented in `benchmark.py`. The "
        "scene was assumed static for the whole run, so saved comparison frames are directly "
        "comparable across formats even though they were captured at different times."
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
        "| Config | Output type | Size | Req FPS | Achieved FPS | Avg latency (ms) | "
        "Avg frame size (KB) | Throughput (Mbps) | Avg cv2 conversion (ms) | "
        "P95 cv2 conversion (ms) | Comparison frame |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        if r.error:
            lines.append(
                f"| {r.label} | {r.output_type} | {r.size[0]}x{r.size[1]} | {r.requested_fps} | "
                f"FAILED: {r.error} | | | | | | |"
            )
            continue
        image_cell = r.image_path if r.image_path else "—"
        lines.append(
            f"| {r.label} | {r.output_type} | {r.size[0]}x{r.size[1]} | {r.requested_fps} | "
            f"{r.achieved_fps:.2f} | {r.avg_latency_ms:.1f} | {r.avg_frame_bytes / 1024:.1f} | "
            f"{r.mbps:.2f} | {r.avg_conversion_ms:.2f} | {r.p95_conversion_ms:.2f} | {image_cell} |"
        )
    lines.append("")

    lines.append("## NV12 vs standard, paired by resolution")
    lines.append("")
    lines.append(
        "| Resolution | FPS ratio (NV12/standard) | Bandwidth ratio (NV12/standard) | "
        "cv2 conversion overhead (NV12 − standard, ms) |"
    )
    lines.append("|---|---|---|---|")
    for i in range(0, len(results) - 1, len(FORMAT_VARIANTS)):
        nv12, standard = results[i], results[i + 1]
        if nv12.error or standard.error:
            lines.append(f"| {RESOLUTION_MATRIX[i // len(FORMAT_VARIANTS)][0]} | FAILED | | |")
            continue
        fps_ratio = nv12.achieved_fps / standard.achieved_fps if standard.achieved_fps else float("nan")
        bw_ratio = nv12.mbps / standard.mbps if standard.mbps else float("nan")
        conv_overhead = nv12.avg_conversion_ms - standard.avg_conversion_ms
        res_label = RESOLUTION_MATRIX[i // len(FORMAT_VARIANTS)][0]
        lines.append(f"| {res_label} | {fps_ratio:.2f}x | {bw_ratio:.2f}x | {conv_overhead:+.2f} |")
    lines.append("")
    lines.append(
        "NV12 is a 4:2:0 chroma-subsampled format at 1.5 bytes/pixel on the wire, vs BGR888p's "
        "full-resolution 3 bytes/pixel — so the bandwidth ratio is expected to land near 0.5x, and "
        "any resulting fps headroom shows up as the FPS ratio above. The trade-off is host-side: any "
        "code that needs a numpy BGR array (this repo's inspection pipeline included) must call "
        "`ImgFrame.getCvFrame()`, which for NV12 runs a `cv2.cvtColor` YUV->BGR pass — the conversion "
        "overhead column quantifies that cost in isolation, separate from the achieved-fps numbers "
        "above. NV12's chroma planes are also coarser (subsampled 2x per dimension) than BGR888p's "
        "at the same pixel size — see the saved comparison frames for a direct visual check."
    )
    lines.append("")

    with open(path, "w") as f:
        f.write("\n".join(lines))
    print(f"\nMarkdown report written to {path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark NV12 vs standard (BGR888p) frame format across resolutions"
    )
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
        "--conversion-samples",
        type=int,
        default=CONVERSION_SAMPLE_COUNT,
        help=f"Frames sampled to time getCvFrame() in isolation (default: {CONVERSION_SAMPLE_COUNT})",
    )
    parser.add_argument(
        "--image-dir",
        default=str(IMAGE_DIR_DEFAULT),
        help=f"Directory to save one comparison frame per config (default: {IMAGE_DIR_DEFAULT})",
    )
    parser.add_argument("--no-images", action="store_true", help="Skip saving comparison frames")
    parser.add_argument(
        "--out",
        default=None,
        help="Path to write a markdown report (e.g. nv12_vs_standard_benchmark.md)",
    )
    # Internal: run a single measurement in a child process, print JSON to stdout.
    parser.add_argument("--child", choices=["format"], default=None, help=argparse.SUPPRESS)
    parser.add_argument("--output-type", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--width", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--height", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--fps", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--with-conn-info", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--save-image", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    identifier: "int | str" = args.ip or args.mxid or 0

    if args.child == "format":
        result = child_run_format(
            identifier,
            args.output_type,
            (args.width, args.height),
            args.fps,
            args.duration,
            args.conversion_samples,
            args.with_conn_info,
            args.save_image,
        )
        print(json.dumps(result))
        return

    image_dir = None if args.no_images else Path(args.image_dir)
    conn_info, results = run_all(
        str(identifier), args.duration, args.cooldown, args.conversion_samples, image_dir
    )
    if not conn_info:
        print("Benchmark failed: could not establish connection info (first config failed).")
        sys.exit(1)

    print_report(conn_info, results)

    if args.out:
        write_markdown_report(args.out, conn_info, results)


if __name__ == "__main__":
    main()
