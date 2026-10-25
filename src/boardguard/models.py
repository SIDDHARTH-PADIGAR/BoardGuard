from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$")
SAFE_TEXT_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,32}$")


class RequestError(ValueError):
    """A request is malformed or violates the input contract."""


@dataclass(frozen=True)
class Operation:
    type: str
    file: str
    text: str
    x_mm: float
    y_mm: float
    height_mm: float = 1.0

    @classmethod
    def parse(cls, raw: Any) -> "Operation":
        if not isinstance(raw, dict):
            raise RequestError("operation must be an object")
        allowed = {"type", "file", "text", "x_mm", "y_mm", "height_mm"}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise RequestError(f"unknown operation fields: {', '.join(unknown)}")
        if raw.get("type") != "add_silkscreen_text":
            raise RequestError("only add_silkscreen_text is supported")
        file = raw.get("file")
        if not isinstance(file, str) or not file or Path(file).is_absolute():
            raise RequestError("operation.file must be a relative path")
        path = Path(file)
        if ".." in path.parts or path.suffix != ".kicad_pcb":
            raise RequestError("operation.file must be a project-relative .kicad_pcb path without '..'")
        text = raw.get("text")
        if not isinstance(text, str) or not SAFE_TEXT_RE.fullmatch(text):
            raise RequestError("text must be 1-32 safe ASCII characters: letters, digits, space, dot, underscore, hyphen")
        try:
            x_mm, y_mm = float(raw["x_mm"]), float(raw["y_mm"])
            height = float(raw.get("height_mm", 1.0))
        except (KeyError, TypeError, ValueError) as exc:
            raise RequestError("x_mm and y_mm must be numbers") from exc
        if not all(map(lambda n: n == n and abs(n) < 10_000, (x_mm, y_mm, height))):
            raise RequestError("coordinates must be finite and within 10000 mm")
        if not 0.5 <= height <= 2.5:
            raise RequestError("height_mm must be between 0.5 and 2.5 mm")
        return cls(type="add_silkscreen_text", file=file, text=text, x_mm=x_mm, y_mm=y_mm, height_mm=height)


@dataclass(frozen=True)
class ChangeRequest:
    schema_version: int
    request_id: str
    project_dir: str
    operation: Operation
    approved: bool
    max_retries: int = 1
    acceptance_text_present: bool = True

    @classmethod
    def from_dict(cls, raw: Any) -> "ChangeRequest":
        if not isinstance(raw, dict):
            raise RequestError("request root must be an object")
        allowed = {"schema_version", "request_id", "project_dir", "operation", "approval", "max_retries", "acceptance"}
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise RequestError(f"unknown request fields: {', '.join(unknown)}")
        if raw.get("schema_version") != 1:
            raise RequestError("schema_version must be 1")
        request_id = raw.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
            raise RequestError("request_id must be 3-80 safe characters")
        project_dir = raw.get("project_dir")
        if not isinstance(project_dir, str) or not project_dir:
            raise RequestError("project_dir must be a non-empty path")
        approval = raw.get("approval", {})
        if not isinstance(approval, dict) or set(approval) - {"approved", "reason"}:
            raise RequestError("approval must contain only approved and reason")
        approved = approval.get("approved", False)
        if not isinstance(approved, bool):
            raise RequestError("approval.approved must be a boolean")
        retries = raw.get("max_retries", 1)
        if not isinstance(retries, int) or isinstance(retries, bool) or retries not in (0, 1, 2):
            raise RequestError("max_retries must be 0, 1, or 2")
        acceptance = raw.get("acceptance", {})
        if not isinstance(acceptance, dict) or set(acceptance) - {"text_present"}:
            raise RequestError("acceptance may contain only text_present")
        text_present = acceptance.get("text_present", True)
        if not isinstance(text_present, bool) or text_present is not True:
            raise RequestError("this operation requires acceptance.text_present=true")
        return cls(1, request_id, project_dir, Operation.parse(raw.get("operation")), approved, retries, text_present)

    @classmethod
    def load(cls, path: Path) -> tuple["ChangeRequest", Path]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RequestError(f"cannot read request JSON: {exc}") from exc
        return cls.from_dict(raw), path.resolve().parent

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


@dataclass
class Violation:
    severity: str
    rule: str
    message: str
    identity: str = ""

    def signature(self) -> str:
        return self.identity or f"{self.severity}|{self.rule}|{self.message}"


@dataclass
class ValidationReport:
    available: bool
    tool: str
    exit_code: int | None
    violations: list[Violation] = field(default_factory=list)
    duration_ms: float = 0.0
    error: str | None = None
    raw_report_path: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: dict[str, Any] | None = None

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in self.violations:
            result[item.severity] = result.get(item.severity, 0) + 1
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "tool": self.tool,
            "exit_code": self.exit_code,
            "violations": [asdict(v) for v in self.violations],
            "counts": self.counts(),
            "duration_ms": round(self.duration_ms, 3),
            "error": self.error,
            "raw_report_path": self.raw_report_path,
            "tool_input": self.tool_input,
            "tool_output": self.tool_output,
        }


@dataclass
class RunResult:
    request_id: str
    status: str
    checkpoint: str
    project_preserved: bool
    changed_files: list[str] = field(default_factory=list)
    new_violations: list[str] = field(default_factory=list)
    baseline: dict[str, Any] | None = None
    final_validation: dict[str, Any] | None = None
    recovery: str = "none"
    human_review_required: bool = False
    reason: str | None = None
    report_path: str | None = None
    workspace: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
