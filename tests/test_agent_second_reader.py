"""A second implementation for the subsystems that had none.

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
