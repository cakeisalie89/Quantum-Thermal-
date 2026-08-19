"""Output-location guard and deterministic-writer helpers for Stage 10.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

The canonical outputs of this project live at the repository root and are
byte-gated against regeneration by ``package_consistency_check.py``. Stage-10
adapters are additive and must never write there, so every Stage-10 writer
routes its output directory through :func:`guard_output_dir`, which fails
closed on anything that resolves to the repository root or to a tracked
canonical directory.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Union

# Anything accepted as a filesystem location by the Stage-10 writers.
StrPath = Union[str, "os.PathLike[str]"]

DEFAULT_WORKSPACE = Path("verification/stage10")

# Directories that hold governed/canonical state. Stage-10 never writes here.
PROTECTED_DIRS = ("ro-crate", "qta_multiphysics", "tests",
                  "EXPERIMENT_PLAYBOOKS", "QTA_stage9_release_verification",
                  "stage7_reports", "stage8_reports", "matrix_update_examples",
                  ".github", "attic")


def repo_root() -> Path:
    """Repository root, resolved from this file's location (no cwd guessing)."""
    return Path(__file__).resolve().parents[2]


def guard_output_dir(out_dir: StrPath) -> Path:
    """Resolve ``out_dir`` and refuse canonical/governed write locations.

    Fails closed: the repository root itself, any tracked canonical
    subdirectory, and any path outside the repository are all rejected.
    """
    root = repo_root()
    p = Path(out_dir).expanduser()
    p = (root / p) if not p.is_absolute() else p
    p = p.resolve()
    if p == root:
        raise ValueError(
            "Stage-10 writers may not write into the canonical repository "
            f"root ({root}); pass a workspace directory such as "
            f"{DEFAULT_WORKSPACE}")
    try:
        rel = p.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"Stage-10 output directory must stay inside the repository: {p}"
        ) from exc
    top = rel.parts[0] if rel.parts else ""
    if top in PROTECTED_DIRS:
        raise ValueError(
            f"'{top}/' holds governed state; Stage-10 output may not be "
            f"written there (requested {rel})")
    p.mkdir(parents=True, exist_ok=True)
    return p


def sha256_file(path: StrPath) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_text_deterministic(path: StrPath, text: str) -> str:
    """Write ``text`` with LF endings and no trailing whitespace drift.

    Returns the SHA-256 of the bytes written, so callers can record artifact
    provenance without re-reading the file.
    """
    data = text.replace("\r\n", "\n").encode("utf-8")
    Path(path).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def write_json_deterministic(path: StrPath, obj: Any) -> str:
    """Sorted-key, fixed-indent, newline-terminated JSON (stable bytes)."""
    text = json.dumps(obj, indent=2, sort_keys=True,
                      ensure_ascii=True, allow_nan=False) + "\n"
    return write_text_deterministic(path, text)


def relpath_in_repo(path: StrPath) -> str:
    """POSIX repo-relative path — keeps manifests free of host-specific paths."""
    p = Path(path).resolve()
    try:
        return p.relative_to(repo_root()).as_posix()
    except ValueError:
        return Path(os.path.basename(p)).as_posix()
