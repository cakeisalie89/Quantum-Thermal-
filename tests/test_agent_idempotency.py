"""Owner-scoped idempotency: durable request identity, not a dictionary.

The interesting failures here are not "the same key returned the same task".
They are the ones where a key becomes an authority: a guessed string that
reads someone else's result, a rebinding that makes every later resubmission
of the original request resolve to different work, or a claim of exactly-once
against an external service that never agreed to it.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, invariant, rule

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.canonical import digest  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.governed_stage10 import (  # noqa: E402
    SUBMITTER_ID, VERIFIER_ID, WORKER_ID, GovernedStage10,
)
from qta_agent.idempotency import (  # noqa: E402
    ACT_BIND, IdempotencyConflict, IdempotencyError, IdempotencyLedger,
    binding_from_record, request_identity, scope_identity,
)
from qta_agent.reconstruct import compare_bindings, reconstruct_tasks  # noqa: E402,E501
from qta_agent.tasks import TaskState  # noqa: E402

WS = "verification/stage10/_pytest_idem"


@pytest.fixture()
def log(tmp_path):
    return EventLog(tmp_path / "log.jsonl")


@pytest.fixture()
def ledger(log):
    return IdempotencyLedger(log).load()


@pytest.fixture()
def gov(request):
    """The real governed runner, in its own Stage-10 subtree."""
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


RD_A = digest({"tool_id": "t1", "inputs": {"x": 1}})
RD_B = digest({"tool_id": "t1", "inputs": {"x": 2}})


def _inputs(gov, **over):
    base = {"out_dir": gov.out_rel, "name": "artifact.json",
            "payload": {"label": "MODEL_ONLY", "value": 42}}
    base.update(over)
    return base


def _count(gov, action):
    return sum(1 for e in gov.log.read() if e.action == action)


# --- request identity is canonical ------------------------------------------

def test_request_identity_does_not_depend_on_dict_order():
    """Across a restart is the only time this matters, and it is the only
    time insertion order is guaranteed to differ."""
    a = request_identity(tool_id="t", inputs={"a": 1, "b": [2, {"c": 3}]})
    b = request_identity(tool_id="t", inputs={"b": [2, {"c": 3}], "a": 1})
    assert a == b and len(a) == 64


def test_request_identity_separates_tools():
    assert request_identity(tool_id="t1", inputs={"x": 1}) != \
        request_identity(tool_id="t2", inputs={"x": 1})


@pytest.mark.parametrize("left,right", [
    # The separator has to move a BOUNDARY to collide. My first attempt at
    # this test compared ('a:b', 't', 'c') with ('a', 't', 'b:c'), which
    # joins to "a:b:t:c" and "a:t:b:c" -- different strings, so the test
    # passed against a joined-string implementation and the mutation that
    # introduced one survived. These pairs are the ones that actually meet.
    (("a", "b:c", "d"), ("a:b", "c", "d")),
    (("x", "y", "z:w"), ("x", "y:z", "w")),
    (("p:q", "r", "s"), ("p", "q:r", "s")),
])
def test_the_scope_cannot_be_collided_by_smuggling_a_separator(left, right):
    """Two different namespaces must not hash to one.

    A joined string is not a namespace when the separator can appear inside
    the values: owner 'a' with tool 'b:c' and owner 'a:b' with tool 'c' both
    render as "a:b:c:d", and whoever holds one reads the other's task.
    """
    assert scope_identity(owner=left[0], tool_id=left[1], key=left[2]) != \
        scope_identity(owner=right[0], tool_id=right[1], key=right[2])


# --- the four cases the key exists to distinguish ---------------------------

def test_same_owner_same_key_same_request_returns_the_first_binding(ledger,
                                                                    log):
    first = ledger.bind(owner="alice", tool_id="t1", key="k",
                        request_digest=RD_A, task_id="task-A")
    before = log.verify().head_seq
    again = ledger.bind(owner="alice", tool_id="t1", key="k",
                        request_digest=RD_A, task_id="task-B")
    assert again.task_id == "task-A", "the SECOND task id must not win"
    assert log.verify().head_seq == before, (
        "a resubmission appended an event; suppressing a duplicate that "
        "still grows the log lets a retry storm grow it without bound")
    assert again.bound_seq == first.bound_seq


def test_same_owner_same_key_different_request_is_refused(ledger):
    ledger.bind(owner="alice", tool_id="t1", key="k", request_digest=RD_A,
                task_id="task-A")
    with pytest.raises(IdempotencyConflict, match="DIFFERENT request"):
        ledger.bind(owner="alice", tool_id="t1", key="k",
                    request_digest=RD_B, task_id="task-C")


def test_a_different_owner_using_the_same_key_does_not_collide(ledger):
    """Not "refused with a message" -- not reachable.

    A refusal that says "that key is taken" has already told the caller
    something about another actor. Scoping the namespace by owner means the
    question is never asked.
    """
    ledger.bind(owner="alice", tool_id="t1", key="k", request_digest=RD_A,
                task_id="task-ALICE")
    mine = ledger.bind(owner="mallory", tool_id="t1", key="k",
                       request_digest=RD_B, task_id="task-MALLORY")
    assert mine.task_id == "task-MALLORY"
    assert ledger.lookup(owner="alice", tool_id="t1",
                         key="k").task_id == "task-ALICE"
    assert ledger.lookup(owner="mallory", tool_id="t1",
                         key="k").task_id == "task-MALLORY"


def test_a_guessed_key_never_reaches_another_actors_task(ledger):
    ledger.bind(owner="alice", tool_id="t1", key="k", request_digest=RD_A,
                task_id="task-ALICE")
    assert ledger.lookup(owner="mallory", tool_id="t1", key="k") is None
    assert ledger.lookup(owner="mallory", tool_id="t2", key="k") is None


def test_the_same_key_against_a_different_tool_is_a_different_binding(ledger):
    ledger.bind(owner="alice", tool_id="t1", key="k", request_digest=RD_A,
                task_id="task-ONE")
    two = ledger.bind(owner="alice", tool_id="t2", key="k",
                      request_digest=RD_A, task_id="task-TWO")
    assert two.task_id == "task-TWO" and len(ledger) == 2


# --- replay: a forged history must not rebind or reassign ------------------

def _forge(log, *, actor, **payload):
    log.append(actor=actor, action=ACT_BIND, target=payload.get("task_id",
                                                                "x"),
               payload=payload)
    fresh = IdempotencyLedger(log)
    with pytest.raises(IdempotencyError) as exc:
        fresh.load()
    return str(exc.value)


def test_replay_takes_the_owner_from_the_event_not_the_payload(log):
    msg = _forge(log, actor="mallory", key="k", owner="alice", tool_id="t1",
                 request_digest=RD_A, task_id="task-X")
    assert "names owner 'alice'" in msg and "mallory" in msg


def test_replay_refuses_a_binding_that_backdates_itself(ledger, log):
    """A NEW binding claiming an earlier position than it has.

    The claim has to be for a position the record does not occupy, so a
    legitimate binding goes in first and the forged one lands after it.
    """
    ledger.bind(owner="alice", tool_id="t1", key="first",
                request_digest=RD_A, task_id="task-FIRST")
    msg = _forge(log, actor="alice", key="k", tool_id="t1",
                 request_digest=RD_A, task_id="task-X", bound_seq=0)
    assert "backdates" in msg


def test_replay_refuses_a_rebinding_of_a_live_key(ledger, log):
    ledger.bind(owner="alice", tool_id="t1", key="k", request_digest=RD_A,
                task_id="task-A")
    msg = _forge(log, actor="alice", key="k", tool_id="t1",
                 request_digest=RD_B, task_id="task-STOLEN")
    assert "rebinds it" in msg, (
        "a rebinding makes every later resubmission of the ORIGINAL request "
        "resolve to the attacker's work")


def test_replay_accepts_a_byte_identical_retried_append(ledger, log):
    b = ledger.bind(owner="alice", tool_id="t1", key="k",
                    request_digest=RD_A, task_id="task-A")
    log.append(actor="alice", action=ACT_BIND, target="task-A",
               payload=b.to_record())
    again = IdempotencyLedger(log).load()
    assert again.lookup(owner="alice", tool_id="t1",
                        key="k").task_id == "task-A"


@pytest.mark.parametrize("missing", ["key", "tool_id", "request_digest",
                                     "task_id"])
def test_a_binding_missing_a_required_field_is_refused(missing):
    payload = {"key": "k", "tool_id": "t1", "request_digest": RD_A,
               "task_id": "task-A"}
    del payload[missing]
    with pytest.raises(IdempotencyError, match=missing):
        binding_from_record(payload, actor="alice", seq=3)


def test_a_request_digest_that_is_not_a_digest_is_refused():
    """Identity has to come from canonical bytes or it stops matching."""
    payload = {"key": "k", "tool_id": "t1", "request_digest": "not-a-digest",
               "task_id": "task-A"}
    with pytest.raises(IdempotencyError, match="canonical bytes"):
        binding_from_record(payload, actor="alice", seq=3)


# --- the independent reader agrees, and by its own route -------------------

def test_the_second_reader_reaches_the_same_bindings(ledger, log):
    ledger.bind(owner="alice", tool_id="t1", key="k", request_digest=RD_A,
                task_id="task-A")
    ledger.bind(owner="mallory", tool_id="t1", key="k", request_digest=RD_B,
                task_id="task-M")
    recon = reconstruct_tasks(log, reauthorize=False)
    assert not compare_bindings(ledger, recon)
    assert set(recon.bindings) == {("alice", "t1", "k"),
                                   ("mallory", "t1", "k")}


def test_the_second_reader_reports_a_forged_rebinding_as_an_anomaly(ledger,
                                                                    log):
    ledger.bind(owner="alice", tool_id="t1", key="k", request_digest=RD_A,
                task_id="task-A")
    log.append(actor="alice", action=ACT_BIND, target="task-STOLEN",
               payload={"key": "k", "tool_id": "t1", "request_digest": RD_B,
                        "task_id": "task-STOLEN"})
    recon = reconstruct_tasks(log, reauthorize=False)
    assert any("rebound" in a for a in recon.anomalies), recon.anomalies
    assert recon.bindings[("alice", "t1", "k")]["task_id"] == "task-A", (
        "the second reader kept the ORIGINAL binding; taking the last write "
        "would make it agree with the attacker")


def test_the_second_reader_reports_a_forged_owner_as_an_anomaly(log):
    log.append(actor="mallory", action=ACT_BIND, target="task-X",
               payload={"key": "k", "owner": "alice", "tool_id": "t1",
                        "request_digest": RD_A, "task_id": "task-X"})
    recon = reconstruct_tasks(log, reauthorize=False)
    assert any("names owner 'alice'" in a for a in recon.anomalies)
    assert not recon.bindings, "the forged binding was projected anyway"


# --- production: the real governed path, twice ------------------------------

def test_resubmission_through_the_production_path_runs_the_work_once(gov):
    """The row's actual claim. Not a helper: GovernedStage10.run, twice."""
    inputs = _inputs(gov)
    first = gov.run(tool_id="stage10.emit_artifact", inputs=inputs,
                    idempotency_key="nightly")
    assert first.state is TaskState.VERIFIED and not first.is_duplicate
    head = gov.log.verify().head_seq
    execs, creates = _count(gov, "task.execution"), _count(gov, "task.create")

    second = gov.run(tool_id="stage10.emit_artifact", inputs=inputs,
                     idempotency_key="nightly")
    assert second.task_id == first.task_id
    assert second.is_duplicate and second.outcome == "DUPLICATE"
    assert second.state is TaskState.VERIFIED
    assert second.artifacts == first.artifacts, (
        "a resubmission must report the first submission's artifacts, not an "
        "empty set that a caller would read as 'produced nothing'")
    assert _count(gov, "task.execution") == execs == 1, "the tool ran twice"
    assert _count(gov, "task.create") == creates == 1, "a second task exists"
    assert gov.log.verify().head_seq == head, "the duplicate wrote events"


def test_a_changed_request_under_a_live_key_is_refused_in_production(gov):
    gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
            idempotency_key="nightly")
    with pytest.raises(IdempotencyConflict, match="different request"):
        gov.run(tool_id="stage10.emit_artifact",
                inputs=_inputs(gov, payload={"label": "MODEL_ONLY",
                                             "value": 99}),
                idempotency_key="nightly")


def test_no_key_leaves_every_existing_caller_exactly_as_it_was(gov):
    a = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov))
    b = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov))
    assert a.task_id != b.task_id
    assert not a.is_duplicate and not b.is_duplicate
    assert _count(gov, "task.execution") == 2


def test_a_rejected_submission_keeps_its_key(gov):
    """The key names A REQUEST. That request was rejected.

    Letting a corrected request in under the same key would mean the key
    stopped identifying anything -- and every later resubmission of the
    original would resolve to the corrected work.
    """
    bad = gov.run(tool_id="stage10.emit_artifact",
                  inputs=_inputs(gov, name=42), idempotency_key="k")
    assert bad.state is TaskState.REJECTED
    again = gov.run(tool_id="stage10.emit_artifact",
                    inputs=_inputs(gov, name=42), idempotency_key="k")
    assert again.is_duplicate and again.state is TaskState.REJECTED
    with pytest.raises(IdempotencyConflict):
        gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                idempotency_key="k")


def test_the_binding_is_durable_before_the_work_is_dispatched(gov):
    """A crash between binding and execution must be recoverable.

    Asserted on ORDER in the durable log rather than by killing a process:
    the bind has to precede the first dispatch, or the window it exists to
    cover is not covered.
    """
    gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
            idempotency_key="k")
    order = [e.action for e in gov.log.read()]
    assert order.index(ACT_BIND) < order.index("scheduler.enqueue")
    assert order.index("task.create") < order.index(ACT_BIND)


def test_a_resubmission_after_a_lost_response_needs_no_live_state(gov):
    """The caller crashed and lost the answer. Everything is rebuilt."""
    inputs = _inputs(gov)
    first = gov.run(tool_id="stage10.emit_artifact", inputs=inputs,
                    idempotency_key="k")
    reopened = GovernedStage10(root=gov.root,
                               log=EventLog(gov.log.path),
                               evidence=gov.evidence)
    reopened.out_rel = gov.out_rel
    again = reopened.run(tool_id="stage10.emit_artifact", inputs=inputs,
                         idempotency_key="k")
    assert again.task_id == first.task_id and again.is_duplicate
    assert _count(gov, "task.execution") == 1


def test_an_unregistered_submitter_cannot_own_a_namespace(gov):
    """Whoever submits owns a durable key namespace and is the policy
    subject. An arbitrary string held both until this check existed."""
    from qta_agent.agents import IdentityError

    with pytest.raises(IdentityError):
        gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                submitter="ghost-submitter", idempotency_key="k")


def test_two_submitters_do_not_share_a_key_in_production(gov):
    """The production path inherits the scoping, rather than re-deriving it.

    WORKER_ID holds EXECUTOR, not PROPOSER, so the second submitter here is
    refused by the directory before the key is even consulted -- which is
    the correct order: identity first, then namespace.
    """
    from qta_agent.agents import IdentityError

    gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
            idempotency_key="shared")
    with pytest.raises(IdentityError):
        gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                submitter=WORKER_ID, verifier=VERIFIER_ID,
                idempotency_key="shared")


def test_the_audit_explains_the_binding_and_the_execution_count(gov):
    from qta_agent.audit import AuditIndex

    gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
            idempotency_key="nightly")
    run = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                  idempotency_key="nightly")
    ex = AuditIndex.from_log(gov.log).explain_task(run.task_id)
    binds = [s for s in ex.steps if s.action == ACT_BIND]
    assert len(binds) == 1
    assert "does not re-execute" in binds[0].summary
    assert binds[0].detail["owner"] == SUBMITTER_ID
    assert len([s for s in ex.steps if s.action == "task.execution"]) == 1
    assert not any("execution records" in g for g in ex.gaps)


def test_the_audit_reports_the_binding_owner_from_the_event_not_the_payload(
        gov):
    """An honest binding has both fields agreeing, so it proves nothing.

    This is the fixture problem that let a mutation survive: asserting the
    audit reports SUBMITTER_ID against a record the real path wrote passes
    whether the auditor reads ev.actor or payload['owner'], because they are
    the same value. Only a record where they DISAGREE distinguishes the two,
    and that record is the one an attacker writes.
    """
    from qta_agent.audit import AuditIndex

    gov.log.append(
        actor="stage10-worker", action=ACT_BIND, target="task-forged",
        payload={"key": "k", "owner": SUBMITTER_ID,
                 "tool_id": "stage10.emit_artifact",
                 "request_digest": RD_A, "task_id": "task-forged"})
    ex = AuditIndex.from_log(gov.log).explain_task("task-forged")
    (step,) = [s for s in ex.steps if s.action == ACT_BIND]
    assert step.detail["owner"] == "stage10-worker", (
        "the auditor reported the payload's claimed owner, telling a reader "
        "the forged binding belonged to the honest submitter")
    assert "stage10-worker" in step.summary


def test_the_audit_reports_a_task_that_ran_twice_as_a_gap(gov):
    """The evidence for 'it did not run twice' is the absence of a second
    record, so the auditor has to be able to notice a second record."""
    from qta_agent.audit import AuditIndex

    run = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                  idempotency_key="nightly")
    dup = [e for e in gov.log.read() if e.action == "task.execution"][0]
    gov.log.append(actor=dup.actor, action="task.execution",
                   target=run.task_id, payload=dict(dup.payload))
    ex = AuditIndex.from_log(gov.log).explain_task(run.task_id)
    assert any("execution records" in g for g in ex.gaps), ex.gaps


# --- property and stateful coverage ----------------------------------------

# Values that CONTAIN the separator a naive implementation would join on.
# Without them the generator never produces the colliding pair, and a
# joined-string scope passes every property here -- which it did.
ACTORS = st.sampled_from(["alice", "bob", "a", "a:b"])
TOOLS = st.sampled_from(["t1", "t2", "b:c", "c"])
KEYS = st.sampled_from(["k1", "k2", "d"])
REQUESTS = st.sampled_from([RD_A, RD_B, digest({"tool_id": "t", "i": 3})])


@given(owner=ACTORS, tool=TOOLS, key=KEYS,
       other=ACTORS, other_tool=TOOLS, other_key=KEYS)
@settings(max_examples=300, deadline=None)
def test_two_scopes_collide_only_when_all_three_parts_match(
        owner, tool, key, other, other_tool, other_key):
    """The namespace property, stated as an iff rather than an example.

    A single example proves a collision is possible or a separation holds
    for one triple. This is the claim the design actually rests on: two
    submissions share a binding EXACTLY when the submitter, the tool and the
    key all match, and never otherwise.
    """
    same = (owner, tool, key) == (other, other_tool, other_key)
    collide = (scope_identity(owner=owner, tool_id=tool, key=key)
               == scope_identity(owner=other, tool_id=other_tool,
                                 key=other_key))
    assert collide == same


@given(submissions=st.lists(st.tuples(ACTORS, TOOLS, KEYS, REQUESTS),
                            min_size=1, max_size=25))
@settings(max_examples=150, deadline=None)
def test_the_first_binding_of_a_scope_always_wins(submissions):
    """Across any interleaving, first-write-wins per scope.

    A last-write-wins ledger passes every single-submission test and every
    two-submission test where the second is identical. It fails here,
    because the generator produces the case that matters: a scope claimed
    twice with different requests, somewhere in the middle of other traffic.

    NO ``tmp_path``. Hypothesis reuses a function-scoped fixture across
    every generated example, so the log would accumulate and example five
    would inherit example four's bindings -- which this test did, and the
    failure it produced was a real one about the harness rather than about
    the ledger. The health check that says so exists for exactly this.
    """
    work = Path(tempfile.mkdtemp())
    try:
        _first_binding_wins(work, submissions)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _first_binding_wins(work: Path, submissions) -> None:
    led = IdempotencyLedger(EventLog(work / "p.jsonl")).load()
    first: dict = {}
    for i, (owner, tool, key, rd) in enumerate(submissions):
        scope = (owner, tool, key)
        task = f"task-{i}"
        if scope in first and first[scope][1] != rd:
            with pytest.raises(IdempotencyConflict):
                led.bind(owner=owner, tool_id=tool, key=key,
                         request_digest=rd, task_id=task)
            continue
        got = led.bind(owner=owner, tool_id=tool, key=key,
                       request_digest=rd, task_id=task)
        if scope in first:
            assert got.task_id == first[scope][0], "a later bind won"
        else:
            first[scope] = (task, rd)
            assert got.task_id == task

    for (owner, tool, key), (task, _rd) in first.items():
        assert led.lookup(owner=owner, tool_id=tool,
                          key=key).task_id == task


class IdempotencyMachine(RuleBasedStateMachine):
    """Bind and look up under an adversarial interleaving.

    The invariant that matters is not "the ledger has bindings" -- it is
    that NO actor's lookup ever returns a binding another actor made. A
    stateful machine is the right shape for it because the interesting
    states are reached by a sequence: bind as A, bind as B under the same
    key, restart, look up as B.
    """

    def __init__(self):
        super().__init__()
        self._dir = tempfile.mkdtemp()
        self.log = EventLog(Path(self._dir) / "m.jsonl")
        self.led = IdempotencyLedger(self.log).load()
        self.model: dict = {}
        self.n = 0

    scopes = Bundle("scopes")

    @rule(target=scopes, owner=ACTORS, tool=TOOLS, key=KEYS, rd=REQUESTS)
    def bind(self, owner, tool, key, rd):
        scope = (owner, tool, key)
        self.n += 1
        task = f"task-{self.n}"
        if scope in self.model and self.model[scope][1] != rd:
            with pytest.raises(IdempotencyConflict):
                self.led.bind(owner=owner, tool_id=tool, key=key,
                              request_digest=rd, task_id=task)
            return scope
        got = self.led.bind(owner=owner, tool_id=tool, key=key,
                            request_digest=rd, task_id=task)
        self.model.setdefault(scope, (task, rd))
        assert got.task_id == self.model[scope][0]
        return scope

    @rule(scope=scopes, asker=ACTORS)
    def lookup_as(self, scope, asker):
        owner, tool, key = scope
        got = self.led.lookup(owner=asker, tool_id=tool, key=key)
        expected = self.model.get((asker, tool, key))
        if expected is None:
            assert got is None, (
                f"{asker} reached a binding it never made: {got}")
        else:
            assert got.task_id == expected[0]

    @rule()
    def restart(self):
        """Everything in memory is discarded and rebuilt from the log."""
        self.led = IdempotencyLedger(EventLog(self.log.path)).load()

    @invariant()
    def every_binding_is_owned_by_who_made_it(self):
        for scope_digest, b in self.led.bindings().items():
            assert b.scope == scope_digest
            assert (b.owner, b.tool_id, b.key) in self.model

    def teardown(self):
        shutil.rmtree(self._dir, ignore_errors=True)


TestIdempotencyMachine = IdempotencyMachine.TestCase
TestIdempotencyMachine.settings = settings(
    max_examples=60, stateful_step_count=30, deadline=None,
    suppress_health_check=[HealthCheck.too_slow])


# --- the external case: uncertainty is reported, not resolved ---------------

def _external_gov(gov, timeout_s=1.0):
    """Point the runner at an EXTERNAL-effect tool with the same contract."""
    from qta_agent.tools import (Determinism, Field_, OutputFile, Registry,
                                 SideEffect, ToolSpec)

    reg = Registry([ToolSpec(
        tool_id="stage10.emit_artifact", version="1.0.0",
        summary="an external-effect tool, for the uncertainty semantics",
        inputs=(Field_("out_dir", "str"), Field_("name", "str"),
                Field_("payload", "dict")),
        outputs=(Field_("path", "str"), Field_("sha256", "str")),
        output_files=(OutputFile("artifact", "{out_dir}/{name}"),),
        determinism=Determinism.BYTE_IDENTICAL,
        side_effect=SideEffect.EXTERNAL,
        compensation="revoke the published record by its citation id",
        timeout_s=timeout_s)])
    gov.registry = reg
    gov.executor = type(gov.executor)(reg, workspace=gov.root)
    return reg


def test_an_unsettled_external_resubmission_is_uncertain_not_duplicate(
        gov, monkeypatch):
    """The one case local bookkeeping cannot resolve, reported as such.

    A tool that may have changed state nobody here owns timed out, so
    nothing observed whether the effect happened. Re-running would repeat
    it; reporting DUPLICATE would assert the first attempt's outcome is
    known. Neither is true, and the answer says so.
    """
    from qta_agent.execution import Limits

    _external_gov(gov)
    real_run = type(gov.executor).run

    def _slow(self, **kw):
        kw["argv"] = [sys.executable, "-c", "import time; time.sleep(30)"]
        kw["limits"] = Limits(wall_seconds=1.0)
        return real_run(self, **kw)

    monkeypatch.setattr(type(gov.executor), "run", _slow)
    first = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                    idempotency_key="ext")
    assert first.state is TaskState.TIMED_OUT

    again = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                    idempotency_key="ext")
    assert again.outcome == "UNCERTAIN", (
        "a timed-out EXTERNAL attempt was reported as a settled duplicate; "
        "that asserts the one fact nobody has")
    assert again.is_duplicate and again.task_id == first.task_id
    assert "NOT KNOWN" in again.reason
    assert "revoke the published record" in again.reason, (
        "an operator reading this has to be told what would undo it")
    assert _count(gov, "task.execution") == 1, "the external effect repeated"


def test_a_settled_external_resubmission_is_an_ordinary_duplicate(gov):
    """VERIFIED is reachable only through COMPLETED, so the run did finish.

    Without this the guard could be 'always UNCERTAIN for EXTERNAL', which
    would make the distinction meaningless in the other direction.
    """
    _external_gov(gov, timeout_s=60.0)
    first = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                    idempotency_key="ext")
    assert first.state is TaskState.VERIFIED
    again = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                    idempotency_key="ext")
    assert again.outcome == "DUPLICATE" and again.is_duplicate


def test_a_scoped_tool_that_timed_out_is_an_ordinary_duplicate(gov,
                                                               monkeypatch):
    """The uncertainty is about the SIDE EFFECT, not about the timeout.

    A scoped tool that timed out wrote only inside its own workspace, and
    that is recoverable by looking. Reporting it as UNCERTAIN would make
    every timeout unresolvable and the classification useless.
    """
    from qta_agent.execution import Limits

    real_run = type(gov.executor).run

    def _slow(self, **kw):
        kw["argv"] = [sys.executable, "-c", "import time; time.sleep(30)"]
        kw["limits"] = Limits(wall_seconds=1.0)
        return real_run(self, **kw)

    monkeypatch.setattr(type(gov.executor), "run", _slow)
    first = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                    idempotency_key="scoped")
    assert first.state is TaskState.TIMED_OUT
    again = gov.run(tool_id="stage10.emit_artifact", inputs=_inputs(gov),
                    idempotency_key="scoped")
    assert again.outcome == "DUPLICATE", (
        "a scoped timeout is not an unresolvable external effect")
