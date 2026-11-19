"""What a ``pip install`` of this module actually contains (module-packaging plan, Phase 4 prerequisite).

The wheel used to omit the web UI templates and the default camera configs, ship a top-level ``web``
package, and leave the broker outside the package. These tests build the wheel in a temp copy and check
the installed layout, so a missing ``package-data`` entry fails here rather than in another project.

Self-contained on purpose: this file travels with the module when it is extracted to its own repo.
"""

from __future__ import annotations

import shutil
import tomllib
import zipfile
from pathlib import Path
from typing import List

import pytest

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def wheel_names(tmp_path_factory: pytest.TempPathFactory) -> List[str]:
    build_meta = pytest.importorskip("setuptools.build_meta")
    src = tmp_path_factory.mktemp("src") / "mydepthai_src"
    shutil.copytree(
        _ROOT,
        src,
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.egg-info", "build", "dist", ".git", "tests", ".pytest_cache", ".mypy_cache"
        ),
    )
    out = tmp_path_factory.mktemp("wheel")
    import os

    cwd = os.getcwd()
    os.chdir(src)  # setuptools writes build/ and *.egg-info into the cwd; keep that out of the checkout
    try:
        wheel = build_meta.build_wheel(str(out))
    finally:
        os.chdir(cwd)
    with zipfile.ZipFile(out / wheel) as z:
        return z.namelist()


def test_wheel_ships_web_templates_and_default_configs(wheel_names: List[str]) -> None:
    templates = {n for n in wheel_names if n.startswith("mydepthai/web/templates/") and n.endswith(".html")}
    assert {"base.html", "cameras.html", "controls.html", "status.html"} <= {Path(n).name for n in templates}
    configs = {Path(n).name for n in wheel_names if n.startswith("mydepthai/configs/defaults/")}
    assert {"camera_manager_config.json", "sampleconfig_camera.json"} <= configs


def test_wheel_has_a_single_top_level_package(wheel_names: List[str]) -> None:
    tops = {n.split("/")[0] for n in wheel_names if not n.split("/")[0].endswith(".dist-info")}
    assert tops == {"mydepthai"}, f"unexpected top-level entries (collide with other projects): {tops}"


def test_broker_is_in_the_wheel_and_exposed_as_a_console_script(wheel_names: List[str]) -> None:
    assert "mydepthai/broker/camera_broker.py" in wheel_names
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["name"] == "mydepthai"
    assert project["scripts"]["mydepthai-broker"] == "mydepthai.broker.camera_broker:main"


def test_default_config_path_points_at_a_shipped_file() -> None:
    from mydepthai.camera_manager.config_utils import default_config_path

    for name in ("camera_manager_config.json", "sampleconfig_camera.json"):
        assert default_config_path(name).is_file()
