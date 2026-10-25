from __future__ import annotations

import re
from pathlib import Path

from .models import ChangeRequest, RequestError

MAX_PROJECT_BYTES = 80 * 1024 * 1024
MAX_PROJECT_FILES = 2_000
MAX_BOARD_BYTES = 20 * 1024 * 1024


def inspect_project(source: Path, request: ChangeRequest) -> tuple[Path, list[Path]]:
    """Validate project paths and project shape before copying or editing anything."""
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise RequestError(f"project directory does not exist: {source}") from exc
    if not resolved.is_dir() or source.is_symlink():
        raise RequestError("project_dir must be a real directory, not a symlink")
    files: list[Path] = []
    total_bytes = 0
    for path in resolved.rglob("*"):
        if path.is_symlink():
            raise RequestError(f"symlinks are not allowed in project input: {path.relative_to(resolved)}")
        if path.is_file():
            files.append(path)
            total_bytes += path.stat().st_size
            if len(files) > MAX_PROJECT_FILES:
                raise RequestError(f"project exceeds {MAX_PROJECT_FILES} files")
            if total_bytes > MAX_PROJECT_BYTES:
                raise RequestError("project exceeds the 80 MiB copy limit")
    target = (resolved / request.operation.file).resolve()
    try:
        target.relative_to(resolved)
    except ValueError as exc:
        raise RequestError("target path escapes the project directory") from exc
    if not target.is_file() or target.is_symlink():
        raise RequestError(f"target board file does not exist: {request.operation.file}")
    if target.stat().st_size > MAX_BOARD_BYTES:
        raise RequestError("target board exceeds the 20 MiB edit limit")
    try:
        header = target.read_text(encoding="utf-8")[:512]
    except UnicodeDecodeError as exc:
        raise RequestError("target board is not UTF-8 text") from exc
    if not re.search(r"^\s*\(kicad_pcb\b", header):
        raise RequestError("target is not recognizably a KiCad PCB s-expression")
    return resolved, files


def require_approved(request: ChangeRequest, cli_approved: bool) -> None:
    # Approval is intentionally enforced in deterministic code, outside any model/tool result.
    if not (request.approved and cli_approved):
        raise PermissionError("modification requires approval in request JSON and an explicit --approve flag")
