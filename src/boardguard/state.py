from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SECRET_KEY = re.compile(r"(secret|token|password|api.?key|authorization)", re.I)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def redact(value: Any, key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(api[_-]?key\s*[=:]\s*)\S+", r"\1[REDACTED]", value)
        value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._-]+", r"\1[REDACTED]", value)
        return value[:8000]
    return value


class StateStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                request_id TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                checkpoint TEXT NOT NULL,
                workspace TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                result_json TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                stage TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details_json TEXT NOT NULL,
                FOREIGN KEY(request_id) REFERENCES runs(request_id)
            );
            CREATE INDEX IF NOT EXISTS events_by_run ON events(request_id, event_id);
            """
        )

    def close(self) -> None:
        self.db.close()

    def get_run(self, request_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM runs WHERE request_id=?", (request_id,)).fetchone()
        return dict(row) if row else None

    def create_run(self, request_id: str, request_hash: str, workspace: Path) -> None:
        now = utc_now()
        self.db.execute(
            "INSERT INTO runs(request_id,request_hash,status,checkpoint,workspace,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, request_hash, "RUNNING", "CREATED", str(workspace), now, now),
        )
        self.event(request_id, "plan", "run_created", {"workspace": str(workspace), "checkpoint": "CREATED"})

    def reset_approval(self, request_id: str, workspace: Path) -> None:
        now = utc_now()
        self.db.execute("UPDATE runs SET status='RUNNING',checkpoint='CREATED',workspace=?,result_json=NULL,updated_at=? WHERE request_id=?", (str(workspace), now, request_id))
        self.event(request_id, "policy", "approval_granted", {"checkpoint": "CREATED", "workspace": str(workspace)})

    def set_checkpoint(self, request_id: str, checkpoint: str, status: str | None = None) -> None:
        now = utc_now()
        if status is None:
            self.db.execute("UPDATE runs SET checkpoint=?,updated_at=? WHERE request_id=?", (checkpoint, now, request_id))
        else:
            self.db.execute("UPDATE runs SET checkpoint=?,status=?,updated_at=? WHERE request_id=?", (checkpoint, status, now, request_id))
        self.event(request_id, checkpoint.lower(), "checkpoint", {"checkpoint": checkpoint, "status": status or "RUNNING"})

    def save_result(self, request_id: str, result: dict[str, Any], status: str, checkpoint: str = "COMPLETE") -> None:
        now = utc_now()
        self.db.execute(
            "UPDATE runs SET status=?,checkpoint=?,result_json=?,updated_at=? WHERE request_id=?",
            (status, checkpoint, json.dumps(redact(result), sort_keys=True), now, request_id),
        )
        self.event(request_id, "report", "run_finished", {"status": status, "checkpoint": checkpoint})

    def event(self, request_id: str, stage: str, event_type: str, details: Any) -> None:
        # The DB is append-oriented. A failed trace write should fail the run closed rather than hide evidence.
        self.db.execute(
            "INSERT INTO events(request_id,timestamp,stage,event_type,details_json) VALUES(?,?,?,?,?)",
            (request_id, utc_now(), stage, event_type, json.dumps(redact(details), sort_keys=True)),
        )

    def events(self, request_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM events WHERE request_id=? ORDER BY event_id", (request_id,)).fetchall()
        return [dict(row) for row in rows]

    def trace_path(self, request_id: str, dest: Path) -> Path:
        lines = []
        for event in self.events(request_id):
            details = json.loads(event.pop("details_json"))
            lines.append(json.dumps({**event, "details": details}, sort_keys=True))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return dest
