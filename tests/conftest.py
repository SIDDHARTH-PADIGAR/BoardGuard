from __future__ import annotations

import shutil
from pathlib import Path

import pytest



@pytest.fixture
def fixture_project(tmp_path: Path) -> Path:
    root = Path(__file__).resolve().parents[1] / "examples" / "demo_board"
    target = tmp_path / "project"
    shutil.copytree(root, target)
    return target
