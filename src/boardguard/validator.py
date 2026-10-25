from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any, Protocol

from .models import ValidationReport, Violation


class Validator(Protocol):
    def validate(self, project_dir: Path, board_file: Path, report_dir: Path) -> ValidationReport: ...


def _walk_violations(obj: Any) -> list[Violation]:
    """Parse common KiCad JSON report layouts; fail closed if KiCad's schema is unknown."""
    found: list[Violation] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            severity = value.get("severity")
            rule = value.get("type") or value.get("rule") or value.get("code") or value.get("id")
            msg = value.get("description") or value.get("message") or value.get("text")
            if isinstance(severity, str) and severity.lower() in {"error", "warning", "info", "exclusion"} and isinstance(rule, str) and isinstance(msg, str):
                # Keep object-level evidence in the signature. Two same-text rules
                # attached to different items must not collapse into one violation.
                items = value.get("items")
                item_ids: list[str] = []
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        item_uuid = item.get("uuid")
                        position = item.get("pos")
                        if isinstance(item_uuid, str):
                            item_ids.append("uuid=" + item_uuid)
                        if isinstance(position, dict):
                            x, y = position.get("x"), position.get("y")
                            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                                item_ids.append(f"pos={x:g},{y:g}")
                        if not isinstance(item_uuid, str) and not isinstance(position, dict) and isinstance(item.get("description"), str):
                            item_ids.append("item=" + item["description"])
                identity = "|".join([severity.lower(), rule, msg, *sorted(item_ids)])
                # Preserve multiplicity: two identical violations at different locations
                # still count as two, so a new duplicate cannot disappear in the diff.
                found.append(Violation(severity.lower(), rule, msg, identity))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(obj)
    return found


class KiCadCliValidator:
    """Runs real kicad-cli DRC and ERC when schematics exist; missing tools never pass."""

    DEFAULT_TIMEOUT_SECONDS = 180.0
    TIMEOUT_ENV_VAR = "BOARDGUARD_KICAD_TIMEOUT_SECONDS"

    def __init__(self, executable: str | None = None, timeout_seconds: float | None = None):
        self.executable = (
            executable
            or os.environ.get("BOARDGUARD_KICAD_CLI")
            or shutil.which("kicad-cli")
        )
        if timeout_seconds is None:
            configured_timeout = os.environ.get(self.TIMEOUT_ENV_VAR)
            try:
                timeout_seconds = (
                    self.DEFAULT_TIMEOUT_SECONDS
                    if configured_timeout is None
                    else float(configured_timeout)
                )
            except ValueError as exc:
                raise ValueError(
                    f"{self.TIMEOUT_ENV_VAR} must be a positive finite number of seconds"
                ) from exc
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError(
                f"timeout_seconds must be a positive finite number; got {timeout_seconds!r}"
            )
        self.timeout_seconds = float(timeout_seconds)

    def _run_report(self, command: list[str], cwd: Path, report: Path, prefix: str) -> tuple[bool, int | None, list[Violation], float, str | None, dict[str, Any], dict[str, Any]]:
        tool_input = {"command": command, "cwd": str(cwd), "timeout_seconds": self.timeout_seconds}
        if not self.executable:
            return False, None, [], 0.0, "kicad-cli not found on PATH", tool_input, {"error": "kicad-cli not found"}
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
                # Inherit the user environment so KiCad can locate Windows user settings,
                # temporary directories, and any configured KiCad library paths.
            )
        except subprocess.TimeoutExpired as exc:
            def partial_text(value: str | bytes | None) -> str:
                if isinstance(value, bytes):
                    return value.decode("utf-8", errors="replace")
                return value if isinstance(value, str) else ""

            timeout_output = {
                "timeout": True,
                "timeout_seconds": self.timeout_seconds,
                "stdout": partial_text(exc.stdout)[:3000],
                "stderr": partial_text(exc.stderr)[:3000],
            }
            return (
                False,
                None,
                [],
                (time.perf_counter() - started) * 1000,
                f"timed out after {self.timeout_seconds:g}s",
                tool_input,
                timeout_output,
            )
        except OSError as exc:
            return False, None, [], (time.perf_counter() - started) * 1000, f"could not start validator: {exc}", tool_input, {"error": str(exc)}
        elapsed = (time.perf_counter() - started) * 1000
        if not report.is_file():
            detail = (completed.stderr or completed.stdout or "no report produced")[:1000]
            return False, completed.returncode, [], elapsed, f"KiCad produced no JSON report: {detail}", tool_input, {"returncode": completed.returncode, "stdout": completed.stdout[:3000], "stderr": completed.stderr[:3000], "report_missing": True}
        try:
            raw = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, completed.returncode, [], elapsed, f"invalid KiCad JSON report: {exc}", tool_input, {"returncode": completed.returncode, "stdout": completed.stdout[:3000], "stderr": completed.stderr[:3000], "report_invalid": True}
        violations = _walk_violations(raw)
        if completed.returncode != 0:
            return False, completed.returncode, violations, elapsed, \
                f"KiCad command exited nonzero ({completed.returncode}); refusing to trust the report", \
                tool_input, {"returncode": completed.returncode, "stdout": completed.stdout[:3000],
                             "stderr": completed.stderr[:3000], "report_path": str(report),
                             "violation_count": len(violations)}
        report_kind = prefix.partition(":")[0]
        def rows_have_known_schema(rows: Any) -> bool:
            if not isinstance(rows, list):
                return False
            for row in rows:
                if not isinstance(row, dict):
                    return False
                severity = row.get("severity")
                rule = row.get("type") or row.get("rule") or row.get("code") or row.get("id")
                message = row.get("description") or row.get("message") or row.get("text")
                if not (isinstance(severity, str) and severity.lower() in {"error", "warning", "info", "exclusion"}
                        and isinstance(rule, str) and isinstance(message, str)):
                    return False
            return True

        if report_kind == "drc":
            known_shape = (isinstance(raw, dict)
                           and rows_have_known_schema(raw.get("violations"))
                           and rows_have_known_schema(raw.get("unconnected_items")))
            if known_shape and "--schematic-parity" in command:
                known_shape = rows_have_known_schema(raw.get("schematic_parity"))
        elif report_kind == "erc":
            sheets = raw.get("sheets") if isinstance(raw, dict) else None
            known_shape = isinstance(sheets, list) and all(
                isinstance(sheet, dict) and rows_have_known_schema(sheet.get("violations"))
                for sheet in sheets
            )
        else:
            known_shape = False
        if not known_shape:
            return False, completed.returncode, [], elapsed, \
                f"unknown KiCad {report_kind.upper()} JSON report schema; refusing to treat report as clean", \
                tool_input, {"returncode": completed.returncode, "stdout": completed.stdout[:3000],
                             "stderr": completed.stderr[:3000], "report_path": str(report),
                             "report_invalid_schema": True}
        if isinstance(raw, dict) and isinstance(raw.get("violations"), list) and raw["violations"] and not violations:
            return False, completed.returncode, [], elapsed, "unknown KiCad JSON violation schema; refusing to treat report as clean", tool_input, {"returncode": completed.returncode, "stdout": completed.stdout[:3000], "stderr": completed.stderr[:3000], "report_path": str(report), "report_invalid_schema": True}
        for violation in violations:
            violation.identity = f"{prefix}:{violation.signature()}"
        return True, completed.returncode, violations, elapsed, None, tool_input, {"returncode": completed.returncode, "stdout": completed.stdout[:3000], "stderr": completed.stderr[:3000], "report_path": str(report), "violation_count": len(violations)}

    def validate(self, project_dir: Path, board_file: Path, report_dir: Path) -> ValidationReport:
        if not self.executable:
            return ValidationReport(False, "kicad-cli pcb drc / sch erc", None, error="kicad-cli not found on PATH",
                                    tool_input={"executable": None}, tool_output={"error": "kicad-cli not found on PATH"})
        report_dir.mkdir(parents=True, exist_ok=True)
        commands: list[tuple[str, list[str], Path]] = []
        drc_out = report_dir / f"{board_file.stem}-drc.json"
        schematics = sorted(project_dir.rglob("*.kicad_sch"))
        drc_command = [self.executable, "pcb", "drc"]
        if schematics:
            drc_command.append("--schematic-parity")
        drc_command.extend(["--format", "json", "--output", str(drc_out), str(board_file)])
        commands.append(("drc", drc_command, drc_out))
        # Prefer the project root schematic; otherwise exclude files referenced as child sheets.
        root_schematic = project_dir / f"{board_file.stem}.kicad_sch"
        if root_schematic.is_file():
            schematics = [root_schematic]
        elif schematics:
            referenced = set()
            import re
            for schematic in schematics:
                try:
                    content = schematic.read_text(encoding="utf-8")
                except OSError:
                    continue
                referenced.update(re.findall(r'\(property\s+"Sheetfile"\s+"([^"]+\.kicad_sch)"', content))
            schematics = [item for item in schematics if item.name not in referenced] or schematics[:1]
        for index, schematic in enumerate(schematics):
            erc_out = report_dir / f"{schematic.stem}-{index + 1}-erc.json"
            commands.append(("erc", [self.executable, "sch", "erc", "--format", "json", "--output", str(erc_out), str(schematic)], erc_out))
        all_violations: list[Violation] = []
        durations = 0.0
        exit_codes: list[int] = []
        tool_inputs: list[dict[str, Any]] = []
        tool_outputs: list[dict[str, Any]] = []
        for index, (kind, command, output) in enumerate(commands):
            available, exit_code, violations, duration, error, tool_input, tool_output = self._run_report(command, project_dir, output, f"{kind}:{index}")
            tool_inputs.append(tool_input)
            tool_outputs.append(tool_output)
            durations += duration
            if exit_code is not None:
                exit_codes.append(exit_code)
            if not available:
                return ValidationReport(False, "kicad-cli pcb drc / sch erc", exit_code, all_violations, durations,
                                        error=f"{kind.upper()} unavailable or invalid: {error}", raw_report_path=str(output) if output.exists() else None,
                                        tool_input={"commands": tool_inputs}, tool_output={"commands": tool_outputs})
            all_violations.extend(violations)
        return ValidationReport(True, "kicad-cli pcb drc" + (" + sch erc" if schematics else ""),
                                max(exit_codes) if exit_codes else 0, all_violations, durations,
                                raw_report_path=str(drc_out), tool_input={"commands": tool_inputs}, tool_output={"commands": tool_outputs})


class SimulatedValidator:
    """Fault-injection validator for tests and offline benchmark only; never calls itself KiCad."""

    def __init__(self, mode: str = "normal"):
        self.mode = mode
        self.calls = 0

    def validate(self, project_dir: Path, board_file: Path, report_dir: Path) -> ValidationReport:
        self.calls += 1
        start = time.perf_counter()
        content = board_file.read_text(encoding="utf-8")
        violations: list[Violation] = []
        if "BG_SIM_BASELINE_DRC" in content:
            violations.append(Violation("error", "sim_baseline_rule", "fixture baseline violation", "baseline:error:sim_baseline_rule"))
        if "BG_SIM_NEW_DRC" in content or "BAD_CLEARANCE" in content:
            violations.append(Violation("error", "sim_clearance", "fault-injected clearance violation", "new:error:sim_clearance"))
        if self.mode == "transient" and self.calls == 2:
            return ValidationReport(False, "simulator (fault injection)", None, duration_ms=(time.perf_counter() - start) * 1000, error="injected transient validator failure", tool_input={"mode": self.mode, "call": self.calls}, tool_output={"error": "injected transient validator failure"})
        if self.mode == "unavailable":
            return ValidationReport(False, "simulator (fault injection)", None, duration_ms=(time.perf_counter() - start) * 1000, error="injected unavailable validator", tool_input={"mode": self.mode, "call": self.calls}, tool_output={"error": "injected unavailable validator"})
        if self.mode == "timeout":
            return ValidationReport(False, "simulator (fault injection)", None, duration_ms=(time.perf_counter() - start) * 1000, error="injected timeout", tool_input={"mode": self.mode, "call": self.calls}, tool_output={"error": "injected timeout"})
        report_dir.mkdir(parents=True, exist_ok=True)
        report = report_dir / f"{board_file.stem}-simulated-drc.json"
        report.write_text(json.dumps({"mode": self.mode, "violations": [v.__dict__ for v in violations]}, indent=2), encoding="utf-8")
        return ValidationReport(True, "simulator (fault injection; not KiCad)", 0 if not violations else 1, violations,
                                (time.perf_counter() - start) * 1000, raw_report_path=str(report),
                                tool_input={"mode": self.mode, "call": self.calls, "board_file": str(board_file)},
                                tool_output={"violation_count": len(violations), "violations": [v.__dict__ for v in violations], "report_path": str(report)})


def new_violations(baseline: ValidationReport, current: ValidationReport) -> list[Violation]:
    before = Counter(v.signature() for v in baseline.violations)
    after = Counter(v.signature() for v in current.violations)
    delta = after - before
    remaining = delta.copy()
    result: list[Violation] = []
    for violation in current.violations:
        sig = violation.signature()
        if remaining[sig] > 0:
            result.append(violation)
            remaining[sig] -= 1
    return result
