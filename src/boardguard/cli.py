from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .benchmark import run_benchmark
from .engine import BoardGuardEngine
from .models import ChangeRequest, RequestError
from .state import StateStore
from .validator import KiCadCliValidator, SimulatedValidator


def _defaults() -> tuple[Path, Path]:
    repo = Path.cwd()
    return repo / ".boardguard-state" / "state.sqlite3", repo / ".boardguard-workspaces"


def _add_runtime_args(parser: argparse.ArgumentParser) -> None:
    default_state, default_workspaces = _defaults()
    parser.add_argument("--state-dir", type=Path, default=default_state.parent, help="directory for SQLite state and traces")
    parser.add_argument("--workspace-root", type=Path, default=default_workspaces, help="isolated run workspaces")
    parser.add_argument("--backend", choices=("kicad", "simulator"), default="kicad",
                        help="kicad runs the real CLI; simulator is for explicit offline demos/tests only")
    parser.add_argument("--approve", action="store_true", help="explicitly approve the requested file mutation")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="boardguard", description="Controlled, auditable KiCad change execution")
    parser.add_argument("--version", action="version", version=f"BoardGuard {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="execute a structured request in an isolated copy")
    run.add_argument("request", type=Path)
    _add_runtime_args(run)
    resume = commands.add_parser("resume", help="resume the same request from its last persisted checkpoint")
    resume.add_argument("request_id")
    resume.add_argument("--request", type=Path, required=True)
    _add_runtime_args(resume)
    demo = commands.add_parser("demo", help="run an explicitly simulated, offline end-to-end demo")
    demo.add_argument("--clean-workspace", action="store_true", help="remove the isolated copy after reporting; disabled by default")
    demo.add_argument("--state-dir", type=Path, default=Path(".boardguard-state"))
    demo.add_argument("--workspace-root", type=Path, default=Path(".boardguard-workspaces"))
    benchmark = commands.add_parser("benchmark", help="run paired offline fault-injection benchmark")
    benchmark.add_argument("--repeats", type=int, default=3)
    benchmark.add_argument("--output", type=Path, default=Path("evaluation/latest.json"))
    inspect = commands.add_parser("inspect", help="inspect project files and hashes without editing")
    inspect.add_argument("project", type=Path)
    status = commands.add_parser("status", help="show a persisted run and trace events")
    status.add_argument("request_id")
    status.add_argument("--state-dir", type=Path, default=Path(".boardguard-state"))
    return parser


def _execute(request_path: Path, args: Any, resume: bool = False):
    try:
        request, base = ChangeRequest.load(request_path.resolve())
    except RequestError as exc:
        print(json.dumps({"status": "REJECTED", "reason": str(exc)}, indent=2))
        return 2
    if resume and request.request_id != args.request_id:
        print(json.dumps({"status": "REJECTED", "reason": "request_id does not match request JSON"}, indent=2))
        return 2
    validator = KiCadCliValidator() if args.backend == "kicad" else SimulatedValidator()
    args.state_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(args.state_dir / "state.sqlite3")
    try:
        engine = BoardGuardEngine(store, args.workspace_root, validator)
        result = engine.run(request, base, cli_approved=args.approve, resume=resume)
    finally:
        store.close()
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0 if result.status == "VERIFIED" else (3 if result.status == "NEEDS_APPROVAL" else 2)


def _demo(args: Any) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    fixture = repo_root / "examples" / "demo_board"
    run_root = args.workspace_root.resolve()
    state_root = args.state_dir.resolve()
    request_id = f"demo-{time.time_ns()}"
    request = ChangeRequest.from_dict({
        "schema_version": 1,
        "request_id": request_id,
        "project_dir": str(fixture),
        "operation": {"type": "add_silkscreen_text", "file": "demo_board.kicad_pcb", "text": "BOARDGUARD PASS", "x_mm": 30, "y_mm": 20, "height_mm": 1.0},
        "approval": {"approved": True, "reason": "explicit local demo invocation"},
        "max_retries": 1,
        "acceptance": {"text_present": True},
    })
    state_root.mkdir(parents=True, exist_ok=True)
    store = StateStore(state_root / "state.sqlite3")
    try:
        engine = BoardGuardEngine(store, run_root, SimulatedValidator())
        result = engine.run(request, repo_root, cli_approved=True)
    finally:
        store.close()
    payload = result.to_dict()
    payload["execution_mode"] = "SIMULATED; no kicad-cli invocation occurred"
    print(json.dumps(payload, indent=2, sort_keys=True))
    if args.clean_workspace and result.workspace:
        # Preserve DB report/trace for audit; remove only the copied project contents.
        shutil.rmtree(result.workspace, ignore_errors=True)
    return 0 if result.status == "VERIFIED" else 2


def _inspect(project: Path) -> int:
    project = project.resolve()
    if not project.is_dir():
        print(json.dumps({"status": "REJECTED", "reason": "project directory does not exist"}, indent=2))
        return 2
    files = []
    for path in sorted(project.rglob("*")):
        if path.is_symlink():
            print(json.dumps({"status": "REJECTED", "reason": f"symlink found: {path}"}, indent=2))
            return 2
        if path.is_file():
            import hashlib
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            files.append({"path": path.relative_to(project).as_posix(), "bytes": path.stat().st_size, "sha256": digest})
    print(json.dumps({"status": "INSPECTED", "project": str(project), "files": files}, indent=2))
    return 0


def _status(request_id: str, state_dir: Path) -> int:
    db_path = state_dir / "state.sqlite3"
    if not db_path.exists():
        print(json.dumps({"status": "NOT_FOUND", "reason": str(db_path)}, indent=2))
        return 1
    store = StateStore(db_path)
    try:
        record = store.get_run(request_id)
        events = store.events(request_id)
    finally:
        store.close()
    if not record:
        print(json.dumps({"status": "NOT_FOUND", "request_id": request_id}, indent=2))
        return 1
    if record.get("result_json"):
        record["result"] = json.loads(record["result_json"])
    print(json.dumps({"run": record, "events": events}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _execute(args.request, args)
    if args.command == "resume":
        return _execute(args.request, args, resume=True)
    if args.command == "demo":
        return _demo(args)
    if args.command == "benchmark":
        if args.repeats < 1 or args.repeats > 20:
            parser.error("--repeats must be between 1 and 20")
        report = run_benchmark(args.repeats)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if args.command == "inspect":
        return _inspect(args.project)
    if args.command == "status":
        return _status(args.request_id, args.state_dir)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
