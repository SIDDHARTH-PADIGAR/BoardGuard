from __future__ import annotations

import json
from pathlib import Path

import pytest

from boardguard.engine import BoardGuardEngine
from boardguard.models import ChangeRequest, ValidationReport
from boardguard.state import StateStore, redact
from boardguard.tools import tree_hashes
from boardguard.validator import SimulatedValidator


def request_for(project: Path, text="BOARDGUARD TEST", request_id="engine-test-01", x=30, y=22):
    return ChangeRequest.from_dict({
        "schema_version": 1,
        "request_id": request_id,
        "project_dir": str(project),
        "operation": {"type": "add_silkscreen_text", "file": "demo_board.kicad_pcb", "text": text, "x_mm": x, "y_mm": y, "height_mm": 1.0},
        "approval": {"approved": True},
        "max_retries": 1,
        "acceptance": {"text_present": True},
    })


def engine_for(tmp_path: Path, validator):
    store = StateStore(tmp_path / "state" / "state.sqlite3")
    engine = BoardGuardEngine(store, tmp_path / "workspaces", validator)
    return engine, store


def test_success_compares_against_baseline_and_preserves_source(tmp_path: Path, fixture_project: Path):
    original = (fixture_project / "demo_board.kicad_pcb").read_bytes()
    engine, store = engine_for(tmp_path, SimulatedValidator())
    try:
        request = request_for(fixture_project)
        result = engine.run(request, tmp_path, cli_approved=True)
        assert result.status == "VERIFIED"
        assert result.project_preserved is True
        assert result.changed_files == ["demo_board.kicad_pcb"]
        assert (fixture_project / "demo_board.kicad_pcb").read_bytes() == original
        workspace_board = Path(result.workspace) / "demo_board.kicad_pcb"
        assert '(gr_text "BOARDGUARD TEST"' in workspace_board.read_text(encoding="utf-8")
        assert (Path(result.report_path)).is_file()
        run_dir = Path(result.report_path).parent
        assert (run_dir / "trace.jsonl").is_file()
        plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
        assert plan["goal"]["operation"] == "add_silkscreen_text"
        assert len(plan["steps"]) == 6
        assert plan["success_conditions"]
        trace = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
        assert any(e["event_type"] == "tool_call" and e["details"]["tool"] == "add_silkscreen_text" for e in trace)
    finally:
        store.close()


def test_preexisting_violation_does_not_block_non_regression(tmp_path: Path, fixture_project: Path):
    board = fixture_project / "demo_board.kicad_pcb"
    content = board.read_text(encoding="utf-8")
    board.write_text(content.replace('(title "BoardGuard Demo Board")', '(title "BoardGuard Demo Board")\n    (comment 2 "BG_SIM_BASELINE_DRC")'), encoding="utf-8")
    engine, store = engine_for(tmp_path, SimulatedValidator())
    try:
        result = engine.run(request_for(fixture_project, request_id="baseline-test-01"), tmp_path, cli_approved=True)
        assert result.status == "VERIFIED"
        assert result.baseline["counts"]["error"] == 1
        assert result.final_validation["counts"]["error"] == 1
        assert result.new_violations == []
    finally:
        store.close()


def test_new_violation_rolls_back_workspace_and_escalates(tmp_path: Path, fixture_project: Path):
    before_source = (fixture_project / "demo_board.kicad_pcb").read_bytes()
    engine, store = engine_for(tmp_path, SimulatedValidator())
    try:
        result = engine.run(request_for(fixture_project, text="BAD_CLEARANCE", request_id="rollback-test-01"), tmp_path, cli_approved=True)
        assert result.status == "ROLLED_BACK"
        assert result.recovery == "restored_full_workspace_checkpoint"
        assert result.human_review_required is True
        assert result.new_violations
        workspace_board = Path(result.workspace) / "demo_board.kicad_pcb"
        assert workspace_board.read_bytes() == before_source
        assert (fixture_project / "demo_board.kicad_pcb").read_bytes() == before_source
    finally:
        store.close()


def test_missing_validator_stops_before_edit(tmp_path: Path, fixture_project: Path):
    engine, store = engine_for(tmp_path, SimulatedValidator("unavailable"))
    try:
        result = engine.run(request_for(fixture_project, request_id="missing-tool-01"), tmp_path, cli_approved=True)
        assert result.status == "VALIDATION_UNAVAILABLE"
        assert result.human_review_required
        workspace_board = Path(result.workspace) / "demo_board.kicad_pcb"
        assert '(gr_text "BOARDGUARD TEST"' not in workspace_board.read_text(encoding="utf-8")
    finally:
        store.close()


def test_transient_validator_failure_is_retried_with_a_bound(tmp_path: Path, fixture_project: Path):
    engine, store = engine_for(tmp_path, SimulatedValidator("transient"))
    try:
        result = engine.run(request_for(fixture_project, request_id="retry-test-01"), tmp_path, cli_approved=True)
        assert result.status == "VERIFIED"
        assert result.metrics["validation_retries"] == 1
    finally:
        store.close()


def test_unapproved_run_can_resume_after_explicit_approval(tmp_path: Path, fixture_project: Path):
    engine, store = engine_for(tmp_path, SimulatedValidator())
    request = request_for(fixture_project, request_id="approval-test-01")
    try:
        denied = engine.run(request, tmp_path, cli_approved=False)
        assert denied.status == "NEEDS_APPROVAL"
        granted = engine.run(request, tmp_path, cli_approved=True)
        assert granted.status == "VERIFIED"
    finally:
        store.close()


def test_idempotency_reuses_terminal_result_without_more_tool_calls(tmp_path: Path, fixture_project: Path):
    validator = SimulatedValidator()
    engine, store = engine_for(tmp_path, validator)
    request = request_for(fixture_project, request_id="idempotency-test-01")
    try:
        first = engine.run(request, tmp_path, cli_approved=True)
        calls_after_first = validator.calls
        second = engine.run(request, tmp_path, cli_approved=True)
        assert first.status == second.status == "VERIFIED"
        assert validator.calls == calls_after_first
        board = Path(first.workspace) / "demo_board.kicad_pcb"
        assert board.read_text(encoding="utf-8").count('(gr_text "BOARDGUARD TEST"') == 1
    finally:
        store.close()


def test_request_id_conflict_is_rejected(tmp_path: Path, fixture_project: Path):
    engine, store = engine_for(tmp_path, SimulatedValidator())
    try:
        first = engine.run(request_for(fixture_project, request_id="conflict-test-01"), tmp_path, cli_approved=True)
        other = engine.run(request_for(fixture_project, text="OTHER LABEL", request_id="conflict-test-01"), tmp_path, cli_approved=True)
        assert first.status == "VERIFIED"
        assert other.status == "REJECTED"
        assert "different payload" in other.reason
    finally:
        store.close()


def test_secret_redaction_is_recursive():
    cleaned = redact({"api_key": "abc123", "nested": {"Authorization": "Bearer secret-token", "normal": "visible"}})
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["nested"]["Authorization"] == "[REDACTED]"
    assert cleaned["nested"]["normal"] == "visible"


class CrashAfterEditValidator(SimulatedValidator):
    def validate(self, project_dir: Path, board_file: Path, report_dir: Path) -> ValidationReport:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("simulated process interruption after edit checkpoint")
        self.calls -= 1
        return super().validate(project_dir, board_file, report_dir)


def test_interrupted_run_resumes_without_reapplying_edit(tmp_path: Path, fixture_project: Path):
    request = request_for(fixture_project, request_id="resume-test-01")
    crashing, store = engine_for(tmp_path, CrashAfterEditValidator())
    try:
        with pytest.raises(RuntimeError, match="simulated process interruption"):
            crashing.run(request, tmp_path, cli_approved=True)
        row = store.get_run(request.request_id)
        assert row["status"] == "RUNNING"
        assert row["checkpoint"] == "VALIDATING"
        board = Path(row["workspace"]) / "demo_board.kicad_pcb"
        assert board.read_text(encoding="utf-8").count('(gr_text "BOARDGUARD TEST"') == 1
        crashing.validator = SimulatedValidator()
        result = crashing.run(request, tmp_path, cli_approved=True, resume=True)
        assert result.status == "VERIFIED"
        assert board.read_text(encoding="utf-8").count('(gr_text "BOARDGUARD TEST"') == 1
    finally:
        store.close()


class ExternalSourceMutatingValidator(SimulatedValidator):
    def __init__(self, source_board: Path):
        super().__init__()
        self.source_board = source_board

    def validate(self, project_dir: Path, board_file: Path, report_dir: Path) -> ValidationReport:
        report = super().validate(project_dir, board_file, report_dir)
        if self.calls == 2:
            original = self.source_board.read_text(encoding="utf-8")
            self.source_board.write_text(original + "\n; external concurrent edit\n", encoding="utf-8")
        return report


def test_external_change_to_original_project_prevents_verified_status(tmp_path: Path, fixture_project: Path):
    source_board = fixture_project / "demo_board.kicad_pcb"
    engine, store = engine_for(tmp_path, ExternalSourceMutatingValidator(source_board))
    try:
        result = engine.run(request_for(fixture_project, request_id="external-edit-test-01"), tmp_path, cli_approved=True)
        assert result.status == "ROLLED_BACK"
        assert result.project_preserved is False
        assert result.human_review_required is True
        workspace_board = Path(result.workspace) / "demo_board.kicad_pcb"
        assert "BOARDGUARD TEST" not in workspace_board.read_text(encoding="utf-8")
        # BoardGuard does not erase an external actor's source edit while restoring its own workspace.
        assert "external concurrent edit" in source_board.read_text(encoding="utf-8")
    finally:
        store.close()


class WorkspaceFileDeletingValidator(SimulatedValidator):
    def __init__(self):
        super().__init__("normal")

    def validate(self, project_dir: Path, board_file: Path, report_dir: Path) -> ValidationReport:
        report = super().validate(project_dir, board_file, report_dir)
        if self.calls == 2:
            (project_dir / "demo_board.kicad_pro").unlink()
        return report


def test_deleted_workspace_file_is_detected_and_full_project_is_restored(tmp_path: Path, fixture_project: Path):
    engine, store = engine_for(tmp_path, WorkspaceFileDeletingValidator())
    try:
        result = engine.run(request_for(fixture_project, request_id="workspace-delete-01"), tmp_path, cli_approved=True)
        assert result.status == "ROLLED_BACK"
        assert "demo_board.kicad_pro" in result.changed_files
        workspace = Path(result.workspace)
        assert (workspace / "demo_board.kicad_pro").is_file()
        assert tree_hashes(workspace) == json.loads((Path(result.report_path).parent / "source-hashes.json").read_text())
        assert "BOARDGUARD TEST" not in (workspace / "demo_board.kicad_pcb").read_text(encoding="utf-8")
    finally:
        store.close()


def test_resume_detects_source_drift_against_persisted_hash_manifest(tmp_path: Path, fixture_project: Path):
    request = request_for(fixture_project, request_id="resume-source-drift-01")
    crashing, store = engine_for(tmp_path, CrashAfterEditValidator())
    try:
        with pytest.raises(RuntimeError, match="simulated process interruption"):
            crashing.run(request, tmp_path, cli_approved=True)
        source_pro = fixture_project / "demo_board.kicad_pro"
        source_pro.write_text(source_pro.read_text(encoding="utf-8") + "\n; external source change\n", encoding="utf-8")
        crashing.validator = SimulatedValidator()
        result = crashing.run(request, tmp_path, cli_approved=True, resume=True)
        assert result.status == "ROLLED_BACK"
        assert result.human_review_required
        assert result.project_preserved is False
        assert "changed since the run checkpoint" in result.reason
        workspace = Path(result.workspace)
        assert "BOARDGUARD TEST" not in (workspace / "demo_board.kicad_pcb").read_text(encoding="utf-8")
        assert (workspace / "demo_board.kicad_pro").read_bytes() != source_pro.read_bytes()
    finally:
        store.close()
