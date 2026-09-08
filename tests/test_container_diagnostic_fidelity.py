"""The diagnostic must reproduce the pipeline it is diagnosing.

WHY THIS FILE EXISTS

`analysis/collect_container_3d.py` regenerates the canonical outputs so a
container's bytes can be compared against the committed copies. It called
``run_all(out, verbose=False)`` while ``qta_full_sim.py`` calls
``run_all(..., mc_samples=30)`` -- and ``run_all``'s default is 60.

So the collector regenerated ``multiphysics_summary.json`` with twice the
Monte Carlo samples, and every distribution differed. In an environment where
all 62 other files were byte-identical, that one file looked like exactly the
cross-environment divergence the collector was built to investigate. It was
the collector.

A diagnostic that does not reproduce the pipeline it is diagnosing
manufactures the divergence it was built to explain, and every conclusion
drawn from it is about the tool. These tests pin the correspondence.
"""
from __future__ import annotations

import ast
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COLLECTOR = ROOT / "analysis" / "collect_container_3d.py"
CANONICAL = ROOT / "qta_full_sim.py"


def _calls(path: Path, func: str) -> list:
    """Every call to ``func`` in ``path``, as (args, keywords) pairs."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name == func:
            out.append(node)
    return out


def _kwarg(call: ast.Call, name: str):
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def test_the_collector_exists_where_the_matrix_says_it_does():
    assert COLLECTOR.is_file(), (
        "docs/R59_CROSS_ENVIRONMENT_ANALYSIS.md and the completion matrix "
        "both point at this file")


def test_the_collector_passes_the_canonical_monte_carlo_sample_count():
    """The defect, pinned.

    Not "the collector passes 30" -- that would go stale the moment the
    canonical value changed. The assertion is that the two AGREE.
    """
    canonical_calls = [c for c in _calls(CANONICAL, "run_all")
                       if _kwarg(c, "mc_samples") is not None]
    assert canonical_calls, (
        "qta_full_sim.py no longer passes mc_samples to run_all; the "
        "correspondence this test pins has moved and must be re-established")
    canonical_value = _kwarg(canonical_calls[0], "mc_samples")

    collector_calls = _calls(COLLECTOR, "run_all")
    assert collector_calls, "the collector no longer calls run_all"
    for call in collector_calls:
        passed = _kwarg(call, "mc_samples")
        if passed is None:
            # It may pass the module constant rather than a literal.
            names = [kw.value.id for kw in call.keywords
                     if kw.arg == "mc_samples" and isinstance(kw.value,
                                                              ast.Name)]
            assert names, (
                "the collector calls run_all without mc_samples, so it uses "
                f"the default (60) while qta_full_sim.py passes "
                f"{canonical_value}. That regenerates "
                "multiphysics_summary.json with twice the Monte Carlo "
                "samples and manufactures a divergence.")
            source = COLLECTOR.read_text(encoding="utf-8")
            m = re.search(rf"^{names[0]}\s*=\s*(\d+)", source, re.M)
            assert m, f"{names[0]} is not a module-level integer constant"
            passed = int(m.group(1))
        assert passed == canonical_value, (
            f"the collector passes mc_samples={passed} and qta_full_sim.py "
            f"passes {canonical_value}; the diagnostic is measuring a "
            "different pipeline from the one under investigation")


def test_the_collector_uses_the_same_3d_entry_point_and_mode():
    """``heavy`` changes which meshes run. A mismatch there is the same
    class of defect as the sample count, and would be harder to spot."""
    canonical = _calls(CANONICAL, "_run_3d_all") or _calls(CANONICAL,
                                                           "run_3d_all")
    assert canonical, "qta_full_sim.py no longer calls run_3d_all"
    collector = _calls(COLLECTOR, "run_3d_all")
    assert collector, "the collector no longer calls run_3d_all"
    # The canonical call derives heavy from argv; the collector must use the
    # default (reduced CI mesh) that a plain `python qta_full_sim.py` uses.
    assert _kwarg(collector[0], "heavy") is False, (
        "the collector must regenerate with heavy=False, which is what a "
        "plain `python qta_full_sim.py` run does")


def test_the_collector_writes_only_into_a_caller_supplied_empty_directory():
    """Captured bytes must be freshly generated, never the committed copies
    read back by accident."""
    source = COLLECTOR.read_text(encoding="utf-8")
    assert "if any(out.iterdir())" in source
    assert "REFUSING" in source
    assert "NOT the committed repository copies" in source


def test_the_collector_records_the_fingerprint_the_analysis_relies_on():
    source = COLLECTOR.read_text(encoding="utf-8")
    for field in ("numpy", "scipy", "blas", "lapack", "simd", "cpu",
                  "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                  "PYTHONHASHSEED"):
        assert field in source, (
            f"the fingerprint no longer records {field!r}, which "
            "docs/R59_CROSS_ENVIRONMENT_ANALYSIS.md cites")


def test_the_analysis_document_states_what_it_could_not_establish():
    """An analysis that only lists findings reads as more complete than it
    is."""
    doc = (ROOT / "docs" / "R59_CROSS_ENVIRONMENT_ANALYSIS.md").read_text(
        encoding="utf-8")
    flat = " ".join(doc.split())
    assert "**Not established**" in doc
    # The artifact host that cannot be reached, still named. Matched on the
    # host rather than on a sentence: the wording changed when the section
    # was superseded, and a test pinned to a phrase would have failed for
    # the document being updated rather than for it hiding anything.
    assert "productionresultssa14.blob.core.windows.net" in doc
    assert "denied by this environment's egress policy" in flat
    assert "inference from `set -e` ordering" in doc
    assert "No tolerance was widened" in doc

    # AND WHAT SUPERSEDED IT. A record that repairs itself quietly is not a
    # record, so the two claims that turned out to be wrong have to still be
    # visible as claims, marked.
    assert flat.count("SUPERSEDED") >= 3, (
        "the superseded conclusions were edited out rather than marked")
    assert "regeneration against a regeneration" in flat, (
        "the document does not say why the earlier byte-identity result "
        "could not have disagreed")
    assert "slice width" in flat, (
        "the document does not say what the 8-file count actually was")
    assert "OPENBLAS_CORETYPE" in doc and "SkylakeX" in doc, (
        "the document names no kernel, so the divergence has no cause in it")


# --- the comparison the collector never made --------------------------------

def test_the_collector_compares_against_the_committed_copies():
    """R59's actual measurement, which the collector did not perform.

    It captured hashes into an artifact and left the comparison to whoever
    downloaded the zip. When that zip turned out to be unreachable -- its
    signed URL points at a storage host some egress policies refuse -- the
    question "what did the 8-file divergence count measure" had no answer
    anywhere, because nothing had ever computed one.
    """
    import analysis.collect_container_3d as C

    assert hasattr(C, "compare_with_committed")
    assert hasattr(C, "emit_summary")


def test_the_comparison_counts_identical_differing_and_missing(tmp_path,
                                                               monkeypatch):
    import analysis.collect_container_3d as C

    # The committed copies live at the repository ROOT, not under outputs/
    # -- which is gitignored, absent on a fresh checkout, and where it does
    # exist is itself a regeneration. This fixture built them under outputs/
    # and so agreed with the defect instead of catching it.
    (tmp_path / "same.json").write_text("a")
    (tmp_path / "differs.json").write_text("b")
    monkeypatch.setattr(C, "REPO_ROOT", tmp_path)
    inventory = {
        "same.json": {"sha256": hashlib.sha256(b"a").hexdigest(), "size": 1},
        "differs.json": {"sha256": hashlib.sha256(b"XX").hexdigest(),
                         "size": 2},
        "absent.json": {"sha256": "0" * 64, "size": 0},
    }
    got = C.compare_with_committed(inventory)
    assert got["identical"] == 1
    assert got["differing"] == 1 and got["differing_files"] == ["differs.json"]
    assert got["not_committed"] == 1


def test_the_summary_is_greppable_out_of_a_job_log(capsys):
    """The markers are the retrieval route.

    An artifact needs a signed URL to a storage host; a job log is served by
    the logs API with no redirect at all, so the evidence has to be IN the
    log rather than pointed at from it.
    """
    import analysis.collect_container_3d as C

    C.emit_summary(
        {"python": "3.12.3", "numpy": "2.4.4", "cpu": {"model": "x",
                                                       "count": 4,
                                                       "flags": ["avx2"]}},
        {"regenerated": 63, "identical": 62, "differing": 1,
         "not_committed": 0, "differing_files": ["a.json"],
         "not_committed_files": [], "identical_files": []})
    out = capsys.readouterr().out
    assert "::QTA-3D-ENV::" in out
    assert "::QTA-3D-CPU::" in out
    assert "::QTA-3D-COMPARISON::" in out
    assert "::QTA-3D-DIFFERS:: a.json" in out
    assert "::QTA-3D-VERDICT:: DIVERGENT (1 file(s))" in out


def test_a_clean_comparison_reports_identical(capsys):
    import analysis.collect_container_3d as C

    C.emit_summary({}, {"regenerated": 63, "identical": 63, "differing": 0,
                        "not_committed": 0, "differing_files": [],
                        "not_committed_files": [], "identical_files": []})
    out = capsys.readouterr().out
    assert "::QTA-3D-VERDICT:: IDENTICAL (63/63" in out
    assert "::QTA-3D-DIFFERS::" not in out


def test_the_diagnostic_never_fails_the_build_on_a_divergence():
    """It reports; it does not gate.

    A diagnostic that exits non-zero on a byte difference has become a gate,
    and nothing in this repository outside the declared scientific
    authority is allowed to be one.
    """
    src = (ROOT / "analysis" / "collect_container_3d.py").read_text(
        encoding="utf-8")
    assert "DIAGNOSTIC ONLY: a divergence is reported, never a failure" in src


def test_the_container_script_reports_every_step():
    """R59 recorded steps 1-3 as passing by INFERENCE from set -e ordering.

    The job-logs API served only the pytest tail, so those steps' output was
    never actually read. A marker per step turns the inference into a
    record.
    """
    src = (ROOT / "container_verify.sh").read_text(encoding="utf-8")
    for step in ("environment", "git-available", "qta_full_sim",
                 "package_consistency", "manuscript_consistency",
                 "cross-environment-3d", "manifest_freshness"):
        assert f'step "{step}"' in src, f"{step} does not announce itself"
        assert f'done_ "{step}"' in src, f"{step} does not report success"

    # AND THE SUMMARY AT THE END, because the beginning is not reachable.
    # The logs API serves the tail; by the time this script has run
    # qta_full_sim.py the early markers are far above it.
    assert "trap _qta_summary EXIT" in src, (
        "no exit trap, so the step record is only readable by scrolling to "
        "a part of the log the API does not serve")
    assert "::QTA-STEPS-COMPLETED::" in src
    assert "::QTA-STEP-FAILED::" in src
    assert "::QTA-EXIT::" in src


def test_the_summary_prints_on_failure_and_names_the_failing_step(tmp_path):
    """Run the trap for real. A trap nobody fires is a comment.

    The case that matters is the failing one: knowing how far the script got
    is exactly what an inference from `set -e` ordering could not tell you.
    """
    import subprocess

    src = (ROOT / "container_verify.sh").read_text(encoding="utf-8")
    start = src.index("QTA_STEPS_OK=")
    end = src.index("trap _qta_summary EXIT") + len("trap _qta_summary EXIT")
    harness = tmp_path / "demo.sh"
    harness.write_text(
        "set -euo pipefail\n" + src[start:end] + "\n"
        'step "one"; true; done_ "one"\n'
        'step "two"; true; done_ "two"\n'
        'step "three"; false; done_ "three"\n')

    proc = subprocess.run(["bash", str(harness)], capture_output=True,
                          text=True)
    assert proc.returncode == 1, proc.returncode
    out = proc.stdout
    assert "::QTA-STEPS-COMPLETED:: one,two" in out, out
    assert "::QTA-STEP-FAILED:: three" in out, out
    assert "::QTA-EXIT:: 1" in out, out
    # ANTI-VACUITY: the summary must not claim a step that did not finish.
    assert "three" not in out.split("::QTA-STEPS-COMPLETED::")[1].split(
        "\n")[0]


def test_the_diagnostic_runs_before_the_checkers_that_can_fail():
    """The one case the measurement matters most was the one it was skipped in.

    The package check failed on a byte divergence, `set -e` ended the run,
    and the diagnostic that exists to EXPLAIN a byte divergence never
    executed. It is a diagnostic -- it reports and never fails -- so putting
    it first cannot mask a failure, only inform one.
    """
    src = (ROOT / "container_verify.sh").read_text(encoding="utf-8")
    diag = src.index('step "cross-environment-3d"')
    for later in ("package_consistency", "manuscript_consistency"):
        assert diag < src.index(f'step "{later}"'), (
            f"{later} runs before the 3D diagnostic, so a failure there "
            "suppresses the measurement that would explain it")


def test_the_container_installs_git_and_ships_the_repository():
    """48 governance tests died with FileNotFoundError in hosted run
    33113363458 because the image has no git.

    Installing git without shipping .git would be worse than neither: the
    binary exists, `git ls-files` exits 128 outside a repository, and a scan
    built on it returns an EMPTY set -- so every structural guard passes
    having examined nothing.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "install -y --no-install-recommends git" in dockerfile
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert not any(line.strip() == ".git" for line in ignore.splitlines()), (
        ".git is excluded from the build context, so the git binary above "
        "would find no repository")


# ==========================================================================
# THE VERDICT THAT LIED
#
# A hosted run printed
#
#     ::QTA-3D-VERDICT:: IDENTICAL (0/63 byte-identical ...)
#
# while comparing nothing at all. The collector was looking for the canonical
# copies under the wrong path, so all 63 landed in not_committed, `differing`
# was zero because nothing was compared, and "no differences" read as "no
# differences found". The anti-vacuity check of the day asserted only that 63
# files had been REGENERATED -- true, and about the wrong quantity.
#
# Every shape below is one the verdict must never call IDENTICAL.
# ==========================================================================

def _c(**over):
    base = {"regenerated": 63, "identical": 63, "differing": 0,
            "not_committed": 0, "differing_files": [],
            "not_committed_files": [], "identical_files": []}
    base.update(over)
    return base


def test_the_exact_shape_that_shipped_is_not_identical():
    """63 regenerated, 63 uncompared, 0 differing."""
    import analysis.collect_container_3d as C

    v = C.verdict_for(_c(identical=0, not_committed=63,
                         not_committed_files=[f"f{i}.json" for i in range(63)]))
    assert not v.startswith("IDENTICAL")
    assert v.startswith("INCOMPARABLE")
    assert "no committed copy" in v


def test_comparing_nothing_at_all_is_vacuous():
    import analysis.collect_container_3d as C

    v = C.verdict_for(_c(regenerated=0, identical=0))
    assert v.startswith("VACUOUS")
    assert "establishes nothing" in v


def test_numbers_that_do_not_add_up_are_an_accounting_error():
    """Neither 'it matched' nor 'it differed' is safe to say when the
    comparison lost track of files."""
    import analysis.collect_container_3d as C

    v = C.verdict_for(_c(identical=60, differing=1))
    assert v.startswith("ACCOUNTING ERROR")


def test_a_comparison_far_below_the_expected_coverage_says_so():
    import analysis.collect_container_3d as C

    v = C.verdict_for(_c(regenerated=3, identical=3))
    assert v.startswith("UNDER-COVERED")
    assert str(C.EXPECTED_CANONICAL) in v


def test_a_real_divergence_is_still_divergent():
    import analysis.collect_container_3d as C

    v = C.verdict_for(_c(identical=55, differing=8,
                         differing_files=[f"d{i}.json" for i in range(8)]))
    assert v.startswith("DIVERGENT (8 file(s))")


def test_the_honest_pass_is_still_reachable():
    """ANTI-VACUITY in the other direction. A verdict that could never say
    IDENTICAL would be as useless as one that always did."""
    import analysis.collect_container_3d as C

    assert C.verdict_for(_c()).startswith("IDENTICAL (63/63")


def test_uncompared_files_are_named_in_the_log(capsys):
    """An operator must be able to see WHICH files were not compared,
    without downloading anything."""
    import analysis.collect_container_3d as C

    C.emit_summary({}, _c(identical=0, not_committed=2,
                          not_committed_files=["a.json", "b.json"]))
    out = capsys.readouterr().out
    assert "::QTA-3D-NOT-COMMITTED:: a.json" in out
    assert "::QTA-3D-NOT-COMMITTED:: b.json" in out
    assert "::QTA-3D-VERDICT:: INCOMPARABLE" in out


def _committed_output_names():
    """Canonical output filenames, read from the tracked manifest.

    The manifest is the repository's own record of what is committed, so
    this cannot drift toward whatever happens to be in a scratch directory.
    Root-level ``.csv``/``.json`` files only: those are the canonical
    outputs the byte gate is about.
    """
    import json

    manifest = json.loads(
        (ROOT / "final_manifest.json").read_text(encoding="utf-8"))
    paths = [e.get("filename", "") for e in manifest["files"]]
    return {p for p in paths
            if "/" not in p and p.endswith((".csv", ".json"))
            and (ROOT / p).is_file()}


def test_the_comparison_finds_the_canonical_copies_where_they_live():
    """THE root cause of the 0/63, tested directly.

    The verdict logic above is worth nothing if the comparison cannot find
    the committed files: every one would land in not_committed and the run
    would report INCOMPARABLE forever. This feeds the real committed outputs
    back in as if they had just been regenerated, so a wrong path shows up
    here rather than in a hosted job an hour later.
    """
    import hashlib

    import analysis.collect_container_3d as C

    # FROM THE COMMITTED COPIES, which is the only set that exists on a
    # fresh checkout. This test used to read names AND bytes out of
    # ``outputs/`` -- the same directory the comparison then looked in -- so
    # it passed by asking one copy whether it matched itself, and could not
    # see that ``outputs/`` is gitignored, absent in CI, and on any machine
    # where it DOES exist is a regeneration rather than the committed
    # canonical file. A hosted run reported 0 of 63 compared while this was
    # green.
    names = sorted(_committed_output_names())
    assert len(names) >= C.EXPECTED_CANONICAL, (
        f"only {len(names)} committed outputs; the comparison could not "
        "reach its expected coverage even in principle")

    inventory = {
        n: {"sha256":
            hashlib.sha256((C.REPO_ROOT / n).read_bytes()).hexdigest()}
        for n in names
    }
    c = C.compare_with_committed(inventory)

    assert c["not_committed"] == 0, (
        f"{c['not_committed']} committed file(s) were not found where the "
        f"comparison looks: {c['not_committed_files'][:5]}. This is exactly "
        "the defect that produced '0/63 identical' in a hosted run")
    assert c["identical"] == len(names)
    assert c["differing"] == 0
    assert C.verdict_for(c).startswith("IDENTICAL")


# --------------------------------------------------------------------------
# The BLAS kernel, which is what the divergence turned out to be about
# --------------------------------------------------------------------------

def test_the_fingerprint_records_the_kernel_that_was_actually_selected():
    """The build string and the running kernel are different facts.

    ``numpy.show_config`` reports what the wheel was BUILT with -- on this
    machine it says "Haswell" -- while OpenBLAS DYNAMIC_ARCH selects
    SkylakeX at load time. Every conclusion R59 draws turns on the second
    one, and the fingerprint recorded only the first.
    """
    import analysis.collect_container_3d as C

    core = C.openblas_runtime_core()
    assert core and not core.startswith("UNKNOWN"), core
    assert core.isidentifier() or core.isalnum(), core

    fp = C.fingerprint()
    assert fp["openblas_runtime_core"] == core
    assert "OPENBLAS_CORETYPE" in fp


def test_forcing_the_kernel_actually_changes_the_selected_one():
    """ANTI-VACUITY for the whole sensitivity claim.

    If ``OPENBLAS_CORETYPE`` were ignored, the sweep would compare a kernel
    against itself several times and report "no sensitivity" -- a true
    sentence about a comparison that varied nothing. Run in a subprocess
    because the selection happens once, when the library is loaded.
    """
    import os
    import subprocess

    probe = ("import sys; sys.path.insert(0, %r);"
             "from analysis.collect_container_3d import openblas_runtime_core;"
             "print(openblas_runtime_core())" % str(ROOT))
    env = dict(os.environ)
    env["OPENBLAS_CORETYPE"] = "Nehalem"
    forced = subprocess.run([sys.executable, "-c", probe], cwd=ROOT,
                            env=env, capture_output=True, text=True)
    assert forced.returncode == 0, forced.stderr[-400:]
    env.pop("OPENBLAS_CORETYPE")
    default = subprocess.run([sys.executable, "-c", probe], cwd=ROOT,
                             env=env, capture_output=True, text=True)
    assert default.returncode == 0, default.stderr[-400:]

    assert forced.stdout.strip() == "Nehalem", forced.stdout
    assert default.stdout.strip() != "Nehalem", (
        "the default selection is already the forced one, so this machine "
        "cannot demonstrate the variable")


def test_the_sensitivity_sweep_refuses_a_result_it_did_not_measure():
    """The sweep's own anti-vacuity, provoked rather than described."""
    sys.path.insert(0, str(ROOT / "tools"))
    import blas_kernel_sensitivity as B

    ok_report = {"runs": [
        {"asked_for": None, "selected": "SkylakeX",
         "comparison": {"identical": 63, "regenerated": 63, "differing": 0,
                        "differing_files": []}},
        {"asked_for": "Nehalem", "selected": "Nehalem",
         "comparison": {"identical": 41, "regenerated": 63, "differing": 22,
                        "differing_files": ["a"]}},
    ]}
    assert B.problems(ok_report) == []

    all_same = {"runs": [dict(r, selected="SkylakeX")
                         for r in ok_report["runs"]]}
    assert any("changed nothing" in p for p in B.problems(all_same))

    one_run = {"runs": ok_report["runs"][:1]}
    assert any("nothing was compared" in p for p in B.problems(one_run))

    broken = {"runs": [ok_report["runs"][0],
                       {"asked_for": "Zen", "selected": "Zen"}]}
    assert any("no comparison" in p for p in B.problems(broken))

    # A KERNEL THE HOST CANNOT RUN IS AN OBSERVATION, not a failure. A
    # GitHub ubuntu-latest runner has no AVX-512 and cannot select SkylakeX
    # -- which is the kernel the committed outputs were produced with, and
    # therefore the single most useful row a hosted sweep produces. The
    # first version of this tool recorded it as a FAILED run and went red.
    with_unsupported = {"runs": ok_report["runs"] + [
        {"asked_for": "SkylakeX", "unsupported": True, "selected": "Haswell",
         "why": "asked for SkylakeX, this host selects 'Haswell' instead"}]}
    assert B.problems(with_unsupported) == []

    # ...but it must not stand in for a measurement either: a sweep where
    # everything was unsupported measured nothing.
    all_unsupported = {"runs": [
        {"asked_for": c, "unsupported": True, "selected": "Haswell",
         "why": "no"} for c in ("SkylakeX", "Zen")]}
    assert any("measured anything" in p
               for p in B.problems(all_unsupported))


def test_each_pinned_variable_actually_reaches_the_child_environment():
    """A row that pins nothing measures the host, not the variable.

    Three variables, three libraries, and only one of them is BLAS. numpy
    dispatches its own element-wise loops from the CPU's features
    independently of OPENBLAS_CORETYPE -- which is why pinning the kernel
    alone left this sandbox and a hosted runner three files apart, and
    pinning numpy as well landed on the runner exactly.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import blas_kernel_sensitivity as B

    bare = B._env(None, None, None)
    assert "OPENBLAS_CORETYPE" not in bare
    assert "NPY_DISABLE_CPU_FEATURES" not in bare

    full = B._env("Haswell", 1, B.AVX2_ONLY)
    assert full["OPENBLAS_CORETYPE"] == "Haswell"
    assert full["NPY_DISABLE_CPU_FEATURES"] == B.AVX2_ONLY
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS",
                "MKL_NUM_THREADS"):
        assert full[var] == "1", var

    # And an inherited value is CLEARED rather than carried, or a sweep run
    # in an already-pinned shell would report the shell's dispatch under
    # every row's name.
    import os
    os.environ["OPENBLAS_CORETYPE"] = "Nehalem"
    os.environ["NPY_DISABLE_CPU_FEATURES"] = "X86_V4"
    try:
        assert "OPENBLAS_CORETYPE" not in B._env(None, None, None)
        assert "NPY_DISABLE_CPU_FEATURES" not in B._env(None, None, None)
    finally:
        del os.environ["OPENBLAS_CORETYPE"]
        del os.environ["NPY_DISABLE_CPU_FEATURES"]

    # The default sweep must contain the row that reproduces another host.
    assert any(npy for _c, _t, npy in B.DEFAULT_ROWS), (
        "no default row pins numpy's dispatch, so the sweep cannot "
        "reproduce a host without AVX-512")


def test_the_probe_reports_the_kernel_a_host_would_actually_select():
    """Cheap, and asked BEFORE spending four minutes on a sweep row."""
    sys.path.insert(0, str(ROOT / "tools"))
    import blas_kernel_sensitivity as B

    selected, error = B._probe(None)
    assert not error and selected, (selected, error)
    forced, error = B._probe("Nehalem")
    assert not error and forced == "Nehalem", (forced, error)
    assert forced != selected, (
        "this host already selects Nehalem, so it cannot demonstrate that "
        "forcing changes anything")


def test_the_recorded_sweep_shows_a_real_kernel_dependence():
    """The committed measurement, checked for the shape it claims.

    Not re-run here -- each kernel is a full regeneration and the sweep
    takes minutes -- but a committed result that did not vary its variable,
    or that reported no dependence at all, would make the R59 boundary an
    assertion instead of a measurement.
    """
    import json

    rec = json.loads((ROOT / "docs" / "blas_kernel_sensitivity.json")
                     .read_text(encoding="utf-8"))
    assert rec["automatic_gate_effect"] == "NONE"
    assert rec["scientific_PASS_count"] == 0
    runs = rec["runs"]
    assert len(runs) >= 3, runs
    selected = {r["selected"] for r in runs}
    assert len(selected) >= 2, (
        f"every recorded run selected {selected}; the sweep varied nothing")
    measured = [r for r in runs if "comparison" in r]
    diffs = [r["comparison"]["differing"] for r in measured]
    assert any(v == 0 for v in diffs), (
        "no kernel reproduced the committed bytes, so the committed outputs "
        f"correspond to no kernel in this record: {diffs}")
    assert any(v > 0 for v in diffs), (
        f"no kernel diverged, so there is no dependence to report: {diffs}")

    # THE ROW THAT REPRODUCES A DIFFERENT MACHINE. Pinning the BLAS kernel
    # alone left this sandbox at 43/63 and a GitHub runner at 40/63 -- close,
    # and not the same, which is the state an explanation gets stuck in.
    # Pinning numpy's own SIMD dispatch as well lands on the runner's exact
    # numbers. A record without this row would be a story that fitted most
    # of the data.
    reproducing = [r for r in measured if r.get("npy_disable")]
    assert reproducing, (
        "the record contains no row that pins numpy's SIMD dispatch, so the "
        "residual between two hosts at the same BLAS kernel is unattributed")
    assert any(r["comparison"]["identical"] == 40
               and r["comparison"]["differing"] == 23
               for r in reproducing), (
        "no recorded row reproduces the hosted runner's 40/63; the "
        "attribution in docs/R59_CROSS_ENVIRONMENT_ANALYSIS.md is not "
        f"supported by this file: {[r['comparison'] for r in reproducing]}")

    # ANTI-VACUITY: pinning threads is in the record because it changed
    # NOTHING, which is a result and has to stay visible as one.
    threaded = [r for r in measured if r.get("threads")]
    assert threaded, "no thread-pinned row, so that variable is untested"
