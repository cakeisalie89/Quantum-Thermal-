"""The separate process reads a governed history under its roots (Stage 2,
S2-2).

THE GAPS, REPRODUCED ON THE STAGE 1 TREE.

1. The child process (``tools/independent_verify.py``) opened the log with
   no authenticator and no role registry: it may not import the key layer,
   and nothing handed it the roots. On a GOVERNED history it read nothing,
   so a governed task run was REJECTED by its own independent check -- the
   harness could not run governed at all.
2. The child counted the second reader's anomalies and dropped what it
   REFUSES (``tests/test_agent_separate_verify.py`` holds that one).
3. ``derive_decision_proof`` and ``memory_trust.assess`` re-read the history
   with no roots, so on a GOVERNED history every decision read as not
   accepted and every memory as inconsistently reconstructed.

THE CLOSURE. The parent hands the child the history's PUBLIC roots -- the
attestation file's path and both registry documents -- and the child reads
under them with the authentication restated in ``tools/verify_assurance.py``
(``tests/test_restated_authentication.py`` holds it to the producer's
verdicts). A GOVERNED history the child is not handed them for still reads
as nothing, and an authenticator that cannot be handed over is refused, not
dropped. The decision proof and memory trust read under the roots they are
given.

Every key is a TEST identity; every history below is real.
"""
from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate import principals as pr  # noqa: E402
from agent_substrate import roles as R  # noqa: E402
from agent_substrate.authority import Role, State  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.governed_tasks import (  # noqa: E402
    ACT_SEPARATE_VERIFY, GovernedTasks,
)
from agent_substrate.memory import MemoryStore  # noqa: E402
from agent_substrate.memory_trust import assess  # noqa: E402
from agent_substrate.reconstruct import derive_decision_proof  # noqa: E402
from agent_substrate.separate_verify import (  # noqa: E402
    EXIT_CLEAN, verify_in_separate_process,
)
from agent_substrate.store import AuthorityStore  # noqa: E402
from agent_substrate.tasks import TaskState  # noqa: E402

GOVERNED = "GOVERNED"
ACTORS = ("owner", "system", "scheduler", "alice", "bob", "carol", "mallory",
          "stage10-submitter", "stage10-worker", "stage10-verifier")
SIGNERS = {a: pr.test_identity(a) for a in ACTORS}
TOOL = "stage10.emit_artifact"
WS = "verification/stage10/_pytest_separate_governed"


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _keys(*extra):
    return pr.KeyRegistry([s.registered() for s in (*SIGNERS.values(),
                                                    *extra)],
                          allow_test_keys=True)


GRANTS = (
    R.RoleGrant("g-alice", "alice", R.PROPOSER, ("kind:note",)),
    R.RoleGrant("g-bob", "bob", R.VERIFIER, ("kind:note",)),
    R.RoleGrant("g-carol", "carol", R.PROMOTER, ("kind:note",)),
    R.RoleGrant("g-owner", "owner", R.POLICY_OWNER, ("policy:*",)),
    R.RoleGrant("g-submit", "stage10-submitter", R.PROPOSER,
                (f"tool:{TOOL}",)),
    R.RoleGrant("g-work", "stage10-worker", R.EXECUTOR, (f"tool:{TOOL}",)),
    R.RoleGrant("g-check", "stage10-verifier", R.VERIFIER,
                (f"tool:{TOOL}",)),
)


class Governed:
    """A GOVERNED history, written through signing handles."""

    def __init__(self, base: Path):
        self.base = base
        self.path = base / "log.jsonl"
        self.att = pr.Attestations(base / "log.attestations.jsonl")
        self.keys = _keys()
        self.roles = R.RoleRegistry(list(GRANTS))
        self.evidence = EvidenceStore(base / "evidence")
        pr.begin_history(self.open(), self.att, SIGNERS["owner"],
                         profile=GOVERNED, roles=self.roles)

    def auth(self, keys=None):
        return pr.Authenticator(self.att, keys or self.keys)

    def open(self, *, signing=False) -> EventLog:
        return EventLog(self.path, authenticator=self.auth(),
                        roles=self.roles,
                        signing=pr.Signing(self.att, SIGNERS.values())
                        if signing else None)

    def walk(self, *, promoter="carol"):
        claim = self.evidence.put(b"r1: the claim")
        report = self.evidence.put(b"r1: the reviewer's report")
        store = AuthorityStore(self.open(signing=True)).load()
        store.create(record_id="r1", kind="note", proposer="alice",
                     evidence={"claim": claim})
        store.transition(record_id="r1", dst=State.UNDER_REVIEW,
                         actor="bob", role=Role.VERIFIER)
        store.transition(record_id="r1", dst=State.VERIFIED, actor="bob",
                         role=Role.VERIFIER,
                         evidence={"verification_report": report})
        if promoter == "carol":
            store.transition(record_id="r1", dst=State.PROMOTED,
                             actor="carol", role=Role.PROMOTER,
                             evidence={"policy_id": "p1"}, policy_id="p1")
        else:
            # authenticated, and not authorized: a signed record no store
            # would write, made the way any holder of a key can make one
            pr.signed_append(self.open(), self.att, SIGNERS[promoter],
                             action="record.transition", target="r1",
                             payload={"record_id": "r1", "src": "VERIFIED",
                                      "dst": "PROMOTED", "role": "PROMOTER",
                                      "evidence": {"policy_id": "p1"},
                                      "policy_id": "p1",
                                      "stale_reason": None,
                                      "edge_reason": "",
                                      "idempotency_key": None})

    def separately(self, **kw):
        kw.setdefault("authenticator", self.auth())
        kw.setdefault("roles", self.roles)
        return verify_in_separate_process(self.path, root=ROOT, **kw)


@pytest.fixture()
def g(tmp_path):
    return Governed(tmp_path)


def test_the_other_process_reads_a_governed_history_under_its_roots(g):
    g.walk()
    v = g.separately()
    assert v.ok and v.exit_status == EXIT_CLEAN, (v.reason, v.findings)
    assert v.events_replayed == 5


@pytest.mark.parametrize("given", ["no roots", "no role registry",
                                   "another role registry",
                                   "another key registry"])
def test_without_its_own_roots_the_other_process_reads_nothing(g, given):
    g.walk()
    kw = {"no roots": {"authenticator": None, "roles": None},
          "no role registry": {"roles": None},
          "another role registry": {"roles": R.RoleRegistry(
              [*GRANTS, R.RoleGrant("g-x", "mallory", R.PROMOTER,
                                    ("kind:note",))])},
          "another key registry": {"authenticator": g.auth(
              _keys(pr.test_identity("zed")))}}[given]
    v = g.separately(**kw)
    assert not v.ok, v.reason
    assert GOVERNED in v.reason + " ".join(v.findings), (v.reason,
                                                         v.findings)


def test_an_authenticated_unauthorized_promotion_is_a_finding_there(g):
    g.walk(promoter="mallory")
    v = g.separately()
    assert not v.ok
    assert any("mallory" in f and R.NOT_GRANTED in f for f in v.findings), \
        v.findings


def test_a_record_its_writer_did_not_sign_is_a_finding_there(g):
    """A writer that bypasses signing -- its pre-write hook writes no
    attestation -- leaves a chained record no key vouches for."""
    g.walk()
    g.open().append(actor="carol", action="note.write", target="n1",
                    payload={}, before_write=lambda ev: None)
    v = g.separately()
    assert not v.ok
    assert "MISSING" in v.reason + " ".join(v.findings), (v.reason,
                                                          v.findings)


def test_an_authenticator_that_cannot_be_handed_over_is_refused(g):
    """Dropped, the child would read an authenticated history with no
    authenticator; it is not run at all."""
    g.walk()
    v = g.separately(authenticator=lambda events, complete=True: None)
    assert not v.ok and v.exit_status is None
    assert "cannot be handed" in v.reason


def test_a_governed_task_run_is_confirmed_by_the_other_process(tmp_path):
    """End to end: the control plane runs a tool on a GOVERNED history,
    every record signed through the handle, and its own independent check
    -- another process, reading under the roots it is handed -- confirms
    it. On the Stage 1 tree the same run was REJECTED: the child could not
    read the history."""
    base = ROOT / WS / tmp_path.name
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True)
    try:
        h = Governed(base)
        gov = GovernedTasks(root=ROOT, log=h.open(signing=True),
                            evidence=EvidenceStore(base / "evidence"))
        out = f"{WS}/{tmp_path.name}/out"
        run = gov.run(tool_id=TOOL, inputs={
            "out_dir": out, "name": "artifact.json",
            "payload": {"label": "MODEL_ONLY", "value": 42}})
        assert run.state is TaskState.VERIFIED, run.reason
        report, events = h.open().read_verified()
        assert report.ok, report.problems
        sep = [e.payload for e in events if e.action == ACT_SEPARATE_VERIFY]
        assert len(sep) == 1 and sep[0]["ok"], sep
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_a_decision_of_a_governed_history_is_proved_under_its_roots(g):
    g.walk()
    _, events = g.open().read_verified()
    verified = next(e for e in events if e.action == "record.transition"
                    and e.payload.get("dst") == "VERIFIED")
    proof = derive_decision_proof(g.open(), verified.seq,
                                  evidence=g.evidence,
                                  authenticator=g.auth(), roles=g.roles)
    assert proof["accepted"] is True
    assert proof["separation_of_duties"]["distinct"] is True
    # without the roots the second reader folds nothing of the history
    bare = derive_decision_proof(g.open(), verified.seq,
                                 evidence=g.evidence)
    assert bare["accepted"] is False
    # and the other process re-derives it from the log on disk
    v = g.separately(proof=proof, evidence_root=g.evidence.root)
    assert v.ok, (v.reason, v.findings)


def test_a_prefix_of_a_governed_history_is_read_as_a_catch_up(g):
    """The decision proof re-reads the history as it stood at a decision,
    and the attestations of every record after it are in the file. Read as
    a whole history, each one more than a record past the prefix is called
    dangling -- the record right after it reads as a pending prepare -- and
    the second reader lists those as authentication findings of a history
    that has none. The prefix is read as the catch-up it is."""
    from agent_substrate.reconstruct import _at, _prefix_roots, reconstruct
    g.walk()
    report, events = g.open().read_verified()
    upto = events[-3].seq          # two records follow it
    whole = reconstruct(_at(report, events, upto), authenticator=g.auth(),
                        roles=g.roles)
    assert any("DANGLING" in u for u in whole.unauthorized), \
        whole.unauthorized
    caught_up = reconstruct(_at(report, events, upto),
                            **_prefix_roots(g.auth(), g.roles))
    assert caught_up.unauthorized == [], caught_up.unauthorized


def test_memory_trust_reconstructs_a_governed_history_under_its_roots(g):
    MemoryStore(g.open(signing=True)).load().remember(
        memory_id="m1", text="the slab question is open", author="alice")
    trust = assess(g.open(), "m1")
    answer = trust.answers["consistently_reconstructed"]
    assert answer.holds, answer
    assert trust.answers["authenticated_actor"].holds
