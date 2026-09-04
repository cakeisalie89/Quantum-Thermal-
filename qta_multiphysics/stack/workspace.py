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
from typing import Any, NoReturn, Union

# Anything accepted as a filesystem location by the Stage-10 writers.
StrPath = Union[str, "os.PathLike[str]"]

DEFAULT_WORKSPACE = Path("verification/stage10")

# Historical note, kept because the replacement is the point: this module once
# carried a DENYLIST of governed directories, and everything not named in it was
# writable. Two things were wrong with that.
#
# First, absence of a rule became permission -- `outputs/`, `docs/`, `ro-crate`'s
# neighbours and the repository's loose canonical files at the root were all
# writable because nobody had thought to list them.
#
# Second, and worse, the guard only ever ran on DIRECTORIES. The writers below
# took any path at all and called `write_bytes` on it, so the guard was advisory:
# a caller who forgot `guard_output_dir` wrote wherever it liked. A recovered
# adversarial test proved this was not theoretical -- it overwrote README.md and
# wrote `{"status": "PASS"}` into results_gate_table.csv, the canonical gate
# table, in the course of a normal test run.
#
# The replacement is an allowlist enforced at the point of the write. Stage-10
# may write inside its workspace and nowhere else; every other location is
# refused because it was not permitted, not because it was remembered.
PROTECTED_DIRS = ("ro-crate", "qta_multiphysics", "tests",
                  "EXPERIMENT_PLAYBOOKS", "QTA_stage9_release_verification",
                  "stage7_reports", "stage8_reports", "matrix_update_examples",
                  ".github", "attic")


def workspace_root() -> Path:
    """The ONE subtree Stage-10 may write into, fully resolved."""
    return (repo_root() / DEFAULT_WORKSPACE).resolve()


def _refuse(target: Path, what: str) -> "NoReturn":
    raise ValueError(
        f"Stage-10 {what} must stay inside {DEFAULT_WORKSPACE}/; refusing "
        f"{target}. This is an allowlist: a location is writable because it "
        "was permitted, never because it was not listed as protected.")


def assert_in_workspace(path: StrPath, *, what: str = "writes") -> Path:
    """Resolve ``path`` and require the result to be inside the workspace.

    ``resolve()`` follows symlinks, which is the entire reason this is the
    check rather than a string comparison: a link created inside the
    workspace that points at a canonical file resolves to that file and is
    refused. Comparing the unresolved path would accept it.
    """
    root = repo_root()
    p = Path(path).expanduser()
    p = (root / p) if not p.is_absolute() else p
    p = p.resolve()
    ws = workspace_root()
    if p != ws and ws not in p.parents:
        _refuse(p, what)
    return p


def repo_root() -> Path:
    """Repository root, resolved from this file's location (no cwd guessing)."""
    return Path(__file__).resolve().parents[2]


def guard_output_dir(out_dir: StrPath) -> Path:
    """Resolve ``out_dir``, require it inside the workspace, and create it.

    Fails closed. The repository root, every governed subtree, every path
    outside the repository, and every directory symlink that escapes the
    workspace are all refused -- not by enumeration, but because none of them
    is inside the one place Stage-10 is allowed to write.
    """
    p = assert_in_workspace(out_dir, what="output directories")
    p.mkdir(parents=True, exist_ok=True)
    return p


def sha256_file(path: StrPath) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_text_deterministic(path: StrPath, text: str) -> str:
    """Write ``text`` with LF endings and no trailing whitespace drift.

    Returns the SHA-256 of the bytes written, so callers can record artifact
    provenance without re-reading the file.
    """
    target = assert_in_workspace(path)
    data = text.replace("\r\n", "\n").encode("utf-8")
    target.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def write_json_deterministic(path: StrPath, obj: Any) -> str:
    """Sorted-key, fixed-indent, newline-terminated JSON (stable bytes)."""
    text = json.dumps(obj, indent=2, sort_keys=True,
                      ensure_ascii=True, allow_nan=False) + "\n"
    return write_text_deterministic(path, text)


def relpath_in_repo(path: StrPath) -> str:
    """POSIX repo-relative path — keeps manifests free of host-specific paths.

    Refuses a path outside the repository rather than falling back to its
    basename. The fallback looked harmless and was not: it laundered
    ``/etc/secret.md`` into ``secret.md``, so a provenance record would name a
    repository-relative file that is not the file the bytes came from. A
    manifest entry that names the wrong file is worse than one that is
    missing, because it reads as evidence.
    """
    p = Path(path).resolve()
    try:
        return p.relative_to(repo_root()).as_posix()
    except ValueError as exc:
        raise ValueError(
            f"{p} is outside the repository; refusing to record it as a "
            "repository-relative path") from exc
