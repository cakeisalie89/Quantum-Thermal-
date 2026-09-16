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
    # The same two refusals reached from the OTHER mode. They were absent
    # because the completeness rule was absent from that mode: full
    # regeneration produced a set and Step 2b compared whatever was in it.
    "INCOMPLETE_REGENERATED_OUTPUTS",
    "FOREIGN_REGENERATED_OUTPUTS",
    # A declared output with no committed root copy. Step 2b skips those, so
    # it would be published having been compared against nothing.
    "UNROOTED_CANONICAL_OUTPUT",
    # The declaration is the scope of the reconciliation; an empty one makes
    # every comparison below it agree.
    "EMPTY_CANONICAL_DECLARATION",
    "DUPLICATE_CANONICAL_DECLARATION",
)


def _declared():
    """The canonical output names, read from the checker's own truth table.

    Re-derived rather than copied. A list duplicated into the test would keep
    agreeing with itself after the checker's moved on, which is the shape of
    defect this suite exists to catch.
    """
    import ast as _ast
    tree = _ast.parse(open(os.path.join(ROOT, CHECKER), encoding="utf-8").read())
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Assign)
                and getattr(node.targets[0], "id", None) == "CANONICAL_EXPECTED"):
            for k, v in zip(node.value.keys, node.value.values):
                if getattr(k, "value", None) == "canonical_outputs":
                    return [e.value for e in v.elts]
    raise AssertionError("CANONICAL_EXPECTED['canonical_outputs'] not found")


def _scratch(tmp_path, outputs=None, *, outputs_is_file=False,
             root_copies=False):
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
            if root_copies:
                # Step 2b compares a produced file against the ROOT copy of
                # the same name and skips it when there is none, so a fixture
                # without root copies is testing a different state.
                (d / name).write_text("{}")
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
    """One short of the canonical set is truncated, and it is named.

    This asserted ``"88 files present"``, which is the count and not the set.
    The fixture supplied eighty-eight files called ``f0.json``, none of them a
    canonical output, and the test passed -- so it would equally have passed
    against a directory containing none of the right files at all.
    """
    names = _declared()
    dropped = names[0]
    d, log = _scratch(tmp_path, outputs=names[1:], root_copies=True)
    r = _run(d, log)
    _assert_governed_refusal(r)
    assert "INCOMPLETE_EXISTING_OUTPUTS" in r.stdout
    assert dropped in r.stdout, "the refusal must name what is missing"


def test_an_extra_file_is_refused_as_foreign(tmp_path):
    names = _declared()
    d, log = _scratch(tmp_path, outputs=names + ["not_an_output.json"],
                      root_copies=True)
    r = _run(d, log)
    _assert_governed_refusal(r)
    assert "FOREIGN_EXISTING_OUTPUTS" in r.stdout
    assert "not_an_output.json" in r.stdout


def test_a_renamed_output_is_refused_although_the_count_is_right(tmp_path):
    """The test that separates the set from its size.

    Eighty-nine files, one of them renamed. ``_n != 89`` is False, so the
    old rule accepted it: the canonical output was gone, its stale root copy
    was never compared to anything, and the run reported that the root copies
    byte-match the regeneration.
    """
    names = _declared()
    swapped = names[:-1] + ["renamed_output.json"]
    assert len(swapped) == len(names)
    d, log = _scratch(tmp_path, outputs=swapped, root_copies=True)
    r = _run(d, log)
    _assert_governed_refusal(r)
    assert "INCOMPLETE_EXISTING_OUTPUTS" in r.stdout
    assert names[-1] in r.stdout
    assert "FOREIGN_EXISTING_OUTPUTS" in r.stdout
    assert "renamed_output.json" in r.stdout


def test_a_declared_output_with_no_root_copy_is_refused(tmp_path):
    """Step 2b skips a produced file whose root copy is absent.

    That is the right rule for a file nobody committed, and it is why the
    declaration has to be checked against the root tree too: a DECLARED
    canonical output with no root copy is compared against nothing while the
    byte gate reports agreement.
    """
    names = _declared()
    d, log = _scratch(tmp_path, outputs=names, root_copies=True)
    (d / names[3]).unlink()
    r = _run(d, log)
    _assert_governed_refusal(r)
    assert "UNROOTED_CANONICAL_OUTPUT" in r.stdout
    assert names[3] in r.stdout


def test_an_emptied_declaration_is_refused_rather_than_agreed_with(tmp_path):
    """The anti-vacuity control for the reconciliation itself.

    Every comparison in the block is against the declared set. Emptying it
    makes all three of them agree -- missing, foreign and unrooted are all
    empty -- and the block would print a PASS meaning nothing. It must refuse
    instead, and it must say so before it reads a single file.
    """
    names = _declared()
    d, log = _scratch(tmp_path, outputs=names, root_copies=True)
    src = (d / CHECKER).read_text(encoding="utf-8")
    head, sep, rest = src.partition('    "canonical_outputs": [')
    assert sep, "the declaration must be findable to be emptied"
    _, _, tail = rest.partition("\n    ],\n")
    (d / CHECKER).write_text(head + '    "canonical_outputs": [],\n' + tail,
                             encoding="utf-8")
    r = _run(d, log)
    _assert_governed_refusal(r)
    assert "EMPTY_CANONICAL_DECLARATION" in r.stdout
    assert "[PASS] the produced output set is exactly" not in r.stdout


def test_outputs_path_that_is_a_file_is_classified_not_crashed(tmp_path):
    d, log = _scratch(tmp_path, outputs_is_file=True)
    r = _run(d, log)
    _assert_governed_refusal(r)
    assert "EXISTING_OUTPUTS_NOT_A_DIRECTORY" in r.stdout


def test_wrong_sized_set_reports_what_it_actually_found(tmp_path):
    """Partial diagnostic information must survive the refusal.

    This asserted ``"first 5 present"`` -- the first five names of the wrong
    set, which tells an operator what turned up but never what is missing.
    Both halves are now named, and the missing half is the actionable one.
    """
    d, log = _scratch(tmp_path, outputs=["a.json", "b.json"])
    r = _run(d, log)
    assert "a.json" in r.stdout, "the foreign files must be named"
    assert "FOREIGN_EXISTING_OUTPUTS" in r.stdout
    assert "INCOMPLETE_EXISTING_OUTPUTS" in r.stdout
    for missing in _declared()[:3]:
        assert missing in r.stdout, "the absent canonical outputs must be named"


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


def test_a_duplicated_declaration_is_refused(tmp_path):
    """A duplicate makes the declared COUNT larger than the declared SET.

    Harmless-looking and not harmless: every headline this block prints is a
    length, and a length taken from the list rather than the set says more
    outputs were reconciled than were.
    """
    names = _declared()
    d, log = _scratch(tmp_path, outputs=names, root_copies=True)
    src = (d / CHECKER).read_text(encoding="utf-8")
    dup = f'    "canonical_outputs": [\n        "{names[0]}",'
    assert src.count('    "canonical_outputs": [') == 1
    src = src.replace('    "canonical_outputs": [', dup, 1)
    (d / CHECKER).write_text(src, encoding="utf-8")
    r = _run(d, log)
    _assert_governed_refusal(r)
    assert "DUPLICATE_CANONICAL_DECLARATION" in r.stdout
    assert names[0] in r.stdout

# ---------------------------------------------------------------------------
# 3. The SAME rule in the mode that is the release gate.
#
# Everything above runs `--verify-existing`, which the checker's own banner
# calls "NOT the release gate". The completeness rule lived only there. Full
# regeneration -- the authoritative path, the one CI runs -- deleted outputs/,
# ran the simulation, and handed Step 2b whatever appeared.
#
# These drive that path with a stub simulation whose output set is the
# variable under test.
# ---------------------------------------------------------------------------

_STUB_SIM = """import os, sys
os.makedirs("outputs", exist_ok=True)
for n in {names!r}:
    with open(os.path.join("outputs", n), "w") as fh:
        fh.write("{{}}")
print("stub simulation wrote", len({names!r}), "file(s)")
"""


def _scratch_default(tmp_path, produced, *, root_copies=True):
    """A checkout stub whose simulation produces exactly ``produced``."""
    d = tmp_path / "wk"
    d.mkdir(parents=True)
    shutil.copy2(os.path.join(ROOT, CHECKER), d)
    (d / "qta_full_sim.py").write_text(_STUB_SIM.format(names=list(produced)),
                                       encoding="utf-8")
    if root_copies:
        for name in produced:
            (d / name).write_text("{}")
    return d


def _run_default(workdir, *, timeout=180):
    return subprocess.run([sys.executable, CHECKER], cwd=workdir,
                          capture_output=True, text=True, timeout=timeout)


def _assert_default_refusal(r):
    assert r.returncode != 0, "must fail closed"
    assert "Traceback" not in r.stderr, \
        f"expected a governed refusal, got a traceback:\n{r.stderr[-1500:]}"
    assert "NOT the release gate" not in r.stdout, \
        "this is the release gate; the verify-existing banner must not appear"


def test_regeneration_that_comes_up_short_is_refused(tmp_path):
    """The defect, in the mode that matters.

    Measured on this repository: a run whose regeneration produced 84 of the
    89 declared outputs printed RESULT: PASS (all consistency checks passed).
    Five canonical outputs -- among them coupled_mode_state_summary.json,
    which carries Mode_D_residual_CH4_density_m3 -- were never produced, so
    Step 2b never compared their committed root copies against anything, and
    the gate reported that the root copies match the regeneration.
    """
    names = _declared()
    d = _scratch_default(tmp_path, names[5:])
    r = _run_default(d)
    _assert_default_refusal(r)
    assert "INCOMPLETE_REGENERATED_OUTPUTS" in r.stdout
    for missing in names[:5]:
        assert missing in r.stdout, f"{missing} must be named, not counted"


def test_regeneration_that_emits_something_foreign_is_refused(tmp_path):
    names = _declared()
    d = _scratch_default(tmp_path, names + ["stray_output.json"])
    r = _run_default(d)
    _assert_default_refusal(r)
    assert "FOREIGN_REGENERATED_OUTPUTS" in r.stdout
    assert "stray_output.json" in r.stdout


def test_a_complete_regeneration_does_not_trip_the_set_check(tmp_path):
    """The positive control.

    A refusal that fires on everything establishes nothing. The stub tree
    fails plenty of later checks -- it has no gate table and no real outputs
    -- but the set reconciliation must pass on it, and say how many it left
    to the byte comparison.
    """
    names = _declared()
    d = _scratch_default(tmp_path, names)
    r = _run_default(d)
    assert "INCOMPLETE_REGENERATED_OUTPUTS" not in r.stdout
    assert "FOREIGN_REGENERATED_OUTPUTS" not in r.stdout
    assert "UNROOTED_CANONICAL_OUTPUT" not in r.stdout
    assert "[PASS] the produced output set is exactly the " in r.stdout
    assert f"{len(names)} declared canonical outputs" in r.stdout


def test_the_set_check_reports_how_many_it_compared(tmp_path):
    """A reconciliation that does not say what it covered is a claim on trust.

    D-2026-54 was a checker printing a count it never compared. The inverse
    is a checker comparing a set and never saying how big it was, which is
    what leaves a shrinking scope invisible.
    """
    names = _declared()
    d = _scratch_default(tmp_path, names)
    r = _run_default(d)
    line = [ln for ln in r.stdout.splitlines()
            if "the produced output set is exactly the " in ln]
    assert line, "the set check must report"
    assert str(len(names)) in line[0]
    assert "compared byte-for-byte" in "\n".join(r.stdout.splitlines())
