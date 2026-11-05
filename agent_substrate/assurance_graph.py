"""The causal assurance graph: what rests on what, as the history says (6.1).

A DISPOSABLE PROJECTION

Built from a verified read of the log and nothing else, rebuilt on every
call, never written anywhere. It decides nothing: the authority store, the
research ledger and memory decide, and they carry a withdrawal through their
own cascades. The graph is how an auditor asks a question across all of them
at once -- which observations a conclusion depended on, what a model was
shown, who authorized it, what goes stale if one premise is withdrawn -- and
how a test checks that the cascades did what the graph says they should.

EDGES ARE RECORDED, NEVER INFERRED

Every edge comes from a field a writer recorded: a record's cited evidence
and dependencies, an observation's source record, a verdict's basis, a
memory's sources, a context's items, an inference's context, a proposal's
citations and the task it asked for. Two things that merely occur together,
or near each other in the log, are not connected. An edge runs from a
premise to what rests on it.

``DIRECT_EVIDENCE``          bytes or a record a thing was read from
``CLAIMED_SUPPORT``          a citation, or the proposal a claim came from:
                             what somebody SAID supports it
``SCIENTIFIC_VERIFICATION``  the report and the verifier a verdict rests on
``LOGICAL_DERIVATION``       a computed verdict's basis; a conclusion's
                             hypothesis; a hypothesis's premises
``HISTORICAL_DEPENDENCY``    what a record declared it depends on; the task
                             that produced some bytes
``CONTEXT_INCLUSION``        what a model or a task was shown
``AUTHORIZATION``            the policy and the actor a decision was taken
                             under
``SUMMARY_DERIVATION``       a memory derived from bytes or summarizing
                             another memory
``SUPERSESSION``             what withdrew or replaced it
``MODEL_OUTPUT``             what a model said, given what it was shown
``REQUEST``                  the proposal that asked for a task

The last two are not in the directive's list, and are kept apart from it on
purpose: folded into evidence or derivation, a model's answer or an agent's
request would read as support.

WHAT A WITHDRAWAL REACHES (:func:`withdrawal`)

Currency travels only along DIRECT_EVIDENCE, SCIENTIFIC_VERIFICATION,
LOGICAL_DERIVATION, HISTORICAL_DEPENDENCY and SUMMARY_DERIVATION. Bytes keep
their currency while ANY task that produced them stands (a second governed
origin keeps a result standing); everything else loses it when ANY premise
does. A context or a proposal that included withdrawn content is EXPOSED,
not stale: the model saw it, and the record of what it saw stays true.

WHAT THIS IS NOT

Complete. Only the edges listed above are built; an action this module does
not read contributes nothing, and :attr:`Graph.unread` counts them, so a
reader can see how much of the history the graph covers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Edge(str, Enum):
    DIRECT_EVIDENCE = "DIRECT_EVIDENCE"
    CLAIMED_SUPPORT = "CLAIMED_SUPPORT"
    SCIENTIFIC_VERIFICATION = "SCIENTIFIC_VERIFICATION"
    LOGICAL_DERIVATION = "LOGICAL_DERIVATION"
    HISTORICAL_DEPENDENCY = "HISTORICAL_DEPENDENCY"
    CONTEXT_INCLUSION = "CONTEXT_INCLUSION"
    AUTHORIZATION = "AUTHORIZATION"
    SUMMARY_DERIVATION = "SUMMARY_DERIVATION"
    SUPERSESSION = "SUPERSESSION"
    MODEL_OUTPUT = "MODEL_OUTPUT"
    REQUEST = "REQUEST"


#: The edges currency travels along.
CARRIES = frozenset({Edge.DIRECT_EVIDENCE, Edge.SCIENTIFIC_VERIFICATION,
                     Edge.LOGICAL_DERIVATION, Edge.HISTORICAL_DEPENDENCY,
                     Edge.SUMMARY_DERIVATION})
#: The edges along which withdrawn content is exposed, not made stale.
EXPOSES = frozenset({Edge.CONTEXT_INCLUSION, Edge.MODEL_OUTPUT,
                     Edge.CLAIMED_SUPPORT, Edge.REQUEST})

#: State each stateful kind is in when it no longer stands.
NOT_STANDING = {
    "record": {"STALE", "INVALIDATED", "REVOKED", "SUPERSEDED", "REJECTED"},
    "task": {"INVALIDATED", "REJECTED", "FAILED", "CANCELLED"},
    "observation": {"STALE", "EXPIRED"},
    "hypothesis": {"STALE", "RETRACTED", "SUPERSEDED"},
    "objective": {"STALE"},
    "memory": {"STALE", "SUPERSEDED", "RETRACTED"},
}
_RECORD_EVIDENCE_EDGE = {"verification_report": Edge.SCIENTIFIC_VERIFICATION,
                         "invalidated_by": Edge.SUPERSESSION}


@dataclass(frozen=True)
class Link:
    src: str
    dst: str
    kind: Edge
    seq: int
    note: str = ""


@dataclass
class Graph:
    """Nodes are ``kind:id`` strings; ``state`` holds what the history last
    recorded for the stateful ones."""

    links: list = field(default_factory=list)
    state: dict = field(default_factory=dict)
    #: node -> facts recorded about it: who proposed, under which policy
    facts: dict = field(default_factory=dict)
    #: actions this projection does not read, and how often they occurred
    unread: dict = field(default_factory=dict)
    head_seq: int = -1

    def add(self, src: str, dst: str, kind: Edge, seq: int,
            note: str = "") -> None:
        if src and dst and not src.endswith(":") and not dst.endswith(":"):
            self.links.append(Link(src, dst, kind, seq, note))

    def into(self, node: str) -> list:
        return [x for x in self.links if x.dst == node]

    def out_of(self, node: str) -> list:
        return [x for x in self.links if x.src == node]

    def nodes(self) -> set:
        return {x.src for x in self.links} | {x.dst for x in self.links} | \
            set(self.state)


def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _b(sha) -> str:
    return f"bytes:{sha}" if isinstance(sha, str) and sha else ""


def build(events) -> Graph:
    """The graph of a VERIFIED read of a log."""
    g = Graph()
    proposals: set = set()
    for ev in events:
        g.head_seq = ev.seq
        p = ev.payload if isinstance(ev.payload, dict) else {}
        a, s = ev.action, ev.seq
        if a == "record.create":
            rid = f"record:{p.get('record_id')}"
            g.state[rid] = "PROPOSED"
            g.facts.setdefault(rid, {})["proposer"] = ev.actor
            for k, sha in sorted((p.get("evidence") or {}).items()):
                g.add(_b(sha), rid, _RECORD_EVIDENCE_EDGE.get(
                    k, Edge.DIRECT_EVIDENCE), s, k)
            for dep in p.get("depends_on") or ():
                g.add(f"record:{dep}", rid, Edge.HISTORICAL_DEPENDENCY, s)
        elif a == "record.depend":
            rid = f"record:{p.get('record_id')}"
            for dep in p.get("depends_on") or ([p.get("depends_on_id")]
                                               if p.get("depends_on_id")
                                               else []):
                g.add(f"record:{dep}", rid, Edge.HISTORICAL_DEPENDENCY, s)
        elif a == "record.transition":
            rid = f"record:{p.get('record_id')}"
            dst = p.get("dst")
            g.state[rid] = dst
            for k, sha in sorted((p.get("evidence") or {}).items()):
                if k == "policy_id":
                    continue
                g.add(_b(sha), rid, _RECORD_EVIDENCE_EDGE.get(
                    k, Edge.DIRECT_EVIDENCE), s, k)
            if dst == "VERIFIED":
                g.add(f"actor:{ev.actor}", rid, Edge.SCIENTIFIC_VERIFICATION,
                      s, "verified by")
            if dst == "PROMOTED":
                g.add(f"actor:{ev.actor}", rid, Edge.AUTHORIZATION, s,
                      "promoted by")
                if p.get("policy_id"):
                    g.add(f"policy:{p['policy_id']}", rid, Edge.AUTHORIZATION,
                          s, "in force")
            if p.get("admission_policy"):
                g.facts.setdefault(rid, {})["admission_policy"] = \
                    p["admission_policy"]
        elif a == "task.create":
            g.state[f"task:{p.get('task_id')}"] = "SUBMITTED"
        elif a == "task.transition":
            g.state[f"task:{p.get('task_id')}"] = p.get("dst")
        elif a == "task.evidence":
            for sha in sorted((p.get("artifacts") or {}).values()):
                g.add(f"task:{p.get('task_id')}", _b(sha),
                      Edge.HISTORICAL_DEPENDENCY, s, "produced")
        elif a == "idempotency.bind":
            if p.get("key") in proposals:
                g.add(f"proposal:{p['key']}", f"task:{p.get('task_id')}",
                      Edge.REQUEST, s)
        elif a == "policy.decision":
            d = _dict(p.get("decision"))
            if d.get("policy_id") and d.get("allowed"):
                g.add(f"policy:{d['policy_id']}", f"task:{ev.target}",
                      Edge.AUTHORIZATION, s, "decision")
        elif a == "context.build":
            m = p.get("manifest") or {}
            ctx = f"context:{p.get('manifest_digest')}"
            for item in m.get("items") or ():
                src = item.get("source") or ""
                node = (f"memory:{src[len('memory:'):]}"
                        if src.startswith("memory:") else _b(src)
                        if len(src) == 64 else "")
                g.add(node, ctx, Edge.CONTEXT_INCLUSION, s,
                      item.get("item_id", ""))
            if m.get("task_id"):
                g.add(ctx, f"task:{m['task_id']}", Edge.CONTEXT_INCLUSION, s,
                      "shown to the task's run")
            if m.get("policy_identity"):
                g.facts.setdefault(ctx, {})["policy"] = m["policy_identity"]
        elif a == "inference.reserve":
            req = p.get("request") or {}
            g.add(f"context:{req.get('context_manifest_digest')}",
                  f"inference:{p.get('request_id')}", Edge.CONTEXT_INCLUSION,
                  s, "what the model was shown")
        elif a == "proposal.receive":
            pid = f"proposal:{ev.target}"
            proposals.add(ev.target)
            env = p.get("envelope") or {}
            g.facts.setdefault(pid, {})["agent"] = env.get("agent_id")
            inf = _dict(env.get("inference"))
            if inf.get("request_id"):
                g.add(f"inference:{inf['request_id']}", pid,
                      Edge.MODEL_OUTPUT, s)
            for c in (env.get("citations") or ()):
                if isinstance(c, dict):
                    g.add(_b(c.get("source_sha256")), pid,
                          Edge.CLAIMED_SUPPORT, s, c.get("path", ""))
        elif a == "research.observation":
            b = p.get("observation") or {}
            oid = f"observation:{b.get('observation_id')}"
            g.state[oid] = "CURRENT"
            g.add(f"record:{b.get('record_id')}", oid, Edge.DIRECT_EVIDENCE,
                  s)
            g.add(_b(b.get("result_bundle")), oid, Edge.DIRECT_EVIDENCE, s)
        elif a == "research.hypothesis":
            b = p.get("hypothesis") or {}
            hid = f"hypothesis:{b.get('hypothesis_id')}"
            g.state[hid] = "OPEN"
            g.add(f"proposal:{b.get('proposal_id')}", hid,
                  Edge.CLAIMED_SUPPORT, s, "proposed")
            for dep in b.get("depends_on") or ():
                g.add(_research_node(g, dep), hid, Edge.LOGICAL_DERIVATION, s)
        elif a == "research.objective":
            b = p.get("objective") or {}
            g.state[f"objective:{b.get('objective_id')}"] = "OPEN"
        elif a == "research.status":
            node = _research_node(g, p.get("target_id"))
            if node:
                g.state[node] = p.get("status")
                for x in p.get("basis") or ():
                    g.add(_research_node(g, x), node,
                          Edge.LOGICAL_DERIVATION, s, "basis")
        elif a == "memory.write":
            e = _dict(p.get("entry")) or p
            mid = f"memory:{e.get('memory_id')}"
            g.state[mid] = "ACTIVE"
            for sha in e.get("derived_from") or ():
                g.add(_b(sha), mid, Edge.SUMMARY_DERIVATION, s)
            for m in e.get("summarizes") or ():
                g.add(f"memory:{m}", mid, Edge.SUMMARY_DERIVATION, s)
        elif a == "memory.status":
            mid = f"memory:{p.get('memory_id')}"
            g.state[mid] = p.get("status")
            if p.get("superseded_by"):
                g.add(f"memory:{p['superseded_by']}", mid,
                      Edge.SUPERSESSION, s)
        else:
            g.unread[a] = g.unread.get(a, 0) + 1
    return g


def _research_node(g: Graph, rid) -> str:
    for kind in ("observation", "hypothesis", "objective"):
        if f"{kind}:{rid}" in g.state:
            return f"{kind}:{rid}"
    return ""


def standing(g: Graph, node: str) -> bool:
    kind = node.split(":", 1)[0]
    return g.state.get(node) not in NOT_STANDING.get(kind, ())


def withdrawal(g: Graph, roots) -> dict:
    """What withdrawing ``roots`` reaches: ``{"stale": ..., "exposed": ...}``.

    ``stale``: every node whose currency rests on a root, through the edges
    in :data:`CARRIES` -- bytes only once every task that produced them is
    withdrawn. ``exposed``: what was shown, said or asked on the strength of
    something stale, which keeps saying what it said."""
    lost = set(roots)
    changed = True
    while changed:
        changed = False
        for node in g.nodes() - lost:
            premises = [x for x in g.into(node) if x.kind in CARRIES]
            if not premises:
                continue
            if node.startswith("bytes:"):
                producers = [x for x in premises
                             if x.kind is Edge.HISTORICAL_DEPENDENCY]
                gone = bool(producers) and all(
                    x.src in lost or not standing(g, x.src)
                    for x in producers)
            else:
                gone = any(x.src in lost for x in premises)
            if gone:
                lost.add(node)
                changed = True
    exposed = {x.dst for x in g.links
               if x.kind in EXPOSES and x.src in lost} - lost
    return {"stale": lost - set(roots), "exposed": exposed}


def upstream(g: Graph, node: str) -> list:
    """Every link on a path INTO ``node``: what it rests on, transitively."""
    out, seen, frontier = [], {node}, [node]
    while frontier:
        for x in g.into(frontier.pop()):
            out.append(x)
            if x.src not in seen:
                seen.add(x.src)
                frontier.append(x.src)
    return out


def downstream(g: Graph, node: str) -> list:
    out, seen, frontier = [], {node}, [node]
    while frontier:
        for x in g.out_of(frontier.pop()):
            out.append(x)
            if x.dst not in seen:
                seen.add(x.dst)
                frontier.append(x.dst)
    return out


def explain(g: Graph, node: str, *, held=None) -> dict:
    """The auditor's questions about ``node``, answered from the graph.

    ``held``, when given, is ``held(sha) -> bool``: whether the bytes are in
    the evidence store now. Every answer is a list of nodes or facts the
    history recorded; nothing is inferred from co-occurrence."""
    up = upstream(g, node)
    nodes = {x.src for x in up}

    def of(kind):
        return sorted(n for n in nodes if n.startswith(kind + ":"))

    def by(kinds):
        return sorted({f"{x.src} ({x.note})" if x.note else x.src
                       for x in up if x.kind in kinds})

    shown = sorted({x.src for x in up
                    if x.kind is Edge.CONTEXT_INCLUSION
                    and x.dst.startswith(("context:", "inference:"))})
    gone = withdrawal(g, [node])
    return {
        "node": node, "state": g.state.get(node),
        "observations": of("observation"),
        "source_bytes": {b: (None if held is None else bool(held(b[6:])))
                         for b in of("bytes")},
        "model_was_shown": shown,
        "assumptions": sorted({f"{n}: {k}={v}" for n in nodes | {node}
                               for k, v in g.facts.get(n, {}).items()
                               if k in ("admission_policy", "policy")}),
        "policies_in_force": of("policy"),
        "verified_by": by({Edge.SCIENTIFIC_VERIFICATION}),
        "authorized_by": by({Edge.AUTHORIZATION}),
        "dependents": sorted({x.dst for x in downstream(g, node)}),
        "stale_if_withdrawn": sorted(gone["stale"]),
        "exposed_if_withdrawn": sorted(gone["exposed"]),
    }
