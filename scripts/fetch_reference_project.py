#!/usr/bin/env python3
"""Fetch a pinned, Apache-2.0 real KiCad reference project and verify Git blob hashes."""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPOSITORY = "copperheadhq/copperbench"
COMMIT = "09863d77aa87b86c8764db73964b256c7ce8b020"
FIXTURE_ROOT = "fixtures/antmicro-microphone-board"
DESTINATION = Path(__file__).resolve().parents[1] / "examples" / "antmicro_microphone"

# Blob hashes are from the pinned Git tree. Files are copied byte-for-byte.
FILES = {
    f"{FIXTURE_ROOT}/tree/hardware/microphone-board.kicad_pcb": (
        "hardware/microphone-board.kicad_pcb", "774e34f1c140808eddca77e3780ad51a49a8d81b"
    ),
    f"{FIXTURE_ROOT}/tree/hardware/microphone-board.kicad_pro": (
        "hardware/microphone-board.kicad_pro", "3a4c7dd159f5b424d7b1fd4b778e2b5ac330d307"
    ),
    f"{FIXTURE_ROOT}/tree/hardware/microphone-board.kicad_sch": (
        "hardware/microphone-board.kicad_sch", "9ad66363728b09db2e8f4f6b88e18138dff3995f"
    ),
    f"{FIXTURE_ROOT}/tree/hardware/microphone-board.kicad_dru": (
        "hardware/microphone-board.kicad_dru", "5ee7400f65982515d873a8d4bc59b1c2fb589d20"
    ),
    f"{FIXTURE_ROOT}/LICENSE": ("LICENSE-ANTMICRO.txt", "57bc88a15a0ee8266c259b2667e64608d3f7e292"),
}

ATTRIBUTION = f"""Reference project attribution

Upstream project: https://github.com/antmicro/pdm-microphone-board
Upstream commit: 20813e2bc99f52837a780334b16a30c2afb7644c
Author: Antmicro, Copyright (c) 2024-2025
License: Apache-2.0 (full text in LICENSE-ANTMICRO.txt)
Source fixture: https://github.com/{REPOSITORY}/tree/{COMMIT}/{FIXTURE_ROOT}
Source pinned Copperbench commit: {COMMIT}

The KiCad design files are fetched byte-for-byte from Copperbench's pinned
fixture. This script checks their Git blob object hashes before writing them.
BoardGuard does not claim authorship of the hardware design.
"""


def git_blob_sha(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def fetch(url: str, timeout: float = 25.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "BoardGuard-reference-fixture/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} downloading {url}")
        payload = response.read()
    if not payload:
        raise RuntimeError(f"empty download: {url}")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="replace existing files after hash verification")
    parser.add_argument("--destination", type=Path, default=DESTINATION, help="output folder")
    args = parser.parse_args()
    destination = args.destination.resolve()
    existing = [destination / rel for rel, _ in FILES.values() if (destination / rel).exists()]
    if existing and not args.force:
        parser.error("destination already contains reference files; pass --force to replace them: " + ", ".join(map(str, existing)))

    staged: list[tuple[Path, bytes]] = []
    for source_path, (relative_target, expected_sha) in FILES.items():
        url = f"https://raw.githubusercontent.com/{REPOSITORY}/{COMMIT}/{source_path}"
        try:
            payload = fetch(url)
        except (OSError, urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        actual_sha = git_blob_sha(payload)
        if actual_sha != expected_sha:
            print(f"ERROR: blob hash mismatch for {source_path}: expected {expected_sha}, got {actual_sha}", file=sys.stderr)
            return 2
        staged.append((destination / relative_target, payload))
        print(f"verified {relative_target} ({len(payload):,} bytes, git blob {actual_sha})")

    # Only write after every artifact has passed its hash check.
    for path, payload in staged:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "SOURCE.txt").write_text(ATTRIBUTION, encoding="utf-8")
    print(f"\nVerified reference project written to: {destination}")
    print("No schematic or board design file was modified.")
    print("KiCad validation still needs to be run separately with kicad-cli.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
