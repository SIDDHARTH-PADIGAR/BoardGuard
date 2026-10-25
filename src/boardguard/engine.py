from __future__ import annotations

import difflib
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import ChangeRequest, RequestError, RunResult
from .policy import inspect_project, require_approved
from .state import StateStore, redact
from .tools import add_silkscreen_text, annotation_matches, copy_project, tree_hashes
from .validator import Validator, new_violations

TERMINAL = {"VERIFIED", "ROLLED_BACK", "REJECTED", "VALIDATION_UNAVAILABLE", "FAILED"}


class BoardGuardEngine:
    def __init__(self, store: StateStore, workspace_root: Path, validator: Validator):
        self.store = store
        self.workspace_root = workspace_root.resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.validator = validator

    def run(self, request: ChangeRequest, request_base: Path, *, cli_approved: bool = False, resume: bool = False) -> RunResult:
        started = time.perf_counter()
        stage_latencies: dict[str, float] = {}
        tool_calls = 0
        request_hash = request.digest()
        existing = self.store.get_run(request.request_id)
        if existing:
            if existing["request_hash"] != request_hash:
                return RunResult(request.request_id, "REJECTED", existing["checkpoint"], True,
                                 reason="request_id reused with a different payload; create a new id")
            if existing["status"] == "VALIDATION_UNAVAILABLE" and resume and existing["checkpoint"] == "BASELINE":
                # Explicit resume may retry a baseline that was blocked by a missing/failed validator.
                self.store.set_checkpoint(request.request_id, "INSPECTED", "RUNNING")
                run_dir = Path(existing["workspace"]).parent if existing.get("workspace") else self.workspace_root / request.request_id
                workspace = Path(existing["workspace"]) if existing.get("workspace") else run_dir / "workspace"
            elif existing["status"] in TERMINAL:
                if existing.get("result_json"):
                    return RunResult(**json.loads(existing["result_json"]))
                return RunResult(request.request_id, existing["status"], existing["checkpoint"], True,
                                 reason="terminal run has no saved result")
            if existing["status"] == "NEEDS_APPROVAL":
                if not (request.approved and cli_approved):
                    return RunResult(request.request_id, "NEEDS_APPROVAL", existing["checkpoint"], True,
                                     reason="modification still requires request approval and explicit --approve")
                self.store.reset_approval(request.request_id, self.workspace_root / request.request_id / "workspace")
                run_dir = self.workspace_root / request.request_id
                workspace = run_dir / "workspace"
            elif existing["status"] == "VALIDATION_UNAVAILABLE" and resume and existing["checkpoint"] == "BASELINE":
                pass  # run_dir/workspace were restored by the pre-checkpoint retry branch above
            else:
                if not resume:
                    return RunResult(request.request_id, "IN_PROGRESS", existing["checkpoint"], True,
                                     reason="same request is already active; use `boardguard resume` to continue it",
                                     workspace=existing.get("workspace"))
                run_dir = Path(existing["workspace"]).parent if existing.get("workspace") else self.workspace_root / request.request_id
                workspace = Path(existing["workspace"]) if existing.get("workspace") else run_dir / "workspace"
        else:
            run_dir = self.workspace_root / request.request_id
            workspace = run_dir / "workspace"
            run_dir.mkdir(parents=True, exist_ok=True)
            self.store.create_run(request.request_id, request_hash, workspace)

        def event(stage: str, kind: str, details: Any) -> None:
            self.store.event(request.request_id, stage, kind, details)

        plan_path = run_dir / "plan.json"
        plan_started = time.perf_counter()
        if not plan_path.exists():
            plan = {
                "schema_version": 1,
                "request_id": request.request_id,
                "goal": {
                    "operation": request.operation.type,
                    "file": request.operation.file,
                    "text": request.operation.text,
                    "x_mm": request.operation.x_mm,
                    "y_mm": request.operation.y_mm,
                    "height_mm": request.operation.height_mm,
                },
                "strategy": "deterministic sequential execution; no model-generated commands",
                "steps": [
                    {"id": "approval", "depends_on": [], "checkpoint": "APPROVAL", "completion": "request approval and explicit CLI approval are both present"},
                    {"id": "inspect", "depends_on": ["approval"], "checkpoint": "INSPECTED", "completion": "project paths, file types, size limits, and target board are accepted"},
                    {"id": "copy_and_baseline", "depends_on": ["inspect"], "checkpoint": "BASELINED", "completion": "isolated copy exists and baseline engineering report is parseable"},
                    {"id": "typed_edit", "depends_on": ["copy_and_baseline"], "checkpoint": "EDITED", "completion": "only the requested F.SilkS annotation is persisted in the target board"},
                    {"id": "verify", "depends_on": ["typed_edit"], "checkpoint": "VALIDATING", "completion": "post-edit validation is available and no new violations are present"},
                    {"id": "reflect_and_report", "depends_on": ["verify"], "checkpoint": "VERIFIED", "completion": "label occurs exactly once, source hashes are unchanged, and audit report is persisted"},
                ],
                "retry_policy": {"max_validation_retries": request.max_retries, "retry_only": "validation calls", "never_retry": "mutating edit tool"},
                "stopping_conditions": [
                    "malformed request or policy/path violation",
                    "missing approval",
                    "baseline or final validation unavailable, timed out, or returned an unknown report schema",
                    "new violation signature after the edit",
                    "unexpected workspace file modification",
                    "source project changed during execution",
                    "checkpoint restore failed",
                ],
                "success_conditions": [
                    "both baseline and post-edit validators returned parseable reports",
                    "requested text is persisted exactly once",
                    "post-edit violation multiset has no new signatures relative to baseline",
                    "only the requested board file changed in the workspace",
                    "source project hash set remains unchanged",
                ],
            }
            plan_path.write_text(json.dumps(redact(plan), indent=2, sort_keys=True), encoding="utf-8")
            event("plan", "plan_persisted", {"path": str(plan_path), "step_count": len(plan["steps"])})
        stage_latencies["plan_ms"] = round((time.perf_counter() - plan_started) * 1000, 3)

        def finish(result: RunResult, status: str | None = None) -> RunResult:
            result.metrics.setdefault("elapsed_ms", round((time.perf_counter() - started) * 1000, 3))
            state_row = self.store.get_run(request.request_id)
            if state_row and state_row.get("created_at"):
                try:
                    created_at = datetime.fromisoformat(state_row["created_at"])
                    result.metrics.setdefault("wall_clock_elapsed_ms", round((datetime.now(timezone.utc) - created_at).total_seconds() * 1000, 3))
                except (TypeError, ValueError):
                    pass
            result.metrics.setdefault("tool_calls", tool_calls)
            report_path = run_dir / "report.json"
            result.report_path = str(report_path)
            result.metrics["stage_latency_ms"] = {**stage_latencies}
            report_started = time.perf_counter()
            report_path.write_text(json.dumps(redact(result.to_dict()), indent=2, sort_keys=True), encoding="utf-8")
            stage_latencies["report_ms"] = round((time.perf_counter() - report_started) * 1000, 3)
            result.metrics["stage_latency_ms"] = {**stage_latencies}
            report_path.write_text(json.dumps(redact(result.to_dict()), indent=2, sort_keys=True), encoding="utf-8")
            self.store.save_result(request.request_id, result.to_dict(), status or result.status, result.checkpoint)
            trace_path = run_dir / "trace.jsonl"
            self.store.trace_path(request.request_id, trace_path)
            return result

        policy_started = time.perf_counter()
        try:
            require_approved(request, cli_approved)
        except PermissionError as exc:
            stage_latencies["policy_ms"] = round((time.perf_counter() - policy_started) * 1000, 3)
            event("policy", "approval_denied", {"reason": str(exc)})
            result = RunResult(request.request_id, "NEEDS_APPROVAL", "APPROVAL", True,
                               reason=str(exc), human_review_required=True)
            return finish(result, "NEEDS_APPROVAL")

        stage_latencies["policy_ms"] = round((time.perf_counter() - policy_started) * 1000, 3)
        inspect_started = time.perf_counter()
        source = Path(request.project_dir)
        if not source.is_absolute():
            source = request_base / source
        try:
            source, source_files = inspect_project(source, request)
        except (RequestError, OSError) as exc:
            event("inspect", "project_rejected", {"error": str(exc)})
            return finish(RunResult(request.request_id, "REJECTED", "INSPECT", True,
                                    reason=str(exc), human_review_required=True))

        stage_latencies["inspect_ms"] = round((time.perf_counter() - inspect_started) * 1000, 3)
        current_source_hashes = tree_hashes(source)
        state_row = self.store.get_run(request.request_id)
        checkpoint = state_row["checkpoint"] if state_row else "CREATED"
        manifest_path = run_dir / "source-hashes.json"
        if checkpoint == "CREATED":
            source_hashes = current_source_hashes
            manifest_path.write_text(json.dumps(source_hashes, indent=2, sort_keys=True), encoding="utf-8")
        else:
            try:
                source_hashes = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not isinstance(source_hashes, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in source_hashes.items()):
                    raise ValueError("source hash manifest has an invalid schema")
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                event("inspect", "source_manifest_invalid", {"error": str(exc)})
                return finish(RunResult(request.request_id, "FAILED", checkpoint, False,
                                        reason="original source hash manifest is missing or malformed; refusing unsafe resume",
                                        human_review_required=True, workspace=str(workspace)))
            if current_source_hashes != source_hashes:
                restored, restore_error = self._restore_workspace(run_dir, workspace, source_hashes)
                event("recovery", "source_changed_before_resume", {
                    "restored": restored, "error": restore_error,
                    "current_source_hashes": current_source_hashes,
                })
                return finish(RunResult(
                    request.request_id, "ROLLED_BACK" if restored else "FAILED",
                    "ROLLED_BACK" if restored else "RECOVERY_FAILED", False,
                    reason="source project changed since the run checkpoint; workspace restored and human review required" if restored else "source changed and workspace restoration failed: " + str(restore_error),
                    recovery="restored_full_workspace_checkpoint" if restored else "rollback_failed",
                    human_review_required=True, workspace=str(workspace),
                    metrics={"recovery_error": restore_error} if restore_error else {},
                ), "ROLLED_BACK" if restored else "FAILED")
        event("inspect", "project_inspected", {
            "project": source.name,
            "file_count": len(source_files),
            "bytes": sum(p.stat().st_size for p in source_files),
            "source_hashes": source_hashes,
        })
        if checkpoint == "CREATED":
            if workspace.exists():
                # Untracked workspace is never silently reused; this prevents clobbering foreign files.
                return finish(RunResult(request.request_id, "REJECTED", "COPY", True,
                                        reason="workspace exists without a recorded copy checkpoint", human_review_required=True))
            copy_started = time.perf_counter()
            copy_project(source, workspace)
            if tree_hashes(source) != source_hashes or tree_hashes(workspace) != source_hashes:
                shutil.rmtree(workspace, ignore_errors=True)
                return finish(RunResult(request.request_id, "FAILED", "COPY", False,
                                        reason="source changed during copy or isolated copy hash verification failed; no edit was applied",
                                        human_review_required=True, workspace=str(workspace)))
            snapshot = run_dir / "checkpoints" / "project-before"
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(workspace, snapshot, symlinks=False)
            if tree_hashes(snapshot) != source_hashes:
                shutil.rmtree(workspace, ignore_errors=True)
                shutil.rmtree(snapshot, ignore_errors=True)
                return finish(RunResult(request.request_id, "FAILED", "COPY", False,
                                        reason="full-project checkpoint failed hash verification; no edit was applied",
                                        human_review_required=True, workspace=str(workspace)))
            stage_latencies["copy_ms"] = round((time.perf_counter() - copy_started) * 1000, 3)
            self.store.set_checkpoint(request.request_id, "COPIED")
            checkpoint = "COPIED"
        if not workspace.is_dir():
            return finish(RunResult(request.request_id, "FAILED", checkpoint, True,
                                    reason="resumable workspace is missing; original project was not touched", human_review_required=True))

        board = (workspace / request.operation.file).resolve()
        try:
            board.relative_to(workspace.resolve())
        except ValueError:
            return finish(RunResult(request.request_id, "REJECTED", checkpoint, True,
                                    reason="workspace target path escapes the copied project", human_review_required=True))
        original_copy = run_dir / "checkpoints" / "before-edit.kicad_pcb"
        original_copy.parent.mkdir(parents=True, exist_ok=True)
        if checkpoint == "COPIED":
            shutil.copy2(board, original_copy)
            self.store.set_checkpoint(request.request_id, "INSPECTED")
            checkpoint = "INSPECTED"

        baseline = None
        after = None
        edit_result = None
        target_was_changed = False
        # A resumed EDITED run must not repeat the write operation.
        if checkpoint in {"INSPECTED", "BASELINED"}:
            if checkpoint == "INSPECTED":
                baseline_started = time.perf_counter()
                tool_calls += 1
                baseline = self.validator.validate(workspace, board, run_dir / "reports" / "baseline")
                stage_latencies["baseline_validation_ms"] = round((time.perf_counter() - baseline_started) * 1000, 3)
                event("validate_baseline", "tool_result", baseline.to_dict())
                if not baseline.available:
                    return finish(RunResult(request.request_id, "VALIDATION_UNAVAILABLE", "BASELINE", True,
                                            baseline=baseline.to_dict(), reason=baseline.error,
                                            human_review_required=True, workspace=str(workspace)))
                # Save the baseline report so resume and audit never depend on rerunning a mutable tool.
                (run_dir / "baseline.json").write_text(json.dumps(baseline.to_dict(), indent=2), encoding="utf-8")
                self.store.set_checkpoint(request.request_id, "BASELINED")
                checkpoint = "BASELINED"
            else:
                try:
                    baseline_data = json.loads((run_dir / "baseline.json").read_text(encoding="utf-8"))
                    baseline = _report_from_dict(baseline_data)
                except (OSError, json.JSONDecodeError, KeyError, TypeError):
                    return finish(RunResult(request.request_id, "FAILED", checkpoint, True,
                                            reason="baseline checkpoint is missing or malformed; refusing to guess", human_review_required=True))
            before_text = original_copy.read_text(encoding="utf-8")
            edit_started = time.perf_counter()
            tool_calls += 1
            event("edit", "tool_call", {"tool": "add_silkscreen_text", "input": {
                "file": request.operation.file, "text": request.operation.text,
                "x_mm": request.operation.x_mm, "y_mm": request.operation.y_mm,
                "height_mm": request.operation.height_mm,
            }})
            try:
                edit_result = add_silkscreen_text(board, request.operation, request.request_id)
            except (RequestError, OSError, UnicodeError) as exc:
                stage_latencies["edit_ms"] = round((time.perf_counter() - edit_started) * 1000, 3)
                event("edit", "tool_failed", {"tool": "add_silkscreen_text", "error": str(exc)})
                return finish(RunResult(request.request_id, "REJECTED", "EDIT", True,
                                        baseline=baseline.to_dict(), reason=str(exc), human_review_required=True,
                                        workspace=str(workspace), metrics={"tool_calls": tool_calls}))
            target_was_changed = board.read_text(encoding="utf-8") != before_text
            stage_latencies["edit_ms"] = round((time.perf_counter() - edit_started) * 1000, 3)
            (run_dir / "edit.json").write_text(json.dumps(edit_result, indent=2), encoding="utf-8")
            patch = "".join(difflib.unified_diff(before_text.splitlines(True), board.read_text(encoding="utf-8").splitlines(True),
                                                   fromfile="original/" + request.operation.file,
                                                   tofile="workspace/" + request.operation.file))
            (run_dir / "changes.patch").write_text(patch, encoding="utf-8")
            event("edit", "tool_result", {"tool": "add_silkscreen_text", **edit_result})
            self.store.set_checkpoint(request.request_id, "EDITED")
            checkpoint = "EDITED"
        else:
            try:
                baseline = _report_from_dict(json.loads((run_dir / "baseline.json").read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                return finish(RunResult(request.request_id, "FAILED", checkpoint, True,
                                        reason="resume cannot reconstruct baseline report", human_review_required=True))
            edit_result_path = run_dir / "edit.json"
            if edit_result_path.exists():
                edit_result = json.loads(edit_result_path.read_text(encoding="utf-8"))
            else:
                edit_result = {"action": "resumed", "file": request.operation.file, "text": request.operation.text}

        # Reflection: persisted text, one target-file change, bounded validation retries, baseline delta.
        reflection_started = time.perf_counter()
        persisted = board.read_text(encoding="utf-8")
        if not annotation_matches(persisted, request.operation):
            return self._rollback_and_finish(request, finish, event, run_dir, workspace, board, original_copy,
                baseline, None, "persisted acceptance check failed: exactly one matching annotation was not found at the requested layer, size, and position", started,
                source, source_hashes, changed_files=[request.operation.file], stage_latencies=stage_latencies)
        workspace_hashes = tree_hashes(workspace)
        changed_files = sorted(name for name in (set(source_hashes) | set(workspace_hashes))
                               if source_hashes.get(name) != workspace_hashes.get(name))
        unexpected = [name for name in changed_files if name != request.operation.file]
        if unexpected:
            return self._rollback_and_finish(request, finish, event, run_dir, workspace, board, original_copy,
                baseline, None, f"policy violation: unexpected changed files: {unexpected}", started,
                source, source_hashes, changed_files=changed_files, stage_latencies=stage_latencies)

        if checkpoint == "EDITED":
            pass  # Persisted checkpoint is the boundary after the only mutating tool.
        stage_latencies["reflection_ms"] = round((time.perf_counter() - reflection_started) * 1000, 3)
        self.store.set_checkpoint(request.request_id, "VALIDATING")
        final_validation = None
        retries_used = 0
        validation_started = time.perf_counter()
        for attempt in range(request.max_retries + 1):
            tool_calls += 1
            final_validation = self.validator.validate(workspace, board, run_dir / "reports" / f"after-{attempt + 1}")
            event("validate_after", "tool_result", {"attempt": attempt + 1, **final_validation.to_dict()})
            if final_validation.available:
                break
            if attempt < request.max_retries:
                retries_used += 1
                event("recovery", "retry_validation", {"attempt": attempt + 1, "reason": final_validation.error})
        stage_latencies["post_validation_ms"] = round((time.perf_counter() - validation_started) * 1000, 3)
        if not final_validation or not final_validation.available:
            return self._rollback_and_finish(request, finish, event, run_dir, workspace, board, original_copy,
                baseline, final_validation, "validation unavailable after bounded retries: " + (final_validation.error or "unknown"), started,
                source, source_hashes, changed_files=changed_files, retries_used=retries_used, validation_unavailable=True, stage_latencies=stage_latencies)

        # Validation is not allowed to mutate the project. Re-check the whole
        # tree afterwards, including removed files and symlink changes.
        post_validation_hashes = tree_hashes(workspace)
        post_validation_changes = sorted(
            name for name in (set(workspace_hashes) | set(post_validation_hashes))
            if workspace_hashes.get(name) != post_validation_hashes.get(name)
        )
        if post_validation_changes:
            changed_files = sorted(set(changed_files) | set(
                name for name in (set(source_hashes) | set(post_validation_hashes))
                if source_hashes.get(name) != post_validation_hashes.get(name)
            ))
            event("reflection", "validator_mutated_workspace", {"changed_files": post_validation_changes})
            return self._rollback_and_finish(
                request, finish, event, run_dir, workspace, board, original_copy, baseline,
                final_validation, "validation tool unexpectedly modified the isolated project: " + ", ".join(post_validation_changes),
                started, source, source_hashes, changed_files=changed_files, retries_used=retries_used,
                stage_latencies=stage_latencies,
            )

        introduced = new_violations(baseline, final_validation)
        if introduced:
            event("reflection", "acceptance_failed", {"new_violations": [v.__dict__ for v in introduced]})
            return self._rollback_and_finish(request, finish, event, run_dir, workspace, board, original_copy,
                baseline, final_validation, "new DRC/ERC violations were introduced; restored the pre-edit checkpoint", started,
                source, source_hashes, changed_files=changed_files, new_violations=[v.signature() for v in introduced],
                retries_used=retries_used, stage_latencies=stage_latencies)

        # Validate persisted state after validation as the final acceptance criterion.
        after_text = board.read_text(encoding="utf-8")
        if not annotation_matches(after_text, request.operation):
            return self._rollback_and_finish(request, finish, event, run_dir, workspace, board, original_copy,
                baseline, final_validation, "post-validation persisted-state reflection failed: requested annotation does not match persisted position/layer/size", started,
                source, source_hashes, changed_files=changed_files, retries_used=retries_used, stage_latencies=stage_latencies)
        preserved = tree_hashes(source) == source_hashes
        if not preserved:
            # Never report success if another actor changed the caller's source tree mid-run.
            # Restore only our isolated workspace; do not overwrite the external source change.
            return self._rollback_and_finish(
                request, finish, event, run_dir, workspace, board, original_copy, baseline,
                final_validation, "source project changed during execution; isolated workspace restored and human review required",
                started, source, source_hashes, changed_files=changed_files, retries_used=retries_used,
                stage_latencies=stage_latencies,
            )
        self.store.set_checkpoint(request.request_id, "VERIFIED", "VERIFIED")
        result = RunResult(request.request_id, "VERIFIED", "VERIFIED", preserved, changed_files,
                           baseline=baseline.to_dict(), final_validation=final_validation.to_dict(),
                           recovery="none", human_review_required=False,
                           reason="Acceptance criteria satisfied; this is not a claim of overall electrical correctness.",
                           workspace=str(workspace), metrics={
                               "tool_calls": 1 + 1 + 1 + retries_used,
                               "validation_retries": retries_used,
                               "edit_action": edit_result.get("action") if isinstance(edit_result, dict) else "unknown",
                               "source_files": len(source_hashes),
                               "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                           })
        event("reflection", "acceptance_passed", {"changed_files": changed_files, "project_preserved": preserved})
        return finish(result, "VERIFIED")

    def _restore_workspace(self, run_dir: Path, workspace: Path, expected_hashes: dict[str, str]) -> tuple[bool, str | None]:
        """Restore the complete isolated project from its verified pre-edit snapshot."""
        snapshot = run_dir / "checkpoints" / "project-before"
        try:
            if not snapshot.is_dir():
                raise OSError("full-project checkpoint is missing")
            if tree_hashes(snapshot) != expected_hashes:
                raise OSError("full-project checkpoint does not match its source hash manifest")
            if workspace.exists():
                shutil.rmtree(workspace)
            shutil.copytree(snapshot, workspace, symlinks=False)
            if tree_hashes(workspace) != expected_hashes:
                raise OSError("restored workspace does not match the full-project checkpoint")
            return True, None
        except OSError as exc:
            return False, str(exc)

    def _rollback_and_finish(self, request, finish, event, run_dir, workspace, board, original_copy, baseline,
                             final_validation, reason, started, source, source_hashes, *, changed_files=None,
                             new_violations=None, retries_used=0, validation_unavailable=False, stage_latencies=None):
        recovery_started = time.perf_counter()
        restored, restore_error = self._restore_workspace(run_dir, workspace, source_hashes)
        recovery = "restored_full_workspace_checkpoint" if restored else "rollback_failed"
        if restored:
            event("recovery", "rollback_succeeded", {"workspace": str(workspace), "restored_file_count": len(source_hashes)})
        else:
            event("recovery", "rollback_failed", {"error": restore_error})
        recovery_ms = round((time.perf_counter() - recovery_started) * 1000, 3)
        if stage_latencies is not None:
            stage_latencies["recovery_ms"] = recovery_ms
        preserved = tree_hashes(source) == source_hashes
        status = "VALIDATION_UNAVAILABLE" if validation_unavailable and restored else ("ROLLED_BACK" if restored else "FAILED")
        result = RunResult(request.request_id, status, "ROLLED_BACK" if restored else "RECOVERY_FAILED",
                           preserved, changed_files or [], new_violations or [],
                           baseline.to_dict() if baseline else None,
                           final_validation.to_dict() if final_validation else None,
                           recovery=recovery, human_review_required=True,
                           reason=reason if restore_error is None else reason + "; checkpoint restore failed: " + restore_error,
                           workspace=str(workspace),
                           metrics={"tool_calls": 1 + 1 + 1 + retries_used, "validation_retries": retries_used,
                                    "recovery_ms": recovery_ms,
                                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)})
        return finish(result, status)


def _report_from_dict(raw: dict[str, Any]):
    from .models import ValidationReport, Violation
    return ValidationReport(
        available=raw["available"], tool=raw["tool"], exit_code=raw.get("exit_code"),
        violations=[Violation(**item) for item in raw.get("violations", [])],
        duration_ms=raw.get("duration_ms", 0), error=raw.get("error"), raw_report_path=raw.get("raw_report_path"),
    )
