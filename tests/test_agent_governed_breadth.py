"""The second governed workflow, its read guard, and the governed-only subtree.

WHAT THIS SUITE IS FOR

Three claims that used to be prose in the completion matrix and are now
checked here:

  1. more than one tool can actually run through the governed path. The
     registry held one entry and the subprocess module name was a literal in
     three places, so "the registry is a default-deny set of tools" described
     a set of size one that could not have grown without an edit in a
     different file;
  2. inside the governed output subtree the governed path is the ONLY route.
     Outside it the Stage-10 adapters stay directly callable on purpose, and
     that is asserted here rather than assumed, because a guard that turned
     out to block the ungoverned pipeline would be a regression the governed
     tests could not see;
  3. a tool that hashes paths a caller names needs a READ guard. The write
     allowlist says nothing about reads, and a governed digest of
     ``results_gate_table.csv`` would put a canonical output into a
     provenance chain the substrate is not allowed to mediate.

Every test runs against the real workspace, because the guards under test are
the real workspace's guards.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.canonical import digest_bytes  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_stage10 import (  # noqa: E402
    ACT_REEXECUTION, GovernedStage10, stage10_registry, tool_argv,
)
from qta_agent.tasks import TaskState  # noqa: E402
from qta_agent.tools import Determinism, ToolNotRegistered  # noqa: E402
from qta_multiphysics.stack import workspace as WS_GUARD  # noqa: E402

WS = "verification/stage10/_pytest_breadth"


@pytest.fixture()
def gov(request):
    name = request.node.name.replace("/", "_")[:60]
    base = ROOT / WS / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    g = GovernedStage10(root=ROOT, log=EventLog(base / "log.jsonl"),
                        evidence=EvidenceStore(base / "evidence"))
    g.out_rel = f"{WS}/{name}/out"
    yield g
    if base.exists():
        shutil.rmtree(base)


def _seed(gov, n=3):
    """Real files in the workspace for the index tool to read."""
    out = ROOT / gov.out_rel
    out.mkdir(parents=True, exist_ok=True)
    rels = []
    for i in range(n):
        p = out / f"seed_{i}.json"
        p.write_text(json.dumps({"i": i}, indent=2, sort_keys=True) + "\n")
        rels.append(p.relative_to(ROOT).as_posix())
    return rels


def _index(gov, files, name="index.json"):
    return gov.run(tool_id="stage10.digest_index",
                   inputs={"out_dir": gov.out_rel, "name": name,
                           "files": files})


def _declared(gov, run, name):
    """The run's DECLARED output, not whatever the sweep collected first.

    ``artifacts`` is the capture of the whole output directory -- the seed
    files and any earlier step's artifacts are in it too -- and taking its
    first key compares the wrong file. This test found that by passing when
    it should not have.
    """
    rel = f"{gov.out_rel}/{name}"
    assert rel in run.artifacts, (rel, sorted(run.artifacts))
    return json.loads((ROOT / rel).read_text())


# --------------------------------------------------------------------------
# 1. A SECOND TOOL THAT ACTUALLY RUNS
# --------------------------------------------------------------------------

def test_the_registry_holds_more_than_one_launchable_tool():
    """Registered AND launchable. The two used to be different sets."""
    reg = stage10_registry()
    ids = sorted(reg.ids())
    assert len(ids) >= 2, f"the governed registry still holds only {ids}"
    for tool_id in ids:
        argv = tool_argv(tool_id, {})
        assert argv[1] == "-m" and argv[2].startswith("qta_agent."), argv
    # Distinct entry points. Two registry rows pointing at one module would
    # satisfy the count above and would still be one tool.
    modules = {tool_argv(t, {})[2] for t in ids}
    assert len(modules) == len(ids), (
        f"{len(ids)} tools share {len(modules)} entry point(s): {modules}")


def test_an_unmapped_tool_id_is_refused_rather_than_defaulted():
    """A default entry point would run the WRONG tool under a real record."""
    with pytest.raises(ToolNotRegistered, match="no subprocess module"):
        tool_argv("stage10.not_a_tool", {})


def test_the_second_tool_reaches_VERIFIED_over_real_workspace_files(gov):
    run = _index(gov, _seed(gov))
    assert run.state is TaskState.VERIFIED, run.reason
    assert run.artifacts, "a verified run with no artifacts proves nothing"
    body = _declared(gov, run, "index.json")
    assert body["n_files"] == 3, body
    assert body["automatic_gate_effect"] == "NONE"
    for entry in body["files"]:
        assert digest_bytes((ROOT / entry["path"]).read_bytes()) \
            == entry["sha256"]


def test_the_index_result_depends_on_the_workspace_not_on_the_request(gov):
    """The property the first tool could not have.

    ``emit_artifact``'s output is a function of an argument already in the
    task record, so re-running it can only agree. This one's output is a
    function of bytes on disk: change them and the same request produces a
    different artifact.
    """
    files = _seed(gov)
    first = _index(gov, files, name="a.json")
    (ROOT / files[0]).write_text('{"i": 999}\n')
    second = _index(gov, files, name="b.json")
    assert first.state is TaskState.VERIFIED
    assert second.state is TaskState.VERIFIED
    a = _declared(gov, first, "a.json")
    b = _declared(gov, second, "b.json")
    assert a["files"] != b["files"], (
        "the index did not change when the workspace did, so it is not "
        "reading the workspace")


def test_re_execution_of_the_index_tool_is_a_real_comparison(gov):
    """A re-execution record, and it names the tool that was re-run."""
    run = _index(gov, _seed(gov))
    assert run.state is TaskState.VERIFIED, run.reason
    events = [e for e in gov.log.read() if e.action == ACT_REEXECUTION]
    assert events, "the verified run recorded no re-execution"
    assert events[-1].payload["tool_id"] == "stage10.digest_index"
    assert events[-1].payload["determinism"] == \
        Determinism.BYTE_IDENTICAL.value
    assert "reproduced byte-for-byte" in run.reason, run.reason


def test_a_missing_input_file_fails_the_run_rather_than_indexing_nothing(gov):
    """A genuine failure mode. The first tool has none reachable from here."""
    run = _index(gov, _seed(gov) + [f"{gov.out_rel}/absent.json"])
    assert run.state is not TaskState.VERIFIED
    assert run.state in (TaskState.FAILED, TaskState.TIMED_OUT), run.state


def test_an_empty_file_list_is_refused_rather_than_indexing_zero_files(gov):
    """"0 of 0 files hashed" reads exactly like a complete index."""
    run = _index(gov, [])
    assert run.state is not TaskState.VERIFIED, (
        "a zero-file index reached VERIFIED; vacuous success is the one "
        "failure this repository has already shipped once")


def test_a_runner_built_the_way_the_workflow_builds_one_re_executes(request):
    """THE FIXTURE WAS DOING THE PRODUCTION CALLER'S JOB.

    The re-execution check read its scratch directory from an attribute that
    only this suite's fixture ever set, so the Snakemake rule reached that
    line and raised ``AttributeError``. Every governed test passed. This
    builds the runner with exactly the three arguments the workflow passes
    and nothing else, which is the only way that class of defect is visible.
    """
    name = request.node.name[:60]
    base = ROOT / WS / name
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    try:
        g = GovernedStage10(root=ROOT, log=EventLog(base / "log.jsonl"),
                            evidence=EvidenceStore(base / "evidence"))
        assert not hasattr(g, "out_rel"), (
            "the runner grew the attribute the fixture used to supply; the "
            "regression this test exists for is back in reach")
        run = g.run(tool_id="stage10.emit_artifact",
                    inputs={"out_dir": f"{WS}/{name}/out",
                            "name": "artifact.json",
                            "payload": {"label": "MODEL_ONLY"}})
        assert run.state is TaskState.VERIFIED, run.reason
        assert "reproduced byte-for-byte" in run.reason, run.reason
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --------------------------------------------------------------------------
# 1b. THE RE-EXECUTION COMPARISON ITSELF
#
# Two mutations survived the first run of this suite's matrix, and both were
# findings rather than noise. Nothing made the re-run DISAGREE, so the
# comparison was exercised only in the case where it passes; and nothing
# asked what the comparison did when it covered no artifacts at all, which
# it answered with the same sentence it uses for a real one.
# --------------------------------------------------------------------------

def test_a_re_execution_disagreement_is_reported_rather_than_swallowed(gov):
    """The comparison must be able to say no.

    Driven directly rather than end to end: a tool that genuinely produced
    different bytes on a second run would be a tool declared BYTE_IDENTICAL
    that is not, and inventing one would test the invention. What can be
    made true honestly is the other half -- the run cites a digest the
    re-execution does not reproduce -- and that is the branch a verifier
    reaches when the artifact it is checking was tampered with.
    """
    files = _seed(gov)
    run = _index(gov, files)
    assert run.state is TaskState.VERIFIED, run.reason
    rel = f"{gov.out_rel}/index.json"
    ok, why = gov._reexecute_and_compare(
        tool_id="stage10.digest_index",
        inputs={"out_dir": gov.out_rel, "name": "index.json",
                "files": files},
        declared={rel: "0" * 64},
        verifier="verifier", task_id=run.task_id)
    assert not ok, (
        "the re-execution reported agreement with a digest it did not "
        f"reproduce: {why}")
    assert "different bytes" in why, why


def test_a_comparison_that_covers_nothing_is_not_agreement(gov):
    """"0 artifact(s) reproduced byte-for-byte" is the vacuous sentence."""
    files = _seed(gov)
    run = _index(gov, files)
    ok, why = gov._reexecute_and_compare(
        tool_id="stage10.digest_index",
        inputs={"out_dir": gov.out_rel, "name": "index.json",
                "files": files},
        declared={},
        verifier="verifier", task_id=run.task_id)
    assert not ok, (
        f"an empty comparison reported success: {why}")
    assert "empty comparison" in why, why


# --------------------------------------------------------------------------
# 2. THE READ GUARD
# --------------------------------------------------------------------------

@pytest.mark.parametrize("target", [
    "results_gate_table.csv",           # a canonical, byte-gated output
    "final_manifest.json",              # the manifest
    "README.md",
    "/etc/hostname",
])
def test_the_index_tool_refuses_to_hash_anything_outside_the_workspace(
        gov, target):
    run = _index(gov, _seed(gov) + [target])
    assert run.state is not TaskState.VERIFIED, (
        f"a governed run hashed {target}, which is outside the workspace; "
        "a digest of a canonical output inside a provenance chain reads as "
        "the substrate having mediated it")


def test_a_symlink_out_of_the_workspace_is_refused_by_the_read_guard(gov):
    files = _seed(gov)
    link = ROOT / gov.out_rel / "escape.json"
    link.symlink_to(ROOT / "final_manifest.json")
    try:
        run = _index(gov, files + [link.relative_to(ROOT).as_posix()])
        assert run.state is not TaskState.VERIFIED, (
            "a symlink inside the workspace resolved to a canonical file and "
            "was hashed under its workspace name")
    finally:
        link.unlink(missing_ok=True)


def test_the_read_guard_is_the_subprocess_exit_status_not_a_convention():
    """Run the tool directly. A guard only the caller applies is advisory."""
    scratch = ROOT / "verification/stage10/_pytest_breadth_direct"
    try:
        proc = subprocess.run(
            tool_argv("stage10.digest_index",
                      {"out_dir": scratch.relative_to(ROOT).as_posix(),
                       "name": "x.json", "files": ["final_manifest.json"]}),
            cwd=ROOT, capture_output=True, text=True)
        assert proc.returncode == 3, (proc.returncode, proc.stderr[-400:])
        assert "refusing to index" in proc.stderr
        assert not scratch.exists(), (
            "the tool created its output directory before deciding whether "
            "it was allowed to read its inputs")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --------------------------------------------------------------------------
# 3. THE GOVERNED-ONLY SUBTREE
# --------------------------------------------------------------------------

def test_an_ungoverned_writer_cannot_write_into_the_governed_subtree():
    # ANTI-VACUITY FIRST. The body below is a loop, and a loop over an empty
    # tuple passes without checking anything -- which is exactly what
    # happened when a mutation emptied GOVERNED_ONLY.
    prefixes = WS_GUARD.GOVERNED_ONLY
    assert prefixes, "no governed-only prefixes, so this test checked nothing"
    for prefix in prefixes:
        target = WS_GUARD.workspace_root() / prefix / "_pytest_intruder.json"
        # CLEANED UP IN A FINALLY, because a failing run of this test leaves
        # the forged file behind and every LATER run then fails on the
        # leftover rather than on the guard. Under a mutation matrix that is
        # not an inconvenience: the suite goes red at the first surviving
        # mutation and every mutation after it is scored against a red
        # baseline, which is the one trap the harness exists to close.
        try:
            with pytest.raises(ValueError,
                               match="governed Stage-10 path only"):
                WS_GUARD.write_json_deterministic(target, {"forged": True})
            assert not target.exists(), (
                f"{target} exists after a refused write")
        finally:
            target.unlink(missing_ok=True)


def test_the_governed_scope_permits_it_and_restores_on_the_way_out():
    prefix = WS_GUARD.GOVERNED_ONLY[0]
    target = WS_GUARD.workspace_root() / prefix / "_pytest_scoped.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with WS_GUARD.governed_writer():
            assert WS_GUARD.is_governed_writer()
            WS_GUARD.write_json_deterministic(target, {"ok": True})
        assert target.is_file()
        assert not WS_GUARD.is_governed_writer()
    finally:
        target.unlink(missing_ok=True)


def test_the_scope_is_restored_even_when_the_body_raises():
    """A tool that fails partway must not leave the process able to write."""
    with pytest.raises(RuntimeError):
        with WS_GUARD.governed_writer():
            raise RuntimeError("boom")
    assert not WS_GUARD.is_governed_writer()


def test_nesting_restores_the_previous_state_rather_than_clearing_it():
    with WS_GUARD.governed_writer():
        with WS_GUARD.governed_writer():
            pass
        assert WS_GUARD.is_governed_writer(), (
            "an inner scope closing revoked the outer one's permission")
    assert not WS_GUARD.is_governed_writer()


def test_the_ungoverned_adapters_are_still_callable_everywhere_else():
    """THE OTHER HALF, and the one a guard can silently break.

    The substrate is additive by design: the byte-gated scientific pipeline
    must not depend on it. If this ever fails, the guard above stopped being
    a route guard and became a dependency.
    """
    target = (WS_GUARD.workspace_root() / "_pytest_ungoverned"
              / "plain.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        assert not WS_GUARD.is_governed_writer()
        sha = WS_GUARD.write_json_deterministic(target, {"plain": True})
        assert len(sha) == 64 and target.is_file()
    finally:
        shutil.rmtree(target.parent, ignore_errors=True)


def test_the_governed_prefixes_are_inside_the_workspace_allowlist():
    """A prefix outside the workspace would be unreachable by every writer."""
    assert WS_GUARD.GOVERNED_ONLY, "an empty governed-only set guards nothing"
    for prefix in WS_GUARD.GOVERNED_ONLY:
        p = WS_GUARD.workspace_root() / prefix
        assert WS_GUARD.governed_only_prefix(p) == prefix
        assert WS_GUARD.governed_only_prefix(p / "deep" / "file.json") == prefix
    # And a sibling whose name merely starts with a governed prefix is NOT
    # covered: prefix matching on strings would have swallowed it.
    sibling = WS_GUARD.workspace_root() / (WS_GUARD.GOVERNED_ONLY[0] + "side")
    assert WS_GUARD.governed_only_prefix(sibling) == ""
