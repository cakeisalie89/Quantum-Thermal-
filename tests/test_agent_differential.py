"""Two implementations, one log: do they agree?

WHY THIS IS A SEPARATE KIND OF EVIDENCE

A test asserts that one implementation does what its author expected. A
differential test asserts that two implementations, written separately and
sharing no reducer, reach the same verdict from the same bytes. The second
kind survives a shared misunderstanding that the first does not: an author who
misread the spec writes the test to match the code.

It also has a specific job here. The task projection is the one on the
PRODUCTION path, and it turned out to re-authorize forged records against a
starting state the record itself declared -- a hole that every one of its own
tests passed straight over. A second reader is the defence against that class,
not because it is more careful, but because two readers that disagree say so
while a single reader with a hole says nothing.

WHAT A DIVERGENCE MEANS, AND WHAT IT DOES NOT

An empty diff is evidence, not proof: both could share a mistake the log
cannot reveal. A non-empty diff is a finding one of the two has to answer for,
and it names which field disagrees rather than reporting "they differ".
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.governed_stage10 import (  # noqa: E402
    ACT_TASK_TRANSITION, SUBMITTER_ID, VERIFIER_ID, WORKER_ID,
    GovernedStage10,
)
from qta_agent.reconstruct import (  # noqa: E402
    compare_tasks, reconstruct_tasks,
)
from qta_agent.tasks import TaskState, TaskTransitionError  # noqa: E402

WS = "verification/stage10/_pytest_diff"


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


def _run(gov, **over):
    kw = dict(tool_id="stage10.emit_artifact",
              inputs={"out_dir": gov.out_rel, "name": "a.json",
                      "payload": {"v": 1}},
              submitter=SUBMITTER_ID, worker=WORKER_ID, verifier=VERIFIER_ID)
    kw.update(over)
    return gov.run(**kw)


# --- agreement on a healthy history -----------------------------------------

def test_the_two_readers_agree_on_a_governed_run(gov):
    run = _run(gov)
    recon = reconstruct_tasks(gov.log)
    assert compare_tasks(gov.projection(), recon) == ()
    assert recon.verified_ids() == (run.task_id,)
    assert not recon.unauthorized and not recon.anomalies


def test_the_two_readers_agree_across_several_runs(gov):
    ids = []
    for i in range(3):
        ids.append(_run(gov, inputs={"out_dir": gov.out_rel,
                                     "name": f"a{i}.json",
                                     "payload": {"v": i}}).task_id)
    recon = reconstruct_tasks(gov.log)
    assert compare_tasks(gov.projection(), recon) == ()
    assert set(recon.verified_ids()) == set(ids)


def test_they_agree_on_a_run_that_was_rejected(gov):
    """Agreement on refusals matters more than agreement on successes.

    A success is the path both were written for. A rejection is where two
    readers most easily drift, because it is the branch nobody re-reads.
    """
    run = _run(gov, inputs={"out_dir": gov.out_rel, "name": "a.json",
                            "payload": "not-a-dict"})
    assert run.state is TaskState.REJECTED
    recon = reconstruct_tasks(gov.log)
    assert compare_tasks(gov.projection(), recon) == ()
    assert recon.states()[run.task_id] == TaskState.REJECTED.value


def test_the_reconstruction_shares_no_reducer_with_the_projection():
    """Guard against someone 'simplifying' the duplication away.

    Reusing the projection's reducer here would make every comparison in this
    file circular and worthless, while still passing.
    """
    import ast

    path = ROOT / "qta_agent" / "reconstruct.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.lstrip("."))
            imported.update(f"{node.module.lstrip('.')}.{a.name}"
                            for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    # Checked as IMPORTS rather than as text: the first version of this test
    # matched the module name in a docstring that explains the separation,
    # which would have failed for saying the right thing.
    assert not {i for i in imported if "governed_stage10" in i}, (
        "reconstruct imported the module it is supposed to check "
        "independently")
    assert "tasks.apply_transition" not in imported, (
        "reconstruct reused the projection's transition applier, which makes "
        "every comparison in this file circular while still passing")
    # THIS ASSERTION USED TO SAY THE OPPOSITE.
    #
    # It read:
    #
    #     # It MAY import the transition table -- re-authorizing against a
    #     # different table would compare two different questions -- but it
    #     # must derive the resulting state itself.
    #     assert "tasks.check" in imported or "tasks" in imported
    #
    # which made the coupling a REQUIREMENT: the second reader had to call
    # the gate it exists to second-guess, and a test stood guard over that.
    # The concern behind it was real -- two tables that drift compare two
    # different questions -- but the remedy was the wrong one. Drift is now
    # a conformance failure with a named difference (see the tests below);
    # agreement by construction was a failure nothing outside could see.
    assert not {i for i in imported
                if i == "tasks" or i.startswith("tasks.")
                or i == "authority" or i.startswith("authority.")}, (
        "reconstruct imports the authorization gates it exists to check "
        "independently; it would then agree with a weakened gate perfectly")


# --- the divergence the second reader exists to catch -----------------------

def test_both_readers_refuse_a_record_that_names_its_own_starting_state(gov):
    """The defect a hostile campaign found, checked from the other side.

    The record declares src=EXECUTING for a task sitting in VERIFIED, and
    EXECUTING -> TIMED_OUT is a real edge, so a reader that trusted the claim
    would apply it and move a sealed task.
    """
    run = _run(gov)
    gov.log.append(
        actor="attacker", action=ACT_TASK_TRANSITION, target=run.task_id,
        payload={"task_id": run.task_id, "src": TaskState.EXECUTING.value,
                 "dst": TaskState.TIMED_OUT.value, "role": "SYSTEM"})

    # Enforcement refuses the whole history and says why.
    with pytest.raises(TaskTransitionError, match="moves it from"):
        gov.projection()

    # Diagnosis keeps going, names the anomaly, and does NOT apply it.
    recon = reconstruct_tasks(gov.log)
    assert any("claims src EXECUTING" in a for a in recon.anomalies), \
        recon.anomalies
    assert recon.states()[run.task_id] == TaskState.VERIFIED.value, (
        "the independent replay applied a record whose starting state it "
        "disagreed with")


def test_an_unauthorized_transition_is_reported_and_not_applied(gov):
    """A record the machine would refuse today stays a record, not a state."""
    run = _run(gov)
    gov.log.append(
        actor=WORKER_ID, action=ACT_TASK_TRANSITION, target=run.task_id,
        payload={"task_id": run.task_id, "src": TaskState.VERIFIED.value,
                 "dst": TaskState.COMPLETED.value, "role": "WORKER"})
    recon = reconstruct_tasks(gov.log)
    assert any("would be refused today" in u for u in recon.unauthorized), \
        recon.unauthorized
    assert recon.states()[run.task_id] == TaskState.VERIFIED.value


def test_a_transition_for_a_task_that_was_never_created_is_an_anomaly(
        tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    log.append(actor="w", action=ACT_TASK_TRANSITION, target="ghost",
               payload={"task_id": "ghost", "src": "CREATED",
                        "dst": "VALIDATED", "role": "SUBMITTER"})
    recon = reconstruct_tasks(log)
    assert any("unknown task" in a for a in recon.anomalies), recon.anomalies
    assert recon.tasks == {}


def test_a_task_created_twice_is_an_anomaly(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    for _ in range(2):
        log.append(actor="s", action="task.create", target="t1",
                   payload={"task_id": "t1", "tool_id": "probe",
                            "submitter": "s", "inputs_digest": "a" * 64})
    recon = reconstruct_tasks(log)
    assert any("created twice" in a for a in recon.anomalies), recon.anomalies


def test_an_unknown_action_is_reported_rather_than_skipped(tmp_path):
    """The FOREIGN/UNKNOWN split, from the diagnostic side.

    This module reports instead of raising -- one unreadable record must not
    hide the twenty after it -- but it must not silently drop one either.
    """
    log = EventLog(tmp_path / "log.jsonl")
    log.append(actor="x", action="future.schema", target="t",
               payload={"task_id": "t"})
    recon = reconstruct_tasks(log)
    assert any("unknown action" in a for a in recon.anomalies), recon.anomalies


def test_foreign_events_are_counted_not_mistaken_for_task_records(gov):
    """Several subsystems share one log; the count says so out loud."""
    _run(gov)
    recon = reconstruct_tasks(gov.log)
    assert recon.foreign_events > 0
    assert recon.events_replayed == gov.log.verify().count


def test_compare_names_the_field_that_disagrees(gov):
    """A diff saying only 'they differ' would send a reader to read both."""
    run = _run(gov)
    recon = reconstruct_tasks(gov.log)
    recon.tasks[run.task_id]["state"] = TaskState.FAILED.value
    diffs = compare_tasks(gov.projection(), recon)
    assert len(diffs) == 1
    assert diffs[0].record_id == run.task_id
    assert diffs[0].field_name == "state"
    assert diffs[0].live == "VERIFIED" and diffs[0].reconstructed == "FAILED"
    assert "live='VERIFIED'" in str(diffs[0])


def test_a_task_present_in_one_reader_only_is_a_divergence(gov):
    run = _run(gov)
    recon = reconstruct_tasks(gov.log)
    recon.tasks["phantom"] = dict(recon.tasks[run.task_id], task_id="phantom")
    diffs = compare_tasks(gov.projection(), recon)
    assert [d.field_name for d in diffs] == ["<presence>"]
    assert diffs[0].record_id == "phantom"
    assert diffs[0].live == "ABSENT"


def test_the_reconstruction_refuses_a_tampered_log(gov):
    """A second opinion about a rewritten document is not a second opinion."""
    from qta_agent.events import ChainBroken

    _run(gov)
    lines = gov.log.path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["payload"]["note"] = "tampered"
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    gov.log.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ChainBroken):
        reconstruct_tasks(gov.log)


def test_the_differential_is_part_of_the_production_path():
    """A second reader nobody runs is a second reader of nothing."""
    rule = (ROOT / "Snakefile").read_text(encoding="utf-8") \
        .split("rule s10_governed:", 1)[1].split("\nrule ", 1)[0]
    assert "reconstruct_tasks" in rule
    assert "compare_tasks" in rule
    assert "assert not divergences" in rule
    assert "recon.unauthorized" in rule and "recon.anomalies" in rule
    # And the reader for every OTHER subsystem, on the same path for the
    # same reason: one that only tests run is a second reader of nothing.
    assert "reconstruct_subsystems" in rule
    assert "compare_subsystems" in rule
    assert "assert not sub_divergences" in rule
    assert "subs.anomalies" in rule


def test_the_two_readers_agree_about_who_holds_a_lease(gov):
    """A lease that outlives its work is state, and a diff must see it.

    Nothing else in the comparison would notice: the task's STATE is right
    either way, and only the lease says whether the work is still owned by a
    worker that has already stopped. Stranded work looks exactly like busy
    work until somebody tries to take it.
    """
    run = _run(gov)
    live = gov.projection().tasks[run.task_id]
    recon = reconstruct_tasks(gov.log)

    assert live.lease is None, (
        "a VERIFIED task still holds a lease; nobody else could take this "
        "work if it ever needed redoing")
    assert recon.tasks[run.task_id]["lease"] is None
    assert compare_tasks(gov.projection(), recon) == ()

    # And a divergence in the lease alone is reported, named.
    recon.tasks[run.task_id]["lease"] = {"lease_id": "L-ghost",
                                         "holder": "worker-gone",
                                         "granted_seq": 1,
                                         "expires_after_seq": 99}
    diffs = compare_tasks(gov.projection(), recon)
    assert [d.field_name for d in diffs] == ["lease"]


# --- the second reader had the same hole, one line lower ---------------------

def _forged_history(tmp_path, *, execution_record: bool):
    """A task moved end to end by ONE actor, naming a ghost as its executor."""
    from qta_agent.canonical import digest_bytes

    log = EventLog(tmp_path / "log.jsonl")
    tid = "t-forged"
    dg = digest_bytes(b"a result nobody produced")

    def tr(src, dst, role, **extra):
        payload = {"task_id": tid, "src": src, "dst": dst, "role": role}
        payload.update(extra)
        log.append(actor="mallory", action=ACT_TASK_TRANSITION, target=tid,
                   payload=payload)

    log.append(actor="mallory", action="task.create", target=tid,
               payload={"task_id": tid, "tool_id": "probe",
                        "submitter": "mallory", "inputs_digest": dg})
    tr("CREATED", "VALIDATED", "SUBMITTER")
    tr("VALIDATED", "QUEUED", "SCHEDULER")
    tr("QUEUED", "LEASED", "WORKER",
       lease={"lease_id": "L1", "holder": "mallory", "granted_seq": 3,
              "expires_after_seq": 9999})
    tr("LEASED", "EXECUTING", "WORKER", lease_id="L1")
    if execution_record:
        log.append(actor="mallory", action="task.execution", target=tid,
                   payload={"task_id": tid, "result_digest": dg,
                            "outcome": "COMPLETED", "tool_id": "probe"})
    tr("EXECUTING", "COMPLETED", "WORKER", lease_id="L1",
       executed_by="a-ghost", result_digest=dg)
    tr("COMPLETED", "VERIFIED", "VERIFIER")
    return log, tid


@pytest.mark.parametrize("execution_record", [True, False])
def test_the_reconstruction_does_not_take_the_executor_from_a_payload(
        tmp_path, execution_record):
    """The line that put this reader back underneath a fixed bypass.

    The re-authorization above it was already correct: it probed with the
    executor THIS replay had derived, from the execution record. Then, five
    lines later, ``cur["executed_by"] = p["executed_by"]`` overwrote that
    with the payload's claim -- in time for the NEXT transition to be checked
    against the forger's choice of counterparty.

    So the second opinion agreed with the first one's defect while looking
    like an independent check. Both parametrizations matter: with an
    execution record the claim contradicts a known executor, and without one
    it invents an executor from nothing.
    """
    log, tid = _forged_history(tmp_path, execution_record=execution_record)
    recon = reconstruct_tasks(log)

    assert recon.states()[tid] != TaskState.VERIFIED.value
    assert recon.verified_ids() == ()
    assert any("as its executor" in a for a in recon.anomalies), \
        recon.anomalies
    assert any("would be refused today" in u for u in recon.unauthorized), \
        recon.unauthorized


def test_both_readers_refuse_the_forged_history_the_same_way(tmp_path, gov):
    """Agreement about a REFUSAL is the comparison that matters here.

    The production projection raises; this reader records and continues --
    that difference is designed. What must not differ is the verdict: if one
    of them called this task VERIFIED the package's central claim would be
    false, and the diff is what says so.
    """
    from qta_agent.governed_stage10 import GovernedStage10

    log, tid = _forged_history(tmp_path, execution_record=True)
    g2 = GovernedStage10(root=ROOT, log=log, evidence=gov.evidence)
    with pytest.raises(TaskTransitionError, match="execution record says"):
        g2.projection()
    assert reconstruct_tasks(log).verified_ids() == ()


def test_the_second_reader_refuses_a_create_in_somebody_elses_name(gov):
    """The submitter is who wrote the request, in this reader's words too.

    Restated here for the reason every rule in the second reader is: the
    production projection refuses this record, so a log carrying it never
    reaches compare_tasks at all. If this reader is silent, nothing in the
    system says anything about the forgery.
    """
    _run(gov)
    gov.log.append(actor="mallory", action="task.create", target="t-forged",
                   payload={"task_id": "t-forged",
                            "tool_id": "stage10.emit_artifact",
                            "submitter": SUBMITTER_ID,
                            "inputs_digest": "a" * 64, "depends_on": []})
    recon = reconstruct_tasks(gov.log)
    assert any("appended by 'mallory'" in a for a in recon.anomalies), \
        recon.anomalies
    # Reported AND not folded: a reader that notes the anomaly and then
    # builds the task anyway has told the truth and believed the lie.
    assert "t-forged" not in recon.tasks


def test_an_honest_create_is_not_flagged_by_the_second_reader(gov):
    """Anti-vacuity for the rule above."""
    run = _run(gov)
    recon = reconstruct_tasks(gov.log)
    assert not recon.anomalies, recon.anomalies
    assert recon.tasks[run.task_id]["submitter"] == SUBMITTER_ID


# ---------------------------------------------------------------------------
# D-2026-27 (P0-R12): the second reader used to ask the first reader whether
# the first reader would have allowed it.
#
# reconstruct.py imported authority.check and tasks.check and handed every
# replayed record straight back to them. Everything below asserts the three
# things that makes necessary:
#
#   1. THE COUPLING IS GONE, checked over the parsed source rather than over
#      prose about the separation.
#   2. THE RESTATEMENT IS FAITHFUL, element by element, so that drift between
#      the two statements is a named difference rather than a silent one.
#      This is the concern the old assertion was trying to serve; it is
#      served here, where it cannot make the reader circular.
#   3. THE RESTATEMENT IS LOAD-BEARING: weaken the production gate at runtime
#      and the second reader still refuses. That is the property the whole
#      module claims and the one the import made impossible.
#
# Every rule restated in reconstruct.py gets a pair below: a record the rule
# refuses, and a neighbouring record it does not. A refusal that fires for
# everything proves nothing about the rule it is named after.
# ---------------------------------------------------------------------------
import ast as _ast  # noqa: E402

from qta_agent import authority as _authority  # noqa: E402
from qta_agent import reconstruct as _R  # noqa: E402
from qta_agent import tasks as _tasks  # noqa: E402
from qta_agent.canonical import digest_bytes as _digest_bytes  # noqa: E402
from qta_agent.reconstruct import reconstruct as _reconstruct  # noqa: E402

_DG = _digest_bytes(b"a report")
_DG2 = _digest_bytes(b"a result")


def _recon_source_tree():
    path = ROOT / "qta_agent" / "reconstruct.py"
    return _ast.parse(path.read_text(encoding="utf-8"))


def test_the_second_reader_imports_neither_authorization_gate():
    """No import of authority or tasks, under any spelling.

    Checked as parsed imports, not as text: this file's own prose names both
    modules repeatedly, and a substring search would fail for explaining the
    separation correctly.
    """
    imported: set = set()
    for node in _ast.walk(_recon_source_tree()):
        if isinstance(node, _ast.ImportFrom) and node.module:
            imported.add(node.module.lstrip("."))
        elif isinstance(node, _ast.Import):
            imported.update(a.name for a in node.names)

    leaked = sorted(
        i for i in imported
        if i.split(".")[-1] in {"authority", "tasks"}
        or i in {"authority", "tasks"})
    assert not leaked, (
        f"the second reader imports {leaked}; every rule it re-checks would "
        "then come from the implementation it is re-checking")


def test_the_second_reader_calls_no_function_named_check():
    """The import guard's blind spot: a late import inside a function body.

    ``from .tasks import check`` at module scope is what the guard above
    catches. ``from .tasks import check`` inside ``reconstruct_tasks`` would
    slip past a check that only looked at the top of the file, so the call
    graph is inspected too: nothing in this module may call a bare ``check``
    or ``task_check``.
    """
    called: set = set()
    for node in _ast.walk(_recon_source_tree()):
        if not isinstance(node, _ast.Call):
            continue
        fn = node.func
        if isinstance(fn, _ast.Name):
            called.add(fn.id)
        elif isinstance(fn, _ast.Attribute):
            called.add(fn.attr)

    forbidden = sorted(called & {"check", "task_check", "apply_transition",
                                 "find_edge", "allowed_targets"})
    assert not forbidden, (
        f"the second reader calls {forbidden}; a reader that asks the gate "
        "whether the gate would have allowed something agrees with a broken "
        "gate perfectly")


# --- 2. the restatement is faithful ----------------------------------------

def test_the_restated_authority_vocabulary_matches_production():
    """Drift between the two statements is a named difference, not silence.

    This is the ONLY thing in the system that compares them. It lives here,
    in a test that may import both, rather than in the reader, where the
    comparison would have to be made by calling one of them.
    """
    assert {s.value for s in _authority.State} == _R._AUTH_STATES
    assert {r.value for r in _authority.Role} == _R._AUTH_ROLES
    assert {s.value for s in _authority.TERMINAL} == _R._AUTH_TERMINAL
    assert _authority.INITIAL.value == _R._AUTH_INITIAL
    assert {s.value for s in _authority.CANONICAL} == {_R._AUTH_PROMOTED}


def test_the_restated_authority_edges_match_production():
    live = {
        (e.src.value, e.dst.value): (
            frozenset(r.value for r in e.roles),
            frozenset(e.requires_evidence),
            e.requires_distinct_actor,
        )
        for e in _authority.EDGES
    }
    mine = {
        pair: (rule.roles, rule.evidence, rule.distinct_actor)
        for pair, rule in _R._AUTH_EDGES.items()
    }
    assert sorted(live) == sorted(mine), (
        f"edges only in production: {sorted(set(live) - set(mine))}; "
        f"only in the second reader: {sorted(set(mine) - set(live))}")
    for pair in sorted(live):
        assert live[pair] == mine[pair], (
            f"{pair} differs: production {live[pair]}, "
            f"second reader {mine[pair]}")


def test_the_restated_task_vocabulary_matches_production():
    assert {s.value for s in _tasks.TaskState} == _R._TASK_STATES
    assert {r.value for r in _tasks.TaskRole} == _R._TASK_ROLES
    assert {s.value for s in _tasks.TERMINAL} == _R._TASK_TERMINAL
    assert _tasks.INITIAL.value == _R._TASK_INITIAL


def test_the_restated_task_edges_match_production():
    live = {
        (e.src.value, e.dst.value): (
            frozenset(r.value for r in e.roles),
            e.requires_distinct_actor,
            e.requires_lease,
        )
        for e in _tasks.EDGES
    }
    mine = {
        pair: (rule.roles, rule.distinct_actor, rule.lease)
        for pair, rule in _R._TASK_EDGES.items()
    }
    assert sorted(live) == sorted(mine), (
        f"edges only in production: {sorted(set(live) - set(mine))}; "
        f"only in the second reader: {sorted(set(mine) - set(live))}")
    for pair in sorted(live):
        assert live[pair] == mine[pair], (
            f"{pair} differs: production {live[pair]}, "
            f"second reader {mine[pair]}")


def test_the_restated_lease_shape_matches_production():
    """A lease this reader accepts is one the production record can hold."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(_tasks.Lease)}
    required = {f.name for f in dataclasses.fields(_tasks.Lease)
                if f.default is dataclasses.MISSING}
    assert required == _R._LEASE_REQUIRED
    assert fields - required == _R._LEASE_OPTIONAL


def test_the_restated_digest_rule_matches_production():
    """Including the cases that are not 64 lowercase hex characters."""
    from qta_agent.canonical import is_digest

    for value in ("a" * 64, "A" * 64, "a" * 63, "a" * 65, "", "g" * 64,
                  "0123456789abcdef" * 4, None, 7, b"a" * 64,
                  "a" * 63 + "\n"):
        assert is_digest(value) == _R._is_digest(value), value


# --- 3. the restatement is load-bearing ------------------------------------
#
# Weaken the production gate at runtime and ask the second reader again. It
# must reach the same refusal from its own rules. Each of these would have
# FAILED before D-2026-27: the reader called the weakened function.

def _auth_log(tmp_path, name, moves, *, proposer="alice", create=None):
    """A record and a sequence of transitions, straight into the log."""
    log = EventLog(tmp_path / f"{name}.jsonl")
    payload = {"record_id": "r1", "kind": "claim", "proposer": proposer,
               "evidence": {}, "depends_on": [], "policy_id": None}
    payload.update(create or {})
    log.append(actor=proposer or "nobody", action="record.create",
               target="r1", payload=payload)
    for actor, move in moves:
        log.append(actor=actor, action="record.transition", target="r1",
                   payload={"record_id": "r1", **move})
    return log


_REVIEW = ("bob", {"src": "PROPOSED", "dst": "UNDER_REVIEW",
                   "role": "VERIFIER"})
_VERIFY = ("bob", {"src": "UNDER_REVIEW", "dst": "VERIFIED",
                   "role": "VERIFIER",
                   "evidence": {"verification_report": _DG}})
_SELF_VERIFY = ("alice", {"src": "UNDER_REVIEW", "dst": "VERIFIED",
                          "role": "VERIFIER",
                          "evidence": {"verification_report": _DG}})


def test_the_second_reader_refuses_a_self_verification_a_weakened_gate_allows(
        tmp_path, monkeypatch):
    """Separation of duties, with the production rule switched off."""
    import dataclasses

    req = _authority.TransitionRequest(
        record_id="r1", src=_authority.State.UNDER_REVIEW,
        dst=_authority.State.VERIFIED, actor="alice",
        role=_authority.Role.VERIFIER,
        evidence={"verification_report": _DG}, proposer="alice")

    # Today's gate refuses it. Asserted so the weakening below is a CHANGE.
    with pytest.raises(_authority.TransitionError, match="I4"):
        _authority.check(req)

    weak = tuple(dataclasses.replace(e, requires_distinct_actor=False)
                 for e in _authority.EDGES)
    monkeypatch.setattr(_authority, "EDGES", weak)
    monkeypatch.setattr(_authority, "_BY_PAIR",
                        {(e.src, e.dst): e for e in weak})

    # Anti-vacuity: the weakening is real, and it reaches the gate.
    assert _authority.check(req) is not None

    log = _auth_log(tmp_path, "weak_i4", [_REVIEW, _SELF_VERIFY])
    recon = _reconstruct(log)
    assert any("I4:" in u for u in recon.unauthorized), recon.unauthorized
    assert recon.records["r1"]["state"] == "UNDER_REVIEW"


def test_the_second_reader_refuses_an_edge_a_weakened_gate_invents(
        tmp_path, monkeypatch):
    """I1: promotion requires prior verification, even if the table forgets.

    The mutation here is an ADDITION rather than a removal, because that is
    how this invariant actually dies: not by deleting the VERIFIED ->
    PROMOTED edge but by adding a shortcut beside it.
    """
    shortcut = _authority.Edge(
        _authority.State.PROPOSED, _authority.State.PROMOTED,
        frozenset({_authority.Role.PROMOTER}), reason="a shortcut")
    weak = _authority.EDGES + (shortcut,)
    monkeypatch.setattr(_authority, "EDGES", weak)
    monkeypatch.setattr(_authority, "_BY_PAIR",
                        {(e.src, e.dst): e for e in weak})

    req = _authority.TransitionRequest(
        record_id="r1", src=_authority.State.PROPOSED,
        dst=_authority.State.PROMOTED, actor="carol",
        role=_authority.Role.PROMOTER, proposer="alice", policy_id="pol-1")
    assert _authority.check(req) is not None          # anti-vacuity

    log = _auth_log(tmp_path, "weak_i1", [
        ("carol", {"src": "PROPOSED", "dst": "PROMOTED", "role": "PROMOTER",
                   "policy_id": "pol-1"})])
    recon = _reconstruct(log)
    assert any("no edge PROPOSED -> PROMOTED" in u
               for u in recon.unauthorized), recon.unauthorized
    assert recon.canonical_ids() == ()


def test_the_second_reader_refuses_a_self_verified_task_a_weakened_gate_allows(
        tmp_path, monkeypatch):
    """The same mutation on the other machine."""
    import dataclasses

    weak = tuple(dataclasses.replace(e, requires_distinct_actor=False)
                 for e in _tasks.EDGES)
    monkeypatch.setattr(_tasks, "EDGES", weak)
    monkeypatch.setattr(_tasks, "_BY_PAIR",
                        {(e.src, e.dst): e for e in weak})

    task = _tasks.Task(task_id="t1", tool_id="probe", submitter="alice",
                       inputs_digest=_DG, state=_tasks.TaskState.COMPLETED,
                       executed_by="worker", result_digest=_DG2)
    req = _tasks.TaskTransition(
        task_id="t1", src=_tasks.TaskState.COMPLETED,
        dst=_tasks.TaskState.VERIFIED, actor="worker",
        role=_tasks.TaskRole.VERIFIER, at_seq=99)
    # Anti-vacuity: the weakening is real, and it reaches the gate.
    assert _tasks.check(req, task) is not None

    log = _task_log(tmp_path, "weak_task_i4", _SELF_VERIFIED_CHAIN)
    recon = reconstruct_tasks(log)
    assert any("may not also perform" in u for u in recon.unauthorized), \
        recon.unauthorized
    assert recon.verified_ids() == ()


def test_the_second_reader_refuses_a_lapsed_lease_a_weakened_gate_allows(
        tmp_path, monkeypatch):
    """Possession, with the production requirement switched off."""
    import dataclasses

    weak = tuple(dataclasses.replace(e, requires_lease=False)
                 for e in _tasks.EDGES)
    monkeypatch.setattr(_tasks, "EDGES", weak)
    monkeypatch.setattr(_tasks, "_BY_PAIR",
                        {(e.src, e.dst): e for e in weak})

    task = _tasks.Task(task_id="t1", tool_id="probe", submitter="alice",
                       inputs_digest=_DG, state=_tasks.TaskState.LEASED,
                       lease=_tasks.Lease(lease_id="L1", holder="worker",
                                          granted_seq=0,
                                          expires_after_seq=0))
    req = _tasks.TaskTransition(
        task_id="t1", src=_tasks.TaskState.LEASED,
        dst=_tasks.TaskState.EXECUTING, actor="worker",
        role=_tasks.TaskRole.WORKER, at_seq=99, lease_id="L1")
    assert _tasks.check(req, task) is not None        # anti-vacuity

    log = _task_log(tmp_path, "weak_task_lease", _LAPSED_LEASE_CHAIN)
    recon = reconstruct_tasks(log)
    assert any("lapsed after seq" in u for u in recon.unauthorized), \
        recon.unauthorized
    assert recon.tasks["t1"]["state"] == "LEASED"


def _task_log(tmp_path, name, moves, *, submitter="alice"):
    """A task and a sequence of records, straight into the log."""
    log = EventLog(tmp_path / f"{name}.jsonl")
    log.append(actor=submitter, action="task.create", target="t1",
               payload={"task_id": "t1", "tool_id": "probe",
                        "submitter": submitter, "inputs_digest": _DG})
    for actor, action, body in moves:
        log.append(actor=actor, action=action, target="t1",
                   payload={"task_id": "t1", **body})
    return log


def _tr(actor, src, dst, role, **extra):
    return (actor, ACT_TASK_TRANSITION,
            {"src": src, "dst": dst, "role": role, **extra})


_LEASE = {"lease_id": "L1", "holder": "worker", "granted_seq": 2,
          "expires_after_seq": 9999}
_DEAD_LEASE = {"lease_id": "L1", "holder": "worker", "granted_seq": 2,
               "expires_after_seq": 0}

#: Up to the moment work is owned and running.
_TO_EXECUTING = [
    _tr("alice", "CREATED", "VALIDATED", "SUBMITTER"),
    _tr("alice", "VALIDATED", "QUEUED", "SCHEDULER"),
    _tr("worker", "QUEUED", "LEASED", "WORKER", lease=_LEASE),
    _tr("worker", "LEASED", "EXECUTING", "WORKER", lease_id="L1"),
]

_EXECUTION = ("worker", "task.execution",
              {"result_digest": _DG2, "outcome": "COMPLETED",
               "tool_id": "probe"})

#: ...and through a legitimate completion.
_TO_COMPLETED = _TO_EXECUTING + [
    _EXECUTION,
    _tr("worker", "EXECUTING", "COMPLETED", "WORKER", lease_id="L1",
        result_digest=_DG2),
]

#: The executor verifying its own work.
_SELF_VERIFIED_CHAIN = _TO_COMPLETED + [
    _tr("worker", "COMPLETED", "VERIFIED", "VERIFIER"),
]

#: Possession that ran out before the work was reported.
_LAPSED_LEASE_CHAIN = [
    _tr("alice", "CREATED", "VALIDATED", "SUBMITTER"),
    _tr("alice", "VALIDATED", "QUEUED", "SCHEDULER"),
    _tr("worker", "QUEUED", "LEASED", "WORKER", lease=_DEAD_LEASE),
    _tr("worker", "LEASED", "EXECUTING", "WORKER", lease_id="L1"),
]


# --- the paired matrix: every restated rule, refused and not refused -------
#
# A refusal that fires for everything proves nothing about the rule it is
# named after, so each row that expects a refusal has a neighbour that
# expects none, differing only in the thing the rule is about.

_AUTH_MATRIX = [
    ("honest-review", {}, [_REVIEW], None),
    ("role-not-permitted", {},
     [("bob", {"src": "PROPOSED", "dst": "UNDER_REVIEW",
               "role": "PROMOTER"})],
     "role PROMOTER may not perform PROPOSED -> UNDER_REVIEW"),

    ("honest-reject", {},
     [("bob", {"src": "PROPOSED", "dst": "REJECTED", "role": "VERIFIER",
               "evidence": {"rejection_reason": _DG}})], None),
    ("leaves-a-terminal-state", {},
     [("bob", {"src": "PROPOSED", "dst": "REJECTED", "role": "VERIFIER",
               "evidence": {"rejection_reason": _DG}}),
      ("bob", {"src": "REJECTED", "dst": "UNDER_REVIEW",
               "role": "VERIFIER"})],
     "I2: REJECTED is terminal"),

    ("honest-verify", {}, [_REVIEW, _VERIFY], None),
    ("verified-by-its-own-proposer", {}, [_REVIEW, _SELF_VERIFY],
     "I4: 'alice' proposed r1"),
    ("verified-with-no-proposer-on-record", {"proposer": None},
     [_REVIEW, ("bob", {"src": "UNDER_REVIEW", "dst": "VERIFIED",
                        "role": "VERIFIER",
                        "evidence": {"verification_report": _DG}})],
     "the record's proposer is unknown"),

    ("verified-with-no-evidence", {},
     [_REVIEW, ("bob", {"src": "UNDER_REVIEW", "dst": "VERIFIED",
                        "role": "VERIFIER"})],
     "requires evidence ['verification_report']"),
    ("verified-with-prose-for-evidence", {},
     [_REVIEW, ("bob", {"src": "UNDER_REVIEW", "dst": "VERIFIED",
                        "role": "VERIFIER",
                        "evidence": {"verification_report": "trust me"}})],
     "must be a sha256 digest"),
    ("verified-with-an-uppercase-digest", {},
     [_REVIEW, ("bob", {"src": "UNDER_REVIEW", "dst": "VERIFIED",
                        "role": "VERIFIER",
                        "evidence": {"verification_report": "A" * 64}})],
     "must be a sha256 digest"),

    ("honest-promotion", {},
     [_REVIEW, _VERIFY,
      ("carol", {"src": "VERIFIED", "dst": "PROMOTED", "role": "PROMOTER",
                 "evidence": {"verification_report": _DG,
                              "policy_id": "pol-1"},
                 "policy_id": "pol-1"})], None),
    ("promotion-with-no-policy-in-force", {},
     [_REVIEW, _VERIFY,
      ("carol", {"src": "VERIFIED", "dst": "PROMOTED", "role": "PROMOTER",
                 "evidence": {"verification_report": _DG,
                              "policy_id": "pol-1"}})],
     "I5: promotion requires an explicit policy identity"),
    ("promotion-citing-an-empty-policy", {},
     [_REVIEW, _VERIFY,
      ("carol", {"src": "VERIFIED", "dst": "PROMOTED", "role": "PROMOTER",
                 "evidence": {"verification_report": _DG, "policy_id": ""},
                 "policy_id": "pol-1"})],
     "I5: policy_id must be a non-empty id"),

    ("no-such-edge", {},
     [("carol", {"src": "PROPOSED", "dst": "PROMOTED", "role": "PROMOTER",
                 "policy_id": "pol-1"})],
     "no edge PROPOSED -> PROMOTED"),
    ("a-state-nobody-defined", {},
     [("bob", {"src": "PROPOSED", "dst": "ASCENDED", "role": "VERIFIER"})],
     "'ASCENDED' is not an authority state this reader knows"),
    ("a-role-nobody-defined", {},
     [("bob", {"src": "PROPOSED", "dst": "UNDER_REVIEW", "role": "GOD"})],
     "'GOD' is not a role this reader knows"),
    ("moving-out-of-a-state-nobody-defined", {"create": {"state": "LIMBO"}},
     [("bob", {"src": "LIMBO", "dst": "UNDER_REVIEW", "role": "VERIFIER"})],
     "'LIMBO' is not an authority state this reader knows"),
]


@pytest.mark.parametrize(
    "case,kwargs,moves,expected",
    [(c, k, m, e) for c, k, m, e in _AUTH_MATRIX],
    ids=[c for c, _, _, _ in _AUTH_MATRIX])
def test_the_restated_authority_rules_decide_each_case(
        tmp_path, case, kwargs, moves, expected):
    """One row per restated rule, and one neighbour per row that is allowed.

    The pairing is the point. ``verified-with-prose-for-evidence`` only says
    something about the digest rule because ``honest-verify`` -- the same
    move with a real digest -- is not refused.
    """
    log = _auth_log(tmp_path, f"m_{case}".replace("-", "_"), moves, **kwargs)
    recon = _reconstruct(log)
    if expected is None:
        assert not recon.unauthorized, recon.unauthorized
    else:
        assert any(expected in u for u in recon.unauthorized), (
            f"{case}: expected {expected!r}, got {recon.unauthorized}")


_TASK_MATRIX = [
    ("honest-run-through-verified",
     _TO_COMPLETED + [_tr("checker", "COMPLETED", "VERIFIED", "VERIFIER")],
     None),

    ("executing-with-no-lease-at-all",
     [_tr("alice", "CREATED", "VALIDATED", "SUBMITTER"),
      _tr("alice", "VALIDATED", "QUEUED", "SCHEDULER"),
      _tr("worker", "QUEUED", "LEASED", "WORKER"),
      _tr("worker", "LEASED", "EXECUTING", "WORKER")],
     "requires the task's lease, and it holds none"),
    ("executing-citing-somebody-elses-lease",
     _TO_EXECUTING[:3] + [
         _tr("worker", "LEASED", "EXECUTING", "WORKER", lease_id="L2")],
     "is not this task's lease"),
    ("executing-on-a-lease-held-by-another-worker",
     _TO_EXECUTING[:3] + [
         _tr("mallory", "LEASED", "EXECUTING", "WORKER", lease_id="L1")],
     "is held by 'worker', not 'mallory'"),
    ("executing-after-possession-ran-out", _LAPSED_LEASE_CHAIN,
     "lapsed after seq 0"),
    ("executing-on-a-lease-with-no-expiry-this-reader-can-read",
     [_tr("alice", "CREATED", "VALIDATED", "SUBMITTER"),
      _tr("alice", "VALIDATED", "QUEUED", "SCHEDULER"),
      _tr("worker", "QUEUED", "LEASED", "WORKER",
          lease={**_LEASE, "expires_after_seq": "soon"}),
      _tr("worker", "LEASED", "EXECUTING", "WORKER", lease_id="L1")],
     "names no sequence it expires after"),

    ("completed-with-no-result-to-point-at",
     _TO_EXECUTING + [_EXECUTION,
                      _tr("worker", "EXECUTING", "COMPLETED", "WORKER",
                          lease_id="L1")],
     "COMPLETED requires the digest of the execution result"),

    ("verified-by-the-actor-that-executed-it", _SELF_VERIFIED_CHAIN,
     "may not also perform COMPLETED -> VERIFIED"),
    ("verified-with-no-executor-on-record",
     _TO_EXECUTING + [
         _tr("worker", "EXECUTING", "COMPLETED", "WORKER", lease_id="L1",
             result_digest=_DG2),
         _tr("checker", "COMPLETED", "VERIFIED", "VERIFIER")],
     "no executor is recorded"),

    ("invalidating-a-verified-task-is-allowed",
     _TO_COMPLETED + [_tr("checker", "COMPLETED", "VERIFIED", "VERIFIER"),
                      _tr("system", "VERIFIED", "INVALIDATED", "SYSTEM")],
     None),
    ("requeueing-a-verified-task-is-not",
     _TO_COMPLETED + [_tr("checker", "COMPLETED", "VERIFIED", "VERIFIER"),
                      _tr("alice", "VERIFIED", "QUEUED", "SCHEDULER")],
     "VERIFIED is terminal"),

    ("cancelling-running-work-is-allowed",
     _TO_EXECUTING + [_tr("alice", "EXECUTING", "CANCELLED", "SCHEDULER")],
     None),
    ("verifying-a-task-that-never-ran",
     [_tr("checker", "CREATED", "VERIFIED", "VERIFIER")],
     "no edge CREATED -> VERIFIED"),
    ("validated-by-a-role-that-may-not",
     [_tr("worker", "CREATED", "VALIDATED", "WORKER")],
     "role WORKER may not perform CREATED -> VALIDATED"),
    ("a-task-state-nobody-defined",
     [_tr("alice", "CREATED", "DONE", "SUBMITTER")],
     "'DONE' is not a task state this reader knows"),
    ("a-task-role-nobody-defined",
     [_tr("alice", "CREATED", "VALIDATED", "BOSS")],
     "'BOSS' is not a task role this reader knows"),
]


@pytest.mark.parametrize(
    "case,moves,expected", _TASK_MATRIX,
    ids=[c for c, _, _ in _TASK_MATRIX])
def test_the_restated_task_rules_decide_each_case(
        tmp_path, case, moves, expected):
    """One row per restated task rule, paired the same way."""
    log = _task_log(tmp_path, f"t_{case}".replace("-", "_"), moves)
    recon = reconstruct_tasks(log)
    if expected is None:
        assert not recon.unauthorized, recon.unauthorized
    else:
        assert any(expected in u for u in recon.unauthorized), (
            f"{case}: expected {expected!r}, got {recon.unauthorized}")


def test_a_lease_record_this_reader_cannot_read_is_reported(tmp_path):
    """Not silently treated as no lease, and not a crash either.

    The production dataclass raised TypeError on a shape it could not take,
    and this module caught it. Restating the shape means the reader has to
    say what it expects, which is also the only way the anomaly can name the
    problem instead of the exception type.
    """
    log = _task_log(tmp_path, "bad_lease", [
        _tr("alice", "CREATED", "VALIDATED", "SUBMITTER"),
        _tr("alice", "VALIDATED", "QUEUED", "SCHEDULER"),
        _tr("worker", "QUEUED", "LEASED", "WORKER",
            lease={"lease_id": "L1", "holder": "worker"}),
        _tr("worker", "LEASED", "EXECUTING", "WORKER", lease_id="L1")])
    recon = reconstruct_tasks(log)
    assert any("cannot interpret" in a for a in recon.anomalies), \
        recon.anomalies
    assert any("holds none" in u for u in recon.unauthorized), \
        recon.unauthorized
    assert recon.tasks["t1"]["state"] == "LEASED"


def test_a_well_formed_lease_is_not_reported(tmp_path):
    """Anti-vacuity for the rule above."""
    log = _task_log(tmp_path, "good_lease", _TO_EXECUTING)
    recon = reconstruct_tasks(log)
    assert not recon.anomalies, recon.anomalies
    assert not recon.unauthorized, recon.unauthorized
    assert recon.tasks["t1"]["state"] == "EXECUTING"


# --- the diagnostic mode is not a permissive one ---------------------------

def test_diagnostic_mode_still_refuses_a_state_this_reader_cannot_name(
        tmp_path):
    """``reauthorize=False`` turns the GATE off, not the vocabulary.

    Callers use it to ask "what does this log say, taking every record at
    face value" -- a question about history, not a licence to fold a state
    nothing in the system defines into the answer. A record moving to
    ASCENDED would otherwise be projected as being in ASCENDED, and every
    later reader of that projection would be confidently wrong about a state
    that does not exist.
    """
    log = _auth_log(tmp_path, "diag_auth", [
        ("bob", {"src": "PROPOSED", "dst": "ASCENDED", "role": "VERIFIER"})])
    recon = _reconstruct(log, reauthorize=False)
    assert not recon.unauthorized, recon.unauthorized       # the gate is off
    assert any("not an authority state this reader knows" in a
               for a in recon.anomalies), recon.anomalies
    assert recon.records["r1"]["state"] == "PROPOSED"


def test_diagnostic_mode_still_applies_a_state_it_can_name(tmp_path):
    """Anti-vacuity: with the gate off, a known state IS folded in.

    Including one the gate would have refused -- that is what the mode is
    for, and it is what makes the assertion above about the VOCABULARY
    rather than about the rules.
    """
    log = _auth_log(tmp_path, "diag_auth_ok", [
        ("alice", {"src": "PROPOSED", "dst": "UNDER_REVIEW",
                   "role": "PROPOSER"})])
    recon = _reconstruct(log, reauthorize=False)
    assert not recon.anomalies, recon.anomalies
    assert recon.records["r1"]["state"] == "UNDER_REVIEW"
    # ...and with the gate on, the same record is refused.
    assert any("role PROPOSER may not perform" in u
               for u in _reconstruct(log).unauthorized)


def test_diagnostic_mode_still_refuses_a_task_state_it_cannot_name(tmp_path):
    log = _task_log(tmp_path, "diag_task", [
        _tr("alice", "CREATED", "DONE", "SUBMITTER")])
    recon = reconstruct_tasks(log, reauthorize=False)
    assert not recon.unauthorized, recon.unauthorized
    assert any("not a task state this reader knows" in a
               for a in recon.anomalies), recon.anomalies
    assert recon.tasks["t1"]["state"] == "CREATED"


def test_diagnostic_mode_still_applies_a_task_state_it_can_name(tmp_path):
    """Anti-vacuity, and the same shape: a refused move is still folded."""
    log = _task_log(tmp_path, "diag_task_ok", [
        _tr("worker", "CREATED", "VALIDATED", "WORKER")])
    recon = reconstruct_tasks(log, reauthorize=False)
    assert not recon.anomalies, recon.anomalies
    assert recon.tasks["t1"]["state"] == "VALIDATED"
    assert any("role WORKER may not perform" in u
               for u in reconstruct_tasks(log).unauthorized)


# --- 4. the transitive canonical rule, which had no test here --------------
#
# D-2026-36. `reconstruct.canonical_ids()` restates a rule `store.canonical()`
# also applies: a record is canonical when its own state says so AND
# everything it rests on is canonical too, transitively, with a cycle
# resolving to no. The store side has four tests for it. The second reader
# had none in the suites its own mutation specification runs, and four
# mutations survived a hosted run saying exactly that:
#
#   R98  the replay ignores the foundations entirely
#   R99  the replay checks only the IMMEDIATE foundations
#   R100 a cycle reads as sound
#   R92  a checkpoint claim need not name a head hash
#
# One test that would have caught R98 and R99 does exist -- in
# tests/test_agent_substrate.py, which the `agent_second_reader` spec does
# not run. So the rule was protected and the spec could not see it, and I
# added the code and the mutations in the same commit without ever holding
# evidence that they died. The tests below live in a suite the spec runs.

from qta_agent.authority import Role as _Role, State as _State  # noqa: E402
from qta_agent.store import AuthorityStore as _Store  # noqa: E402
from qta_agent.reconstruct import reconstruct as _reconstruct_auth  # noqa: E402
from qta_agent.reconstruct import (  # noqa: E402
    compare as _compare, reconstruct_subsystems as _subsystems,
)

_EVIDENCE_DIGEST = "d" * 64


def _promoted(store, rid, deps=()):
    """One record driven to PROMOTED through the production gate."""
    store.create(record_id=rid, kind="result", proposer="alice",
                 policy_id="p1", depends_on=deps)
    store.transition(record_id=rid, dst=_State.UNDER_REVIEW, actor="bob",
                     role=_Role.VERIFIER)
    store.transition(record_id=rid, dst=_State.VERIFIED, actor="bob",
                     role=_Role.VERIFIER,
                     evidence={"verification_report": _EVIDENCE_DIGEST})
    return store.transition(record_id=rid, dst=_State.PROMOTED, actor="carol",
                            role=_Role.PROMOTER, policy_id="p1",
                            evidence={"policy_id": "p1"})


def _chain(tmp_path, name):
    """param <- result <- summary, all three promoted, both readers agreeing."""
    store = _Store(EventLog(tmp_path / f"{name}.jsonl")).load()
    _promoted(store, "param")
    _promoted(store, "result", ("param",))
    _promoted(store, "summary", ("result",))
    return store


def test_both_readers_call_a_sound_chain_canonical(tmp_path):
    """ANTI-VACUITY FIRST, and it is not decoration here.

    Every test below asserts that something is NOT canonical. Without this
    one they would all pass against a reader that called nothing canonical
    ever, which is the cheapest way to satisfy a rule about exclusion.
    """
    store = _chain(tmp_path, "sound")
    recon = _reconstruct_auth(store.log)

    assert recon.canonical_ids() == ("param", "result", "summary")
    assert sorted(store.canonical()) == ["param", "result", "summary"]
    assert _compare(store, recon) == ()


def test_both_readers_drop_a_record_resting_on_a_revoked_foundation(
        tmp_path):
    """R98: the replay must not read the record's own state and stop there."""
    store = _chain(tmp_path, "revoked")
    store.transition(record_id="param", dst=_State.REVOKED, actor="carol",
                     role=_Role.PROMOTER,
                     evidence={"revocation_reason": _EVIDENCE_DIGEST})
    recon = _reconstruct_auth(store.log)

    # The STATES are untouched: a withdrawal does not rewrite its dependents,
    # so a reader that only looked at each record's own state would call two
    # of these three canonical.
    assert recon.records["result"]["state"] == "PROMOTED"
    assert recon.records["summary"]["state"] == "PROMOTED"

    assert recon.canonical_ids() == ()
    assert sorted(store.canonical()) == []
    assert _compare(store, recon) == ()


def test_both_readers_drop_the_GRANDCHILD_of_a_revoked_foundation(tmp_path):
    """R99: immediate foundations are not enough, and this is the case.

    `result` is excluded because `param` is revoked. `summary` rests on
    `result`, whose STATE still reads PROMOTED -- so a reader checking each
    dependency's state rather than its soundness leaves the grandchild
    standing on the same withdrawn input. It is the one record that
    separates the transitive rule from the shallow one.
    """
    store = _chain(tmp_path, "grandchild")
    store.transition(record_id="param", dst=_State.REVOKED, actor="carol",
                     role=_Role.PROMOTER,
                     evidence={"revocation_reason": _EVIDENCE_DIGEST})
    recon = _reconstruct_auth(store.log)

    shallow = tuple(sorted(
        rid for rid, rec in recon.records.items()
        if rec["state"] == "PROMOTED"
        and all(recon.records[d]["state"] == "PROMOTED"
                for d in rec["depends_on"])))
    assert shallow == ("summary",), (
        "the fixture does not separate the two rules: a shallow reader would "
        "reach the same answer as a transitive one, so this proves nothing")

    assert "summary" not in recon.canonical_ids()
    assert recon.canonical_ids() == ()
    assert _compare(store, recon) == ()


def test_both_readers_refuse_a_dependency_cycle(tmp_path):
    """R100: the fail-closed answer when the graph cannot say.

    A cycle is a modelling error, and "is this authority sound" has no other
    safe answer than no. The reader whose job is to disagree is the last
    place that should resolve an unanswerable graph in the permissive
    direction.
    """
    store = _Store(EventLog(tmp_path / "cycle.jsonl")).load()
    _promoted(store, "a")
    _promoted(store, "b", ("a",))
    # The store refuses a record naming a dependency that does not exist, so
    # the cycle can only be closed afterwards.
    store.add_dependency(record_id="a", depends_on=("b",), actor="carol")
    recon = _reconstruct_auth(store.log)

    assert recon.records["a"]["depends_on"] == ["b"], recon.records["a"]
    assert recon.records["b"]["depends_on"] == ["a"], recon.records["b"]
    assert recon.records["a"]["state"] == "PROMOTED"
    assert recon.records["b"]["state"] == "PROMOTED"

    assert recon.canonical_ids() == ()
    assert sorted(store.canonical()) == []
    assert _compare(store, recon) == ()


def test_the_second_reader_refuses_an_anchor_naming_a_head_hash_of_prose(
        tmp_path):
    """R92: the position a claim is about must be bound to a history.

    The sibling rule for `state_digest` has a test; this one did not. The
    anchor here names a position that is NOT the record immediately before
    it, which is the case that matters: when the claim sits directly after
    the position it names, the reader compares the hash against the record
    there and the lie dies on that comparison instead. Two records back,
    there is nothing left but the digest rule.
    """
    log = EventLog(tmp_path / "anchor.jsonl")
    for i in range(4):
        log.append(actor="a", action="record.create", target=f"t{i}",
                   payload={"record_id": f"t{i}", "kind": "note",
                            "proposer": "a"})
    head = log.verify()
    through = head.head_seq - 2

    log.append(actor="checkpointer", action="checkpoint.state",
               target=f"seq:{through}",
               payload={"through_seq": through,
                        "state_digest": "e" * 64,
                        "head_hash": "the state as it stood"})
    recon = _subsystems(log)

    assert any("which is not a digest" in a for a in recon.anomalies), \
        recon.anomalies
    assert through not in recon.checkpoints, (
        "the claim was recorded anyway, so the position it is about is "
        "pinned to a head hash no history has")


def test_an_anchor_two_records_back_is_otherwise_accepted(tmp_path):
    """ANTI-VACUITY for the test above: the DISTANCE is not what refused it.

    If an anchor naming an older position were rejected on its own, the test
    above would pass without the digest rule it exists for.
    """
    log = EventLog(tmp_path / "anchor_ok.jsonl")
    for i in range(4):
        log.append(actor="a", action="record.create", target=f"t{i}",
                   payload={"record_id": f"t{i}", "kind": "note",
                            "proposer": "a"})
    head = log.verify()
    through = head.head_seq - 2

    log.append(actor="checkpointer", action="checkpoint.state",
               target=f"seq:{through}",
               payload={"through_seq": through, "state_digest": "e" * 64,
                        "head_hash": "f" * 64})
    recon = _subsystems(log)

    assert recon.anomalies == [], recon.anomalies
    assert recon.checkpoints[through]["head_hash_checked"] is False, (
        "the reader claims it checked a hash it cannot reach from here")
