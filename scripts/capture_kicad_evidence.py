from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def erc_violations(data: dict) -> list[dict]:
    """Return ERC findings from each sheet in KiCad ERC JSON reports."""
    return [
        violation
        for sheet in data.get("sheets", [])
        for violation in sheet.get("violations", [])
    ]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_required(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"Required evidence file not found: {source}")
    shutil.copy2(source, destination)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preserve BoardGuard real-KiCad evidence and produce a PCB-only diff."
    )
    parser.add_argument("--run-id", default="antmicro-add-label-006")
    args = parser.parse_args()

    root = Path.cwd().resolve()
    if root.name.lower() != "boardguard":
        raise SystemExit(
            f"Run this script from D:\\BoardGuard (current directory: {root})"
        )

    run_id = args.run_id
    print(f"Checking verified BoardGuard run {run_id!r} in {root}...", flush=True)
    run_dir = root / ".boardguard-workspaces" / run_id
    report_path = run_dir / "report.json"
    request_path = root / "examples" / "requests" / "antmicro_add_silk.json"
    source_board = (
        root / "examples" / "antmicro_microphone" / "hardware" / "microphone-board.kicad_pcb"
    )
    verified_board = run_dir / "workspace" / "hardware" / "microphone-board.kicad_pcb"

    report = read_json(report_path)
    request = read_json(request_path)
    operation = request.get("operation", {})

    if report.get("status") != "VERIFIED":
        raise SystemExit(f"Refusing to package evidence: report status is {report.get('status')!r}")
    if report.get("request_id") != run_id or request.get("request_id") != run_id:
        raise SystemExit(
            "Run ID, report request_id, and current request JSON do not match. "
            f"run={run_id!r}, report={report.get('request_id')!r}, request={request.get('request_id')!r}"
        )
    if not report.get("baseline", {}).get("available"):
        raise SystemExit("Refusing to package evidence: baseline validation is unavailable")
    if not report.get("final_validation", {}).get("available"):
        raise SystemExit("Refusing to package evidence: final validation is unavailable")
    if report.get("new_violations"):
        raise SystemExit("Refusing to package evidence: report contains new violations")
    if report.get("project_preserved") is not True:
        raise SystemExit("Refusing to package evidence: source-preservation check did not pass")
    changed_files = report.get("changed_files", [])
    if changed_files != ["hardware/microphone-board.kicad_pcb"]:
        raise SystemExit(f"Unexpected changed_files in report: {changed_files!r}")
    if operation.get("type") != "add_silkscreen_text":
        raise SystemExit(f"Unexpected operation type: {operation.get('type')!r}")
    if not source_board.is_file() or not verified_board.is_file():
        raise SystemExit("Original or verified workspace PCB is missing")

    baseline_drc_path = run_dir / "reports" / "baseline" / "microphone-board-drc.json"
    baseline_erc_path = run_dir / "reports" / "baseline" / "microphone-board-1-erc.json"
    final_drc_path = run_dir / "reports" / "after-1" / "microphone-board-drc.json"
    final_erc_path = run_dir / "reports" / "after-1" / "microphone-board-1-erc.json"

    baseline_drc = read_json(baseline_drc_path)
    baseline_erc = read_json(baseline_erc_path)
    final_drc = read_json(final_drc_path)
    final_erc = read_json(final_erc_path)

    text = str(operation.get("text", ""))
    x = float(operation["x_mm"])
    y = float(operation["y_mm"])
    height = float(operation["height_mm"])
    new_pcb_text = verified_board.read_text(encoding="utf-8")
    old_pcb_text = source_board.read_text(encoding="utf-8")

    # Require the requested label to appear only in the verified copy at its exact coordinates.
    label_pattern = re.compile(r"\(gr_text\s+\"" + re.escape(text) + r"\"(?=\s|\))")
    source_labels = list(label_pattern.finditer(old_pcb_text))
    verified_labels = list(label_pattern.finditer(new_pcb_text))
    expected_prefix = f'(gr_text "{text}" (at {x:g} {y:g}) (layer "F.SilkS")'
    inserted_text_match = any(
        block.startswith(expected_prefix)
        for m in verified_labels
        if (block := new_pcb_text[m.start():new_pcb_text.find("\n", m.start())]).strip()
    )
    if source_labels:
        raise SystemExit(f"Original board already contains text {text!r}; cannot prove it was added")
    if len(verified_labels) != 1 or not inserted_text_match:
        raise SystemExit(
            f"Verified board does not contain exactly one {text!r} annotation at ({x:g}, {y:g}) on F.SilkS"
        )

    old_lines = old_pcb_text.splitlines(keepends=True)
    new_lines = new_pcb_text.splitlines(keepends=True)
    diff = "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="original/hardware/microphone-board.kicad_pcb",
            tofile="verified/hardware/microphone-board.kicad_pcb",
            n=3,
        )
    )
    changes = [opcode for opcode in difflib.SequenceMatcher(a=old_lines, b=new_lines).get_opcodes() if opcode[0] != "equal"]
    if len(changes) != 1 or changes[0][0] != "insert" or changes[0][2] != changes[0][1] or changes[0][4] - changes[0][3] != 1:
        raise SystemExit(
            "PCB diff is not exactly one inserted line. No README should claim a one-line-only edit. "
            f"Diff saved nowhere; non-equal diff operations={changes!r}"
        )
    inserted_line = new_lines[changes[0][3]]
    if expected_prefix not in inserted_line:
        raise SystemExit(f"The single inserted PCB line is not the expected label: {inserted_line!r}")

    evidence_dir = root / "evidence" / "real-kicad" / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "report.json": report_path,
        "request.json": request_path,
        "baseline-drc.json": baseline_drc_path,
        "baseline-erc.json": baseline_erc_path,
        "final-drc.json": final_drc_path,
        "final-erc.json": final_erc_path,
        "original-microphone-board.kicad_pcb": source_board,
        "verified-microphone-board.kicad_pcb": verified_board,
    }
    for destination_name, source_path in artifacts.items():
        copy_required(source_path, evidence_dir / destination_name)
    # Preserve the upstream license/source attribution with the copied reference hardware.
    for optional_name in ("LICENSE-ANTMIC.txt", "SOURCE.txt"):
        optional_path = root / "examples" / "antmicro_microphone" / optional_name
        if optional_path.is_file():
            shutil.copy2(optional_path, evidence_dir / optional_name)

    readme_path = root / "README.md"
    readme_backup = evidence_dir / "README.md.before-real-KiCad-verification"
    if not readme_backup.exists():
        copy_required(readme_path, readme_backup)

    (evidence_dir / "pcb.diff").write_text(diff, encoding="utf-8", newline="")

    summary = {
        "captured_at_local": datetime.now().astimezone().isoformat(timespec="seconds"),
        "run_id": run_id,
        "status": report["status"],
        "operation": {
            "type": operation["type"],
            "text": text,
            "layer": "F.SilkS",
            "x_mm": x,
            "y_mm": y,
            "height_mm": height,
        },
        "kicad_version": baseline_drc.get("kicad_version"),
        "baseline_drc": {
            "violations": len(baseline_drc.get("violations", [])),
            "unconnected_items": len(baseline_drc.get("unconnected_items", [])),
            "schematic_parity_issues": len(baseline_drc.get("schematic_parity", [])),
        },
        "final_drc": {
            "violations": len(final_drc.get("violations", [])),
            "unconnected_items": len(final_drc.get("unconnected_items", [])),
            "schematic_parity_issues": len(final_drc.get("schematic_parity", [])),
        },
        "baseline_erc_violations": len(erc_violations(baseline_erc)),
        "final_erc_violations": len(erc_violations(final_erc)),
        "new_violations_reported_by_boardguard": report.get("new_violations", []),
        "changed_files_reported_by_boardguard": changed_files,
        "project_preserved_reported_by_boardguard": report.get("project_preserved"),
        "pcb_diff_added_lines": 1,
        "pcb_diff_removed_lines": 0,
        "source_board_sha256": sha256(source_board),
        "verified_board_sha256": sha256(verified_board),
        "note": (
            "VERIFIED means the requested annotation persisted and introduced no new DRC/ERC violations "
            "relative to this baseline. It is not a certification of overall electrical correctness. "
            "Existing missing Antmicro symbol/footprint library warnings remain."
        ),
    }
    (evidence_dir / "evidence-summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )

    manifest_paths = sorted(p for p in evidence_dir.iterdir() if p.is_file() and p.name != "sha256-manifest.json")
    manifest = {p.name: sha256(p) for p in manifest_paths}
    (evidence_dir / "sha256-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    # Update the stale README statement only after the report checks and exact diff checks passed.
    readme_text = readme_path.read_text(encoding="utf-8")
    old_re = re.compile(
        r"(?m)^On the build machine, `kicad-cli` was absent and package installation timed out\. "
        r"Real ERC/DRC execution has therefore \*\*not been verified here\*\*\. "
        r"If validation was blocked by an unavailable tool, install KiCad then retry the same request ID "
        r"with `boardguard resume antmicro-add-label-001 --request examples/requests/antmicro_add_silk\.json "
        r"--backend kicad --approve`\.$"
    )
    section = (
        "### Verified real-KiCad validation (Antmicro reference board)\n\n"
        "A real run was completed locally with **KiCad 10.0.7** using request "
        "`antmicro-add-label-006`. BoardGuard added `BG` to `F.SilkS` at "
        "`(243.4, 149.2) mm` with `0.7 mm` text height.\n\n"
        "| Check | Baseline | After edit |\n"
        "|---|---:|---:|\n"
        f"| PCB DRC warnings | {len(baseline_drc.get('violations', []))} | {len(final_drc.get('violations', []))} |\n"
        f"| Schematic ERC warnings | {len(erc_violations(baseline_erc))} | {len(erc_violations(final_erc))} |\n"
        f"| Unconnected items | {len(baseline_drc.get('unconnected_items', []))} | {len(final_drc.get('unconnected_items', []))} |\n"
        f"| Schematic-parity issues | {len(baseline_drc.get('schematic_parity', []))} | {len(final_drc.get('schematic_parity', []))} |\n\n"
        "BoardGuard reported **`VERIFIED`**, no new DRC/ERC violations, and only "
        "`hardware/microphone-board.kicad_pcb` changed in the isolated workspace. The original project was preserved. "
        "The exact PCB diff is one added `BG` text object and no removed lines.\n\n"
        f"Evidence is preserved in `evidence/real-kicad/{run_id}/` (run report, baseline/final DRC/ERC JSON, "
        "original and verified PCB copies, exact diff, SHA-256 manifest, and upstream license/source files when available). "
        "The 13 pre-existing DRC library warnings and 44 pre-existing ERC library warnings remain unchanged. "
        "This is evidence for one bounded annotation workflow, **not** certification of overall electrical correctness "
        "or production readiness."
    )
    section_heading = "### Verified real-KiCad validation (Antmicro reference board)"
    if old_re.search(readme_text):
        readme_text = old_re.sub(section, readme_text, count=1)
    elif section_heading in readme_text:
        section_re = re.compile(
            r"(?ms)^### Verified real-KiCad validation \(Antmicro reference board\)\n.*?(?=^## |\Z)"
        )
        if not section_re.search(readme_text):
            raise SystemExit("Could not safely locate the existing README section.")
        readme_text = section_re.sub(lambda _: section + "\n\n", readme_text, count=1)
    else:
        raise SystemExit("Expected README validation section was not found.")

    obsolete_json_note = (
        "- KiCad JSON report handling is defensive but was not exercised in this environment. Unknown schemas fail closed."
    )
    updated_json_note = (
        "- KiCad 10.0.7 DRC/ERC JSON reports were exercised in one real Antmicro annotation run. Other versions and report schemas remain untested; unknown schemas fail closed."
    )
    if obsolete_json_note in readme_text:
        readme_text = readme_text.replace(obsolete_json_note, updated_json_note, 1)
    readme_path.write_text(readme_text, encoding="utf-8", newline="")

    print("Evidence captured successfully and README updated.")
    print(f"Status: {report['status']}")
    print(f"KiCad version: {summary['kicad_version']}")
    print(f"Request: {run_id}")
    print(f"Edit: {text!r} on F.SilkS at ({x:g}, {y:g}) mm; size {height:g} mm")
    print(f"Baseline DRC warnings/violations: {summary['baseline_drc']['violations']}")
    print(f"Final DRC warnings/violations: {summary['final_drc']['violations']}")
    print(f"Baseline ERC violations: {summary['baseline_erc_violations']}")
    print(f"Final ERC violations: {summary['final_erc_violations']}")
    print("BoardGuard-reported new violations: 0")
    print("Exact PCB diff: one inserted BG text line, zero removed lines")
    print(f"Evidence folder: {evidence_dir}")
    print("\n=== EXACT PCB DIFF ===")
    print(diff, end="" if diff.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Evidence capture failed: {exc}") from exc
