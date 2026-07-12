"""Deterministic output writers for the deep layer.

JSON: sorted keys, ensure_ascii, trailing newline. CSV: fixed float format, LF
newlines, header first. Every payload is tagged forecast-only / simulation-
generated via the caller's schema. Determinism is required wherever
mathematically possible.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

_FLOAT = "{:.10e}"


def fmt(x) -> str:
    if x is None:
        return ""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, float):
        if math.isnan(x):
            return "nan"
        if math.isinf(x):
            return "inf" if x > 0 else "-inf"
        return _FLOAT.format(x)
    return str(x)


def write_json(path: Path, obj: dict) -> None:
    Path(path).write_text(
        json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8")


def write_csv(path: Path, header: list, rows: list) -> None:
    lines = [",".join(header)]
    for r in rows:
        lines.append(",".join(fmt(v) for v in r))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def deep_output_dir(repo_root: Optional[Path] = None) -> Path:
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    d = root / "outputs" / "deep_expdesign"
    d.mkdir(parents=True, exist_ok=True)
    return d
