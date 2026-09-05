"""The mutation harness, tested as the instrument it is.

Every claim in `authorities.json` about enforcement being load-bearing is a
claim produced by `tools/mutation_matrix.py`. If the harness reports a kill
that did not happen, the effect is not a wrong number in a table: it is an
unprotected security check certified as protected. So the harness gets the
same treatment it gives everything else -- its own failure modes, provoked
deliberately, with the outcome asserted.

Each test builds a throwaway project tree and runs the harness against it as a
subprocess. The harness derives its root from its own location, so copying it
into ``tmp_path/tools/`` is enough to point it at the synthetic tree; nothing
here touches the real repository.
"""
from __future__ import annotations

import importlib.util
import json
import marshal
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tools" / "mutation_matrix.py"

#: Two guards of identical length. Mutating either to ``if 0:`` removes the
#: same number of bytes, so both mutated files have the SAME size -- which is
#: half of CPython's default bytecode-invalidation key.
MODULE_SRC = '''"""A tiny module with two guards."""


def check_a(x):
    if x < 0:
        raise ValueError("a")
    return x


def check_b(x):
    if x > 9:
        raise ValueError("b")
    return x
'''

SUITE_SRC = '''import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from pkg import mod


def test_guard_a_rejects():
    with pytest.raises(ValueError):
        mod.check_a(-1)


def test_both_accept_valid():
    assert mod.check_a(1) == 1
    assert mod.check_b(1) == 1
'''


def _project(tmp_path: Path, *, module_src: str = MODULE_SRC,
             suite_src: str = SUITE_SRC, mutations=None) -> Path:
    """A minimal tree the harness can be pointed at."""
    (tmp_path / "tools" / "mutations").mkdir(parents=True)
    shutil.copy2(HARNESS, tmp_path / "tools" / "mutation_matrix.py")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "mod.py").write_text(module_src, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_suite.py").write_text(suite_src,
                                                      encoding="utf-8")
    spec = {
        "title": "synthetic",
        "note": "built by tests/test_mutation_harness.py",
        "suites": ["tests/test_suite.py"],
        "mutations": mutations if mutations is not None else [
            {"name": "M1_guard_a_removed", "path": "pkg/mod.py",
             "find": "    if x < 0:", "replace": "    if 0:",
             "rationale": "guard a stops guarding"},
            {"name": "M2_guard_b_removed", "path": "pkg/mod.py",
             "find": "    if x > 9:", "replace": "    if 0:",
             "rationale": "guard b stops guarding"},
        ],
    }
    path = tmp_path / "tools" / "mutations" / "spec.json"
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path


def _run(tmp_path: Path, spec: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(tmp_path / "tools" / "mutation_matrix.py"),
         str(spec)],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=600)


def _verdicts(out: str) -> dict:
    verdicts = {}
    for line in out.splitlines():
        for word in ("KILLED", "SURVIVED", "ANCHOR DRIFT"):
            if f"  {word}" in line:
                verdicts[line.split()[0]] = word
                break
    return verdicts


# ---- the two guards it is built to give ---------------------------------
def test_an_uncovered_mutation_is_reported_as_surviving(tmp_path):
    """The primary signal. If this cannot fail, nothing else here matters."""
    spec = _project(tmp_path)
    proc = _run(tmp_path, spec)
    v = _verdicts(proc.stdout)
    assert v == {"M1_guard_a_removed": "KILLED",
                 "M2_guard_b_removed": "SURVIVED"}, proc.stdout
    assert proc.returncode != 0, "a surviving mutation must fail the run"


def test_sources_are_restored_byte_identical(tmp_path):
    spec = _project(tmp_path)
    before = (tmp_path / "pkg" / "mod.py").read_bytes()
    _run(tmp_path, spec)
    assert (tmp_path / "pkg" / "mod.py").read_bytes() == before


# ---- the stale-bytecode trap -------------------------------------------
def _poison_cache(tmp_path: Path, source: str) -> Path:
    """Plant an UNCHECKED hash-based ``.pyc`` holding ``source``'s code.

    PEP 552's unchecked hash-based pyc is validated against nothing at all, so
    it stands in deterministically for the real defect: a cached module that
    Python executes in place of the source the harness just wrote. The real
    version was subtler -- a timestamp pyc whose recorded (mtime, size) still
    matched, because two mutations changed the file by the same number of
    bytes within one second -- but the consequence is identical, and this
    form does not depend on the clock.
    """
    modpath = tmp_path / "pkg" / "mod.py"
    cache = Path(importlib.util.cache_from_source(str(modpath)))
    cache.parent.mkdir(parents=True, exist_ok=True)
    raw = source.encode("utf-8")
    code = compile(raw, str(modpath), "exec")
    cache.write_bytes(
        importlib.util.MAGIC_NUMBER
        + (0b01).to_bytes(4, "little")          # hash-based, UNCHECKED
        + importlib.util.source_hash(raw)
        + marshal.dumps(code))
    return cache


def test_a_stale_cache_cannot_make_a_mutation_look_killed(tmp_path):
    """The defect this test exists for, reproduced and then refused.

    A cached module holding the ORIGINAL code makes every mutation invisible:
    the suite passes because the guard is still there, and the harness reports
    the mutation as surviving -- or, when the cache holds a DIFFERENT
    mutation's code, reports a kill that belongs to that other mutation.

    Either way the verdict describes code the harness did not write. Running
    each suite under a private ``PYTHONPYCACHEPREFIX`` is what makes the
    verdict describe the mutation it is labelled with.
    """
    spec = _project(tmp_path)
    poisoned = _poison_cache(tmp_path, MODULE_SRC)
    assert poisoned.exists()

    proc = _run(tmp_path, spec)
    v = _verdicts(proc.stdout)
    assert v.get("M1_guard_a_removed") == "KILLED", (
        "the harness executed a cached module instead of the mutated source, "
        "so its verdict is about code it did not write:\n" + proc.stdout)
    assert v.get("M2_guard_b_removed") == "SURVIVED", proc.stdout


def test_each_run_gets_a_private_bytecode_cache_that_is_cleaned_up():
    """Structural companion to the test above.

    The end-to-end test proves the outcome; this proves the mechanism, so a
    change that keeps the outcome by accident is still visible.
    """
    spec = importlib.util.spec_from_file_location("mm_probe", HARNESS)
    mm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mm)

    seen = {}
    real_run = mm.subprocess.run

    def capture(cmd, **kw):
        env = kw.get("env") or {}
        seen.setdefault("prefixes", []).append(env.get("PYTHONPYCACHEPREFIX"))
        return real_run([sys.executable, "-c", "pass"], **{
            **kw, "env": env or None})

    mm.subprocess.run = capture
    try:
        mm.run_suite(["tests/test_suite.py"], sys.executable)
        mm.run_suite(["tests/test_suite.py"], sys.executable)
    finally:
        mm.subprocess.run = real_run

    prefixes = seen["prefixes"]
    assert all(prefixes), "every run must set PYTHONPYCACHEPREFIX"
    assert len(set(prefixes)) == 2, "a shared prefix reintroduces the defect"
    for p in prefixes:
        assert not Path(p).exists(), "the private cache must be removed"


# ---- the traps it already closed, kept closed ---------------------------
def test_a_red_baseline_refuses_to_start(tmp_path):
    """N mutations "killed" for a pre-existing reason reads as a perfect
    score."""
    broken = SUITE_SRC + "\n\ndef test_already_failing():\n    assert False\n"
    spec = _project(tmp_path, suite_src=broken)
    proc = _run(tmp_path, spec)
    assert proc.returncode != 0
    assert "baseline" in proc.stdout.lower() + proc.stderr.lower()
    assert "KILLED" not in proc.stdout


def test_anchor_drift_is_an_error_not_a_skip(tmp_path):
    """A silently skipped mutation tested nothing while appearing in the
    report."""
    spec = _project(tmp_path, mutations=[
        {"name": "D1_anchor_gone", "path": "pkg/mod.py",
         "find": "    if x < 999:", "replace": "    if 0:",
         "rationale": "anchor no longer present"},
    ])
    proc = _run(tmp_path, spec)
    assert _verdicts(proc.stdout).get("D1_anchor_gone") == "ANCHOR DRIFT"
    assert proc.returncode != 0


def test_an_ambiguous_anchor_is_an_error(tmp_path):
    """Matching twice would mutate two places and test neither."""
    spec = _project(tmp_path, mutations=[
        {"name": "D2_anchor_ambiguous", "path": "pkg/mod.py",
         "find": "        raise ValueError(", "replace": "        pass  # (",
         "rationale": "matches both guards"},
    ])
    proc = _run(tmp_path, spec)
    assert _verdicts(proc.stdout).get("D2_anchor_ambiguous") == "ANCHOR DRIFT"


def test_an_interrupted_run_leaves_a_recovery_sidecar(tmp_path):
    """A killed run must not let a disabled guard reach a commit."""
    spec = _project(tmp_path)
    sidecar = tmp_path / ".mutation-recovery.json"
    sidecar.write_text(json.dumps({"pkg/mod.py": MODULE_SRC}),
                       encoding="utf-8")
    proc = _run(tmp_path, spec)
    assert proc.returncode != 0
    assert "--recover" in proc.stdout
    assert "KILLED" not in proc.stdout, (
        "a run that starts on a possibly-mutated tree reports nothing "
        "trustworthy")


def test_recover_restores_and_clears_the_sidecar(tmp_path):
    _project(tmp_path)
    modpath = tmp_path / "pkg" / "mod.py"
    sidecar = tmp_path / ".mutation-recovery.json"
    sidecar.write_text(json.dumps({"pkg/mod.py": MODULE_SRC}),
                       encoding="utf-8")
    modpath.write_text(MODULE_SRC.replace("if x < 0:", "if 0:"),
                       encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(tmp_path / "tools" / "mutation_matrix.py"),
         "--recover"], cwd=str(tmp_path), capture_output=True, text=True,
        timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert modpath.read_text(encoding="utf-8") == MODULE_SRC
    assert not sidecar.exists()


@pytest.mark.parametrize("field", ["find", "replace", "path", "name",
                                  "rationale"])
def test_a_malformed_specification_is_refused_before_anything_is_written(
        tmp_path, field):
    """Refused, not merely crashed on.

    Both fail closed, but a specification checked up front cannot reach the
    point where a source file has been written to -- and it says which field
    is wrong instead of raising a KeyError from inside the mutation loop.
    """
    mutation = {"name": "X", "path": "pkg/mod.py", "find": "    if x < 0:",
                "replace": "    if 0:", "rationale": "r"}
    del mutation[field]
    spec = _project(tmp_path, mutations=[mutation])
    before = (tmp_path / "pkg" / "mod.py").read_bytes()
    proc = _run(tmp_path, spec)
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr, proc.stderr
    assert field in proc.stdout, proc.stdout
    assert "KILLED" not in proc.stdout
    assert not (tmp_path / ".mutation-recovery.json").exists()
    assert (tmp_path / "pkg" / "mod.py").read_bytes() == before


def test_a_no_op_mutation_is_refused(tmp_path):
    """find == replace changes nothing and is 'killed' by nothing.

    The assertion names the whole sentence on purpose. Matching the bare word
    "identical" passed against the unrelated line "all sources restored
    byte-identical", so the test went green with the check deleted -- an
    assertion anchored on text that another line also contains is a test that
    defends nothing.
    """
    spec = _project(tmp_path, mutations=[
        {"name": "N1", "path": "pkg/mod.py", "find": "    if x < 0:",
         "replace": "    if x < 0:", "rationale": "r"}])
    proc = _run(tmp_path, spec)
    assert proc.returncode != 0
    assert "'find' and 'replace' are identical" in proc.stdout, proc.stdout
    assert "KILLED" not in proc.stdout


def test_a_missing_suite_is_refused(tmp_path):
    spec_path = _project(tmp_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["suites"] = ["tests/test_does_not_exist.py"]
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    proc = _run(tmp_path, spec_path)
    assert proc.returncode != 0
    assert "does not exist" in proc.stdout


def test_duplicate_mutation_names_are_refused(tmp_path):
    spec = _project(tmp_path, mutations=[
        {"name": "same", "path": "pkg/mod.py", "find": "    if x < 0:",
         "replace": "    if 0:", "rationale": "r"},
        {"name": "same", "path": "pkg/mod.py", "find": "    if x > 9:",
         "replace": "    if 0:", "rationale": "r"}])
    proc = _run(tmp_path, spec)
    assert proc.returncode != 0
    assert "duplicate mutation name" in proc.stdout


# ---- the baseline must be green, and stably so -------------------------
def test_a_flaky_baseline_is_refused_as_loudly_as_a_red_one(tmp_path):
    """A baseline that can flake red can flake green.

    A green flake is the dangerous direction: the matrix then reports kills
    for mutations nothing tested, which is the exact false negative the
    harness exists to prevent. So a first-run failure followed by a passing
    re-run is refused on its own terms rather than being run past.
    """
    flaky = SUITE_SRC + '''

def test_fails_only_the_first_time(tmp_path_factory):
    marker = Path(__file__).resolve().parent / ".seen"
    if not marker.exists():
        marker.write_text("x")
        assert False, "first run only"
'''
    spec = _project(tmp_path, suite_src=flaky)
    proc = _run(tmp_path, spec)
    assert proc.returncode == 4, proc.stdout
    assert "NON-DETERMINISTIC" in proc.stdout
    assert "KILLED" not in proc.stdout


def test_state_a_mutation_leaves_behind_is_caught_after_the_run(tmp_path):
    """Byte-identical restoration is necessary and not sufficient.

    A mutation can let the suite write a file the source hash cannot see, and
    the harness would report a clean restoration over a tree that no longer
    behaves like the one it was given. The post-run baseline asks the only
    question that matters.
    """
    module = MODULE_SRC + "\n\nLEAK = False\n"
    suite = SUITE_SRC + (
        "\n\ndef test_no_leftover_state():\n"
        "    assert not (Path(__file__).resolve().parent / '.leaked').exists()\n")
    leak = ("LEAK = bool(open(str(Path(__file__).resolve().parent.parent"
            " / 'tests' / '.leaked'), 'w').write('x'))"
            " if True else False\nfrom pathlib import Path")
    spec = _project(
        tmp_path, module_src=module, suite_src=suite,
        mutations=[{"name": "L1_leaks_state", "path": "pkg/mod.py",
                    "find": "LEAK = False",
                    "replace": "from pathlib import Path\n" + leak.split(
                        "\nfrom pathlib")[0],
                    "rationale": "writes a file the source hash cannot see"}])
    proc = _run(tmp_path, spec)
    assert "ANCHOR DRIFT" not in proc.stdout, proc.stdout
    assert "POST-RUN BASELINE RED" in proc.stdout, proc.stdout
    assert proc.returncode != 0


# ---- restoration between mutations, not only at the end -----------------
TWO_FILE_A = '''def guard(x):
    if x < 0:
        raise ValueError("a")
    return x
'''

TWO_FILE_B = '''def guard(x):
    if x > 9:
        raise ValueError("b")
    return x
'''

TWO_FILE_SUITE = '''import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from pkg import mod_a, mod_b


def test_a():
    with pytest.raises(ValueError):
        mod_a.guard(-1)


def test_b():
    with pytest.raises(ValueError):
        mod_b.guard(10)
'''


def test_each_mutation_is_restored_before_the_next_one_runs(tmp_path):
    """Restoring only at the end is not the same as restoring each time.

    While file A is still mutated, the collateral-damage detector sees it as
    a tracked file the harness did not touch on THIS mutation -- and its
    response is ``git checkout HEAD -- A``, which discards whatever
    uncommitted work was in that file. A per-mutation restore is what keeps
    the detector's view of "damaged" true.
    """
    (tmp_path / "tools" / "mutations").mkdir(parents=True)
    shutil.copy2(HARNESS, tmp_path / "tools" / "mutation_matrix.py")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "mod_a.py").write_text(TWO_FILE_A, encoding="utf-8")
    (tmp_path / "pkg" / "mod_b.py").write_text(TWO_FILE_B, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_suite.py").write_text(TWO_FILE_SUITE,
                                                      encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=str(tmp_path), check=True)

    spec_path = tmp_path / "tools" / "mutations" / "spec.json"
    spec_path.write_text(json.dumps({
        "title": "two files", "note": "n", "suites": ["tests/test_suite.py"],
        "mutations": [
            {"name": "A1", "path": "pkg/mod_a.py", "find": "    if x < 0:",
             "replace": "    if 0:", "rationale": "guard a"},
            {"name": "B1", "path": "pkg/mod_b.py", "find": "    if x > 9:",
             "replace": "    if 0:", "rationale": "guard b"},
        ]}), encoding="utf-8")

    proc = _run(tmp_path, spec_path)
    assert "DAMAGED TRACKED FILES" not in proc.stdout, (
        "a source left mutated from the previous step is reported as "
        "collateral damage and git-checked-out, which discards real work:\n"
        + proc.stdout)
    assert _verdicts(proc.stdout) == {"A1": "KILLED", "B1": "KILLED"}, \
        proc.stdout
    assert proc.returncode == 0, proc.stdout
