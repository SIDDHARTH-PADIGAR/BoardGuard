from __future__ import annotations

import json
import statistics
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .engine import BoardGuardEngine
from .models import ChangeRequest
from .state import StateStore
from .tools import add_silkscreen_text, tree_hashes
from .validator import SimulatedValidator
from .models import ValidationReport, Violation


class ScenarioValidator(SimulatedValidator):
    """Deterministic fault injector; outputs are explicitly not real KiCad results."""

    def __init__(self, scenario: str):
        super().__init__("normal")
        self.scenario = scenario
        self.transient_fired = False

    def validate(self, project_dir: Path, board_file: Path, report_dir: Path) -> ValidationReport:
        self.calls += 1
        content = board_file.read_text(encoding="utf-8")
        label_is_present = '(gr_text "BG ' in content or "BAD_CLEARANCE" in content
        if self.scenario == "validator_unavailable":
            return ValidationReport(False, "simulator (fault injection; not KiCad)", None, error="injected missing validator")
        if self.scenario == "persistent_post_failure" and label_is_present:
            return ValidationReport(False, "simulator (fault injection; not KiCad)", None, error="injected persistent post-edit timeout")
        if self.scenario == "transient_validator_failure" and label_is_present and not self.transient_fired:
            self.transient_fired = True
            return ValidationReport(False, "simulator (fault injection; not KiCad)", None, error="injected one-time post-edit timeout")
        violations: list[Violation] = []
        if "BG_SIM_BASELINE_DRC" in content:
            violations.append(Violation("error", "sim_baseline_rule", "fixture baseline violation", "baseline:error:sim_baseline_rule"))
        if "BAD_CLEARANCE" in content:
            violations.append(Violation("error", "sim_clearance", "fault-injected clearance violation", "new:error:sim_clearance"))
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / f"{board_file.stem}-simulated-drc.json"
        report.write_text(json.dumps({"violations": [v.__dict__ for v in violations]}, indent=2), encoding="utf-8")
        return ValidationReport(True, "simulator (fault injection; not KiCad)", 0 if not violations else 1, violations, 0.1, raw_report_path=str(report))


def _fixture_root() -> Path:
    return Path(__file__).resolve().parents[2] / "examples" / "demo_board"


def _request_dict(request_id: str, project_dir: str, scenario: str) -> dict[str, Any]:
    text = "BAD_CLEARANCE" if scenario == "introduced_violation" else f"BG {scenario.replace('_', ' ').upper()[:24]}"
    x, y = (0, 0) if scenario == "out_of_bounds" else (30, 22)
    return {
        "schema_version": 1,
        "request_id": request_id,
        "project_dir": project_dir,
        "operation": {"type": "add_silkscreen_text", "file": "demo_board.kicad_pcb", "text": text, "x_mm": x, "y_mm": y, "height_mm": 1.0},
        "approval": {"approved": True},
        "max_retries": 1,
        "acceptance": {"text_present": True},
    }


def _naive_baseline(project: Path, request: ChangeRequest, scenario: str) -> dict[str, Any]:
    """Definition: unguarded one-pass executor; no approval, baseline diff, retry, rollback, or ledger."""
    board = project / request.operation.file
    before = board.read_text(encoding="utf-8")
    started = time.perf_counter()
    # This is deliberately the baseline under test, not BoardGuard's typed tool.
    # The fixed temp fixture is the only write target in benchmark runs.
    annotation = (
        f'  (gr_text "{request.operation.text}" (at {request.operation.x_mm:g} {request.operation.y_mm:g}) '
        f'(layer "F.SilkS") (uuid "{uuid.uuid4()}") '
        f'(effects (font (size {request.operation.height_mm:g} {request.operation.height_mm:g}) (thickness 0.15))))\n'
    )
    apply_started = time.perf_counter()
    board.write_text(before[:before.rfind(")")] + annotation + before[before.rfind(")"):], encoding="utf-8")
    apply_ms = (time.perf_counter() - apply_started) * 1000
    validator = ScenarioValidator(scenario)
    validation_started = time.perf_counter()
    report = validator.validate(project, board, project / ".baseline-reports")
    validation_ms = (time.perf_counter() - validation_started) * 1000
    elapsed_ms = (time.perf_counter() - started) * 1000
    after = board.read_text(encoding="utf-8")
    new_violation_seen = scenario == "introduced_violation" and "BAD_CLEARANCE" in after
    text_occurrences = after.count(f'(gr_text "{request.operation.text}"')
    completion = bool(report.available and report.exit_code == 0 and not report.violations
                      and text_occurrences == 1 and scenario not in {"missing_approval", "out_of_bounds"})
    return {
        "status": "COMPLETED" if completion else ("TOOL_UNAVAILABLE" if not report.available else "FAILED_VALIDATION"),
        "completed": completion,
        "persisted_new_violations": int(new_violation_seen),
        "rollback_succeeded": False,
        "human_escalation": False,
        "changed": before != after,
        "text_occurrences": text_occurrences,
        "tool_calls": 2,
        "elapsed_ms": round(elapsed_ms, 3),
        "recovery_ms": 0.0,
        "stage_latency_ms": {"apply_ms": round(apply_ms, 3), "validation_ms": round(validation_ms, 3), "recovery_ms": 0.0},
        "validation_available": report.available,
        "violations": [v.signature() for v in report.violations],
    }


def run_benchmark(repeats: int = 3) -> dict[str, Any]:
    if not 1 <= repeats <= 20:
        raise ValueError("repeats must be between 1 and 20")
    scenarios = [
        "clean_success",
        "preexisting_violation",
        "introduced_violation",
        "transient_validator_failure",
        "persistent_post_failure",
        "validator_unavailable",
        "missing_approval",
        "out_of_bounds",
        "duplicate_request",
    ]
    expected_escalation = {
        "clean_success": False,
        "preexisting_violation": False,
        "introduced_violation": True,
        "transient_validator_failure": False,
        "persistent_post_failure": True,
        "validator_unavailable": True,
        "missing_approval": True,
        "out_of_bounds": True,
        "duplicate_request": False,
    }
    boardguard_runs: list[dict[str, Any]] = []
    baseline_runs: list[dict[str, Any]] = []
    duplicate_prevented = 0
    rollback_scenarios = 0
    rollback_ok = 0
    escalation_correct = 0
    escalation_total = 0

    with tempfile.TemporaryDirectory(prefix="boardguard-benchmark-") as temp_dir:
        temp_root = Path(temp_dir)
        fixture = _fixture_root()
        for repeat in range(repeats):
            for scenario in scenarios:
                scenario_root = temp_root / f"r{repeat}-{scenario}"
                project = scenario_root / "project"
                project.parent.mkdir(parents=True, exist_ok=True)
                __import__("shutil").copytree(fixture, project)
                board = project / "demo_board.kicad_pcb"
                if scenario == "preexisting_violation":
                    content = board.read_text(encoding="utf-8")
                    content = content.replace('(title "BoardGuard Demo Board")', '(title "BoardGuard Demo Board")\n    (comment 2 "BG_SIM_BASELINE_DRC")')
                    board.write_text(content, encoding="utf-8")
                req_id = f"bench-{repeat}-{scenario}"
                raw = _request_dict(req_id, "project", scenario)
                request = ChangeRequest.from_dict(raw)
                validator = ScenarioValidator(scenario)
                store = StateStore(scenario_root / "state" / "state.sqlite3")
                engine = BoardGuardEngine(store, scenario_root / "workspaces", validator)
                approved = scenario != "missing_approval"
                start = time.perf_counter()
                try:
                    result = engine.run(request, scenario_root, cli_approved=approved)
                    if scenario == "duplicate_request":
                        previous_calls = validator.calls
                        # A repeated request ID + identical payload must return the existing result.
                        second = engine.run(request, scenario_root, cli_approved=True)
                        if validator.calls == previous_calls and second.status == result.status:
                            duplicate_prevented += 1
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    record = {
                        "scenario": scenario,
                        "repeat": repeat + 1,
                        "status": result.status,
                        "completed": result.status == "VERIFIED",
                        "persisted_new_violations": int(bool(result.new_violations) and result.recovery == "none"),
                        "rollback_succeeded": result.recovery == "restored_full_workspace_checkpoint",
                        "human_escalation": result.human_review_required,
                        "changed_files": result.changed_files,
                        "tool_calls": int(result.metrics.get("tool_calls", 0)),
                        "elapsed_ms": round(elapsed_ms, 3),
                        "recovery_ms": float(result.metrics.get("recovery_ms", 0.0)),
                        "stage_latency_ms": result.metrics.get("stage_latency_ms", {}),
                        "validation_available": bool(result.baseline and result.baseline.get("available")),
                        "baseline_violation_count": len((result.baseline or {}).get("violations", [])),
                        "new_violations": result.new_violations,
                        "recovery": result.recovery,
                    }
                    boardguard_runs.append(record)
                    if scenario in {"introduced_violation", "persistent_post_failure"}:
                        rollback_scenarios += 1
                        rollback_ok += int(record["rollback_succeeded"])
                    if result.human_review_required == expected_escalation[scenario]:
                        escalation_correct += 1
                    escalation_total += 1
                finally:
                    store.close()

                # Same task and same isolated starting board, evaluated by the naive one-pass baseline.
                baseline_root = scenario_root / "baseline-project"
                __import__("shutil").copytree(fixture, baseline_root)
                baseline_board = baseline_root / "demo_board.kicad_pcb"
                if scenario == "preexisting_violation":
                    content = baseline_board.read_text(encoding="utf-8")
                    baseline_board.write_text(content.replace('(title "BoardGuard Demo Board")', '(title "BoardGuard Demo Board")\n    (comment 2 "BG_SIM_BASELINE_DRC")'), encoding="utf-8")
                baseline_result = _naive_baseline(baseline_root, request, scenario)
                if scenario == "duplicate_request":
                    second_pass = _naive_baseline(baseline_root, request, scenario)
                    baseline_board_text = baseline_board.read_text(encoding="utf-8")
                    baseline_result["text_occurrences"] = baseline_board_text.count(f'(gr_text "{request.operation.text}"')
                    baseline_result["completed"] = bool(baseline_result["completed"] and baseline_result["text_occurrences"] == 1)
                    baseline_result["status"] = "FAILED_DUPLICATE_ACTION" if baseline_result["text_occurrences"] > 1 else baseline_result["status"]
                    baseline_result["tool_calls"] += second_pass["tool_calls"]
                    baseline_result["elapsed_ms"] = round(baseline_result["elapsed_ms"] + second_pass["elapsed_ms"], 3)
                    for key, value in second_pass.get("stage_latency_ms", {}).items():
                        baseline_result["stage_latency_ms"][key] = round(baseline_result["stage_latency_ms"].get(key, 0.0) + value, 3)
                baseline_result.update({"scenario": scenario, "repeat": repeat + 1})
                baseline_runs.append(baseline_result)

    total = len(boardguard_runs)
    baseline_total = len(baseline_runs)

    def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
        elapsed = [row["elapsed_ms"] for row in rows]
        tool_calls = [row["tool_calls"] for row in rows]
        stage_names = sorted({stage for row in rows for stage in row.get("stage_latency_ms", {})})
        recovery_durations = [row.get("recovery_ms", 0.0) for row in rows if row.get("recovery_ms", 0.0) > 0]
        return {
            "runs": len(rows),
            "completion_rate": round(sum(int(row["completed"]) for row in rows) / len(rows), 4),
            "newly_introduced_violations_left_in_workspace": sum(row["persisted_new_violations"] for row in rows),
            "rollback_successes": sum(int(row["rollback_succeeded"]) for row in rows),
            "human_escalation_accuracy": None,
            "mean_elapsed_ms": round(statistics.mean(elapsed), 3),
            "p50_elapsed_ms": round(statistics.median(elapsed), 3),
            "mean_tool_calls": round(statistics.mean(tool_calls), 3),
            "mean_recovery_ms_when_attempted": round(statistics.mean(recovery_durations), 3) if recovery_durations else 0.0,
            "stage_latency_ms_mean": {
                stage: round(statistics.mean(row.get("stage_latency_ms", {}).get(stage, 0.0) for row in rows), 3)
                for stage in stage_names
            },
        }

    boardguard_summary = summarize(boardguard_runs)
    baseline_summary = summarize(baseline_runs)
    baseline_summary["human_escalation_accuracy"] = round(sum(not expected_escalation[s] for s in scenarios) / len(scenarios), 4)
    boardguard_summary["human_escalation_accuracy"] = round(escalation_correct / escalation_total, 4) if escalation_total else 0.0
    boardguard_summary["duplicate_request_action_prevention"] = {
        "prevented": duplicate_prevented,
        "attempts": repeats,
        "rate": round(duplicate_prevented / repeats, 4),
    }
    boardguard_summary["rollback_success_rate_when_required"] = round(rollback_ok / rollback_scenarios, 4) if rollback_scenarios else None
    baseline_summary["duplicate_request_action_prevention"] = {"prevented": 0, "attempts": repeats, "rate": 0.0}
    baseline_summary["rollback_success_rate_when_required"] = 0.0

    return {
        "benchmark": "BoardGuard offline fault-injection benchmark",
        "schema_version": 1,
        "timestamp_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="seconds"),
        "repeats_per_scenario": repeats,
        "scenario_count": len(scenarios),
        "sample_size_per_approach": total,
        "tasks": scenarios,
        "methodology": {
            "paired": True,
            "baseline": "Unguarded one-pass executor: mutates a copy, validates once, and has no approval gate, baseline comparison, retries, rollback, or idempotency ledger.",
            "boardguard": "Current BoardGuard workflow using the deterministic simulator and typed text annotation tool; request/task fixtures are paired with the baseline starting state.",
            "timing": "Wall-clock time measured in this process, per task; includes Python/SQLite overhead. It excludes KiCad startup because this benchmark uses the offline simulator.",
            "validation": "Simulated fault injection only. This is not evidence of real KiCad correctness or real-world production reliability.",
            "success_definition": "VERIFIED with the requested text persisted, validator available, no new violations relative to baseline, and original project unchanged.",
            "known_metric_note": "Elapsed time is machine- and load-dependent. Tool-call counts are logical operations, not network or model calls.",
        },
        "results": {"boardguard": boardguard_summary, "baseline": baseline_summary},
        "runs": {"boardguard": boardguard_runs, "baseline": baseline_runs},
    }
