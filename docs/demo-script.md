# Five-minute demo

1. **Show the offline path.** Run `boardguard demo`. Point out the explicit `SIMULATED` label; no KiCad result is implied.
2. **Inspect evidence.** Open the reported run directory and show `plan.json`, `baseline.json`, `changes.patch`, `trace.jsonl`, `report.json`, and `checkpoints/project-before/`.
3. **Show failure recovery.** Run `boardguard benchmark --repeats 3 --output evaluation/latest.json`. Open `evaluation/latest.json` and compare rollback, leftover-violation, idempotency, escalation, and latency metrics.
4. **Run tests.** `python -m pytest -q` exercises malformed requests, approval gates, failure injection, crash/resume, source drift, file deletions, validator schema checks, and rollback.
5. **Optional real hardware path.** Install KiCad 8+ and ensure `kicad-cli` is on `PATH`; run `python scripts/fetch_reference_project.py`, then `boardguard run examples/requests/antmicro_add_silk.json --backend kicad --approve`. Inspect the actual ERC/DRC reports and new-violation comparison. Do not call the board electrically correct merely because the command completes.

**Narration:** “BoardGuard does not design the circuit. It constrains one edit, records the plan, checks the starting state, edits a copy, runs validation, verifies what persisted, and either reports the evidence or restores the complete checkpoint. Our benchmark is fault-injection evidence, not hardware validation.”
