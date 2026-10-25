from __future__ import annotations

import json
from pathlib import Path

import pytest

from boardguard.models import ChangeRequest, RequestError
from boardguard.policy import inspect_project, require_approved


def valid_request(**changes):
    raw = {
        "schema_version": 1,
        "request_id": "test-request-01",
        "project_dir": "project",
        "operation": {"type": "add_silkscreen_text", "file": "demo_board.kicad_pcb", "text": "CHECKPOINT", "x_mm": 30, "y_mm": 22},
        "approval": {"approved": True},
        "max_retries": 1,
        "acceptance": {"text_present": True},
    }
    raw.update(changes)
    return raw


def test_valid_request_is_typed_and_stable():
    request = ChangeRequest.from_dict(valid_request())
    assert request.operation.type == "add_silkscreen_text"
    assert request.digest() == request.digest()


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema_version=2),
    lambda d: d.update(request_id="x"),
    lambda d: d["operation"].update(type="run_shell"),
    lambda d: d["operation"].update(file="../outside.kicad_pcb"),
    lambda d: d["operation"].update(file="unsafe.txt"),
    lambda d: d["operation"].update(text='quote"inject'),
    lambda d: d["operation"].update(x_mm=float("nan")),
    lambda d: d.update(unexpected="field"),
])
def test_malformed_or_unsafe_request_rejected(mutate):
    raw = valid_request()
    mutate(raw)
    with pytest.raises(RequestError):
        ChangeRequest.from_dict(raw)


def test_approval_requires_json_and_cli(fixture_project: Path):
    request = ChangeRequest.from_dict(valid_request())
    with pytest.raises(PermissionError):
        require_approved(request, False)
    require_approved(request, True)
    unapproved = ChangeRequest.from_dict(valid_request(approval={"approved": False}))
    with pytest.raises(PermissionError):
        require_approved(unapproved, True)


def test_project_inspection_rejects_symlink(tmp_path: Path, fixture_project: Path):
    outside = tmp_path / "outside.kicad_pcb"
    outside.write_text("(kicad_pcb)", encoding="utf-8")
    link = fixture_project / "linked.kicad_pcb"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks not supported on this platform")
    request = ChangeRequest.from_dict(valid_request())
    with pytest.raises(RequestError, match="symlinks"):
        inspect_project(fixture_project, request)
