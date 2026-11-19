"""Access control for the broker's :5002 web UI.

Pure helpers with no web-framework import, so they are unit-testable and ``web/server.py`` stays thin.

* The UI binds to loopback by default; a non-loopback bind is an explicit config choice.
* State-changing routes need the shared token in the ``X-Broker-Token`` header. If none is
  configured, one is generated at startup, so the default is secure (it changes on every restart).
* The token value is never logged.
"""

import hmac
import ipaddress
import logging
import secrets
from typing import Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger("my_depthai.broker.auth")

TOKEN_HEADER = "X-Broker-Token"


def is_loopback_host(host: str) -> bool:
    """True for ``localhost`` and any loopback IP literal (``127.0.0.0/8``, ``::1``)."""
    host = (host or "").strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def resolve_web_token(configured: Optional[str]) -> Tuple[str, bool]:
    """Return ``(token, generated)``: the configured token, or a fresh random one if none is set."""
    token = str(configured).strip() if configured is not None else ""
    if token:
        return token, False
    return secrets.token_urlsafe(32), True


def log_bind_and_token(host: str, token_generated: bool) -> None:
    """Log the security posture at startup (never the token itself)."""
    if token_generated:
        logger.info(
            "No services.camera_broker.token / CAMERA_BROKER_TOKEN set: generated a random token for this "
            "run. The broker's own pages receive it automatically; other clients must configure one to POST."
        )
    if not is_loopback_host(host):
        logger.warning(
            "Camera broker web UI is bound to non-loopback host %r: anyone who can reach it and load a page "
            "gets the token and can rewrite camera config and restart cameras. Keep it behind a firewall.",
            host,
        )


def token_matches(supplied: Optional[str], expected: str) -> bool:
    """Constant-time token check; an empty expected token never matches (fail closed)."""
    if not expected or not isinstance(supplied, str) or not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def host_header_allowed(host_header: Optional[str], bind_host: str) -> bool:
    """DNS-rebinding guard: when bound to loopback, only loopback ``Host`` names are served.

    For a non-loopback bind the legitimate hostnames are unknown, so every Host is accepted.
    """
    if not is_loopback_host(bind_host):
        return True
    if not host_header:
        return False
    hostname = urlsplit("//" + host_header).hostname or ""
    return is_loopback_host(hostname)


def origin_allowed(origin: Optional[str], host_header: Optional[str]) -> bool:
    """CSRF guard: a request carrying ``Origin`` must come from the same host:port it targets."""
    if not origin:
        return True  # non-browser clients (curl, scripts) send no Origin
    return bool(host_header) and urlsplit(origin).netloc.lower() == (host_header or "").lower()
