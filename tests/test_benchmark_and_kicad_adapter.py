from __future__ import annotations

import json
from pathlib import Path

from boardguard.benchmark import run_benchmark
from boardguard.validator import KiCadCliValidator, new_violations
from boardguard.models import ValidationReport, Violation


def test_benchmark_runs_paired_scenarios_and_reports_sample_size():
    report = run_benchmark(repeats=1)
    assert report["sample_size_per_approach"] == 9
    assert report["results"]["boardguard"]["runs"] == 9
    assert report["results"]["baseline"]["runs"] == 9
    assert report["results"]["boardguard"]["rollback_success_rate_when_required"] == 1.0
    assert report["results"]["boardguard"]["human_escalation_accuracy"] == 1.0
    assert report["results"]["boardguard"]["duplicate_request_action_prevention"]["rate"] == 1.0
    assert "not evidence of real KiCad correctness" in report["methodology"]["validation"]


def test_real_validator_reports_missing_executable_instead_of_passing(tmp_path: Path):
    fixture = Path(__file__).resolve().parents[1] / "examples" / "demo_board"
    validator = KiCadCliValidator(executable="/definitely/missing/kicad-cli", timeout_seconds=0.1)
    report = validator.validate(fixture, fixture / "demo_board.kicad_pcb", tmp_path / "reports")
    assert report.available is False
    assert "could not start validator" in report.error


def test_real_validator_uses_configurable_default_timeout_and_inherits_environment(
    tmp_path: Path, monkeypatch
):
    import subprocess

    fixture = Path(__file__).resolve().parents[1] / "examples" / "demo_board"
    report_path = tmp_path / "reports" / "demo_board-drc.json"
    observed_kwargs = {}

    def fake_run(command, **kwargs):
        observed_kwargs.update(kwargs)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps({"violations": [], "unconnected_items": []}), encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.delenv("BOARDGUARD_KICAD_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(subprocess, "run", fake_run)
    validator = KiCadCliValidator(executable="kicad-cli")
    report = validator.validate(fixture, fixture / "demo_board.kicad_pcb", tmp_path / "reports")

    assert report.available is True
    assert validator.timeout_seconds == 180.0
    assert observed_kwargs["timeout"] == 180.0
    # Omitting env= is intentional: subprocess inherits APPDATA, USERPROFILE,
    # TEMP, KICAD_CONFIG_HOME, and other OS/user configuration by default.
    assert "env" not in observed_kwargs


def test_real_validator_timeout_can_be_overridden_by_environment(monkeypatch):
    monkeypatch.setenv("BOARDGUARD_KICAD_TIMEOUT_SECONDS", "240")
    assert KiCadCliValidator(executable="kicad-cli").timeout_seconds == 240.0


def test_real_validator_rejects_invalid_timeout_configuration(monkeypatch):
    import pytest

    for value in ("0", "-1", "nan", "inf", "not-a-number"):
        monkeypatch.setenv("BOARDGUARD_KICAD_TIMEOUT_SECONDS", value)
        with pytest.raises(ValueError, match="positive finite"):
            KiCadCliValidator(executable="kicad-cli")


def test_real_validator_fails_closed_when_command_times_out(tmp_path: Path, monkeypatch):
    import subprocess

    fixture = Path(__file__).resolve().parents[1] / "examples" / "demo_board"

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(
            command,
            timeout=kwargs["timeout"],
            output=b"partial stdout",
            stderr=b"partial stderr",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    validator = KiCadCliValidator(executable="kicad-cli", timeout_seconds=0.1)
    report = validator.validate(fixture, fixture / "demo_board.kicad_pcb", tmp_path / "reports")

    assert report.available is False
    assert report.exit_code is None
    assert "timed out after 0.1s" in report.error
    timeout_output = report.tool_output["commands"][0]
    assert timeout_output["timeout"] is True
    assert timeout_output["timeout_seconds"] == 0.1
    assert timeout_output["stdout"] == "partial stdout"
    assert timeout_output["stderr"] == "partial stderr"


def test_new_violation_delta_preserves_duplicate_counts():
    baseline = ValidationReport(True, "test", 0, [Violation("error", "clearance", "same", "stable")])
    after = ValidationReport(True, "test", 1, [Violation("error", "clearance", "same", "stable"),
                                               Violation("error", "clearance", "same", "stable")])
    assert len(new_violations(baseline, after)) == 1


def test_real_validator_fails_closed_on_nonzero_exit_even_with_empty_report(tmp_path: Path, monkeypatch):
    import subprocess

    fixture = Path(__file__).resolve().parents[1] / "examples" / "demo_board"
    report_path = tmp_path / "reports" / "demo_board-drc.json"

    def fake_run(command, **kwargs):
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({"violations": []}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="injected fatal CLI error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    validator = KiCadCliValidator(executable="kicad-cli", timeout_seconds=0.1)
    report = validator.validate(fixture, fixture / "demo_board.kicad_pcb", tmp_path / "reports")
    assert report.available is False
    assert report.exit_code == 1
    assert "nonzero" in report.error


def test_kicad_violation_parser_preserves_duplicate_rule_counts():
    from boardguard.validator import _walk_violations

    row = {"severity": "warning", "type": "lib_footprint_issues", "description": "missing library"}
    parsed = _walk_violations({"violations": [row, row]})
    assert len(parsed) == 2
    assert parsed[0].signature() == parsed[1].signature()


def test_real_validator_fails_closed_on_unknown_erc_report_shape(tmp_path: Path, monkeypatch):
    import subprocess

    fixture = Path(__file__).resolve().parents[1] / "examples" / "demo_board"
    project = tmp_path / "project"
    import shutil
    shutil.copytree(fixture, project)
    report_path = tmp_path / "reports" / "demo_board-1-erc.json"

    def fake_run(command, **kwargs):
        report_path.parent.mkdir(parents=True, exist_ok=True)
        if command[1:3] == ["pcb", "drc"]:
            drc_report = {"violations": [], "unconnected_items": [], "schematic_parity": []}
            (tmp_path / "reports" / "demo_board-drc.json").write_text(json.dumps(drc_report), encoding="utf-8")
        else:
            report_path.write_text(json.dumps({"unexpected": []}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    # Work on a test copy, never mutate the repository fixture.
    (project / "demo_board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    validator = KiCadCliValidator(executable="kicad-cli", timeout_seconds=0.1)
    report = validator.validate(project, project / "demo_board.kicad_pcb", tmp_path / "reports")
    assert report.available is False
    assert "unknown KiCad ERC JSON report schema" in report.error


def test_real_validator_invokes_drc_with_schematic_parity(tmp_path: Path, monkeypatch):
    import subprocess

    fixture = Path(__file__).resolve().parents[1] / "examples" / "demo_board"
    project = tmp_path / "project"
    import shutil
    shutil.copytree(fixture, project)
    (project / "demo_board.kicad_sch").write_text("(kicad_sch)", encoding="utf-8")
    reports = tmp_path / "reports"
    observed = []

    def fake_run(command, **kwargs):
        observed.append(command)
        reports.mkdir(parents=True, exist_ok=True)
        if command[1:3] == ["pcb", "drc"]:
            (reports / "demo_board-drc.json").write_text(json.dumps({"violations": [], "unconnected_items": [], "schematic_parity": []}), encoding="utf-8")
        else:
            (reports / "demo_board-1-erc.json").write_text(json.dumps({"sheets": [{"violations": []}]}), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = KiCadCliValidator(executable="kicad-cli").validate(project, project / "demo_board.kicad_pcb", reports)
    assert report.available is True
    assert len(observed) == 2
    assert "--schematic-parity" in observed[0]
    assert observed[1][1:3] == ["sch", "erc"]


def test_drc_parser_counts_unconnected_and_parity_records():
    from boardguard.validator import _walk_violations

    report = {
        "violations": [],
        "unconnected_items": [{"severity": "error", "type": "unconnected_items", "description": "unconnected pad"}],
        "schematic_parity": [{"severity": "warning", "type": "schematic_parity", "description": "parity issue"}],
    }
    parsed = _walk_violations(report)
    assert {item.rule for item in parsed} == {"unconnected_items", "schematic_parity"}


def test_board_only_drc_does_not_require_schematic_parity_report(tmp_path: Path, monkeypatch):
    import shutil
    import subprocess

    fixture = Path(__file__).resolve().parents[1] / "examples" / "demo_board"
    project = tmp_path / "project"
    shutil.copytree(fixture, project)
    reports = tmp_path / "reports"
    observed = []

    def fake_run(command, **kwargs):
        observed.append(command)
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "demo_board-drc.json").write_text(
            json.dumps({"violations": [], "unconnected_items": []}), encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    report = KiCadCliValidator(executable="kicad-cli").validate(
        project, project / "demo_board.kicad_pcb", reports
    )
    assert report.available is True
    assert len(observed) == 1
    assert "--schematic-parity" not in observed[0]
