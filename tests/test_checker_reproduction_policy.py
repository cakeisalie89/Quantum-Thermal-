"""package_consistency_check.py asks the question its policy names, end to end.

Stub generators in a scratch checkout, the real checker in a subprocess.
Which machinery the scratch carries decides what the checker can know:

* no reproduction machinery at all -> strict refuses before regenerating;
* the machinery but no instrumented wrapper -> the regenerating process's
  backend was never recorded, so it is UNRESOLVED, and a byte difference is
  refused rather than read as "another machine";
* the wrapper too -> the backend is resolved; with no witness profile a byte
  difference is refused as WITNESS_INVALID -- never as "stale root copies".

The real-corpus cases (the witnessed backend reproducing; a deliberately
different dispatch passing only as decision-stable drift) run in CI's
full-suite and dispatch-sensitivity jobs, where their cost belongs.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKER = "package_consistency_check.py"

_STUB = """import os
os.makedirs("outputs", exist_ok=True)
for n in {names!r}:
    with open(os.path.join("outputs", n), "w") as fh:
        fh.write({content!r})
print("stub simulation wrote", len({names!r}), "file(s)")
"""


def _declared():
    sys.path.insert(0, str(ROOT / "tools"))
    import cross_env_semantics
    declared, _ = cross_env_semantics.declared_scope(ROOT)
    return sorted(declared)


def _scratch(tmp_path, *, machinery=True, wrapper=True, output="{}",
             root_copy="{}"):
    d = tmp_path / "wk"
    d.mkdir()
    shutil.copy2(ROOT / CHECKER, d)
    names = _declared()
    (d / "qta_full_sim.py").write_text(
        _STUB.format(names=names, content=output), encoding="utf-8")
    for n in names:
        (d / n).write_text(root_copy)
    if machinery:
        shutil.copytree(ROOT / "scientific", d / "scientific",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (d / "tools").mkdir()
        for t in ("cross_env_semantics.py", "resolution_inventory.py",
                  "reproduction_witness.py") + (
                      ("regenerate_instrumented.py",) if wrapper else ()):
            shutil.copy2(ROOT / "tools" / t, d / "tools" / t)
        (d / "docs").mkdir()
        shutil.copy2(ROOT / "docs" / "resolution_inventory.json",
                     d / "docs" / "resolution_inventory.json")
    return d


def _run(d, *args, timeout=300):
    return subprocess.run([sys.executable, CHECKER, *args], cwd=d,
                          capture_output=True, text=True, timeout=timeout,
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))


def _called_stale(r) -> bool:
    """Whether the checker named its outputs stale -- the words that are
    right only for drift on a witnessed backend."""
    return "stale root copies" in r.stdout or "stale or regressed" in r.stdout


def _line(r):
    lines = [ln for ln in r.stdout.splitlines()
             if "REPRODUCTION_VERDICT" in ln]
    assert lines, r.stdout[-3000:]
    return lines[-1]


def test_strict_is_the_default_and_refuses_before_regenerating(tmp_path):
    d = _scratch(tmp_path, machinery=False)
    r = _run(d)
    assert r.returncode != 0
    assert "Traceback" not in r.stderr, r.stderr[-1500:]
    line = _line(r)
    assert "policy=strict-reproduction" in line
    assert "REPRODUCTION_STATUS=CANONICAL_REPRODUCTION_ENVIRONMENT_REQUIRED" \
        in line
    assert not (d / "outputs").exists(), "nothing was regenerated"
    assert not _called_stale(r)


def test_strict_refuses_a_tree_no_witness_applies_to(tmp_path):
    """The machinery is here but no profile for these bytes: the release
    question cannot be asked on this tree, and says so early."""
    d = _scratch(tmp_path)
    r = _run(d, "--policy", "strict-reproduction")
    assert r.returncode != 0 and "Traceback" not in r.stderr
    assert "CANONICAL_REPRODUCTION_ENVIRONMENT_REQUIRED" in _line(r)
    assert "no witness profile" in r.stdout
    assert not (d / "outputs").exists()


def test_an_unknown_policy_is_refused(tmp_path):
    d = _scratch(tmp_path, machinery=False)
    r = _run(d, "--policy", "lenient")
    assert r.returncode == 2 and "not 'ci' or 'strict-reproduction'" in \
        r.stdout


def test_a_regeneration_nobody_recorded_is_backend_unresolved(tmp_path):
    """No wrapper: the regenerating process's identity is unknown, and the
    VERIFYING process's is not borrowed for it."""
    d = _scratch(tmp_path, wrapper=False, output='{"x": 1.0}',
                 root_copy='{"x": 2.0}')
    r = _run(d, "--policy", "ci")
    assert r.returncode != 0 and "Traceback" not in r.stderr
    line = _line(r)
    assert "BACKEND_STATUS=UNRESOLVED" in line
    assert "REPRODUCTION_STATUS=BACKEND_UNRESOLVED" in line
    assert not _called_stale(r)


def test_drift_without_a_witness_is_refused_and_not_called_stale(tmp_path):
    d = _scratch(tmp_path, output='{"x": 1.0}', root_copy='{"x": 2.0}')
    r = _run(d, "--policy", "ci")
    assert r.returncode != 0 and "Traceback" not in r.stderr
    line = _line(r)
    assert "BACKEND_STATUS=RESOLVED" in line
    assert "REPRODUCTION_STATUS=WITNESS_INVALID" in line
    assert "CROSS_ENV_STATUS=NOT_CHECKED" in line
    assert not _called_stale(r)


def test_identical_bytes_are_byte_identical_in_ci(tmp_path):
    """The positive control for the verdict: the stubs reproduce their root
    copies exactly (the checker's OTHER steps fail on stub content; the
    reproduction verdict is what is asserted here)."""
    d = _scratch(tmp_path)
    r = _run(d, "--policy", "ci", "--summary-json", str(tmp_path / "v.json"))
    line = _line(r)
    assert "PACKAGE_STATUS=CONSISTENT" in line
    assert "REPRODUCTION_STATUS=BYTE_IDENTICAL" in line
    assert "files_differing=0" in line
    import json
    v = json.loads((tmp_path / "v.json").read_text())
    assert v["REPRODUCTION_STATUS"] == "BYTE_IDENTICAL"
    assert v["files_byte_compared"] == len(_declared()) - 1


def test_verify_existing_without_a_generation_record_is_unresolved(tmp_path):
    d = _scratch(tmp_path, root_copy='{"x": 2.0}')
    out = d / "outputs"
    out.mkdir()
    for n in _declared():
        (out / n).write_text('{"x": 1.0}')
    log = d / "sim.log"
    log.write_text("clean run\n")
    r = _run(d, "--verify-existing", "--sim-log", str(log), "--policy", "ci")
    assert r.returncode != 0 and "Traceback" not in r.stderr
    assert "REPRODUCTION_STATUS=BACKEND_UNRESOLVED" in _line(r)
