"""Independent reconstruction of authority state from the event log alone.

This module exists to answer one question without trusting the running
system: *given only the log, what is canonical?*

It is written as a SECOND implementation on purpose. It does not import
:class:`~qta_agent.store.AuthorityStore` and does not share its reducer. If
both agree, that is differential evidence -- two implementations reading the
same evidence reached the same verdict. Reusing the store's reducer here would
make the comparison circular and worthless, which is why the duplication is
deliberate rather than an oversight.

Where the store folds events into dataclasses through ``dataclasses.replace``,
this walks the log with plain dictionaries and re-derives each field from
scratch. The two disagree loudly if either has a bug.

It trusts NOTHING except the log's bytes:
  * not the live process,
  * not any snapshot,
  * not the store's projection,
  * not conversation history or a model's recollection.

Every transition is re-authorized against the state machine during replay. An
event that the machine would refuse today is reported rather than applied --
which is how a log written by a compromised or older writer, or under a since
changed policy, becomes visible instead of being silently absorbed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import actions
from .authority import (
    INITIAL,
    Role,
    State,
    TransitionError,
    TransitionRequest,
    check,
)
from .events import EventLog


@dataclass
class Reconstruction:
    """The verdict, plus everything needed to argue with it."""
    #: record_id -> plain dict of reconstructed fields
    records: dict = field(default_factory=dict)
    #: Transitions the state machine would refuse if replayed today.
    unauthorized: list = field(default_factory=list)
    #: Structural problems in the log that did not stop replay.
    anomalies: list = field(default_factory=list)
    events_replayed: int = 0
    #: Events belonging to another subsystem on the same log. Counted so a
    #: reader can tell "this reconstruction saw a mixed log and ignored the
    #: parts that are not authority records" from "this log had 3 events".
    foreign_events: int = 0
    head_seq: int = -1
    head_hash: str = ""

    def canonical_ids(self) -> tuple:
        return tuple(sorted(
            rid for rid, r in self.records.items()
            if r["state"] == State.PROMOTED.value))

    def states(self) -> dict:
        return {rid: r["state"] for rid, r in self.records.items()}


#: Actions this function interprets. Everything else this package writes is
#: another subsystem's and is counted rather than treated as damage.
_AUTHORITY_ACTIONS = frozenset({"record.create", "record.transition",
                                "record.depend"})


def reconstruct(log: EventLog, *, reauthorize: bool = True) -> Reconstruction:
    """Rebuild authority state from a verified log.

    Verification comes first and is fatal: reconstructing from a chain that
    does not verify would produce a confident answer from untrusted bytes.
    """
    report = log.verify()
    report.raise_if_bad()

    out = Reconstruction(head_seq=report.head_seq, head_hash=report.head_hash)
    # Deliberately dict-of-dicts rather than the store's dataclasses.
    recs: dict = out.records

    for ev in log.read():
        out.events_replayed += 1
        p = ev.payload
        action = ev.action
        rid = p.get("record_id")

        if action == "record.create":
            if rid in recs:
                out.anomalies.append(
                    f"seq {ev.seq}: duplicate create for {rid!r}")
                continue
            recs[rid] = {
                "record_id": rid,
                "kind": p.get("kind"),
                "proposer": p.get("proposer"),
                "state": p.get("state", INITIAL.value),
                "revision": 1,
                "evidence": dict(p.get("evidence", {})),
                "depends_on": list(p.get("depends_on", [])),
                "policy_id": p.get("policy_id"),
                "created_seq": ev.seq,
                "updated_seq": ev.seq,
                "stale_reason": None,
                "history": [(ev.seq, p.get("state", INITIAL.value))],
            }

        elif action == "record.transition":
            cur = recs.get(rid)
            if cur is None:
                out.anomalies.append(
                    f"seq {ev.seq}: transition for unknown record {rid!r}")
                continue
            src_claimed = p.get("src")
            if cur["state"] != src_claimed:
                out.anomalies.append(
                    f"seq {ev.seq}: {rid} claims src {src_claimed} but replay "
                    f"has it in {cur['state']}")
            if reauthorize:
                try:
                    check(TransitionRequest(
                        record_id=rid,
                        src=State(cur["state"]),
                        dst=State(p["dst"]),
                        actor=ev.actor,
                        role=Role(p["role"]),
                        evidence={**cur["evidence"], **p.get("evidence", {})},
                        proposer=cur["proposer"],
                        policy_id=p.get("policy_id") or cur["policy_id"]))
                except (TransitionError, ValueError) as exc:
                    out.unauthorized.append(
                        f"seq {ev.seq}: {rid} {cur['state']} -> "
                        f"{p.get('dst')} "
                        f"would be refused today: {exc}")
                    # Do NOT apply. An unauthorized transition must not become
                    # canonical merely because it is present in the log.
                    continue
            cur["state"] = p["dst"]
            cur["revision"] += 1
            cur["evidence"].update(p.get("evidence", {}))
            cur["updated_seq"] = ev.seq
            if p.get("stale_reason") is not None:
                cur["stale_reason"] = p["stale_reason"]
            if p.get("policy_id") is not None:
                cur["policy_id"] = p["policy_id"]
            cur["history"].append((ev.seq, p["dst"]))

        elif action == "record.depend":
            cur = recs.get(rid)
            if cur is None:
                out.anomalies.append(
                    f"seq {ev.seq}: dependency for unknown record {rid!r}")
                continue
            for dep in p.get("depends_on", []):
                if dep not in cur["depends_on"]:
                    cur["depends_on"].append(dep)
            cur["revision"] += 1
            cur["updated_seq"] = ev.seq

        elif actions.classify(action, mine=_AUTHORITY_ACTIONS) \
                == actions.FOREIGN:
            # Another subsystem's event. Counted, not applied, and NOT an
            # anomaly: the authority records this function rebuilds are not
            # affected by it. What IS an anomaly is the case below.
            out.foreign_events += 1
        else:
            out.anomalies.append(
                f"seq {ev.seq}: unknown action {action!r}; not applied. "
                "Nothing in this package writes it, so this reconstruction "
                "is missing whatever it recorded.")
    return out


@dataclass(frozen=True)
class Divergence:
    """A disagreement between the live projection and the reconstruction."""
    record_id: str
    field_name: str
    live: object
    reconstructed: object

    def __str__(self) -> str:
        return (f"{self.record_id}.{self.field_name}: live={self.live!r} "
                f"reconstructed={self.reconstructed!r}")


def compare(store, recon: Reconstruction) -> tuple:
    """Diff a live store against an independent reconstruction.

    Returns a tuple of :class:`Divergence`. Empty means the two implementations
    agree -- the only outcome that should ever occur in a healthy system.
    """
    diffs: list = []
    live = store.all_records()
    for rid in sorted(set(live) | set(recon.records)):
        if rid not in live:
            diffs.append(Divergence(rid, "<presence>", "ABSENT", "present"))
            continue
        if rid not in recon.records:
            diffs.append(Divergence(rid, "<presence>", "present", "ABSENT"))
            continue
        lv, rc = live[rid], recon.records[rid]
        for name, lval, rval in (
            ("state", lv.state.value, rc["state"]),
            ("kind", lv.kind, rc["kind"]),
            ("proposer", lv.proposer, rc["proposer"]),
            ("revision", lv.revision, rc["revision"]),
            ("evidence", dict(lv.evidence), rc["evidence"]),
            ("depends_on", list(lv.depends_on), rc["depends_on"]),
            ("policy_id", lv.policy_id, rc["policy_id"]),
        ):
            if lval != rval:
                diffs.append(Divergence(rid, name, lval, rval))
    return tuple(diffs)
