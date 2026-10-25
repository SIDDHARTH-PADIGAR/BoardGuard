# BoardGuard demo board

This is a self-authored, board-only KiCad PCB fixture distributed under the repository's MIT license. It contains an explicit Edge.Cuts rectangle and an existing silkscreen annotation so the demo can exercise the typed text-placement tool without any netlist or component edits.

It is a software test fixture, not a functional electronic circuit. A successful DRC run on this fixture does not establish electrical correctness, manufacturing readiness, or schematic/PCB consistency. Use `boardguard run` with a real project directory for those checks.
