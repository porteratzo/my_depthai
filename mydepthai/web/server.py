"""
FastAPI web server for my_depthai camera management UI.

Provides camera configuration editing and hot-reload without restarting the process.
"""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from mydepthai.broker.auth import TOKEN_HEADER, host_header_allowed, origin_allowed, token_matches
from mydepthai.camera_manager.config_utils import write_config

if TYPE_CHECKING:
    from mydepthai.broker.camera_broker import CameraBroker

logger = logging.getLogger("my_depthai.web")

app = FastAPI(title="Camera Manager", docs_url=None, redoc_url=None)

_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

# Broker reference and the configured nav links — set by init_app() before uvicorn starts
_broker = None
_nav_links: list[dict[str, str]] = []
# shared token for the state-changing POST routes, and the host the UI is bound to. An empty
# token matches nothing, so a server that was never given one rejects every POST (fail closed).
_token: str = ""
_bind_host: str = "127.0.0.1"


class _AuthError(Exception):
    """Raised by the POST guard; rendered as the project's error envelope (no detail on why)."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message


def _envelope(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"success": False, "data": None, "error": message},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@app.exception_handler(_AuthError)
async def _auth_error_handler(request: Request, exc: _AuthError) -> JSONResponse:
    return _envelope(exc.status_code, exc.message)


@app.middleware("http")
async def _host_guard(request: Request, call_next: Any) -> Response:
    """DNS-rebinding guard: a loopback-bound UI only answers to loopback Host names."""
    if not host_header_allowed(request.headers.get("host"), _bind_host):
        return _envelope(403, "Forbidden")
    response: Response = await call_next(request)
    return response


async def require_token(request: Request) -> None:
    """Dependency for state-changing routes: cross-origin browsers and token-less callers get refused."""
    if not origin_allowed(request.headers.get("origin"), request.headers.get("host")):
        raise _AuthError(403, "Forbidden")
    if not token_matches(request.headers.get(TOKEN_HEADER), _token):
        # Same answer whether the header is missing, wrong, or no token is configured at all.
        raise _AuthError(401, "Unauthorized")


def init_app(
    broker: "CameraBroker",
    config_loader: Optional[Any] = None,
    token: str = "",
    bind_host: str = "127.0.0.1",
) -> None:
    """Attach the running CameraBroker instance and read the configured nav links.

    ``config_loader`` is the process's existing ``ConfigLoader`` (one per process); a new one is
    only created when none is passed. ``token`` is what POST routes require in ``X-Broker-Token``
    (and what the broker's own pages embed); ``bind_host`` drives the Host-header allowlist.
    """
    global _broker, _nav_links, _token, _bind_host
    _broker = broker
    _token = token
    _bind_host = bind_host

    # Optional nav links to other tools, from ``services.camera_broker.nav_links``
    # (a list of ``{label, url}``). Unset or unreadable config means no links, never a failure.
    try:
        if config_loader is not None:
            cfg = config_loader
        else:
            from shared.core.config_loader import ConfigLoader

            cfg = ConfigLoader()
            cfg.load()
        _nav_links = parse_nav_links(cfg.get("services.camera_broker.nav_links", []))
    except Exception as e:
        _nav_links = []
        logger.warning("Failed to read services.camera_broker.nav_links, nav links will be blank: %s", e)


def parse_nav_links(raw: Any) -> list[dict[str, str]]:
    """Validate configured nav links: each needs a non-empty ``label`` and an ``http(s)`` ``url``.

    Anything else (a ``javascript:`` URL, a missing key, a non-list value) is skipped with a warning,
    because the URL is rendered into an ``href`` on every page.
    """
    if not raw:
        return []
    links: list[dict[str, str]] = []
    try:
        entries = list(raw)
    except TypeError:
        logger.warning("services.camera_broker.nav_links must be a list, ignoring it")
        return []
    for entry in entries:
        try:
            label = str(entry["label"]).strip()
            url = str(entry["url"]).strip()
        except (KeyError, TypeError):
            logger.warning("Ignoring nav link without a label and url: %r", entry)
            continue
        if not label or not url.lower().startswith(("http://", "https://")):
            logger.warning("Ignoring nav link %r: needs a label and an http(s) url", label or entry)
            continue
        links.append({"label": label, "url": url})
    return links


def _nav_context() -> dict[str, Any]:
    """Template context vars injected into every page."""
    return {
        "nav_links": _nav_links,
        "broker_token": _token,
        "token_header": TOKEN_HEADER,
    }


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------


def _page(request: Request, name: str) -> HTMLResponse:
    """Render a page; it embeds the POST token, so it must never be cached."""
    resp = templates.TemplateResponse(request, name, _nav_context())
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/", response_class=HTMLResponse)
async def cameras_page(request: Request) -> HTMLResponse:
    return _page(request, "cameras.html")


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request) -> HTMLResponse:
    return _page(request, "status.html")


@app.get("/controls", response_class=HTMLResponse)
async def controls_page(request: Request) -> HTMLResponse:
    return _page(request, "controls.html")


# ---------------------------------------------------------------------------
# API — camera config
# ---------------------------------------------------------------------------


@app.get("/api/camera_config")
async def get_camera_config() -> JSONResponse:
    """Return current camera_manager_config.json contents."""
    if _broker is None:
        return JSONResponse({})
    config_path = _broker.config_path
    if config_path and Path(config_path).exists():
        with open(config_path) as f:
            return JSONResponse(json.load(f))
    # Fall back to in-memory config_dict
    if _broker.config_dict:
        return JSONResponse(_broker.config_dict)
    return JSONResponse({})


@app.post("/api/camera_config", dependencies=[Depends(require_token)])
async def save_camera_config(request: Request) -> Dict[str, str]:
    """Overwrite camera_manager_config.json with the posted config dict."""
    data: Dict[str, Any] = await request.json()
    if not data or not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    # Validate: every value must be a dict
    for key, val in data.items():
        if not isinstance(val, dict):
            raise HTTPException(status_code=400, detail=f"Config entry '{key}' must be an object")

    if _broker is None or not _broker.config_path:
        raise HTTPException(status_code=400, detail="No config file path configured")

    config_path = Path(_broker.config_path)
    try:
        write_config(config_path, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid camera config: {e}")
    logger.info("Camera config saved to %s", config_path)
    return {"message": "Config saved"}


# ---------------------------------------------------------------------------
# API — cameras control
# ---------------------------------------------------------------------------


@app.post("/api/cameras/reload", dependencies=[Depends(require_token)])
async def reload_cameras(request: Request) -> Any:
    """
    Save config (if body provided) then hot-reload all cameras.

    Stops the current CameraManager and reinitializes from the updated config file.
    """
    if _broker is None:
        raise HTTPException(status_code=503, detail="Broker not available")

    body = await request.body()
    if body:
        try:
            data = json.loads(body)
        except ValueError:
            raise HTTPException(status_code=400, detail="Body must be valid JSON")
        if data and isinstance(data, dict):
            # Validate + save atomically first; an invalid config never reaches disk
            if _broker.config_path:
                try:
                    write_config(_broker.config_path, data)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=f"Invalid camera config: {e}")
                logger.info("Camera config saved before reload")

    try:
        # Blocking (can take minutes on camera connect) — keep it off the event loop.
        result = await run_in_threadpool(_broker.reload_cameras)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid camera config: {e}")
    except Exception as e:
        logger.error("Camera reload failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/cameras/status")
async def cameras_status() -> JSONResponse:
    """Return per-camera health information."""
    if _broker is None:
        return JSONResponse([])
    return JSONResponse(_broker.get_status())


# ---------------------------------------------------------------------------
# API — camera controls (focus / exposure / ISO / ROI)
# ---------------------------------------------------------------------------


@app.post("/api/cameras/{cam_id}/revive", dependencies=[Depends(require_token)])
async def revive_camera(cam_id: str) -> JSONResponse:
    """Force an immediate reconnect of one camera, skipping its backoff wait."""
    if _broker is None or _broker.camera_manager is None:
        raise HTTPException(status_code=503, detail="Broker not available")
    manager = _broker.camera_manager
    if cam_id not in manager.config:
        raise HTTPException(status_code=404, detail=f"Unknown camera '{cam_id}'")
    # Blocking (a reconnect can take many seconds) — keep it off the event loop.
    revived = await run_in_threadpool(manager.revive_camera, cam_id)
    return JSONResponse({"success": bool(revived), "state": manager.get_camera_state(cam_id)})


@app.get("/api/cameras/{cam_id}/frame")
async def get_camera_frame(cam_id: str, output_type: Optional[str] = None) -> Response:
    """Return the last-published JPEG frame for a camera as raw image bytes."""
    if _broker is None:
        raise HTTPException(status_code=503, detail="Broker not available")
    jpeg_bytes = _broker.get_last_jpeg(cam_id, output_type)
    if jpeg_bytes is None:
        raise HTTPException(status_code=404, detail=f"No cached frame for camera '{cam_id}'")
    return Response(content=jpeg_bytes, media_type="image/jpeg")


@app.get("/api/cameras/{cam_id}/control_state")
async def get_camera_control_state(cam_id: str, cam_type: str = "color") -> JSONResponse:
    """Combine cached live frame metadata with the software-commanded control state."""
    if _broker is None or _broker.camera_manager is None:
        raise HTTPException(status_code=503, detail="Broker not available")
    camera = _broker.camera_manager.cameras.get(cam_id)
    if camera is None:
        raise HTTPException(status_code=404, detail=f"Camera '{cam_id}' not found")
    if cam_type not in camera.components:
        raise HTTPException(status_code=404, detail=f"Component '{cam_type}' not found on '{cam_id}'")

    metadata = _broker.camera_manager.get_cached_metadata(cam_id)
    control_state = camera.components[cam_type].get_control_state()

    return JSONResponse(
        {
            "cam_id": cam_id,
            "cam_type": cam_type,
            "live": {
                "lens_position": metadata.get("lens_position"),
                "exposure_time_us": metadata.get("exposure_time_us"),
                "iso": metadata.get("iso"),
                "frame_width": metadata.get("frame_width"),
                "frame_height": metadata.get("frame_height"),
            },
            "commanded": control_state,
        }
    )


@app.post("/api/cameras/{cam_id}/control", dependencies=[Depends(require_token)])
async def post_camera_control(cam_id: str, request: Request) -> JSONResponse:
    """Dispatch a control action (trigger, ROI, lock, etc.) to a camera component."""
    if _broker is None or _broker.camera_manager is None:
        raise HTTPException(status_code=503, detail="Broker not available")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    cam_type = body.get("cam_type", "color")
    action = body.get("action")
    params = body.get("params", {})
    if not action:
        raise HTTPException(status_code=400, detail="Missing 'action'")
    if not isinstance(params, dict):
        raise HTTPException(status_code=400, detail="'params' must be an object")

    try:
        result = _broker.camera_manager.send_camera_action(cam_id, cam_type, action, params)
        return JSONResponse(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Camera control action failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
