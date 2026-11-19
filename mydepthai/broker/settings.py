"""One place that decides the camera broker's settings (MD Phase 3).

Before this, the same defaults were spread over argparse literals, the ``CameraBroker``
constructor, two YAML files and two shell scripts. Precedence is now explicit:

    CLI flag  >  env var / .env / YAML (via ``ConfigLoader``)  >  dataclass default

Lives in the package so a plain import works from any cwd.

Keys read: `redis.*` (host, port, db, password, socket timeouts), `redis.streams.camera_frames.*`
(name, maxlen) and `services.camera_broker.*` (fps, jpeg_quality, host, port, token).
"""

from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Dict, Optional

from shared.core.config_loader import ConfigLoader


@dataclass(frozen=True)
class BrokerSettings:
    """Resolved broker settings. Immutable; use :meth:`with_overrides` for CLI flags."""

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: Optional[str] = None
    stream_name: str = "camera_frames"
    maxlen: int = 100
    fps: int = 5
    jpeg_quality: int = 85
    health_check_interval: float = 5.0
    data_timeout: float = 10.0
    redis_max_retries: int = 5
    redis_socket_connect_timeout: float = 5.0
    redis_socket_timeout: float = 5.0
    web_host: str = "127.0.0.1"  # loopback by default; a LAN bind is an explicit config choice
    web_port: int = 5002
    web_token: Optional[str] = field(default=None, repr=False)  # unset -> generated at startup

    @classmethod
    def from_config(cls, loader: ConfigLoader) -> "BrokerSettings":
        """Build settings from the process's single ``ConfigLoader``."""
        redis_cfg = loader.get_redis_config()
        stream_cfg = loader.get_redis_stream_config("camera_frames")
        return cls(
            redis_host=redis_cfg["host"],
            redis_port=redis_cfg["port"],
            redis_db=redis_cfg["db"],
            redis_password=redis_cfg["password"],
            stream_name=stream_cfg["name"],
            maxlen=stream_cfg["maxlen"],
            fps=int(loader.get("services.camera_broker.fps", cls.fps)),
            jpeg_quality=int(loader.get("services.camera_broker.jpeg_quality", cls.jpeg_quality)),
            redis_socket_connect_timeout=float(redis_cfg["socket_connect_timeout"]),
            redis_socket_timeout=float(redis_cfg["socket_timeout"]),
            web_host=str(loader.get("services.camera_broker.host", cls.web_host)),
            web_port=int(loader.get("services.camera_broker.port", cls.web_port)),
            # str(): YAML types an all-digit env value as int
            web_token=str(loader.get("services.camera_broker.token", "") or "") or None,
        )

    def with_overrides(self, **overrides: Any) -> "BrokerSettings":
        """Return a copy with every non-``None`` override applied (unset CLI flags are ``None``)."""
        known = {f.name for f in fields(self)}
        unknown = set(overrides) - known
        if unknown:
            raise TypeError(f"Unknown broker setting(s): {sorted(unknown)}")
        return replace(self, **{k: v for k, v in overrides.items() if v is not None})

    def broker_kwargs(self) -> Dict[str, Any]:
        """Keyword arguments for ``CameraBroker(...)`` (everything except the web UI bind)."""
        kwargs = asdict(self)
        del kwargs["web_host"], kwargs["web_port"], kwargs["web_token"]
        return kwargs
