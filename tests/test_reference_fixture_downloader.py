from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_reference_project.py"
spec = importlib.util.spec_from_file_location("boardguard_fetch_reference", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_git_blob_hash_matches_known_blob() -> None:
    assert module.git_blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_reference_files_are_pinned_and_have_licenses() -> None:
    assert len(module.COMMIT) == 40
    assert any(target == "LICENSE-ANTMICRO.txt" for target, _ in module.FILES.values())
    assert all(len(blob_sha) == 40 for _, blob_sha in module.FILES.values())
    assert "antmicro/pdm-microphone-board" in module.ATTRIBUTION
