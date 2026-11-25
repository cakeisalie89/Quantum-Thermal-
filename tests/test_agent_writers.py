"""The anchored projections, written by several writers on one log (Wave B).

The memory store's search (tests/test_agent_memory.py, the lifecycle from
several writers) held one projection to a full load after every operation.
These hold the others that decide against the history they write onto: the
capability ledger, the policy store, the agent directory, the scheduler and
the authority store.

Three writers, each a projection of its own on one log, write a history
between them; each catches up from its anchor when it writes and when it is
asked, along the anchored route or the full read, and reopens. After every
operation:

  * every writer holds what a load of the history up to its own position
    holds, read past its public view;
  * a write by a writer, behind the log or not, decides and writes exactly
    what a projection just loaded from a copy of the log decides and writes
    beside it -- so a writer behind the log decides as the log as it is
    decides (AUD-095, AUD-096, AUD-097, AUD-100), and a refusal costs the
    log nothing but the record a refusal is designed to leave;
  * the second reader agrees with a fresh load of the whole history.

The histories are Hypothesis's, derandomized: each search runs the same 200
histories on every machine.
"""
from __future__ import annotations

import collections
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate.agents import (  # noqa: E402
    BOOTSTRAP, AgentDirectory, AgentError, AgentRole, PrincipalKind, identity,
)
from agent_substrate.authority import (  # noqa: E402
    Role, State, TransitionError, allowed_targets, find_edge,
)
from agent_substrate.canonical import digest  # noqa: E402
from agent_substrate.capability import (  # noqa: E402
    Action, CapabilityError, CapabilityLedger, issue,
)
from agent_substrate.events import ChainState, EventLog  # noqa: E402
from agent_substrate.policy import (  # noqa: E402
    ANY, Effect, PolicyError, PolicyRequest, PolicyStore, document, rule,
)
from agent_substrate.reconstruct import (  # noqa: E402
    compare, reconstruct, reconstruct_subsystems,
)
from agent_substrate.scheduler import (  # noqa: E402
    FailureClass, JobState, Scheduler, SchedulerError,
)
from agent_substrate.store import AuthorityStore, StoreError  # noqa: E402

_WRITERS = 3


# ==========================================================================
# THE HARNESS
# ==========================================================================
def _bytes(path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def _lines(path) -> int:
    return len(_bytes(path).splitlines())


def _records(path, start) -> list:
    """What the log holds from line ``start`` on, as written: who, what,
    on what, with what."""
    lines = _bytes(path).splitlines()
    return [(r["actor"], r["action"], r["target"], r["payload"])
            for r in map(json.loads, lines[start:])]


def _copy(path, scratch):
    """A copy of the log and its witness, for a projection to decide on
    beside the writer."""
    scratch.mkdir()
    for f in (path, path.with_suffix(path.suffix + ".head")):
        if f.exists():
            shutil.copy(f, scratch / f.name)
    return scratch / path.name


def _through(path, seq) -> EventLog:
    """The history up to ``seq`` alone: the log's first records, with a
    witness naming the last of them."""
    cut = path.parent / f"through-{seq}" / path.name
    cut.parent.mkdir()
    lines = path.read_bytes().split(b"\n")[:seq + 1] if seq >= 0 else []
    cut.write_bytes(b"".join(line + b"\n" for line in lines))
    log = EventLog(cut)
    if seq >= 0:
        head = json.loads(lines[-1])
        log._write_head(ChainState(seq, head["hash"]))
    return log


def _ops(kinds, weights, first):
    """Histories of 6 to 24 operations, the first of kind ``first``, the
    rest drawn by ``weights``."""
    from hypothesis import strategies as st
    pool = st.sampled_from([k for k, n in weights.items()
                            for _ in range(n)])
    rest = st.lists(pool.flatmap(kinds.__getitem__), min_size=5,
                    max_size=23)
    return st.tuples(kinds[first], rest).map(lambda t: [t[0], *t[1]])


def _house_ops(st) -> dict:
    """Catching up, along either route, and reopening: what every search
    interleaves with its writes."""
    w = st.integers(0, _WRITERS - 1)
    return {"catch_up": st.tuples(st.just("catch_up"), w,
                                  st.sampled_from(("anchored", "full"))),
            "reopen": st.tuples(st.just("reopen"), w)}


class _Search:
    """One projection's search. A subclass says how to build, read, write
    and catch one up; :meth:`run` is the same for all five."""

    #: What a refusal raises.
    refusal: tuple = ()
    #: What a refusal may write: none, or the record of the refusal itself
    #: where that is the design (the scheduler's gate records a denial).
    refusal_records: tuple = ()

    def make(self, log):
        raise NotImplementedError

    def setup(self, path) -> None:
        """The history every example starts from."""

    def held(self, p) -> dict:
        raise NotImplementedError

    def position(self, p) -> int:
        raise NotImplementedError

    def resolve(self, op, fresh):
        """``op`` with what it names bound against a projection just
        loaded, so that writes are made and not only refused."""
        return op

    def write(self, p, op) -> None:
        raise NotImplementedError

    def catch_up(self, p, route) -> None:
        raise NotImplementedError

    def agree(self, fresh, sub, path) -> None:
        """The second reader's reconstruction ``sub`` against ``fresh``."""
        raise NotImplementedError

    def load(self, path):
        return self.make(EventLog(path)).load()

    def second(self, path):
        """The second reader's reconstruction of the history at ``path``."""
        return reconstruct_subsystems(EventLog(path))

    def refused(self, p, op) -> bool:
        try:
            self.write(p, op)
        except self.refusal:
            return True
        return False

    def held_through(self, path, seq, cache) -> dict:
        if seq not in cache:
            cache[seq] = self.held(self.make(_through(path, seq)).load())
        return cache[seq]

    def run(self, d, ops) -> collections.Counter:
        path = d / "log.jsonl"
        self.setup(path)
        writers = [self.load(path) for _ in range(_WRITERS)]
        cache: dict = {}
        fresh, read = self.load(path), _bytes(path)
        n: collections.Counter = collections.Counter()
        for i, op in enumerate(ops):
            kind, w = op[0], op[1]
            if kind == "catch_up":
                self.catch_up(writers[w], op[2])
                n["catch_ups"] += 1
            elif kind == "reopen":
                writers[w] = self.load(path)
                n["reopens"] += 1
            else:
                start = _lines(path)
                beside = self.load(_copy(path, d / f"beside-{i}"))
                op = self.resolve(op, beside)
                expect = (self.refused(beside, op),
                          _records(beside.log.path, start))
                behind = self.position(writers[w]) < start - 1
                refused = self.refused(writers[w], op)
                wrote = _records(path, start)
                if refused:
                    assert all(r[1] in self.refusal_records
                               for r in wrote), op
                assert (refused, wrote) == expect, op
                n["writes"] += 1
                n["refused"] += refused
                n["behind"] += behind
            for p in writers:
                assert self.held(p) == self.held_through(
                    path, self.position(p), cache), op
            now = _bytes(path)
            if now == read:
                continue                  # nothing written: nothing to read
            fresh, read = self.load(path), now
            self.agree(fresh, self.second(path), path)
        for p in writers:
            self.catch_up(p, "anchored")
            assert self.held(p) == self.held(fresh)
        return n


def _search(tmp_path_factory, search, history,
            fixed=()) -> collections.Counter:
    """Run ``search`` over the histories in ``fixed``, then over the
    derandomized ``history``; the totals of what its histories did."""
    from hypothesis import HealthCheck, given, settings
    totals: collections.Counter = collections.Counter()
    for ops in fixed:
        for k, v in search.run(tmp_path_factory.mktemp("writers"),
                               ops).items():
            totals[k] += v

    @settings(max_examples=200, deadline=None, derandomize=True,
              database=None, suppress_health_check=[HealthCheck.too_slow])
    @given(ops=history)
    def run(ops):
        for k, v in search.run(tmp_path_factory.mktemp("writers"),
                               ops).items():
            totals[k] += v

    run()
    return totals


# ==========================================================================
# THE CAPABILITY LEDGER
#
# Grants minted by the root issuer and by others, for terms bounded and
# not; delegated by their holders and by others, narrower and not, from
# grants issued, revoked and expired since a writer last read; revoked by
# their issuers, by the root and by others.
# ==========================================================================
_C_IDS = ("c0", "c1", "c2", "c3", "c4", "c5", "c6")
_C_WHO = ("root", "ann", "bob")
_C_SCOPE = ("verification/stage10",)
_C_NARROW = ("verification/stage10/sub",)


def _c_cap(cid, subject, narrow=False):
    return issue(capability_id=cid, subject=subject,
                 action=Action.READ_PATHS, task_id="t1",
                 scope=_C_NARROW if narrow else _C_SCOPE, issued_seq=1)


def _c_history():
    from hypothesis import strategies as st
    w, cid, who = (st.integers(0, _WRITERS - 1), st.sampled_from(_C_IDS),
                   st.sampled_from(_C_WHO))
    held = st.integers(0, 4)
    kinds = {
        "issue": st.tuples(st.just("issue"), w, cid, who,
                           st.sampled_from(("root", "root", "root", "ann")),
                           st.sampled_from((None, None, None, 4))),
        "delegate": st.tuples(st.just("delegate"), w, cid, held, who,
                              st.sampled_from(("the holder",) * 5 + _C_WHO),
                              st.booleans()),
        "revoke": st.tuples(st.just("revoke"), w, held,
                            st.sampled_from(("the issuer", "the issuer")
                                            + _C_WHO)),
        **_house_ops(st),
    }
    return _ops(kinds, {"issue": 4, "delegate": 3, "revoke": 2,
                        "catch_up": 2, "reopen": 1}, "issue")


class _Capabilities(_Search):
    refusal = (CapabilityError,)

    def make(self, log):
        return CapabilityLedger(log)

    def setup(self, path) -> None:
        CapabilityLedger(EventLog(path)).load().anoint(actor="root")

    def held(self, p) -> dict:
        return {"issued": {k: v.body() for k, v in sorted(p._issued.items())},
                "issued_by": dict(sorted(p._issued_by.items())),
                "revoked": sorted(p._revoked),
                "root": (p._root, p._root_seq), "at": p._at_seq,
                "position": (p._folded_through, p._folded_hash)}

    def position(self, p) -> int:
        return p._folded_through

    def resolve(self, op, fresh):
        ids = sorted(fresh._issued)

        def pick(i):
            return ids[i % len(ids)] if ids else _C_IDS[i % len(_C_IDS)]

        kind = op[0]
        if kind == "delegate":
            _, w, cid, i, subject, actor, narrow = op
            parent = pick(i)
            if actor == "the holder":
                cap = fresh._issued.get(parent)
                actor = cap.subject if cap is not None else _C_WHO[0]
            return (kind, w, cid, parent, subject, actor, narrow)
        if kind == "revoke":
            _, w, i, actor = op
            cid = pick(i)
            if actor == "the issuer":
                actor = fresh._issued_by.get(cid, _C_WHO[0])
            return (kind, w, cid, actor)
        return op

    def write(self, p, op) -> None:
        kind = op[0]
        if kind == "issue":
            _, _, cid, subject, actor, lifetime = op
            p.issue(_c_cap(cid, subject), actor=actor,
                    lifetime_seqs=lifetime)
        elif kind == "delegate":
            _, _, cid, parent, subject, actor, narrow = op
            p.delegate(_c_cap(cid, subject, narrow), parent_id=parent,
                       actor=actor)
        elif kind == "revoke":
            _, _, cid, actor = op
            p.revoke(cid, actor=actor, reason="withdrawn")

    def catch_up(self, p, route) -> None:
        if route == "full":
            p._fold_anchor = None
        p._fold_new()

    def agree(self, fresh, sub, path) -> None:
        assert sub.root_issuer == fresh._root
        assert {k: v["revoked_seq"] is not None
                for k, v in sub.capabilities.items()} == {
            k: k in fresh._revoked for k in fresh._issued}
        assert not sub.anomalies, sub.anomalies


def test_grants_written_by_several_writers_agree_everywhere(
        tmp_path_factory):
    """Wave B. Every writer of the capability ledger holds what a load of
    the history up to its own position holds, after every operation; each
    write decides as a ledger just loaded decides; and the second reader
    agrees with a fresh load."""
    totals = _search(tmp_path_factory, _Capabilities(), _c_history())
    assert totals["writes"] and totals["behind"], totals


# ==========================================================================
# THE POLICY STORE
#
# Versions of two policies published by their owners and by others, open
# and closed, in force and expired; withdrawn by their owners and by
# others; decisions recorded under them, permitted and denied, and asked of
# a policy withdrawn, expired or never published.
# ==========================================================================
_P_IDS = ("p0", "p1")
_P_WHO = ("ops", "ops", "eve")


def _p_doc(pid, version, effect, expired):
    return document(policy_id=pid, version=version,
                    not_after=1.0 if expired else None,
                    rules=(rule(rule_id="the-rule", effect=effect,
                                actions=("act",), subjects=(ANY,),
                                roles=(ANY,), resources=(ANY,),
                                reason="as published"),))


def _p_history():
    from hypothesis import strategies as st
    w, pid, who = (st.integers(0, _WRITERS - 1), st.sampled_from(_P_IDS),
                   st.sampled_from(_P_WHO))
    kinds = {
        "publish": st.tuples(st.just("publish"), w, pid, who,
                             st.sampled_from((Effect.ALLOW, Effect.ALLOW,
                                              Effect.DENY)),
                             st.sampled_from((False, False, False, True))),
        "withdraw": st.tuples(st.just("withdraw"), w, pid, who),
        "decide": st.tuples(st.just("decide"), w, pid,
                            st.sampled_from(("alice", "bob"))),
        **_house_ops(st),
    }
    return _ops(kinds, {"publish": 4, "withdraw": 1, "decide": 3,
                        "catch_up": 2, "reopen": 1}, "publish")


class _Policies(_Search):
    refusal = (PolicyError,)

    def make(self, log):
        return PolicyStore(log)

    def held(self, p) -> dict:
        return {"published": {k: [(seq, d.digest()) for seq, d in v]
                              for k, v in sorted(p._published.items())},
                "owner": dict(sorted(p._owner.items())),
                "withdrawn": dict(sorted(p._withdrawn.items())),
                "latest_wall": p._latest_wall,
                "loaded_through": p._loaded_through,
                "position": (p._folded_through, p._folded_hash)}

    def position(self, p) -> int:
        return p._folded_through

    def resolve(self, op, fresh):
        if op[0] == "publish":
            _, w, pid, actor, effect, expired = op
            return ("publish", w, pid, len(fresh.versions(pid)) + 1, actor,
                    effect, expired)
        return op

    def write(self, p, op) -> None:
        kind = op[0]
        if kind == "publish":
            _, _, pid, version, actor, effect, expired = op
            p.publish(_p_doc(pid, version, effect, expired), actor=actor)
        elif kind == "withdraw":
            _, _, pid, actor = op
            p.withdraw(pid, actor=actor, reason="retired")
        elif kind == "decide":
            _, _, pid, subject = op
            p.decide_and_record(pid, PolicyRequest(
                action="act", subject=subject, role="WORKER",
                resource="res"), actor=subject)

    def catch_up(self, p, route) -> None:
        if route == "full":
            p._anchor = None
        p._fold_new()

    def agree(self, fresh, sub, path) -> None:
        assert {k: [v["version"] for v in vs]
                for k, vs in sub.policies.items()} == {
            k: [d.version for _, d in v] for k, v in fresh._published.items()}
        assert {k: o["withdrawn_seq"]
                for k, o in sub.policy_owners.items()} == {
            k: fresh._withdrawn[k][0] if k in fresh._withdrawn else None
            for k in fresh._owner}
        assert sorted(sub.decisions) == [
            ev.seq for ev in EventLog(path).read()
            if ev.action == "policy.decision"]
        assert not sub.anomalies, sub.anomalies


def test_policies_written_by_several_writers_agree_everywhere(
        tmp_path_factory):
    """Wave B. Every writer of the policy store holds what a load of the
    history up to its own position holds, after every operation; each
    write decides as a store just loaded decides; and the second reader
    agrees with a fresh load."""
    totals = _search(tmp_path_factory, _Policies(), _p_history())
    assert totals["writes"] and totals["behind"], totals


# ==========================================================================
# THE AGENT DIRECTORY
#
# Seeded with two humans and an agent. Principals registered and retired,
# by themselves, by those who admitted them, by humans and by agents;
# messages sent, again and rewritten; claims made, again and contradicted;
# escalations raised, answered, by humans and by others, and withdrawn.
# ==========================================================================
_A_NEW = ("a1", "a2", "h2")
_A_MSGS = ("m0", "m1", "m2")
_A_CLAIMS = ("k0", "k1")
_A_ESC = ("e0", "e1", "e2")
_A_BY = ("h0", "h1", "a0", "a1")


def _a_ident(iid, by):
    human = iid.startswith("h")
    return identity(agent_id=iid, instance_id=iid,
                    kind=PrincipalKind.HUMAN if human
                    else PrincipalKind.AGENT,
                    roles=(AgentRole.REVIEWER,) if human
                    else (AgentRole.PROPOSER, AgentRole.EXECUTOR),
                    registered_by=by)


def _a_history():
    from hypothesis import strategies as st
    w, held = st.integers(0, _WRITERS - 1), st.integers(0, 5)
    kinds = {
        "register": st.tuples(st.just("register"), w,
                              st.sampled_from(_A_NEW),
                              st.sampled_from(_A_BY)),
        "retire": st.tuples(st.just("retire"), w, held,
                            st.sampled_from(("itself", "its registrar")
                                            + _A_BY)),
        "send": st.tuples(st.just("send"), w, st.sampled_from(_A_MSGS),
                          held, st.sampled_from(("a0", "h0", "a1")),
                          st.integers(0, 1)),
        "claim": st.tuples(st.just("claim"), w, st.sampled_from(_A_CLAIMS),
                           held, st.integers(0, 1),
                           st.sampled_from((AgentRole.PROPOSER,
                                            AgentRole.VERIFIER))),
        "escalate": st.tuples(st.just("escalate"), w,
                              st.sampled_from(_A_ESC), held),
        "answer": st.tuples(st.just("answer"), w, held, held,
                            st.sampled_from(("yes", "no", "maybe"))),
        "withdraw": st.tuples(st.just("withdraw"), w, held,
                              st.sampled_from(("the asker", "h0"))),
        **_house_ops(st),
    }
    return _ops(kinds, {"register": 2, "retire": 1, "send": 2, "claim": 2,
                        "escalate": 2, "answer": 3, "withdraw": 1,
                        "catch_up": 2, "reopen": 1}, "register")


class _Agents(_Search):
    refusal = (AgentError,)

    def make(self, log):
        return AgentDirectory(log)

    def setup(self, path) -> None:
        d = AgentDirectory(EventLog(path)).load()
        d.register(_a_ident("h0", BOOTSTRAP), by=BOOTSTRAP)
        d.register(_a_ident("h1", "h0"), by="h0")
        d.register(_a_ident("a0", "h0"), by="h0")

    def held(self, p) -> dict:
        return {"identities": {k: v.to_record()
                               for k, v in sorted(p._identities.items())},
                "messages": {k: v.to_record()
                             for k, v in sorted(p._messages.items())},
                "claims": {k: v.to_record()
                           for k, v in sorted(p._claims.items())},
                "escalations": {k: v.to_record()
                                for k, v in sorted(p._escalations.items())},
                "at": p._at_seq,
                "position": (p._folded_through, p._folded_hash)}

    def position(self, p) -> int:
        return p._folded_through

    def resolve(self, op, fresh):
        ids = sorted(fresh._identities)
        escs = sorted(fresh._escalations)

        def who(i):
            return ids[i % len(ids)]

        def esc(i):
            return escs[i % len(escs)] if escs else _A_ESC[i % len(_A_ESC)]

        kind = op[0]
        if kind == "retire":
            _, w, i, by = op
            iid = who(i)
            if by == "itself":
                by = iid
            elif by == "its registrar":
                by = fresh._identities[iid].registered_by
            return (kind, w, iid, by)
        if kind == "send":
            _, w, mid, i, to, body = op
            return (kind, w, mid, who(i), to, body)
        if kind == "claim":
            _, w, cid, i, value, role = op
            return (kind, w, cid, who(i), value, role)
        if kind == "escalate":
            _, w, eid, i = op
            return (kind, w, eid, who(i))
        if kind == "answer":
            _, w, e, i, answer = op
            return (kind, w, esc(e), who(i), answer)
        if kind == "withdraw":
            _, w, e, by = op
            eid = esc(e)
            if by == "the asker":
                found = fresh._escalations.get(eid)
                by = found.raised_by if found is not None else "a0"
            return (kind, w, eid, by)
        return op

    def write(self, p, op) -> None:
        kind = op[0]
        if kind == "register":
            _, _, iid, by = op
            p.register(_a_ident(iid, by), by=by)
        elif kind == "retire":
            _, _, iid, by = op
            p.retire(iid, by=by, reason="stood down")
        elif kind == "send":
            _, _, mid, sender, to, body = op
            p.send(message_id=mid, sender_instance=sender,
                   recipient_agent=to, task_id="t1", subject="status",
                   body_digest=digest({"body": body}))
        elif kind == "claim":
            _, _, cid, by, value, role = op
            p.claim(claim_id=cid, task_id="t1", subject="answer",
                    value_digest=digest({"value": value}), by_instance=by,
                    role=role)
        elif kind == "escalate":
            _, _, eid, by = op
            p.escalate(escalation_id=eid, task_id="t1", question="which?",
                       raised_by=by, options=("yes", "no"))
        elif kind == "answer":
            _, _, eid, by, answer = op
            p.answer(escalation_id=eid, answered_by=by, answer=answer,
                     reason="decided")
        elif kind == "withdraw":
            _, _, eid, by = op
            p.withdraw(escalation_id=eid, by=by, reason="no longer needed")

    def catch_up(self, p, route) -> None:
        if route == "full":
            p._anchor = None
        p._fold_new()

    def agree(self, fresh, sub, path) -> None:
        assert {k: v["retired_seq"] is not None
                for k, v in sub.agents.items()} == {
            k: v.retired_seq >= 0 for k, v in fresh._identities.items()}
        assert {k: v["state"] for k, v in sub.escalations.items()} == {
            k: v.state.value for k, v in fresh._escalations.items()}
        assert not sub.anomalies, sub.anomalies


def test_the_directory_written_by_several_writers_agrees_everywhere(
        tmp_path_factory):
    """Wave B. Every writer of the agent directory holds what a load of the
    history up to its own position holds, after every operation; each write
    decides as a directory just loaded decides; and the second reader
    agrees with a fresh load."""
    totals = _search(tmp_path_factory, _Agents(), _a_history())
    assert totals["writes"] and totals["behind"], totals


# ==========================================================================
# THE SCHEDULER
#
# Each writer is a scheduler with a policy store of its own. Versions of the
# scheduling policy that open and close enqueueing, and that suspend
# dispatch (AUD-100), published by its owner through any writer's store;
# jobs enqueued under keys, again and for other work, and on one another;
# made ready, dispatched under leases, renewed, reported on by their
# holders and by others, reprioritised, cancelled and invalidated.
# ==========================================================================
_S_POLICY = "scheduler.default"
_S_JOBS = ("j0", "j1", "j2", "j3")
_S_WORK = ("aa" * 32, "bb" * 32)
_S_WORKERS = ("wa", "wb")


def _s_doc(version, form):
    """A version that is open, closed to enqueueing, or suspends
    dispatch."""
    rules = [rule(rule_id="deny-priority-escalation-by-workers",
                  effect=Effect.DENY, actions=("scheduler.raise_priority",),
                  subjects=(ANY,), roles=("WORKER", "SUBMITTER"),
                  resources=(ANY,), reason="only the scheduler")]
    if form == "closed":
        rules.append(rule(rule_id="closed", effect=Effect.DENY,
                          actions=("scheduler.enqueue",), subjects=(ANY,),
                          roles=(ANY,), resources=(ANY,), reason="closed"))
    elif form == "suspended":
        rules.append(rule(rule_id="suspended", effect=Effect.DENY,
                          actions=("scheduler.dispatch",), subjects=(ANY,),
                          roles=(ANY,), resources=(ANY,),
                          reason="dispatch suspended"))
    else:
        assert form == "open", form
    rules.append(rule(rule_id="allow-scheduling", effect=Effect.ALLOW,
                      actions=("scheduler.enqueue", "scheduler.dispatch",
                               "scheduler.raise_priority",
                               "scheduler.cancel"),
                      subjects=(ANY,), roles=(ANY,), resources=(ANY,)))
    return document(policy_id=_S_POLICY, version=version, rules=tuple(rules))


def _s_history():
    from hypothesis import strategies as st
    w, jid, held = (st.integers(0, _WRITERS - 1), st.sampled_from(_S_JOBS),
                    st.integers(0, 3))
    holder = st.sampled_from(_S_WORKERS + ("the holder",) * 3)
    kinds = {
        "publish": st.tuples(st.just("publish"), w,
                             st.sampled_from(("open", "open", "closed",
                                              "suspended"))),
        "enqueue": st.tuples(st.just("enqueue"), w, jid, st.integers(0, 1),
                             st.sampled_from((None, None, "k0", "k1")),
                             st.lists(held, max_size=1).map(tuple),
                             st.integers(0, 3)),
        "ready": st.tuples(st.just("ready"), w, held),
        "dispatch": st.tuples(st.just("dispatch"), w, held,
                              st.sampled_from(_S_WORKERS),
                              st.sampled_from((1, 6))),
        "renew": st.tuples(st.just("renew"), w, held, holder),
        "report": st.tuples(st.just("report"), w, held, holder,
                            st.sampled_from((None, None, "TRANSIENT",
                                             "PERMANENT"))),
        "priority": st.tuples(st.just("priority"), w, held,
                              st.integers(0, 3),
                              st.sampled_from(("SCHEDULER", "WORKER"))),
        "cancel": st.tuples(st.just("cancel"), w, held),
        "invalidate": st.tuples(st.just("invalidate"), w, held),
        "advance": st.tuples(st.just("advance"), w, held,
                             st.sampled_from(_S_WORKERS),
                             st.sampled_from((None, None, None, "renew",
                                              "TRANSIENT", "PERMANENT"))),
        **_house_ops(st),
    }
    return _ops(kinds, {"publish": 2, "enqueue": 3, "advance": 6,
                        "ready": 1, "dispatch": 1, "renew": 1, "report": 1,
                        "priority": 1, "cancel": 1, "invalidate": 1,
                        "catch_up": 2, "reopen": 1}, "enqueue")


class _Schedulers(_Search):
    refusal = (SchedulerError, PolicyError)
    #: A refusal the policy makes is recorded, by design (Scheduler._gate).
    refusal_records = ("policy.decision",)

    def make(self, log):
        return Scheduler(log, policy=PolicyStore(log).load(),
                         policy_id=_S_POLICY)

    def setup(self, path) -> None:
        PolicyStore(EventLog(path)).load().publish(_s_doc(1, "open"),
                                                   actor="ops")

    def held(self, p) -> dict:
        return {"jobs": {k: v.to_record() for k, v in sorted(p._jobs.items())},
                "keys": dict(sorted(p._keys.items())),
                "loaded_through": p._loaded_through,
                # The time a policy's expiry is judged at (AUD-099).
                "latest_wall": getattr(p, "_latest_wall", None),
                "position": (p._seen_through, p._seen_hash)}

    def position(self, p) -> int:
        return p._seen_through

    def resolve(self, op, fresh):
        """An index picks one of the jobs the log holds, and "the holder" is
        the lease's, so that moves are made and not only refused. A job
        enqueued is named afresh, and may name a job that exists already."""
        kind = op[0]
        ids = sorted(fresh._jobs)

        def pick(i):
            return ids[i % len(ids)] if ids else _S_JOBS[i % len(_S_JOBS)]

        def holder(jid, who):
            job = fresh._jobs.get(jid)
            if who != "the holder":
                return who
            return (job.lease_holder if job is not None and job.lease_holder
                    else _S_WORKERS[0])

        if kind == "publish":
            return ("publish", op[1],
                    len(fresh.policy.versions(_S_POLICY)) + 1, op[2])
        if kind == "advance":
            # The job's next step, as its state stands in the log: a move
            # made, so that a history reaches the lifecycle's far end.
            _, w, i, worker, failure = op
            jid = pick(i)
            job = fresh._jobs.get(jid)
            state = job.state if job is not None else None
            if state in (JobState.WAITING, JobState.RETRY_WAIT):
                return ("ready", w, jid)
            if state is JobState.READY:
                return ("dispatch", w, jid, worker, 6)
            if state is JobState.DISPATCHED:
                if failure == "renew":
                    return ("renew", w, jid, job.lease_holder)
                return ("report", w, jid, job.lease_holder, failure)
            if state is JobState.SUCCEEDED:
                return ("invalidate", w, jid)
            # No job yet, or one at its end: new work, under a name the
            # log does not hold where one is left.
            free = [j for j in _S_JOBS if j not in fresh._jobs]
            if state is None or free:
                return ("enqueue", w, jid if state is None else free[0], 0,
                        None, (), 3)
            return ("cancel", w, jid)
        if kind == "enqueue":
            _, w, jid, work, key, deps, priority = op
            return (kind, w, jid, work, key, tuple(pick(d) for d in deps),
                    priority)
        if kind in ("renew", "report"):
            jid = pick(op[2])
            return (kind, op[1], jid, holder(jid, op[3]), *op[4:])
        return (kind, op[1], pick(op[2]), *op[3:])

    def write(self, p, op) -> None:
        kind = op[0]
        if kind == "publish":
            _, _, version, form = op
            p.policy.publish(_s_doc(version, form), actor="ops")
        elif kind == "enqueue":
            _, _, jid, work, key, deps, priority = op
            p.enqueue(job_id=jid, work_digest=_S_WORK[work], submitter="sue",
                      idempotency_key=key, depends_on=deps,
                      priority=priority)
        elif kind == "ready":
            p.mark_ready(op[2])
        elif kind == "dispatch":
            _, _, jid, worker, lease = op
            p.dispatch(job_id=jid, worker=worker, lease_id=f"L-{jid}",
                       lease_seqs=lease)
        elif kind == "renew":
            _, _, jid, worker = op
            p.renew_lease(job_id=jid, worker=worker, lease_id=f"L-{jid}",
                          lease_seqs=6)
        elif kind == "report":
            _, _, jid, worker, failure = op
            p.report(job_id=jid, worker=worker,
                     failure=None if failure is None
                     else FailureClass(failure), detail="as it went")
        elif kind == "priority":
            _, _, jid, priority, role = op
            p.set_priority(job_id=jid, priority=priority,
                           actor="scheduler" if role == "SCHEDULER"
                           else "wa", role=role, reason="reordered")
        elif kind == "cancel":
            p.cancel(job_id=op[2], actor="sue", reason="no longer wanted")
        elif kind == "invalidate":
            p.invalidate(job_id=op[2], actor="system",
                         reason="its input changed")

    def catch_up(self, p, route) -> None:
        if route == "full":
            p._anchor = None
            p.catch_up(force=True)
        else:
            p.catch_up()

    def agree(self, fresh, sub, path) -> None:
        assert {k: v["state"] for k, v in sub.jobs.items()} == {
            k: v.state.value for k, v in fresh._jobs.items()}
        assert not sub.anomalies, sub.anomalies


def test_the_queue_written_by_several_writers_agrees_everywhere(
        tmp_path_factory):
    """Wave B. Every writer of the scheduler, each with a policy store of
    its own, holds what a load of the history up to its own position holds,
    after every operation; each write decides as a scheduler just loaded
    decides; and the second reader agrees with a fresh load."""
    totals = _search(tmp_path_factory, _Schedulers(), _s_history())
    assert totals["writes"] and totals["behind"], totals


# ==========================================================================
# THE AUTHORITY STORE
#
# Records proposed, again under an id taken and under a key spent, on
# dependencies recorded and not; walked along the state machine -- picked
# up, verified, promoted, made stale with a reason, re-verified, superseded,
# revoked, rejected -- and moved off it, by their proposers and by others,
# in the roles the edges name and in others, with the evidence an edge asks
# for, without it and with what the record already carries, at the
# revision read and at one passed since; made to depend on records created
# since a writer last read, and on themselves; promoted under a binding
# declared since, on policies published, withdrawn and expired since, by
# the authorities the binding names and by others.
# ==========================================================================
_R_IDS = ("r0", "r1", "r2")
_R_WHO = ("ann", "bob", "cat")
_R_STALE = "a dependency moved"
#: The walk a record takes when nothing refuses it: picked up, verified,
#: promoted, made stale, picked up again.
_R_ON = {State.PROPOSED: State.UNDER_REVIEW,
         State.UNDER_REVIEW: State.VERIFIED,
         State.VERIFIED: State.PROMOTED,
         State.PROMOTED: State.STALE,
         State.STALE: State.UNDER_REVIEW}


def _r_history():
    from hypothesis import strategies as st
    w, i = st.integers(0, _WRITERS - 1), st.integers(0, 11)
    key = st.sampled_from((None,) * 4 + ("k0", "k1"))
    policy = st.sampled_from(("p0", "p0", "p1", None))
    kinds = {
        "create": st.tuples(st.just("create"), w,
                            st.sampled_from(("new",) * 4 + ("taken",)), i,
                            st.sampled_from(_R_WHO),
                            st.sampled_from((None, None, "recorded",
                                             "unrecorded")), policy, key),
        "transition": st.tuples(
            st.just("transition"), w, i, i,
            st.sampled_from(("on",) * 8 + ("an edge",) * 2 + ("anywhere",)),
            st.sampled_from(("another",) * 9 + ("the proposer",)),
            st.sampled_from(("the edge's",) * 9 + ("any",)),
            st.sampled_from(("the edge's",) * 8 + ("carried", "none")),
            policy,
            st.sampled_from((None,) * 6 + ("read", "passed")), key),
        "depend": st.tuples(st.just("depend"), w, i, i,
                            st.sampled_from(("recorded",) * 3 + (
                                "itself", "unrecorded"))),
        "bind": st.tuples(st.just("bind"), w,
                          st.sampled_from((("ops",), ("ops", "eve"),
                                           ("ops", "eve")))),
        "publish": st.tuples(st.just("publish"), w,
                             st.sampled_from(_P_IDS),
                             st.sampled_from(_P_WHO),
                             st.sampled_from((False,) * 4 + (True,))),
        "withdraw": st.tuples(st.just("withdraw"), w,
                              st.sampled_from(_P_IDS),
                              st.sampled_from(_P_WHO)),
        **_house_ops(st),
    }
    weights = {"create": 3, "transition": 15, "depend": 1, "bind": 1,
               "publish": 3, "withdraw": 1, "catch_up": 2, "reopen": 1}
    pool = st.sampled_from([k for k, n in weights.items()
                            for _ in range(n)])
    # A binding and the policies it names, often in place before the first
    # promotion: drawn among the rest alone, no history promoted under one.
    opening = st.lists(st.sampled_from(("bind", "publish", "publish"))
                       .flatmap(kinds.__getitem__), max_size=3)
    rest = st.lists(pool.flatmap(kinds.__getitem__), min_size=5,
                    max_size=40)
    return st.tuples(kinds["create"], opening, rest).map(
        lambda t: [t[0], *t[1], *t[2]])


def _r_fixed() -> list:
    """A promotion under a binding, by a writer behind the log, for each
    answer the binding gives: permitted; refused for a policy withdrawn,
    expired, published by no authority the binding names, and never
    published. The random histories draw some of these and not others."""
    def made(w=0):
        return ("transition", w, 0, 0, "on", "another", "the edge's",
                "the edge's", "p0", None, None)
    opening = [("create", 0, "new", 0, "ann", None, "p0", None),
               ("bind", 0, ("ops",))]
    walk = [made(), made(), ("catch_up", 1, "anchored")]
    by_ops = [("publish", 0, "p0", "ops", False)]
    return [opening + by_ops + walk + [made(2)],
            opening + by_ops + walk + [("withdraw", 1, "p0", "ops"),
                                       made(2)],
            opening + by_ops + walk + [("publish", 1, "p0", "ops", True),
                                       made(2)],
            opening + [("publish", 0, "p0", "eve", False)] + walk
            + [made(2)],
            opening + walk + [made(2)]]


class _Authority(_Search):
    refusal = (StoreError, TransitionError, PolicyError)

    def make(self, log):
        return AuthorityStore(log)

    def held(self, p) -> dict:
        return {"records": {k: v.to_record()
                            for k, v in sorted(p._records.items())},
                "keys": dict(sorted(p._applied_keys.items())),
                # what each key completed (AUD-102); absent before it
                "requests": json.loads(json.dumps(
                    getattr(p, "_key_requests", None), sort_keys=True)),
                "binding": p._binding,
                "policies": json.loads(json.dumps(p._policy_view)),
                "latest_wall": p._latest_wall,
                "position": (p._loaded_through, p._loaded_hash)}

    def position(self, p) -> int:
        return p._loaded_through

    def resolve(self, op, fresh):
        ids = sorted(fresh._records)

        def pick(i):
            return ids[i % len(ids)] if ids else _R_IDS[i % len(_R_IDS)]

        kind = op[0]
        if kind == "create":
            _, w, which, i, proposer, dep, policy, key = op
            free = [x for x in _R_IDS if x not in fresh._records]
            rid = free[0] if which == "new" and free else pick(i)
            deps = (() if dep is None or (dep == "recorded" and not ids)
                    else ("r9",) if dep == "unrecorded" else (pick(i),))
            return (kind, w, rid, proposer, deps, policy, key)
        if kind == "transition":
            (_, w, i, j, dst, actor, role, evidence, policy, revision,
             key) = op
            rid = pick(i)
            cur = fresh._records.get(rid)
            state = State.PROPOSED if cur is None else cur.state
            carried = {} if cur is None else cur.evidence
            at = 1 if cur is None else cur.revision
            targets = sorted(allowed_targets(state), key=lambda s: s.value)
            dst = (_R_ON.get(state, State.PROMOTED) if dst == "on" else
                   targets[j % len(targets)] if dst == "an edge" and targets
                   else list(State)[j % len(State)])
            edge = find_edge(state, dst)
            proposer = _R_WHO[0] if cur is None else cur.proposer
            others = [x for x in _R_WHO if x != proposer]
            actor = (others[j % len(others)] if actor == "another"
                     else proposer)
            role = (list(Role)[j % len(Role)] if role == "any" or edge is None
                    else min(edge.roles, key=lambda r: r.value))
            cited = {}
            if evidence != "none" and edge is not None:
                # what the edge asks for, made at this revision -- a report
                # supplied now -- or, "carried", what the record already
                # carries where it carries it (I3)
                cited = {k: (policy or "p0") if k == "policy_id"
                         else carried[k] if evidence == "carried"
                         and k in carried
                         else digest({"record": rid, "key": k, "at": at})
                         for k in sorted(edge.requires_evidence)}
            revision = {None: None, "read": at, "passed": at - 1}[revision]
            return (kind, w, rid, dst, actor, role, cited, policy, revision,
                    key)
        if kind == "depend":
            _, w, i, j, dep = op
            rid = pick(i)
            deps = ((rid,) if dep == "itself" else
                    ("r9",) if dep == "unrecorded" else (pick(j),))
            return (kind, w, rid, deps)
        if kind == "publish":
            _, w, pid, owner, expired = op
            version = len(PolicyStore(fresh.log).load().versions(pid)) + 1
            return (kind, w, pid, version, owner, expired)
        return op

    def write(self, p, op) -> None:
        kind = op[0]
        if kind == "create":
            _, _, rid, proposer, deps, policy, key = op
            p.create(record_id=rid, kind="claim", proposer=proposer,
                     depends_on=deps, policy_id=policy,
                     idempotency_key=key)
        elif kind == "transition":
            _, _, rid, dst, actor, role, cited, policy, revision, key = op
            p.transition(record_id=rid, dst=dst, actor=actor, role=role,
                         evidence=cited, policy_id=policy,
                         expected_revision=revision,
                         stale_reason=(_R_STALE if dst is State.STALE
                                       else None),
                         idempotency_key=key)
        elif kind == "depend":
            _, _, rid, deps = op
            p.add_dependency(record_id=rid, depends_on=deps, actor="ann")
        elif kind == "bind":
            _, _, publishers = op
            p.bind_policies(actor="ops", publishers=publishers)
        elif kind == "publish":
            # another subsystem's writer on the same log: what a promotion
            # under the binding is judged on
            _, _, pid, version, owner, expired = op
            PolicyStore(p.log).load().publish(
                _p_doc(pid, version, Effect.ALLOW, expired), actor=owner)
        elif kind == "withdraw":
            _, _, pid, owner = op
            PolicyStore(p.log).load().withdraw(pid, actor=owner,
                                               reason="retired")

    def catch_up(self, p, route) -> None:
        if route == "full":
            p._anchor = None
            p.catch_up(force=True)
        else:
            p.catch_up()

    def second(self, path):
        return reconstruct(EventLog(path))

    def agree(self, fresh, rec, path) -> None:
        assert not rec.unauthorized, rec.unauthorized
        assert not rec.anomalies, rec.anomalies
        assert compare(fresh, rec) == ()
        # every field the two readers both hold, not only those compare()
        # diffs
        fields = ("record_id", "kind", "proposer", "state", "revision",
                  "evidence", "depends_on", "policy_id", "created_seq",
                  "updated_seq", "stale_reason", "admission")
        assert {k: {f: v[f] for f in fields}
                for k, v in sorted(rec.records.items())} == {
            k: {f: r.to_record()[f] for f in fields}
            for k, r in sorted(fresh._records.items())}
        assert sorted(rec.canonical_ids()) == sorted(fresh.canonical())


def test_records_written_by_several_writers_agree_everywhere(
        tmp_path_factory):
    """Wave B. Every writer of the authority store holds what a load of
    the history up to its own position holds, after every operation; each
    write decides as a store just loaded decides, with another subsystem's
    writer publishing and withdrawing the policies its promotions stand
    on; and the second reader agrees with a fresh load, field by field."""
    totals = _search(tmp_path_factory, _Authority(), _r_history(),
                     fixed=_r_fixed())
    assert totals["writes"] and totals["behind"], totals
