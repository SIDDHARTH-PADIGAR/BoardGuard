# Technical decisions

## One typed operation instead of arbitrary edit tools

The vertical slice supports only `add_silkscreen_text` on a `.kicad_pcb`. It cannot alter electrical connectivity, and the tool parses `Edge.Cuts` coordinates and enforces a margin before writing. Supporting arbitrary model-generated s-expression rewrites before having a complete board parser would enlarge the blast radius and make safety claims weaker.

## One worker, sequential checkpoints

The workflow has a dependency chain: inspect → baseline → mutate → validate → compare → report. Running the same project's stages concurrently would race over a mutable board and produce non-repeatable evidence. Worker fan-out is not justified for a single-file mutation; the benchmark should demonstrate reliable orchestration first.

## Baseline-relative verification

A real KiCad board can carry existing warnings or errors. Success is therefore not defined as “the tool returned zero” or “the project has no violations.” BoardGuard compares stable violation signatures before and after the edit. A new violation causes rollback and human escalation. The report explicitly avoids claiming full electrical correctness.

## Recovery rather than speculative repair

The only automatic recovery is restoring the complete verified pre-edit project snapshot. The snapshot and source manifest are hash-checked; source drift on resume causes a stop and review, and validation is not allowed to mutate the project tree. Retrying validation is bounded; it does not repeat mutation or ask a model to “fix” hardware without a typed, independently verified repair plan. Any new violation or persistent validator failure requires human review.

## Persistent state and idempotency

SQLite records the request digest, run state, checkpoint, report and append-oriented events. A repeated request ID with the same payload reuses the terminal result; the same ID with a different payload is rejected. A resumable run reconstructs its state from persisted artifacts and checks the actual file before continuing.

## Evaluation boundaries

The included benchmark deliberately injects deterministic faults so it can run offline without keys or KiCad. It provides reproducible control-flow evidence only. Real KiCad integration must be run on a machine with a supported `kicad-cli` and a non-toy circuit project before making hardware-validation claims.
