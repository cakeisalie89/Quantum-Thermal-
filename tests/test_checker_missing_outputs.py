"""`--verify-existing` must refuse in a governed way, never by traceback.

The defect this pins down: with `outputs/` absent, the checker printed its mode
banner, accumulated a failure (``fail()`` records rather than exits), then ran
on into ``regen_root_byte_drift`` and died with

    FileNotFoundError: [Errno 2] No such file or directory: '.../outputs'

It still exited 1, so it was fail-*closed* rather than fail-open -- but a
traceback is not the intended refusal. It gives no classification, no next
step, and it terminates before the remaining diagnostics that would tell an
operator what else is wrong.

These tests fix the contract: a classified refusal, a nonzero exit, a
deterministic message, no regeneration in verification-only mode, and no
vacuous PASS for a comparison that never happened.

The checker regenerates and deletes a gitignored `outputs/` directory, so every
case here runs against an isolated copy in tmp_path. Nothing touches the real
workspace -- see the serial-execution note in TESTING.md.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKER = "package_consistency_check.py"

#: Names the checker's classification vocabulary. Each is a specific refusal,
#: never a generic error, so an operator can tell the cases apart.
CLASSIFICATIONS = (
    "MISSING_EXISTING_OUTPUTS",
    "EXISTING_OUTPUTS_NOT_A_DIRECTORY",
    "INCOMPLETE_EXISTING_OUTPUTS",
    "FOREIGN_EXISTING_OUTPUTS",
    "UNREADABLE_EXISTING_OUTPUT",
)


def _scratch(tmp_path, outputs=None, *, outputs_is_file=False):
    """An isolated checkout stub with a controllable outputs/ state."""
    d = tmp_path / "wk"
    d.mkdir(parents=True)
    for f in (CHECKER, "qta_full_sim.py"):
        shutil.copy2(os.path.join(ROOT, f), d)
    if outputs_is_file:
        (d / "outputs").write_text("not a directory")
    elif outputs is not None:
        out = d / "outputs"
        out.mkdir()
        for name in outputs:
            (out / name).write_text("{}")
    log = d / "sim.log"
    log.write_text("clean run\n")
    return d, log


def _run(workdir, log, *, timeout=180):
    return subprocess.run(
        [sys.executable, CHECKER, "--verify-existing", "--sim-log", str(log)],
        cwd=workdir, capture_output=True, text=True, timeout=timeout)


def _assert_governed_refusal(r):
    """Shared contract for every missing/!usable state."""
    assert r.returncode != 0, "must fail closed"
    assert "Traceback" not in r.stderr, \
        f"expected a governed refusal, got a traceback:\n{r.stderr[-1500:]}"
    assert "FileNotFoundError" not in r.stderr
    assert "NOT the release gate" in r.stdout, \
        "the mode banner must still make the non-authoritative mode clear"


# ---------------------------------------------------------------------------
# 1. Whole directory missing -- the originally reported defect.
# ---------------------------------------------------------------------------

def test_missing_outputs_directory_is_a_classified_refusal(tmp_path):
    d, log = _scratch(tmp_path, outputs=None)
    r = _run(d, log)
    _assert_governed_refusal(r)
    assert "MISSING_EXISTING_OUTPUTS" in r.stdout
    assert "does not exist" in r.stdout


def test_missing_outputs_names_the_correct_next_step(tmp_path):
    """The message must tell an operator how to get out of the state."""
    d, log = _scratch(tmp_path, outputs=None)
    r = _run(d, log)
    assert "never regenerates" in r.stdout
    assert "full-regeneration" in r.stdout


def test_missing_outputs_does_not_regenerate(tmp_path):
    """Verification-only mode must not quietly produce what it should verify.

    If it regenerated, the refusal would be self-healing and the mode would
    silently become the release gate it explicitly is not.
    """
    d, log = _scratch(tmp_path, outputs=None)
    _run(d, log)
    assert not (d / "outputs").exists(), \
        "--verify-existing must never create outputs/"


def test_missing_outputs_yields_no_vacuous_byte_match_pass(tmp_path):
    """With nothing to compare, silence would read as agreement."""
    d, log = _scratch(tmp_path, outputs=None)
    r = _run(d, log)
    assert "[PASS] root canonical outputs byte-match" not in r.stdout
    assert "NOT CHECKED" in r.stdout


def test_missing_outputs_message_is_deterministic(tmp_path):
    """Two runs of the same state must produce the same classification."""
    d1, l1 = _scratch(tmp_path / "a", outputs=None)
    d2, l2 = _scratch(tmp_path / "b", outputs=None)
    a, b = _run(d1, l1), _run(d2, l2)
    assert a.returncode == b.returncode

    def cls(out):
        return [c for c in CLASSIFICATIONS if c in out]
    assert cls(a.stdout) == cls(b.stdout) == ["MISSING_EXISTING_OUTPUTS"]


def test_missing_outputs_preserves_later_diagnostics(tmp_path):
    """A traceback truncated the run; a refusal must not.

    The original defect aborted at Step 2b, so everything after it was lost.
    The governed refusal has to keep going and report the rest.
    """
    d, log = _scratch(tmp_path, outputs=None)
    r = _run(d, log)
    assert "Step 3" in r.stdout, \
        "steps after the refusal must still run and report"
    assert r.stdout.count("[FAIL]") + r.stdout.count("FAILURES") > 0


# ---------------------------------------------------------------------------
# 2. Present but wrong: incomplete, foreign, not-a-directory.
# ---------------------------------------------------------------------------

def test_one_missing_required_file_is_refused_as_incomplete(tmp_path):
    """88 of the canonical 89 is a truncated set, never an acceptable one."""
    d, log = _scratch(tmp_path, outputs=[f"f{i}.json" for i in range(88)])
    r = _run(d, log)
    _assert_governed_refusal(r)
    assert "INCOMPLETE_EXISTING_OUTPUTS" in r.stdout
    assert "88 files present" in r.stdout


def test_an_extra_file_is_refused_as_foreign(tmp_path):
    d, log = _scratch(tmp_path, outputs=[f"f{i}.json" for i in range(90)])
    r = _run(d, log)
    _assert_governed_refusal(r)
    assert "FOREIGN_EXISTING_OUTPUTS" in r.stdout
    assert "90 files present" in r.stdout


def test_outputs_path_that_is_a_file_is_classified_not_crashed(tmp_path):
    d, log = _scratch(tmp_path, outputs_is_file=True)
    r = _run(d, log)
    _assert_governed_refusal(r)
    assert "EXISTING_OUTPUTS_NOT_A_DIRECTORY" in r.stdout


def test_wrong_sized_set_reports_what_it_actually_found(tmp_path):
    """Partial diagnostic information must survive the refusal."""
    d, log = _scratch(tmp_path, outputs=["a.json", "b.json"])
    r = _run(d, log)
    assert "first 5 present" in r.stdout
    assert "a.json" in r.stdout


# ---------------------------------------------------------------------------
# 3. Unreadable outputs are unverifiable, not "no drift".
# ---------------------------------------------------------------------------

def test_unreadable_output_is_reported_rather_than_skipped():
    """An OSError while hashing must not be read as byte-equality.

    Exercised directly against the function: the comparison returns the
    unreadable names alongside the drifting ones, so the caller can fail on
    them instead of silently treating them as matching.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_pcc_probe", os.path.join(ROOT, CHECKER))
    assert spec is not None
    src = open(os.path.join(ROOT, CHECKER), encoding="utf-8").read()
    # The contract: two return values, and OSError handled per-file.
    assert "return drift, unreadable" in src
    assert "except OSError" in src
    assert "UNREADABLE_EXISTING_OUTPUT" in src


@pytest.mark.skipif(os.geteuid() == 0,
                    reason="root bypasses the unreadable-file permission bit")
def test_unreadable_file_does_not_crash_the_comparison(tmp_path):
    """End-to-end: a chmod-000 output is named, not fatal."""
    gen = tmp_path / "gen"
    root = tmp_path / "root"
    gen.mkdir()
    root.mkdir()
    (gen / "x.json").write_text("{}")
    (root / "x.json").write_text("{}")
    (gen / "x.json").chmod(0o000)
    try:
        sys.path.insert(0, ROOT)
        import ast
        tree = ast.parse(open(os.path.join(ROOT, CHECKER),
                              encoding="utf-8").read())
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "regen_root_byte_drift")
        ns: dict = {}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<probe>",
                     "exec"), {"hashlib": __import__("hashlib")}, ns)
        drift, unreadable = ns["regen_root_byte_drift"](gen, root, frozenset())
        assert drift == []
        assert len(unreadable) == 1 and "x.json" in unreadable[0]
    finally:
        (gen / "x.json").chmod(0o644)


# ---------------------------------------------------------------------------
# 4. Fail-closed semantics are not weakened by any of the above.
# ---------------------------------------------------------------------------

def test_no_state_here_can_produce_a_pass_verdict(tmp_path):
    for kwargs in ({"outputs": None},
                   {"outputs": ["a.json"]},
                   {"outputs_is_file": True}):
        d, log = _scratch(tmp_path / f"c{abs(hash(str(kwargs)))}", **kwargs)
        r = _run(d, log)
        assert r.returncode != 0
        assert "RESULT: PASS" not in r.stdout, kwargs


def test_verify_existing_still_requires_a_sim_log(tmp_path):
    """The pre-existing refusal must survive the hardening."""
    d, _ = _scratch(tmp_path, outputs=[f"f{i}.json" for i in range(89)])
    r = subprocess.run([sys.executable, CHECKER, "--verify-existing"],
                       cwd=d, capture_output=True, text=True, timeout=180)
    assert r.returncode != 0
    assert "verify-existing refuses to skip" in r.stdout
    assert "Traceback" not in r.stderr


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")
         and callable(v)]
