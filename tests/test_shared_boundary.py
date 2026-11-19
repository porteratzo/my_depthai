"""Packaging guard (module-packaging plan, Phase 0) for the my_depthai library.

It may use only the table-free ``shared.core`` utilities: no ``full_tep.db.models``, ``full_tep.db.engine``,
``full_tep.db.migrate`` and no project package. It must also declare its ``tep-shared`` dependency, so a
standalone install does not fail at import.

Self-contained on purpose: this file travels with the module when it is extracted to its own repo.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path
from typing import Iterator, List, Tuple

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_SCAN = ["mydepthai", "camera_broker.py", "examples"]
_ALLOWED_SHARED = "shared.core"
_FORBIDDEN_TOP = {"full_tep"}


def _files() -> Iterator[Path]:
    for entry in _SCAN:
        target = _ROOT / entry
        if target.is_file():
            yield target
        else:
            yield from sorted(target.rglob("*.py"))


def _imports(path: Path) -> Iterator[Tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "shared":  # ``from shared import X`` reaches the package root
                yield node.lineno, "shared." + ",".join(a.name for a in node.names)
            else:
                yield node.lineno, node.module


def _violations() -> List[str]:
    out: List[str] = []
    for path in _files():
        for lineno, module in _imports(path):
            top = module.split(".")[0]
            where = f"{path.relative_to(_ROOT)}:{lineno}"
            if top in _FORBIDDEN_TOP:
                out.append(f"{where} imports {module}")
            elif top == "shared" and not (
                module == _ALLOWED_SHARED or module.startswith(_ALLOWED_SHARED + ".")
            ):
                out.append(f"{where} imports {module} (libraries may use only shared.core.*)")
    return out


def test_scan_finds_source_files() -> None:
    assert len(list(_files())) > 3


def test_imports_only_table_free_shared_core() -> None:
    assert _violations() == []


def test_pyproject_declares_tep_shared_dependency() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    deps = [re.split(r"[<>=!\[ @;]", d, maxsplit=1)[0].strip() for d in project.get("dependencies", [])]
    assert "tep-shared" in deps


def test_pyproject_pins_python_floor_312() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["requires-python"] == ">=3.12"
