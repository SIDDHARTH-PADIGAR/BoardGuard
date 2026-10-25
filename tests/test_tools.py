from __future__ import annotations

from pathlib import Path

import pytest

from boardguard.models import ChangeRequest, RequestError
from boardguard.tools import add_silkscreen_text, board_bounds, tree_hashes


def make_request(text="NEW LABEL", x=30, y=22):
    return ChangeRequest.from_dict({
        "schema_version": 1,
        "request_id": "tool-test-01",
        "project_dir": "project",
        "operation": {"type": "add_silkscreen_text", "file": "demo_board.kicad_pcb", "text": text, "x_mm": x, "y_mm": y},
        "approval": {"approved": True},
    })


def test_bounds_are_read_from_edge_cuts(fixture_project: Path):
    content = (fixture_project / "demo_board.kicad_pcb").read_text(encoding="utf-8")
    assert board_bounds(content) == (10.0, 10.0, 50.0, 30.0)


def test_typed_edit_changes_only_the_requested_board(fixture_project: Path):
    board = fixture_project / "demo_board.kicad_pcb"
    before = tree_hashes(fixture_project)
    request = make_request()
    result = add_silkscreen_text(board, request.operation, request.request_id)
    after = tree_hashes(fixture_project)
    assert result["action"] == "added"
    assert (after.keys() == before.keys())
    changed = [name for name in before if before[name] != after[name]]
    assert changed == ["demo_board.kicad_pcb"]
    assert '(gr_text "NEW LABEL"' in board.read_text(encoding="utf-8")


def test_typed_edit_is_idempotent(fixture_project: Path):
    board = fixture_project / "demo_board.kicad_pcb"
    request = make_request()
    first = add_silkscreen_text(board, request.operation, request.request_id)
    original = board.read_bytes()
    second = add_silkscreen_text(board, request.operation, request.request_id)
    assert first["action"] == "added"
    assert second["action"] == "already_present"
    assert board.read_bytes() == original
    assert board.read_text(encoding="utf-8").count('(gr_text "NEW LABEL"') == 1


def test_typed_edit_rejects_out_of_bounds(fixture_project: Path):
    request = make_request(x=0, y=0)
    board = fixture_project / "demo_board.kicad_pcb"
    original = board.read_bytes()
    with pytest.raises(RequestError, match="board outline"):
        add_silkscreen_text(board, request.operation, request.request_id)
    assert board.read_bytes() == original


def test_board_bounds_ignore_non_edge_lines_before_outline():
    content = '''(kicad_pcb
      (gr_line (start -100 -100) (end -90 -90) (layer "F.SilkS") (width 0.15))
      (gr_line (start 10 10) (end 30 10) (layer "Edge.Cuts") (width 0.05))
      (gr_line (start 30 10) (end 30 20) (layer "Edge.Cuts") (width 0.05))
      (gr_line (start 30 20) (end 10 20) (layer "Edge.Cuts") (width 0.05))
      (gr_line (start 10 20) (end 10 10) (layer "Edge.Cuts") (width 0.05))
    )'''
    assert board_bounds(content) == (10.0, 10.0, 30.0, 20.0)
    
def test_board_bounds_support_edge_cuts_arcs():
    content = '''(kicad_pcb
      (gr_arc (start 234.6 145.7) (mid 239.98 141.4) (end 245.3 145.7)
        (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
      (gr_arc (start 245.3 148.1) (mid 239.98 152.4) (end 234.6 148.1)
        (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))
    )'''

    assert board_bounds(content) == (234.6, 141.4, 245.3, 152.4)


def test_existing_label_at_wrong_position_is_not_mistaken_for_success(fixture_project: Path):
    board = fixture_project / "demo_board.kicad_pcb"
    request = make_request()
    add_silkscreen_text(board, request.operation, request.request_id)
    original = board.read_text(encoding="utf-8")
    wrong_request = make_request(x=35, y=23)
    with pytest.raises(RequestError, match="different position"):
        add_silkscreen_text(board, wrong_request.operation, "tool-test-02")
    assert board.read_text(encoding="utf-8") == original
