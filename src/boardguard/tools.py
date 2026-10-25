from __future__ import annotations

import hashlib
import re
import shutil
import uuid
from pathlib import Path

from .models import Operation, RequestError


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part == ".history" for part in relative.parts) or path.name.endswith(".kicad_prl") or path.name.endswith(".lck") or path.name == "fp-info-cache":
            continue
        if path.is_symlink():
            # Symlinks are never followed for hashing. Recording them makes an
            # unexpected link addition/removal visible to the diff guard.
            result[relative.as_posix()] = "SYMLINK:" + str(path.readlink())
        elif path.is_file():
            result[relative.as_posix()] = sha256_file(path)
    return result


def _balanced_sexpr_block(text: str, start: int) -> str | None:
    """Return one balanced s-expression, respecting quoted strings and escapes."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def board_bounds(board_text: str) -> tuple[float, float, float, float]:
    """Read approximate bounds from Edge.Cuts rectangles, lines, and arcs.

    Arc bounds use their start, midpoint, and end coordinates.
    This is a bounding-box check, not exact outline geometry.
    """
    values: list[tuple[float, float]] = []

    geometry_re = re.compile(r"\((gr_rect|gr_line|gr_arc)\b")
    point_re = re.compile(
        r"\((start|mid|end)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\)"
    )
    edge_layer_re = re.compile(r'\(layer\s+"Edge\.Cuts"\)')

    for match in geometry_re.finditer(board_text):
        block = _balanced_sexpr_block(board_text, match.start())

        if block is None or not edge_layer_re.search(block):
            continue

        points = [
            (float(x), float(y))
            for _, x, y in point_re.findall(block)
        ]

        if len(points) >= 2:
            values.extend(points)

    if len(values) < 2:
        raise RequestError(
            "could not find a supported Edge.Cuts outline; refusing to guess placement bounds"
        )

    xs = [point[0] for point in values]
    ys = [point[1] for point in values]

    bounds = (min(xs), min(ys), max(xs), max(ys))

    if bounds[2] - bounds[0] < 5 or bounds[3] - bounds[1] < 5:
        raise RequestError("board outline is too small or malformed")

    return bounds




def annotation_matches(board_text: str, op: Operation) -> bool:
    """True only when the requested label exists once at the requested layer, size, and position."""
    number = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
    label_re = re.compile(r'\(gr_text\s+"' + re.escape(op.text) + r'"(?=\s|\))')
    found = 0
    exact = 0
    for match in label_re.finditer(board_text):
        block = _balanced_sexpr_block(board_text, match.start())
        if block is None:
            continue
        found += 1
        at = re.search(r"\(at\s+(" + number + r")\s+(" + number + r")(?:\s+(" + number + r"))?\s*\)", block)
        layer = re.search(r'\(layer\s+"([^"]+)"\)', block)
        font = re.search(r"\(font\s+\(size\s+(" + number + r")\s+(" + number + r")\s*\)", block)
        if not at or not layer or not font:
            continue
        x, y = float(at.group(1)), float(at.group(2))
        width, height = float(font.group(1)), float(font.group(2))
        if (layer.group(1) == "F.SilkS" and abs(x - op.x_mm) < 1e-6
                and abs(y - op.y_mm) < 1e-6 and abs(width - op.height_mm) < 1e-6
                and abs(height - op.height_mm) < 1e-6):
            exact += 1
    return found == 1 and exact == 1


def add_silkscreen_text(board_path: Path, op: Operation, request_id: str) -> dict[str, str | int | float]:
    """Typed edit tool: add a non-electrical F.Silkscreen annotation inside the board outline."""
    text = board_path.read_text(encoding="utf-8")
    label_pattern = re.compile(r'\(gr_text\s+"' + re.escape(op.text) + r'"(?=\s|\))')
    if label_pattern.search(text):
        # Idempotency only applies when the persisted object matches the full request.
        # Same text at another position/layer/size is ambiguous and must not pass.
        if annotation_matches(text, op):
            return {"action": "already_present", "file": op.file, "text": op.text}
        raise RequestError("same label already exists with different position, layer, size, or duplicate count")
    bounds = board_bounds(text)
    min_x, min_y, max_x, max_y = bounds
    estimated_width = max(op.height_mm * 0.8, len(op.text) * op.height_mm * 0.62)
    if not (min_x + estimated_width / 2 + 1.0 <= op.x_mm <= max_x - estimated_width / 2 - 1.0):
        raise RequestError("text position leaves the board outline or its 1 mm safety margin")
    if not (min_y + op.height_mm / 2 + 1.0 <= op.y_mm <= max_y - op.height_mm / 2 - 1.0):
        raise RequestError("text position leaves the board outline or its 1 mm safety margin")
    if not text.rstrip().endswith(")"):
        raise RequestError("target board does not end with a closing s-expression")
    object_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"boardguard:{request_id}:{op.file}:{op.text}"))
    quoted = (
        f'  (gr_text "{op.text}" (at {op.x_mm:g} {op.y_mm:g}) (layer "F.SilkS") '
        f'(uuid "{object_uuid}") (effects (font (size {op.height_mm:g} {op.height_mm:g}) '
        f'(thickness 0.15))))\n'
    )
    closing_index = text.rfind(")")
    edited = text[:closing_index] + quoted + text[closing_index:]
    board_path.write_text(edited, encoding="utf-8")
    # Verify the persisted outcome, not only the success code from write_text.
    persisted = board_path.read_text(encoding="utf-8")
    if persisted.count(f'(gr_text "{op.text}"') != 1 or object_uuid not in persisted:
        raise OSError("persisted board does not contain exactly one requested annotation")
    return {
        "action": "added",
        "file": op.file,
        "text": op.text,
        "x_mm": op.x_mm,
        "y_mm": op.y_mm,
        "height_mm": op.height_mm,
        "uuid": object_uuid,
        "bytes_written": len(edited.encode("utf-8")),
    }


def copy_project(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"workspace already exists: {destination}")

    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name.endswith(".kicad_prl") or name.endswith(".lck") or name == ".history" or name == "fp-info-cache"}

    shutil.copytree(source, destination, symlinks=False, ignore=ignore)
