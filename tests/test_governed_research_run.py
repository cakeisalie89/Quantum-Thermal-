"""The research task, run on a GOVERNED history (Stage 2, S2-3).

The adversarial evaluation's legitimate task -- the owner's slab question,
the researcher's model run, its independent check, the reviewer's
verification and the ledger's verdict -- run unchanged on a history that
declares its key and role registries at genesis. Every record is signed
with its actor's key through the log handle; every role-governed act is
held to ``tools/adversarial_eval.GOVERNED_GRANTS``, live and on every
replay; and the governed task runs are confirmed by the separate process
under the roots it is handed.

WHAT IS SHOWN, AND WHAT IS NOT. The grants are the least that let the task
succeed: each is necessary, and the test removes each in turn. Every key is
a TEST identity, derivable from its name: the signatures show the
protocol, not who acted. Nothing here is a live model.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import adversarial_eval as E  # noqa: E402
from agent_substrate import principals as pr  # noqa: E402
from agent_substrate import roles as R  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.reconstruct import (  # noqa: E402
    derive_decision_proof, reconstruct, reconstruct_subsystems,
    reconstruct_tasks,
)
from agent_substrate.separate_verify import (  # noqa: E402
    verify_in_separate_process,
)
from agent_substrate.store import AuthorityStore, StoreError  # noqa: E402


@pytest.fixture(scope="module")
def world():
    w = E.World("pytest-governed-run", E.legitimate(), governed=True)
    w.controller().run(E.OBJECTIVE)
    yield w
    w.clean()


def _verified(w):
    return next(e for e in w.events() if e.action == "record.transition"
                and e.payload.get("dst") == "VERIFIED")


def test_the_task_succeeds_on_a_governed_history(world):
    assert E.task_succeeded(world)
    events = world.events()
    genesis = events[0]
    _, keys, roles = E.governed_roots()
    assert genesis.action == "history.security_profile"
    assert genesis.payload == {
        "profile": "GOVERNED", "requirement": "harness/history-security/v1",
        "key_registry": pr.registry_digest(keys.to_document()),
        "role_registry": pr.registry_digest(roles.to_document())}
    # every record attested once, by the key of its own actor
    attested = world.attestations.read()
    assert not attested.malformed and not attested.aborted
    for ev in events:
        (att,) = attested.by_hash[ev.hash]
        assert keys.get(att["key_id"]).principal == ev.actor


def test_every_reader_agrees_under_the_roots(world):
    """The primary store, the independent readers in this process and in
    another, and the decision proof: each reads the governed history under
    its roots and refuses nothing in it."""
    roots = world.roots()
    store = AuthorityStore(world.log).load()
    rid = _verified(world).payload["record_id"]
    assert store.get(rid).state.value == "VERIFIED"
    rec = reconstruct(world.log, **roots)
    assert rec.unauthorized == [] and rec.records[rid]["state"] == \
        "VERIFIED"
    assert reconstruct_tasks(world.log, roles=roots["roles"]
                             ).unauthorized == []
    assert reconstruct_subsystems(world.log, roles=roots["roles"]
                                  ).anomalies == []
    seps = [e.payload for e in world.events()
            if e.action == "task.separate_verification"]
    assert len(seps) == 2 and all(s["ok"] for s in seps), seps
    v = verify_in_separate_process(world.log.path, root=ROOT, **roots)
    assert v.ok, (v.reason, v.findings)
    proof = derive_decision_proof(world.log, _verified(world).seq,
                                  evidence=world.evidence, **roots)
    assert proof["accepted"] is True
    assert proof["separation_of_duties"]["distinct"] is True


@pytest.mark.parametrize("grant", [g[0] for g in E.GOVERNED_GRANTS])
def test_each_grant_is_needed(grant):
    """Least privilege, measured: without any one grant the task cannot
    succeed -- refused live by the store, the ledger or the task runner
    that the grant's act passes through."""
    without = tuple(g for g in E.GOVERNED_GRANTS if g[0] != grant)
    w = None
    try:
        w = E.World(f"pytest-governed-without-{grant}", E.legitimate(),
                    governed=True, grants=without)
        w.controller().run(E.OBJECTIVE)
        succeeded = E.task_succeeded(w)
    except Exception:                           # noqa: BLE001
        succeeded = False                       # refused before the end
    finally:
        if w is not None:
            w.clean()
        else:
            shutil.rmtree(ROOT / E.WS / f"pytest-governed-without-{grant}",
                          ignore_errors=True)
    assert not succeeded, f"the task succeeded without {grant}"


def test_a_registered_party_without_a_grant_is_refused_by_every_reader(
        world, tmp_path):
    """The colleague is registered, and its key is genuine. In a copy of the
    history taken just after the researcher's result was proposed, it signs
    the result's first review -- a transition the machine allows, by a
    record no store would write for it. Authenticated, not authorized:
    every reader refuses it, and only the missing grant can."""
    events = world.events()
    rid = _verified(world).payload["record_id"]
    created = next(e for e in events if e.action == "record.create"
                   and e.target == rid)
    review = next(e for e in events if e.action == "record.transition"
                  and e.payload.get("record_id") == rid
                  and e.payload.get("dst") == "UNDER_REVIEW")
    kept = [e for e in events if e.seq <= created.seq]
    lines = (world.base / "log.jsonl").read_bytes().split(b"\n")
    (tmp_path / "log.jsonl").write_bytes(b"\n".join(lines[:len(kept)])
                                         + b"\n")
    hashes = {e.hash for e in kept}
    att_lines = [ln for ln in (world.base / "log.attestations.jsonl")
                 .read_bytes().split(b"\n")
                 if ln and json.loads(ln).get("event_hash") in hashes]
    (tmp_path / "log.attestations.jsonl").write_bytes(
        b"\n".join(att_lines) + b"\n")
    _, keys, roles = E.governed_roots()
    att = pr.Attestations(tmp_path / "log.attestations.jsonl")
    auth = pr.Authenticator(att, keys)
    log = EventLog(tmp_path / "log.jsonl", authenticator=auth, roles=roles)
    assert log.read_verified()[0].ok
    pr.signed_append(log, att, pr.test_identity(E.OTHER),
                     action="record.transition", target=rid,
                     payload=dict(review.payload))
    with pytest.raises(StoreError, match=R.NOT_GRANTED):
        AuthorityStore(log).load()
    rec = reconstruct(log, authenticator=auth, roles=roles)
    assert rec.records[rid]["state"] == "PROPOSED"
    assert any(E.OTHER in u and R.NOT_GRANTED in u
               for u in rec.unauthorized), rec.unauthorized
    v = verify_in_separate_process(log.path, root=ROOT, authenticator=auth,
                                   roles=roles)
    assert not v.ok
    assert any(R.NOT_GRANTED in f for f in v.findings), v.findings
