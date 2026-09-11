"""A second implementation for the subsystems that had none.

HOW MUCH IT COVERS, MEASURED. 31 of the 38 durable actions are
independently reconstructed. The nine that are not are named in
docs/identity_inventory.json and the count is checked by
tools/identity_inventory.py, so "the second reader covers every subsystem"
cannot be said here again without the check failing first. Escalations were
among the nine until this session.

R42 and R53 said the same thing from two directions: the independent
reconstruction rebuilt authority records and tasks, and the scheduler,
policy, capability, agent, memory, network, secret and context projections
were "compared only against a fresh replay of themselves, which shares
their reducer and so cannot see a shared misunderstanding".

reconstruct.py sits BELOW every one of those modules in the declared
layering, so the second reader cannot import their enums or call their
helpers even by accident. Everything it does is plain strings and plain
dicts, and every authority rule is restated rather than invoked.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.canonical import digest  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.governed_stage10 import GovernedStage10  # noqa: E402
from qta_agent.reconstruct import (  # noqa: E402
    compare_subsystems, reconstruct_subsystems,
)
from qta_agent.tasks import TaskState  # noqa: E402

WS = "verification/stage10/_pytest_second_reader"


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


def _run(g, **over):
    inputs = {"out_dir": g.out_rel, "name": "artifact.json",
              "payload": {"label": "MODEL_ONLY", "value": 42}}
    inputs.update(over.pop("inputs", {}))
    return g.run(tool_id="stage10.emit_artifact", inputs=inputs, **over)


def _primary(g) -> dict:
    """The live projections, flattened to the fields both readers claim.

    Written HERE rather than in reconstruct.py because that module may not
    import these layers -- which is exactly what keeps the two readers
    independent. It also means this extraction could share a mistake with
    the projections, which is why an empty diff is evidence and not proof.
    """
    return {
        "jobs": {j.job_id: {"state": j.state.value, "attempts": j.attempts,
                            "lease_holder": j.lease_holder or ""}
                 for j in g.scheduler.all_jobs().values()},
        "agents": {i.instance_id: {"kind": i.kind.value,
                                   "roles": tuple(sorted(
                                       r.value for r in i.roles))}
                   for i in g.agents.instances()},
        "memory": {e.memory_id: {"author": e.author, "status": e.status.value}
                   for e in g.memory.all_entries()},
        "capabilities": {c: {"revoked_seq": None}
                         for c in g.capabilities.issued_ids()
                         if c not in g.capabilities.revoked_ids()},
    }


# --- an honest history: both readers must agree ----------------------------

def test_both_readers_agree_on_a_governed_run(gov):
    run = _run(gov, idempotency_key="k")
    assert run.state is TaskState.VERIFIED
    recon = reconstruct_subsystems(gov.log)
    assert recon.anomalies == [], recon.anomalies
    assert compare_subsystems(_primary(gov), recon) == ()


def test_the_second_reader_reaches_every_subsystem(gov):
    """A reader that silently covers nothing agrees with everything."""
    _run(gov, idempotency_key="k")
    recon = reconstruct_subsystems(gov.log)
    assert recon.jobs, "no scheduler state"
    assert recon.capabilities, "no capabilities"
    assert recon.agents, "no identities"
    assert recon.memory, "no memory entries"
    assert recon.policies, "no policy versions"
    assert recon.decisions, "no recorded decisions"
    assert recon.contexts, "no context manifests"


def test_the_second_reader_agrees_across_several_runs(gov):
    for i in range(3):
        _run(gov, inputs={"name": f"a{i}.json"})
    recon = reconstruct_subsystems(gov.log)
    assert recon.anomalies == []
    assert len(recon.jobs) == 3
    assert compare_subsystems(_primary(gov), recon) == ()


# --- forged histories: the second reader must NOT agree --------------------

def _forge(gov, action, payload, actor="mallory", target="x"):
    gov.log.append(actor=actor, action=action, target=target, payload=payload)
    return reconstruct_subsystems(gov.log)


def test_a_job_born_succeeded_is_refused_by_the_second_reader(gov):
    """An enqueue introduces WORK, never an outcome."""
    recon = _forge(gov, "scheduler.enqueue", {"job": {
        "job_id": "j-forged", "state": "SUCCEEDED", "submitter": "mallory",
        "work_digest": "a" * 64, "priority": 0, "attempts": 0}})
    assert any("enqueued directly in 'SUCCEEDED'" in a
               for a in recon.anomalies), recon.anomalies
    assert "j-forged" not in recon.jobs


def test_a_job_born_dispatched_with_its_own_lease_is_refused(gov):
    """A guard that can be handed its own precondition is not a guard."""
    recon = _forge(gov, "scheduler.enqueue", {"job": {
        "job_id": "j-lease", "state": "DISPATCHED", "submitter": "mallory",
        "work_digest": "a" * 64, "lease_holder": "mallory",
        "lease_id": "L", "attempts": 0}})
    assert any("enqueued directly in 'DISPATCHED'" in a
               for a in recon.anomalies)
    assert "j-lease" not in recon.jobs


def test_a_lease_at_enqueue_is_refused_even_from_a_valid_initial_state(gov):
    """ISOLATED from the born-in-a-bad-state check, which masked it.

    The test above forges DISPATCHED, and the state check refuses that
    before the lease check is ever reached -- so a mutation deleting the
    lease check survived. Enqueuing in WAITING, which is legitimate, leaves
    the lease as the only thing that can refuse it.
    """
    recon = _forge(gov, "scheduler.enqueue", {"job": {
        "job_id": "j-quiet", "state": "WAITING", "submitter": "mallory",
        "work_digest": "a" * 64, "lease_holder": "mallory",
        "lease_id": "L", "attempts": 0}})
    assert any("already holding a lease" in a for a in recon.anomalies), \
        recon.anomalies
    assert "j-quiet" not in recon.jobs


def test_spent_attempts_at_enqueue_are_refused_from_a_valid_state(gov):
    """The same isolation for the retry budget.

    A job enqueued with attempts already spent arrives closer to
    exhaustion than anything authorised -- or, with a negative count,
    further from it.
    """
    recon = _forge(gov, "scheduler.enqueue", {"job": {
        "job_id": "j-spent", "state": "WAITING", "submitter": "mallory",
        "work_digest": "a" * 64, "attempts": 3}})
    assert any("attempts already spent" in a for a in recon.anomalies)
    assert "j-spent" not in recon.jobs


def test_a_job_naming_a_submitter_it_is_not_is_refused(gov):
    recon = _forge(gov, "scheduler.enqueue", {"job": {
        "job_id": "j-sub", "state": "WAITING", "submitter": "honest-agent",
        "work_digest": "a" * 64, "attempts": 0}})
    assert any("names submitter 'honest-agent'" in a for a in recon.anomalies)


def test_a_capability_that_predates_its_own_record_is_refused(gov):
    recon = _forge(gov, "capability.issue", {
        "capability_id": "cap-back", "subject": "mallory",
        "action": "EXECUTE_TOOL", "task_id": "t", "tool_id": "x",
        "scope": [], "issued_seq": 0, "expires_after_seq": 9999})
    assert any("would predate its own record" in a for a in recon.anomalies)
    assert "cap-back" not in recon.capabilities


def test_a_reissued_capability_id_with_different_terms_is_refused(gov):
    _run(gov)
    live = gov.capabilities.issued_ids()[0]
    recon = _forge(gov, "capability.issue", {
        "capability_id": live, "subject": "mallory", "action": "EXECUTE_TOOL",
        "task_id": "t", "tool_id": "x", "scope": ["/"],
        "issued_seq": gov.log.verify().head_seq + 1,
        "expires_after_seq": 9999})
    assert any("issued twice with different terms" in a
               for a in recon.anomalies)


def test_a_non_human_minting_a_human_is_refused_by_the_second_reader(gov):
    """One step from answering its own escalation, which is both halves of
    the human gate at once."""
    recon = _forge(gov, "agent.register", {"identity": {
        "instance_id": "fake-human", "agent_id": "fake",
        "kind": "HUMAN", "roles": ["REVIEWER"]}},
        actor="stage10-worker")
    assert any("is not HUMAN and registers" in a for a in recon.anomalies)
    assert "fake-human" not in recon.agents


def test_a_retracted_memory_cannot_be_un_retracted(gov):
    run = _run(gov)
    mid = run.memory_id
    gov.memory.retract(mid, actor="stage10-verifier", reason="withdrawn")
    recon = _forge(gov, "memory.status",
                   {"memory_id": mid, "status": "ACTIVE"},
                   actor="stage10-verifier")
    assert any("un-retracted" in a for a in recon.anomalies), recon.anomalies
    assert recon.memory[mid]["status"] == "RETRACTED"


def test_a_memory_entry_naming_an_author_it_is_not_is_refused(gov):
    recon = _forge(gov, "memory.write", {"entry": {
        "memory_id": "m-forged", "author": "stage10-verifier",
        "text": "x", "status": "ACTIVE"}})
    assert any("names author 'stage10-verifier'" in a
               for a in recon.anomalies)


def test_a_malformed_memory_write_is_refused_not_projected(gov):
    """The raw KeyError that once made a whole store unloadable."""
    recon = _forge(gov, "memory.write", {"not_an_entry": True})
    assert any("carries no entry" in a for a in recon.anomalies)


def test_a_rebound_network_grant_is_refused(gov):
    """A live grant replaced by one nobody reviewed."""
    body = {"grant_id": "g1", "host": "api.example.com", "port": 443}
    _forge(gov, "network.grant",
           {"grant": body, "grant_digest": digest(body), "grant_id": "g1"},
           actor="stage10-worker")
    other = {"grant_id": "g1", "host": "collector.evil.test", "port": 443}
    recon = _forge(gov, "network.grant",
                   {"grant": other, "grant_digest": digest(other),
                    "grant_id": "g1"}, actor="stage10-worker")
    assert any("re-issued with different terms" in a
               for a in recon.anomalies)
    assert recon.net_grants["g1"]["body"]["host"] == "api.example.com"


def test_a_forged_job_transition_from_the_wrong_state_is_refused(gov):
    _run(gov)
    (jid,) = list(reconstruct_subsystems(gov.log).jobs)
    recon = _forge(gov, "scheduler.transition",
                   {"job_id": jid, "src": "READY", "dst": "SUCCEEDED"})
    assert any("claims src 'READY'" in a for a in recon.anomalies)


def test_a_terminal_job_cannot_be_revived(gov):
    _run(gov)
    (jid,) = list(reconstruct_subsystems(gov.log).jobs)
    recon = _forge(gov, "scheduler.transition",
                   {"job_id": jid, "src": "SUCCEEDED", "dst": "READY"})
    assert any("leaves terminal state" in a for a in recon.anomalies)


def test_a_policy_downgrade_is_reported(gov):
    _run(gov)
    pid = list(reconstruct_subsystems(gov.log).policies)[0]
    recon = _forge(gov, "policy.publish", {
        "document": {"policy_id": pid, "version": 0, "rules": []},
        "policy_digest": "d" * 64})
    assert any("publishes version 0 after" in a for a in recon.anomalies)


def test_the_second_reader_never_raises_on_a_hostile_history(gov):
    """Findings, not an exception that hides the rest of the log."""
    for payload in ({}, {"job": None}, {"job": {"job_id": None}},
                    {"identity": "not a dict"}, {"entry": []},
                    {"grant": 5}, {"document": None}, {"decision": "no"}):
        for action in ("scheduler.enqueue", "agent.register", "memory.write",
                       "network.grant", "policy.publish", "policy.decision"):
            gov.log.append(actor="mallory", action=action, target="x",
                           payload=payload)
    recon = reconstruct_subsystems(gov.log)
    assert recon.anomalies, "a hostile history produced no findings at all"
    assert recon.events_replayed > 0


def test_a_divergence_between_the_readers_is_reported(gov):
    """The comparison must be able to FAIL, or it proves nothing."""
    _run(gov)
    recon = reconstruct_subsystems(gov.log)
    primary = _primary(gov)
    (jid,) = list(primary["jobs"])
    primary["jobs"][jid]["state"] = "CANCELLED"
    div = compare_subsystems(primary, recon)
    assert div and any(d.field_name == "state" for d in div)


def test_an_object_only_one_reader_holds_is_a_divergence(gov):
    """PRESENCE, not just field values.

    A forged record that reaches one projection and not the other produces
    no field disagreement at all -- there is no shared object to compare.
    Only presence catches it, and a mutation deleting that check survived
    because every other test here mutates a field of something both
    readers hold.
    """
    _run(gov)
    recon = reconstruct_subsystems(gov.log)

    # In the projection, absent from the second reader.
    primary = _primary(gov)
    primary["jobs"]["j-ghost"] = {"state": "SUCCEEDED", "attempts": 0,
                                  "lease_holder": ""}
    div = compare_subsystems(primary, recon)
    assert any(d.record_id == "jobs/j-ghost" and d.field_name == "presence"
               for d in div), div

    # And the other direction: held by the second reader, absent from the
    # projection. Both matter -- one is a record the projection invented,
    # the other is one it dropped.
    primary = _primary(gov)
    (real,) = [k for k in primary["jobs"]]
    del primary["jobs"][real]
    div = compare_subsystems(primary, recon)
    assert any(d.record_id == f"jobs/{real}" and d.field_name == "presence"
               for d in div), div


# ==========================================================================
# LEASE RENEWAL, read by the reader that does not share the scheduler's code
#
# A renewal EXTENDS possession. It must never re-acquire it: by the time a
# lease has lapsed, reconcile may have returned the work to READY and given
# it to somebody else, and a renewal would take it back leaving a job record
# that looks entirely ordinary afterwards.
# ==========================================================================

def _leased_job(gov, *, job_id="j-lease", worker="w1", lease_id="L1",
                lease_seqs=50):
    """A real DISPATCHED job with a live lease, through the real scheduler."""
    s = gov.scheduler
    s.enqueue(job_id=job_id, work_digest="b" * 64, submitter="sub",
              resources={"slots": 1})
    s.reconcile()
    s.dispatch(job_id=job_id, worker=worker, lease_id=lease_id,
               lease_seqs=lease_seqs)
    return s


def test_the_second_reader_follows_an_honest_renewal(gov):
    s = _leased_job(gov)
    s.renew_lease(job_id="j-lease", worker="w1", lease_id="L1", lease_seqs=80)

    recon = reconstruct_subsystems(gov.log)
    theirs = recon.jobs["j-lease"]
    assert theirs["lease_renewals"] == 1
    assert theirs["lease_expires_after_seq"] == \
        s.get("j-lease").lease_expires_after_seq


def test_the_second_reader_refuses_a_renewal_of_a_lapsed_lease(gov):
    """The scheduler's write path refuses this, so it is forged directly."""
    s = _leased_job(gov, lease_seqs=1)
    for i in range(4):
        s.enqueue(job_id=f"filler-{i}", work_digest="c" * 64, submitter="sub")

    recon = _forge(gov, "scheduler.lease_renew",
                   {"job_id": "j-lease", "lease_id": "L1", "lease_seqs": 500},
                   actor="w1")
    assert any("lapsed" in a and "not renewed" in a
               for a in recon.anomalies), recon.anomalies
    assert recon.jobs["j-lease"]["lease_renewals"] == 0


def test_the_second_reader_refuses_a_renewal_from_a_non_holder(gov):
    _leased_job(gov)
    recon = _forge(gov, "scheduler.lease_renew",
                   {"job_id": "j-lease", "lease_id": "L1", "lease_seqs": 500},
                   actor="mallory")
    assert any("mallory" in a and "held by" in a
               for a in recon.anomalies), recon.anomalies
    assert recon.jobs["j-lease"]["lease_renewals"] == 0


def test_the_second_reader_refuses_a_renewal_citing_a_stale_lease_id(gov):
    _leased_job(gov)
    recon = _forge(gov, "scheduler.lease_renew",
                   {"job_id": "j-lease", "lease_id": "L-old",
                    "lease_seqs": 500}, actor="w1")
    assert any("cites lease" in a for a in recon.anomalies), recon.anomalies
    assert recon.jobs["j-lease"]["lease_renewals"] == 0


def test_the_second_reader_computes_the_new_end_rather_than_reading_it(gov):
    """A payload that names its own expiry must not become one.

    ISOLATED from the shortening check on purpose: the request is a real
    extension, so the only thing that can decide the answer is which of the
    two numbers the reader uses.
    """
    _leased_job(gov)
    recon = _forge(gov, "scheduler.lease_renew",
                   {"job_id": "j-lease", "lease_id": "L1", "lease_seqs": 90,
                    "lease_expires_after_seq": 10 ** 9}, actor="w1")
    assert recon.jobs["j-lease"]["lease_expires_after_seq"] < 10 ** 6
    assert recon.jobs["j-lease"]["lease_renewals"] == 1


def test_the_second_reader_bounds_renewals(gov):
    s = _leased_job(gov, lease_seqs=10 ** 6)
    for _ in range(16):
        s.renew_lease(job_id="j-lease", worker="w1", lease_id="L1",
                      lease_seqs=10 ** 6)
    recon = _forge(gov, "scheduler.lease_renew",
                   {"job_id": "j-lease", "lease_id": "L1",
                    "lease_seqs": 10 ** 6}, actor="w1")
    assert any("renewal bound" in a for a in recon.anomalies), recon.anomalies
    assert recon.jobs["j-lease"]["lease_renewals"] == 16


def test_the_second_reader_drops_the_renewal_count_on_requeue(gov):
    """Both readers must agree after a requeue, or the divergence report is
    reporting on the readers rather than on the log."""
    from qta_agent.scheduler import FailureClass
    s = _leased_job(gov, lease_seqs=10 ** 6)
    s.renew_lease(job_id="j-lease", worker="w1", lease_id="L1",
                  lease_seqs=10 ** 6)
    s.report(job_id="j-lease", worker="w1", failure=FailureClass.TIMEOUT,
             detail="slow")

    recon = reconstruct_subsystems(gov.log)
    mine = s.get("j-lease")
    theirs = recon.jobs["j-lease"]
    assert mine.lease_renewals == 0
    assert theirs["lease_renewals"] == 0
    assert theirs["lease_expires_after_seq"] == mine.lease_expires_after_seq
    assert (theirs["lease_holder"] or None) == mine.lease_holder


# ==========================================================================
# SERVICE CONTRACTS AND BUDGETS, read independently
# ==========================================================================

def _register_service(gov):
    from qta_agent.netauth import ServiceOperation, service
    return gov.network.register_service(
        service(service_id="registrar", hosts=("api.example.com",),
                operations=(ServiceOperation("POST", "/v1/records"),),
                quota_per_task=2),
        actor="scheduler")


def test_the_second_reader_rebuilds_a_service_contract(gov):
    svc = _register_service(gov)
    recon = reconstruct_subsystems(gov.log)
    theirs = recon.services["registrar"]
    assert theirs["digest"] == svc.digest()
    assert theirs["quota_per_task"] == 2
    assert theirs["operations"] == ("POST /v1/records",)


def test_the_second_reader_refuses_a_re_registration_with_new_terms(gov):
    from qta_agent.netauth import ACT_SERVICE_REGISTER, ServiceOperation
    from qta_agent.netauth import service as _service
    _register_service(gov)
    wider = _service(service_id="registrar", hosts=("api.example.com",),
                     operations=(ServiceOperation("POST", "/v1"),),
                     quota_per_task=10_000)
    recon = _forge(gov, ACT_SERVICE_REGISTER,
                   {"service": wider.body(),
                    "service_digest": wider.digest()}, actor="mallory")

    assert any("nobody reviewed" in a for a in recon.anomalies), \
        recon.anomalies
    assert recon.services["registrar"]["quota_per_task"] == 2


def test_the_second_reader_counts_the_budget_and_notices_it_passed(gov):
    """Counted from the log, so a budget the scheduler believes is spent and
    one the log actually shows are two numbers that can be compared."""
    from qta_agent.netauth import ACT_NET_REQUEST
    _register_service(gov)
    for _ in range(3):
        gov.log.append(actor="w", action=ACT_NET_REQUEST, target="t1",
                       payload={"service_id": "registrar", "task_id": "t1",
                                "allowed": True, "request": {},
                                "decision": {}})
    recon = reconstruct_subsystems(gov.log)
    assert recon.service_calls["registrar/t1"] == 3
    assert any("past its 2-call budget" in a for a in recon.anomalies), \
        recon.anomalies


def test_a_refused_call_does_not_count_against_the_budget(gov):
    """ANTI-VACUITY. If every recorded request counted, the budget would be
    spent by attempts that never happened."""
    from qta_agent.netauth import ACT_NET_REQUEST
    _register_service(gov)
    for _ in range(5):
        gov.log.append(actor="w", action=ACT_NET_REQUEST, target="t1",
                       payload={"service_id": "registrar", "task_id": "t1",
                                "allowed": False, "request": {},
                                "decision": {}})
    recon = reconstruct_subsystems(gov.log)
    assert recon.service_calls.get("registrar/t1", 0) == 0
    assert not [a for a in recon.anomalies if "budget" in a]


# --------------------------------------------------------------------------
# Ownership and the retry budget, as the SECOND reader states them.
#
# Everything above about job transitions checks their SHAPE. These check the
# two things the shape says nothing about: who was entitled to write the
# record, and what the record is allowed to do to the count that the retry
# budget is measured against.
#
# The reason this matters more than the usual second-reader test: the
# production reducer refuses every forgery below, so a log carrying one
# CANNOT BE LOADED by the primary at all. The primary-versus-reader
# comparison never runs on these histories. On exactly the logs where a
# second opinion is the point, this reader is the only reader there is --
# and it accepted all of them until these tests were written.
# --------------------------------------------------------------------------

def _lapse(gov, n=6):
    """Push the log past any live lease without touching the job.

    The filler action is deliberately one no ``_sub_*`` reducer handles.
    Advancing the log with a real subsystem record would manufacture
    findings of its own and make the anti-vacuity checks below unreadable.
    """
    for i in range(n):
        gov.log.append(actor="filler", action="filler.tick",
                       target=f"filler-{i}", payload={})


def _job(recon, jid="j-lease"):
    return recon.jobs[jid]


def test_the_second_reader_refuses_an_outcome_signed_by_a_non_holder(gov):
    """An attempt is answered for by whoever was running it."""
    _leased_job(gov)
    recon = _forge(gov, "scheduler.transition",
                   {"job_id": "j-lease", "src": "DISPATCHED",
                    "dst": "SUCCEEDED", "reason": "mine now"},
                   actor="mallory")
    assert any("records its outcome" in a for a in recon.anomalies), \
        recon.anomalies
    # Not merely reported -- not folded. A reader that notes the anomaly and
    # then projects the forged state has told the truth and believed the lie.
    assert _job(recon)["state"] == "DISPATCHED"
    assert _job(recon)["lease_holder"] == "w1"


def test_the_second_reader_refuses_an_outcome_after_possession_ran_out(gov):
    """A late report does not get to decide work somebody else may have redone."""
    _leased_job(gov, lease_seqs=1)
    _lapse(gov)
    recon = _forge(gov, "scheduler.transition",
                   {"job_id": "j-lease", "src": "DISPATCHED",
                    "dst": "SUCCEEDED", "reason": "finished eventually"},
                   actor="w1")
    assert any("had possession only to seq" in a for a in recon.anomalies), \
        recon.anomalies
    assert _job(recon)["state"] == "DISPATCHED"


def test_the_second_reader_refuses_a_requeue_that_reclaims_a_live_lease(gov):
    """A requeue asserts possession ran out. The reader checks the assertion."""
    _leased_job(gov)
    recon = _forge(gov, "scheduler.transition",
                   {"job_id": "j-lease", "src": "DISPATCHED", "dst": "READY",
                    "reason": "lease lapsed (it did not)", "lease_id": "",
                    "lease_holder": "", "lease_expires_after_seq": -1},
                   actor="scheduler")
    assert any("taken back as though possession had run out" in a
               for a in recon.anomalies), recon.anomalies
    assert _job(recon)["state"] == "DISPATCHED"


def test_the_second_reader_refuses_a_give_up_that_reclaims_a_live_lease(gov):
    """The same rule on the other handover edge, which was the newer one."""
    _leased_job(gov)
    recon = _forge(gov, "scheduler.transition",
                   {"job_id": "j-lease", "src": "DISPATCHED", "dst": "FAILED",
                    "reason": "budget spent (it is not)", "lease_id": "",
                    "lease_holder": "", "lease_expires_after_seq": -1},
                   actor="scheduler")
    # Refused as an OUTCOME rather than as a reclamation: while possession
    # is live this edge is not a handover at all, so the ownership rule
    # reaches it first. The production reducer classifies it the same way,
    # which is the agreement being checked -- the two readers refuse the
    # same record for the same stated reason.
    assert any("records its outcome" in a for a in recon.anomalies), \
        recon.anomalies
    assert _job(recon)["state"] == "DISPATCHED"


def test_the_second_reader_refuses_an_attempts_reset_on_a_REAL_lapse(gov):
    """The case the possession rule cannot cover for.

    Possession really has run out, so the handover is legitimate and every
    ownership check above passes. The only thing standing between this log
    and a retry budget that never runs down is the rule about what a record
    may do to the count.
    """
    _leased_job(gov, lease_seqs=1)
    _lapse(gov)
    recon = _forge(gov, "scheduler.transition",
                   {"job_id": "j-lease", "src": "DISPATCHED", "dst": "READY",
                    "reason": "lapsed", "attempts": 0, "lease_id": "",
                    "lease_holder": "", "lease_expires_after_seq": -1},
                   actor="scheduler")
    assert any("only the hand-out edge moves that count" in a
               for a in recon.anomalies), recon.anomalies
    assert _job(recon)["attempts"] == 1


def test_the_second_reader_refuses_an_inflated_attempt_count(gov):
    _leased_job(gov)
    recon = _forge(gov, "scheduler.transition",
                   {"job_id": "j-lease", "src": "DISPATCHED",
                    "dst": "SUCCEEDED", "reason": "done", "attempts": 9999},
                   actor="w1")
    assert any("only the hand-out edge moves that count" in a
               for a in recon.anomalies), recon.anomalies
    assert _job(recon)["attempts"] == 1


def test_the_second_reader_refuses_a_transition_that_grants_possession(gov):
    """Ownership is taken at hand-out, never granted in passing."""
    _leased_job(gov, lease_seqs=1)
    _lapse(gov)
    recon = _forge(gov, "scheduler.transition",
                   {"job_id": "j-lease", "src": "DISPATCHED", "dst": "READY",
                    "reason": "lapsed", "lease_holder": "mallory",
                    "lease_id": "L-mine", "lease_expires_after_seq": 9999},
                   actor="scheduler")
    assert any("carries possession" in a for a in recon.anomalies), \
        recon.anomalies
    assert _job(recon)["lease_holder"] == "w1"


def test_the_second_reader_notices_a_budget_overrun(gov):
    """The count is checked against the bound it exists to be measured against."""
    s = _leased_job(gov, lease_seqs=1)
    budget = s.get("j-lease").max_attempts
    _lapse(gov)
    gov.log.append(
        actor="scheduler", action="scheduler.transition", target="j-lease",
        payload={"job_id": "j-lease", "src": "DISPATCHED", "dst": "READY",
                 "reason": "lapsed", "lease_id": "", "lease_holder": "",
                 "lease_expires_after_seq": -1})
    gov.log.append(
        actor="scheduler", action="scheduler.transition", target="j-lease",
        payload={"job_id": "j-lease", "src": "READY", "dst": "DISPATCHED",
                 "reason": "leased", "attempts": budget + 1,
                 "lease_holder": "w2", "lease_id": "L2",
                 "lease_expires_after_seq": 99999})
    recon = reconstruct_subsystems(gov.log)
    # Refused on the way in -- the count could not reach budget + 1 in one
    # step -- which is the stronger of the two findings and the one that
    # keeps the overrun from ever being projected.
    assert any("only the hand-out edge moves that count" in a
               for a in recon.anomalies), recon.anomalies
    assert _job(recon)["attempts"] <= budget


def test_the_second_reader_refuses_an_enqueue_with_no_retry_budget(gov):
    """A count with no bound is a count nothing is measured against."""
    recon = _forge(gov, "scheduler.enqueue", {"job": {
        "job_id": "j-nobudget", "state": "READY", "submitter": "mallory",
        "work_digest": "c" * 64, "priority": 0, "attempts": 0}})
    assert any("has nothing to be measured against" in a
               for a in recon.anomalies), recon.anomalies
    assert recon.jobs["j-nobudget"]["max_attempts"] is None


def test_an_honest_lapse_handover_is_NOT_flagged(gov):
    """Anti-vacuity: the rules above must still let the real thing through.

    A reader that refuses every handover would pass all seven tests above
    and be useless. This is the same edge, taken legitimately.
    """
    s = _leased_job(gov, lease_seqs=1)
    _lapse(gov)
    s.load()
    s.reconcile()
    recon = reconstruct_subsystems(gov.log)
    assert recon.anomalies == [], recon.anomalies
    assert _job(recon)["state"] == "READY"
    assert _job(recon)["attempts"] == 1
    assert _job(recon)["lease_holder"] == ""


def test_an_honest_outcome_from_the_holder_is_NOT_flagged(gov):
    """The other half of the anti-vacuity check: a real report."""
    s = _leased_job(gov)
    s.report(job_id="j-lease", worker="w1")
    recon = reconstruct_subsystems(gov.log)
    assert recon.anomalies == [], recon.anomalies
    assert _job(recon)["state"] == "SUCCEEDED"


def test_a_history_the_primary_REFUSES_is_still_read_by_the_second(gov):
    """Why these rules had to live here and not only in the reducer.

    The scheduler will not load this log at all, so the comparison between
    the two readers cannot run. If the second reader is silent, nothing in
    the system says anything about the forged record.
    """
    from qta_agent.scheduler import Scheduler
    s = _leased_job(gov)
    gov.log.append(actor="mallory", action="scheduler.transition",
                   target="j-lease",
                   payload={"job_id": "j-lease", "src": "DISPATCHED",
                            "dst": "SUCCEEDED", "reason": "mine now"})
    with pytest.raises(Exception) as primary:
        Scheduler(gov.log, policy=s.policy, policy_id=s.policy_id,
                  capacity={"slots": 8}).load()
    assert "mallory" in str(primary.value)

    recon = reconstruct_subsystems(gov.log)
    assert any("mallory" in a and "records its outcome" in a
               for a in recon.anomalies), recon.anomalies


def test_a_report_at_the_LAST_legal_position_is_not_flagged(gov):
    """Possession is judged where the WRITER decided, not where the record lands.

    A writer decides at the head and its record lands one past it. Judging
    liveness at the record's own position would refuse a report written at
    the last moment the lease was good -- a legitimate report, rejected for
    arithmetic. This is the boundary that off-by-one lives at, so it is
    checked at the boundary rather than in the comfortable middle.
    """
    s = _leased_job(gov, lease_seqs=4)
    end = s.get("j-lease").lease_expires_after_seq
    # Advance to exactly the last position a report may be DECIDED from, so
    # the record itself lands at end + 1.
    while s.at_seq() < end:
        gov.log.append(actor="filler", action="filler.tick",
                       target="pad", payload={})
        s.catch_up()
    s.report(job_id="j-lease", worker="w1")

    landed = [ev for ev in gov.log.read()
              if ev.action == "scheduler.transition"
              and ev.payload.get("dst") == "SUCCEEDED"]
    assert landed and landed[-1].seq == end + 1, (
        f"the report landed at {landed[-1].seq if landed else None}, not the "
        f"boundary {end + 1}; this test is no longer testing the boundary")

    recon = reconstruct_subsystems(gov.log)
    assert recon.anomalies == [], recon.anomalies
    assert _job(recon)["state"] == "SUCCEEDED"


# --------------------------------------------------------------------------
# Destruction, in the second reader's own words.
#
# Every rule below has the same shape as one the reader already applied to
# CREATION: a grant may not be minted by anyone, a human may not be admitted
# by an agent. None of them had a counterpart for taking the thing away, so
# admission was checked and removal was not -- which is the same authority
# reached from the other direction.
#
# For secret grants this reader is not a second opinion but the ONLY one:
# SecretStore has no reducer, so a secret.grant revocation appended around
# its write path is re-read by nothing else in the system.
# --------------------------------------------------------------------------

def _rooted(gov):
    """A run, so the log carries a root issuer and real grants."""
    _run(gov)
    return reconstruct_subsystems(gov.log)


def test_the_second_reader_refuses_a_revocation_from_an_unrelated_actor(gov):
    recon = _rooted(gov)
    cid = sorted(recon.capabilities)[0]
    after = _forge(gov, "capability.revoke",
                   {"capability_id": cid, "reason": "denial of service"},
                   actor="mallory")
    assert any("revokes" in a and "mallory" in a
               for a in after.anomalies), after.anomalies
    assert after.capabilities[cid]["revoked_seq"] is None


def test_the_second_reader_follows_a_revocation_by_the_issuer(gov):
    """Anti-vacuity: a reader that refused every revocation would pass above."""
    recon = _rooted(gov)
    cid = sorted(recon.capabilities)[0]
    issuer = recon.capabilities[cid]["issued_by"]
    after = _forge(gov, "capability.revoke",
                   {"capability_id": cid, "reason": "rotated"}, actor=issuer)
    assert after.anomalies == [], after.anomalies
    assert after.capabilities[cid]["revoked_seq"] is not None


def test_the_second_reader_refuses_an_egress_revocation_by_a_stranger(gov):
    gov.log.append(actor="control", action="network.grant", target="t1",
                   payload={"grant": {"grant_id": "g1"},
                            "grant_digest": "d" * 64})
    recon = _forge(gov, "network.grant",
                   {"grant_id": "g1", "revoke": True, "reason": "dos"},
                   actor="mallory")
    assert any("revokes network grant" in a for a in recon.anomalies), \
        recon.anomalies
    assert recon.net_grants["g1"]["revoked_seq"] is None


def test_the_second_reader_refuses_a_secret_revocation_by_a_stranger(gov):
    """The case where this reader is the only reader there is."""
    gov.log.append(actor="control", action="secret.grant", target="t1",
                   payload={"grant": {"grant_id": "sg1"},
                            "grant_digest": "e" * 64})
    recon = _forge(gov, "secret.grant",
                   {"grant_id": "sg1", "revoke": True, "reason": "dos"},
                   actor="mallory")
    assert any("revokes secret grant" in a for a in recon.anomalies), \
        recon.anomalies
    assert recon.secret_grants["sg1"]["revoked_seq"] is None


def test_the_second_reader_follows_a_grant_revoked_by_its_granter(gov):
    """Anti-vacuity for both grant tables."""
    gov.log.append(actor="control", action="network.grant", target="t1",
                   payload={"grant": {"grant_id": "g1"},
                            "grant_digest": "d" * 64})
    recon = _forge(gov, "network.grant",
                   {"grant_id": "g1", "revoke": True, "reason": "rotated"},
                   actor="control")
    assert recon.anomalies == [], recon.anomalies
    assert recon.net_grants["g1"]["revoked_seq"] is not None


def test_the_second_reader_refuses_an_agent_retiring_a_human(gov):
    gov.log.append(actor="system", action="agent.register", target="bot",
                   payload={"identity": {"instance_id": "bot",
                                         "agent_id": "bot", "kind": "AGENT",
                                         "roles": ["EXECUTOR"]}})
    gov.log.append(actor="out-of-band-bootstrap", action="agent.register",
                   target="person",
                   payload={"identity": {"instance_id": "person",
                                         "agent_id": "person",
                                         "kind": "HUMAN",
                                         "roles": ["REVIEWER"]}})
    recon = _forge(gov, "agent.retire",
                   {"instance_id": "person", "reason": "inconvenient"},
                   actor="bot")
    assert any("retires the HUMAN" in a for a in recon.anomalies), \
        recon.anomalies
    assert recon.agents["person"]["retired_seq"] is None


def test_the_second_reader_follows_a_principal_standing_itself_down(gov):
    """Anti-vacuity: somebody leaving is not a finding."""
    gov.log.append(actor="system", action="agent.register", target="bot",
                   payload={"identity": {"instance_id": "bot",
                                         "agent_id": "bot", "kind": "AGENT",
                                         "roles": ["EXECUTOR"]}})
    recon = _forge(gov, "agent.retire",
                   {"instance_id": "bot", "reason": "shutting down"},
                   actor="bot")
    assert recon.anomalies == [], recon.anomalies
    assert recon.agents["bot"]["retired_seq"] is not None


# --------------------------------------------------------------------------
# Escalations, independently reconstructed.
#
# The mutation campaign was described as covering "the second reader for
# every subsystem" while escalations had no independent reconstruction at
# all. Those cannot both be true. The honest resolutions are to build the
# reader or to weaken the claim; this is the reader.
#
# It shares no code with AgentDirectory. Every rule is restated from plain
# dictionaries -- which is the whole value, because one implementation cannot
# disagree with itself.
# --------------------------------------------------------------------------

from qta_agent.agents import (                                   # noqa: E402
    ACT_ESCALATION, ACT_ESCALATION_ANSWER, AgentRole, BOOTSTRAP,
    PrincipalKind, identity,
)


def _people(gov):
    """One agent and two humans, through the real directory."""
    d = gov.agents
    d.register(identity(agent_id="A", instance_id="A",
                        kind=PrincipalKind.AGENT,
                        roles={AgentRole.PROPOSER}), by="system")
    for who in ("H", "H2"):
        d.register(identity(agent_id=who, instance_id=who,
                            kind=PrincipalKind.HUMAN,
                            roles={AgentRole.REVIEWER}), by=BOOTSTRAP)
    return d


def _opened(gov, raised_by="A"):
    d = _people(gov)
    d.escalate(escalation_id="e1", task_id="t1", question="widen it?",
               raised_by=raised_by, options=("yes", "no"))
    return d


def _answer(**over):
    p = {"escalation_id": "e1", "state": "ANSWERED", "answer": "yes",
         "answered_by": "H", "reason": "checked it"}
    p.update(over)
    return p


def test_the_second_reader_follows_an_honest_escalation(gov):
    """Anti-vacuity, first: a reader that flagged everything is useless."""
    d = _opened(gov)
    d.answer(escalation_id="e1", answered_by="H", answer="yes",
             reason="checked and it holds")
    recon = reconstruct_subsystems(gov.log)
    assert recon.anomalies == [], recon.anomalies
    e = recon.escalations["e1"]
    assert (e["state"], e["raised_by"], e["answer"], e["answered_by"]) == (
        "ANSWERED", "A", "yes", "H")


def test_the_second_reader_follows_an_honest_withdrawal(gov):
    d = _opened(gov)
    d.withdraw(escalation_id="e1", by="A", reason="no longer needed")
    recon = reconstruct_subsystems(gov.log)
    assert recon.anomalies == [], recon.anomalies
    assert recon.escalations["e1"]["state"] == "WITHDRAWN"


def test_the_second_reader_refuses_an_agent_signing_a_humans_answer(gov):
    _opened(gov)
    recon = _forge(gov, ACT_ESCALATION_ANSWER, _answer(), actor="A")
    assert any("names 'H' as its answerer" in a for a in recon.anomalies), \
        recon.anomalies
    assert recon.escalations["e1"]["state"] == "OPEN"


def test_the_second_reader_refuses_an_agent_answering_as_itself(gov):
    _opened(gov)
    recon = _forge(gov, ACT_ESCALATION_ANSWER,
                   _answer(answered_by="A"), actor="A")
    assert any("may not answer" in a for a in recon.anomalies), recon.anomalies


def test_the_second_reader_refuses_the_ASKER_answering_their_own(gov):
    """Isolated on purpose.

    When an agent raises it, the KIND check catches the asker first and this
    rule is never reached. A HUMAN raiser reaches it.
    """
    _opened(gov, raised_by="H")
    recon = _forge(gov, ACT_ESCALATION_ANSWER, _answer(), actor="H")
    assert any("raised escalation 'e1' and may" in a
               for a in recon.anomalies), recon.anomalies


def test_the_second_reader_refuses_a_retired_human_answering(gov):
    d = _opened(gov)
    d.retire("H", by=BOOTSTRAP, reason="left the project")
    recon = _forge(gov, ACT_ESCALATION_ANSWER, _answer(), actor="H")
    assert any("was retired after seq" in a for a in recon.anomalies), \
        recon.anomalies


def test_the_second_reader_refuses_an_answer_outside_the_options(gov):
    _opened(gov)
    recon = _forge(gov, ACT_ESCALATION_ANSWER,
                   _answer(answer="maybe"), actor="H")
    assert any("is not one of" in a for a in recon.anomalies), recon.anomalies


def test_the_second_reader_refuses_a_third_party_withdrawal(gov):
    _opened(gov)
    recon = _forge(gov, ACT_ESCALATION_ANSWER,
                   {"escalation_id": "e1", "state": "WITHDRAWN",
                    "reason": "not mine"}, actor="H")
    assert any("withdraws it" in a for a in recon.anomalies), recon.anomalies
    assert recon.escalations["e1"]["state"] == "OPEN"


def test_the_second_reader_refuses_a_second_decision(gov):
    d = _opened(gov)
    d.answer(escalation_id="e1", answered_by="H", answer="yes", reason="r")
    recon = _forge(gov, ACT_ESCALATION_ANSWER,
                   _answer(answer="no", answered_by="H2"), actor="H2")
    assert any("deciding it again" in a for a in recon.anomalies), \
        recon.anomalies
    assert recon.escalations["e1"]["answer"] == "yes"


def test_the_second_reader_refuses_reopening_a_withdrawn_escalation(gov):
    d = _opened(gov)
    d.withdraw(escalation_id="e1", by="A", reason="done")
    recon = _forge(gov, ACT_ESCALATION_ANSWER, _answer(), actor="H")
    assert any("deciding it again" in a for a in recon.anomalies), \
        recon.anomalies


def test_the_second_reader_refuses_an_answer_to_an_unknown_escalation(gov):
    _opened(gov)
    recon = _forge(gov, ACT_ESCALATION_ANSWER,
                   _answer(escalation_id="e-nope"), actor="H")
    assert any("never opened" in a for a in recon.anomalies), recon.anomalies


@pytest.mark.parametrize("state", ["OPEN", "REOPENED", "", None])
def test_the_second_reader_refuses_a_decision_state_that_is_neither(
        gov, state):
    """Answered or withdrawn. Anything else is not a decision at all.

    Without this the record falls past the withdrawal branch into the answer
    branch and is PROJECTED AS ANSWERED whatever it claimed to be -- so a
    record saying OPEN becomes a recorded human decision.
    """
    _opened(gov)
    recon = _forge(gov, ACT_ESCALATION_ANSWER,
                   _answer(state=state), actor="H")
    assert any("neither answered nor withdrawn" in a
               for a in recon.anomalies), recon.anomalies
    assert recon.escalations["e1"]["state"] == "OPEN"
    assert recon.escalations["e1"]["answer"] is None


def _raise(**over):
    esc = {"escalation_id": "e2", "task_id": "t1", "question": "q?",
           "raised_by": "A", "state": "OPEN", "options": ["yes", "no"]}
    esc.update(over)
    return {"escalation": esc}


def test_the_second_reader_refuses_a_forged_raiser(gov):
    _opened(gov)
    recon = _forge(gov, ACT_ESCALATION, _raise(raised_by="H"), actor="A")
    assert any("attributed to whoever asked it" in a
               for a in recon.anomalies), recon.anomalies
    assert "e2" not in recon.escalations


def test_the_second_reader_refuses_a_duplicate_create(gov):
    _opened(gov)
    recon = _forge(gov, ACT_ESCALATION, _raise(escalation_id="e1"), actor="A")
    assert any("raised twice" in a for a in recon.anomalies), recon.anomalies


def test_the_second_reader_refuses_an_escalation_born_answered(gov):
    _opened(gov)
    recon = _forge(gov, ACT_ESCALATION,
                   _raise(state="ANSWERED", answer="yes", answered_by="H"),
                   actor="A")
    assert any("raising a question is not answering it" in a
               for a in recon.anomalies), recon.anomalies


def test_the_second_reader_refuses_an_escalation_with_one_option(gov):
    _opened(gov)
    recon = _forge(gov, ACT_ESCALATION, _raise(options=["yes"]), actor="A")
    assert any("distinct option(s)" in a for a in recon.anomalies), \
        recon.anomalies


def test_the_second_reader_refuses_an_escalation_that_asks_nothing(gov):
    _opened(gov)
    recon = _forge(gov, ACT_ESCALATION, _raise(question="   "), actor="A")
    assert any("asks nothing" in a for a in recon.anomalies), recon.anomalies


def test_a_HUMAN_registration_forgery_reaches_the_escalation_answer(gov):
    """The two subsystems compose, and this reader sees both.

    An agent that could register a HUMAN would be one step from answering its
    own escalations. The register rule is checked here already; this asserts
    the composition, so a bypass in one is not answered by silence in the
    other.
    """
    _opened(gov)
    gov.log.append(actor="A", action="agent.register", target="fake-human",
                   payload={"identity": {"instance_id": "fake-human",
                                         "agent_id": "fake-human",
                                         "kind": "HUMAN",
                                         "roles": ["REVIEWER"]}})
    recon = _forge(gov, ACT_ESCALATION_ANSWER,
                   _answer(answered_by="fake-human"), actor="fake-human")
    assert any("is not HUMAN and registers" in a for a in recon.anomalies), \
        recon.anomalies
    # ...and the answer does not land, because the principal never existed.
    assert recon.escalations["e1"]["state"] == "OPEN"


def test_the_second_reader_imports_none_of_the_layers_it_reads(gov):
    """A second reader that calls the first is the same decision run twice.

    Checked over the parsed IMPORTS rather than over the text: a substring
    search matches this module's own prose about the layers it deliberately
    does not import, which is a check that fails for being right.
    """
    import ast
    import qta_agent.reconstruct as R

    tree = ast.parse(Path(R.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.lstrip("."))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)

    # authority and tasks joined this list late, and their absence from it
    # was the finding: the two machines at the top of reconstruct.py imported
    # the very check() functions they exist to second-guess, while this test
    # stood over the nine subsystems below them saying the separation held.
    forbidden = {"agents", "scheduler", "policy", "capability", "memory",
                 "netauth", "secrets", "context", "authority", "tasks",
                 "qta_agent.agents", "qta_agent.scheduler",
                 "qta_agent.policy", "qta_agent.capability",
                 "qta_agent.memory", "qta_agent.netauth",
                 "qta_agent.secrets", "qta_agent.context",
                 "qta_agent.authority", "qta_agent.tasks"}
    leaked = sorted(imported & forbidden)
    assert not leaked, (
        f"the second reader imports {leaked}; it would then agree with those "
        "layers by construction, including where they are wrong")


# ---------------------------------------------------------------------------
# D-2026-29 (P0-R14): claims and compensations, independently reconstructed.
#
# "28 of 37 durable actions have a second reader" stood beside a completion
# matrix reading "39/39 complete, 0 residual gaps". Both were true and
# neither said whether the nine uncovered actions MATTERED.
#
# Classifying them says so. Two of the nine change what the system permits or
# whom it attributes a decision to:
#
#   * agent.claim is the INPUT to conflict resolution. Quorum counts claims,
#     PREFER_ROLE selects among them by role, REQUIRE_HUMAN decides a
#     disagreement is not an agent's to settle. A claim attributable to
#     anyone manufactures or suppresses the disagreement two "independent"
#     parties are said to have.
#   * task.compensation names the PERSON who authorized destroying
#     something, copied from the escalation that authorized it.
#
# The other seven are audit records of decisions taken elsewhere, and the
# inventory now refuses a classification of authority-changing that has no
# second reader -- so the gap cannot be widened by re-labelling it.
# ---------------------------------------------------------------------------

from qta_agent.agents import ACT_CLAIM  # noqa: E402
from qta_agent.canonical import digest_bytes as _dg  # noqa: E402

ACT_COMPENSATION = "task.compensation"
VALUE = _dg(b"the value this claim is about")


def _claimants(gov):
    """Two agents that may claim, and one that holds no claiming role."""
    d = gov.agents
    d.register(identity(agent_id="P", instance_id="P",
                        kind=PrincipalKind.AGENT,
                        roles={AgentRole.PROPOSER}), by="system")
    d.register(identity(agent_id="V", instance_id="V",
                        kind=PrincipalKind.AGENT,
                        roles={AgentRole.VERIFIER}), by="system")
    d.register(identity(agent_id="S", instance_id="S",
                        kind=PrincipalKind.AGENT,
                        roles={AgentRole.SCHEDULER}), by="system")
    return d


def _claim(**over):
    p = {"claim_id": "c1", "task_id": "t1", "subject": "peak_temperature",
         "value_digest": VALUE, "by_instance": "P", "role": "PROPOSER"}
    p.update(over)
    return p


def test_the_second_reader_follows_an_honest_claim(gov):
    """Anti-vacuity, first: a reader that flagged every claim says nothing."""
    d = _claimants(gov)
    d.claim(claim_id="c1", task_id="t1", subject="peak_temperature",
            value_digest=VALUE, by_instance="P", role=AgentRole.PROPOSER)
    recon = reconstruct_subsystems(gov.log)
    assert recon.anomalies == [], recon.anomalies
    c = recon.claims["c1"]
    assert (c["by_instance"], c["role"], c["value_digest"]) == (
        "P", "PROPOSER", VALUE)


def test_the_second_reader_follows_two_claims_that_disagree(gov):
    """The case the reader exists for: a real disagreement, read honestly.

    If this were flagged, the reader would be unable to tell a conflict from
    a forgery -- and a conflict is what it has to be able to see, because a
    conflict is what sends a decision to a person.
    """
    d = _claimants(gov)
    other = _dg(b"a different value")
    d.claim(claim_id="c1", task_id="t1", subject="peak_temperature",
            value_digest=VALUE, by_instance="P", role=AgentRole.PROPOSER)
    d.claim(claim_id="c2", task_id="t1", subject="peak_temperature",
            value_digest=other, by_instance="V", role=AgentRole.VERIFIER)
    recon = reconstruct_subsystems(gov.log)
    assert recon.anomalies == [], recon.anomalies
    assert {recon.claims["c1"]["value_digest"],
            recon.claims["c2"]["value_digest"]} == {VALUE, other}


def test_the_second_reader_refuses_a_claim_attributed_to_somebody_else(gov):
    """A claim is attributed to the instance that recorded it."""
    _claimants(gov)
    recon = _forge(gov, ACT_CLAIM, _claim(), actor="V")
    assert any("was made by 'P' and was appended by 'V'" in a
               for a in recon.anomalies), recon.anomalies
    assert "c1" not in recon.claims


def test_the_second_reader_refuses_a_claim_from_a_stranger(gov):
    """An unregistered principal has no roles, so it has no claims either."""
    _claimants(gov)
    recon = _forge(gov, ACT_CLAIM, _claim(by_instance="mallory"),
                   actor="mallory")
    assert any("is not a registered principal" in a
               for a in recon.anomalies), recon.anomalies
    assert "c1" not in recon.claims


def test_the_second_reader_refuses_a_claim_in_a_role_nobody_granted(gov):
    """The role is what PREFER_ROLE selects on.

    S is registered and active, so this fails for the ROLE and for nothing
    else -- which is what makes it a statement about the role check.
    """
    _claimants(gov)
    recon = _forge(gov, ACT_CLAIM,
                   _claim(by_instance="S", role="VERIFIER"), actor="S")
    assert any("claims 'c1' as 'VERIFIER'" in a
               for a in recon.anomalies), recon.anomalies
    assert "c1" not in recon.claims


def test_the_second_reader_refuses_a_claim_in_a_role_nobody_defined(gov):
    _claimants(gov)
    recon = _forge(gov, ACT_CLAIM, _claim(role="ORACLE"), actor="P")
    assert any("not a role this reader knows" in a
               for a in recon.anomalies), recon.anomalies


def test_the_second_reader_refuses_a_claim_from_a_retired_instance(gov):
    """A party that has left does not get one more opinion."""
    d = _claimants(gov)
    d.retire(instance_id="P", by="P", reason="rotated out")
    recon = _forge(gov, ACT_CLAIM, _claim(), actor="P")
    assert any("was retired after seq" in a for a in recon.anomalies), \
        recon.anomalies
    assert "c1" not in recon.claims


def test_the_second_reader_refuses_a_claim_whose_value_is_prose(gov):
    """Two claims are compared by digest, so a claim without one can never
    disagree with anything -- which is a way to be counted by quorum without
    ever being contradicted."""
    _claimants(gov)
    recon = _forge(gov, ACT_CLAIM, _claim(value_digest="about 900 K"),
                   actor="P")
    assert any("names its value as 'about 900 K'" in a
               for a in recon.anomalies), recon.anomalies
    assert "c1" not in recon.claims


def test_the_second_reader_refuses_a_claim_recorded_twice(gov):
    d = _claimants(gov)
    d.claim(claim_id="c1", task_id="t1", subject="peak_temperature",
            value_digest=VALUE, by_instance="P", role=AgentRole.PROPOSER)
    recon = _forge(gov, ACT_CLAIM, _claim(value_digest=_dg(b"other")),
                   actor="P")
    assert any("recorded twice" in a for a in recon.anomalies), \
        recon.anomalies
    # ...and the first one is what stands.
    assert recon.claims["c1"]["value_digest"] == VALUE


# --- compensations ---------------------------------------------------------

def _compensation(**over):
    p = {"task_id": "t1", "compensated_tool": "stage10.emit_artifact",
         "compensating_tool": "stage10.remove_artifact",
         "authorized_by_escalation": "e1", "answered_by": "H",
         "outcome": "SUCCEEDED"}
    p.update(over)
    return p


def _answered(gov):
    d = _opened(gov)
    d.answer(escalation_id="e1", answered_by="H", answer="yes",
             reason="checked and it holds")
    return d


def test_the_second_reader_follows_an_honest_compensation(gov):
    """Anti-vacuity for every refusal below."""
    _answered(gov)
    recon = _forge(gov, ACT_COMPENSATION, _compensation(), actor="A",
                   target="t1")
    assert recon.anomalies == [], recon.anomalies
    (rec,) = recon.compensations["t1"]
    assert (rec["answered_by"], rec["authorized_by_escalation"]) == ("H", "e1")


def test_the_second_reader_refuses_a_compensation_naming_another_answerer(
        gov):
    """The field names a PERSON as having authorized destroying something."""
    _answered(gov)
    recon = _forge(gov, ACT_COMPENSATION, _compensation(answered_by="H2"),
                   actor="A", target="t1")
    assert any("was answered by 'H2'" in a for a in recon.anomalies), \
        recon.anomalies
    # Recorded anyway: a compensation is a fact about the task, and hiding
    # the forged one would lose the evidence that it happened.
    assert recon.compensations["t1"]


def test_the_second_reader_refuses_a_compensation_citing_no_escalation(gov):
    _answered(gov)
    recon = _forge(gov, ACT_COMPENSATION,
                   _compensation(authorized_by_escalation=None),
                   actor="A", target="t1")
    assert any("names no escalation" in a for a in recon.anomalies), \
        recon.anomalies


def test_the_second_reader_refuses_a_compensation_citing_a_ghost(gov):
    """Stricter than the primary, deliberately.

    The primary looks the escalation up and, when the lookup fails, checks
    nothing -- so a compensation authorized by a question nobody asked
    passes it silently. Saying what the log does not support is this
    reader's job.
    """
    _answered(gov)
    recon = _forge(gov, ACT_COMPENSATION,
                   _compensation(authorized_by_escalation="e-nonexistent"),
                   actor="A", target="t1")
    assert any("this log never carried" in a for a in recon.anomalies), \
        recon.anomalies


def test_the_second_reader_refuses_a_compensation_citing_an_open_question(
        gov):
    """A question nobody answered authorizes nothing."""
    _opened(gov)
    recon = _forge(gov, ACT_COMPENSATION, _compensation(answered_by=None),
                   actor="A", target="t1")
    assert any("which is 'OPEN'" in a for a in recon.anomalies), \
        recon.anomalies


# ---------------------------------------------------------------------------
# D-2026-30 (P1): the checkpoint anchor, independently reconstructed.
#
# checkpoint.state is authority-changing -- it is what authorizes restoring a
# projection from a snapshot instead of replaying the log -- so R14's rule
# requires a second reader for it, and this is that reader's tests.
#
# What it can check is bounded and the bound is stated: shape, ordering, one
# position not given two states, and the claimed head hash WHEN the claim
# names the position immediately before it. A claim about an older position
# is recorded as not hash-checked rather than left looking checked.
# ---------------------------------------------------------------------------

ACT_CHECKPOINT_STATE = "checkpoint.state"


def _anchor(gov, **over):
    """Append a checkpoint anchor naming the record just before it."""
    head = gov.log.verify()
    p = {"through_seq": head.head_seq, "state_digest": _dg(b"a projection"),
         "head_hash": head.head_hash}
    p.update(over)
    gov.log.append(actor="checkpointer", action=ACT_CHECKPOINT_STATE,
                   target=f"seq:{p['through_seq']}", payload=p)
    return reconstruct_subsystems(gov.log)


def test_the_second_reader_follows_an_honest_checkpoint_anchor(gov):
    """Anti-vacuity, and the only case where the hash CAN be checked here."""
    _run(gov)
    recon = _anchor(gov)
    assert recon.anomalies == [], recon.anomalies
    (rec,) = list(recon.checkpoints.values())
    assert rec["head_hash_checked"] is True
    assert rec["recorded_by"] == "checkpointer"


def test_the_second_reader_records_an_older_claim_as_unchecked(gov):
    """The bound, asserted rather than described.

    A claim about a position further back is structurally fine and this
    reader cannot verify its hash without an index over the whole log. It
    says so in the record instead of implying it checked.
    """
    _run(gov)
    head = gov.log.verify().head_seq
    recon = _anchor(gov, through_seq=head - 3)
    assert recon.anomalies == [], recon.anomalies
    (rec,) = list(recon.checkpoints.values())
    assert rec["head_hash_checked"] is False


def test_the_second_reader_refuses_a_claim_about_its_own_position(gov):
    """A snapshot cannot contain the record announcing it."""
    _run(gov)
    head = gov.log.verify().head_seq
    recon = _anchor(gov, through_seq=head + 1)
    assert any("cannot cover the record that announces it" in a
               for a in recon.anomalies), recon.anomalies
    assert recon.checkpoints == {}


def test_the_second_reader_refuses_an_anchor_naming_the_wrong_head_hash(gov):
    """The claim is about a history this log does not have."""
    _run(gov)
    recon = _anchor(gov, head_hash=_dg(b"some other history"))
    assert any("history this log does not have" in a
               for a in recon.anomalies), recon.anomalies
    assert recon.checkpoints == {}


def test_the_second_reader_refuses_an_anchor_citing_prose_for_a_snapshot(gov):
    _run(gov)
    recon = _anchor(gov, state_digest="the usual state")
    assert any("cited by digest or it is not cited" in a
               for a in recon.anomalies), recon.anomalies


def test_the_second_reader_refuses_two_states_for_one_position(gov):
    """One position has one projection.

    A reader handed two has no way to say which a checkpoint file means, and
    a checkpoint file is exactly what this record exists to adjudicate.
    """
    _run(gov)
    head = gov.log.verify().head_seq
    _anchor(gov, through_seq=head - 2, state_digest=_dg(b"one state"))
    recon = _anchor(gov, through_seq=head - 2, state_digest=_dg(b"another"))
    assert any("two different states" in a for a in recon.anomalies), \
        recon.anomalies
    # The first one stands; a later record does not replace it.
    assert recon.checkpoints[head - 2]["state_digest"] == _dg(b"one state")


def test_the_second_reader_refuses_an_anchor_with_no_position(gov):
    _run(gov)
    recon = _anchor(gov, through_seq="recently")
    assert any("no through_seq this reader can read" in a
               for a in recon.anomalies), recon.anomalies
