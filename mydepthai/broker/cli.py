"""Command-line parser for ``camera_broker.py`` (split out of the script, MY_DEPTHAI Phase 4)."""

import argparse
from typing import Any

from mydepthai.broker.settings import BrokerSettings


def build_parser(settings: BrokerSettings) -> "argparse.ArgumentParser":
    """CLI parser. Every setting flag defaults to ``None`` so only explicitly passed flags override
    ``settings``; the help text shows the resolved default."""
    parser = argparse.ArgumentParser(
        description="Multi-camera OAK to Redis Stream Publisher with CameraManager"
    )
    parser.add_argument("--config-path", default=None, help="Path to camera configuration JSON file")

    def opt(flag: str, name: str, type_: Any = None, help_: str = "") -> None:
        parser.add_argument(
            flag,
            dest=name,
            type=type_,
            default=None,
            help=f"{help_} (default: {getattr(settings, name)!r}, from config)".strip(),
        )

    opt("--redis-host", "redis_host", None, "Redis host")
    opt("--redis-port", "redis_port", int, "Redis port")
    opt("--redis-db", "redis_db", int, "Redis database")
    opt("--redis-password", "redis_password", None, "Redis password")
    opt("--stream-name", "stream_name", None, "Redis stream name")
    opt("--maxlen", "maxlen", int, "Max stream length (redis.streams.camera_frames.maxlen)")
    opt("--fps", "fps", int, "Frame rate (services.camera_broker.fps / CAMERA_FPS)")
    opt("--jpeg-quality", "jpeg_quality", int, "JPEG quality 1-100 (services.camera_broker.jpeg_quality)")
    opt("--health-check-interval", "health_check_interval", float, "Health check interval in seconds")
    opt("--data-timeout", "data_timeout", float, "Data timeout in seconds")
    opt("--redis-max-retries", "redis_max_retries", int, "Caps the Redis reconnect backoff at 2**(n-1) s")
    opt("--redis-socket-connect-timeout", "redis_socket_connect_timeout", float, "Redis connect timeout (s)")
    opt("--redis-socket-timeout", "redis_socket_timeout", float, "Redis socket read/write timeout (s)")
    opt("--web-host", "web_host", None, "Web UI host (services.camera_broker.host)")
    opt("--web-port", "web_port", int, "Web UI port (services.camera_broker.port)")
    parser.add_argument(
        "--use-superspeed",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use USB SuperSpeed; pass --no-use-superspeed to disable (default: enabled)",
    )
    parser.add_argument(
        "--cam-type",
        default="color",
        help="Camera component type; only used when --config-path is not given (default: color)",
    )
    parser.add_argument(
        "--output-type",
        default="video",
        help="Output type to capture; only used when --config-path is not given (default: video)",
    )
    parser.add_argument(
        "--no-auto-reconnect", action="store_true", help="Disable automatic camera reconnection"
    )
    parser.add_argument(
        "--no-threaded-init", action="store_true", help="Disable threaded camera initialization"
    )
    parser.add_argument("--no-web", action="store_true", help="Disable the web management UI")
    return parser
