"""A read-only W3C PROV export of the verified history (Assurance J).

WHAT IT IS

A PROV-JSON document (the W3C member submission's serialization of the PROV
data model) of one log, versioned ``qt-prov/1``. It maps the history into:

* ACTIVITIES -- one per verified event, at its recorded wall time, with its
  action, position and record hash, associated with the event's actor;
* AGENTS -- every actor that wrote an event or verified or promoted a
  record;
* ENTITIES -- what the causal assurance graph (:mod:`assurance_graph`)
  names: records, observations, hypotheses, objectives, memories, contexts,
  proposals, model calls, tasks, policies and evidence bytes by digest,
  with the state the history last recorded for each;
* RELATIONS -- the graph's recorded edges, each tied to the activity that
  recorded it. An edge along which currency travels is ``wasDerivedFrom``.
  A model's output, an agent's request, a claimed citation and a context's
  contents are only ``wasInfluencedBy``, never a derivation: PROV would
  otherwise say a proposal was derived from what it cited. Verification and
  promotion are ``wasAttributedTo`` the agent who did them. A context holds
  its items as ``hadMember``; a superseded memory ``wasInvalidatedBy`` the
  activity that superseded it.

WHAT IT IS NOT

Authority. It reads a VERIFIED history and writes nothing; the canonical
event log stays the only source of authority, and a PROV document claiming
something the log does not say proves nothing. Nor is it a complete
rendering: what PROV cannot hold is listed in the document itself, under
``qt:notRepresented`` -- the hash chain's linkage (carried as attributes,
not as PROV), every payload field the graph does not read, and the actions
the graph does not read at all, with their counts.

ROUND TRIP

:func:`edges` and :func:`activities` read a document back into the graph's
links and the events' (action, actor, time); a document exported from a
history reads back to exactly that history's graph and events, for the
fields this format represents.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote, unquote

from . import assurance_graph as AG

SCHEMA = "qt-prov/1"
#: A name for this export's terms, not a resolvable address.
NAMESPACE = "urn:qt:prov:1#"

#: The graph's edges along which currency travels, when the source is an
#: entity: these are derivations.
_DERIVED = AG.CARRIES
#: The edges whose source is the agent that acted.
_ATTRIBUTED = frozenset({AG.Edge.SCIENTIFIC_VERIFICATION,
                         AG.Edge.AUTHORIZATION})


class ProvExportError(ValueError):
    """The history does not verify, or a document is not one this format
    reads."""


def _qn(node: str) -> str:
    """The graph's ``kind:id`` -> ``qt:kind/id``, the id percent-encoded."""
    kind, _, ident = node.partition(":")
    return f"qt:{kind}/{quote(ident, safe='-._~')}"


def _node(qn: str) -> str:
    kind, _, ident = qn[len("qt:"):].partition("/")
    return f"{kind}:{unquote(ident)}"


def _event(seq: int) -> str:
    return f"qt:event/{seq}"


def _time(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def export_log(log, *, origin: str = "") -> dict:
    """The document of ``log``'s history, read verified. A history that
    does not verify is not exported: a PROV rendering of it would read as
    an account of what happened."""
    report, events = log.read_verified()
    if not report.ok:
        raise ProvExportError(f"the history does not verify: "
                              f"{report.problems[:3]}")
    return export(events, origin=origin)


def export(events, *, origin: str = "") -> dict:
    """The PROV-JSON document of ``events``, a VERIFIED read of a log (as
    ``EventLog.read_verified`` returns them)."""
    events = list(events)
    g = AG.build(events)
    doc: dict = {"prefix": {"qt": NAMESPACE},
                 "entity": {}, "activity": {}, "agent": {},
                 "wasAssociatedWith": {}, "wasDerivedFrom": {},
                 "wasInfluencedBy": {}, "wasAttributedTo": {},
                 "hadMember": {}, "wasInvalidatedBy": {}}

    def agent(name: str) -> str:
        qn = _qn(f"actor:{name}")
        doc["agent"].setdefault(qn, {"prov:type": "qt:Actor"})
        return qn

    def entity(node: str) -> str:
        qn = _qn(node)
        if qn not in doc["entity"]:
            attrs = {"prov:type": f"qt:{qn[3:].split('/', 1)[0]}"}
            if node in g.state:
                attrs["qt:state"] = g.state[node]
            for k, v in sorted((g.facts.get(node) or {}).items()):
                attrs[f"qt:{k}"] = v
            doc["entity"][qn] = attrs
        return qn

    for ev in events:
        doc["activity"][_event(ev.seq)] = {
            "prov:startTime": _time(ev.wall_time),
            "prov:endTime": _time(ev.wall_time),
            "qt:action": ev.action, "qt:seq": ev.seq, "qt:hash": ev.hash,
            "qt:prevHash": ev.prev_hash}
        doc["wasAssociatedWith"][f"_:assoc/{ev.seq}"] = {
            "prov:activity": _event(ev.seq), "prov:agent": agent(ev.actor)}

    for n, link in enumerate(sorted(g.links, key=lambda x: (
            x.seq, x.src, x.dst, x.kind.value, x.note))):
        qid = f"_:rel/{n}"
        common = {"qt:edge": link.kind.value, "qt:activity": _event(link.seq)}
        if link.note:
            common["qt:note"] = link.note
        if link.src.startswith("actor:") and link.kind in _ATTRIBUTED:
            doc["wasAttributedTo"][qid] = {
                "prov:entity": entity(link.dst),
                "prov:agent": agent(link.src[len("actor:"):]), **common}
        elif link.kind in _DERIVED:
            doc["wasDerivedFrom"][qid] = {
                "prov:generatedEntity": entity(link.dst),
                "prov:usedEntity": entity(link.src),
                "prov:activity": _event(link.seq), **common}
        elif link.kind is AG.Edge.CONTEXT_INCLUSION and \
                link.dst.startswith("context:"):
            doc["hadMember"][qid] = {"prov:collection": entity(link.dst),
                                     "prov:entity": entity(link.src),
                                     **common}
        else:
            doc["wasInfluencedBy"][qid] = {
                "prov:influencee": entity(link.dst),
                "prov:influencer": entity(link.src), **common}
            if link.kind is AG.Edge.SUPERSESSION:
                doc["wasInvalidatedBy"][f"_:inv/{n}"] = {
                    "prov:entity": entity(link.dst),
                    "prov:activity": _event(link.seq)}

    head = events[-1] if events else None
    doc["qt:history"] = {
        "schema": SCHEMA, "origin": origin,
        "head_seq": head.seq if head else -1,
        "head_hash": head.hash if head else None, "events": len(events)}
    doc["qt:notRepresented"] = {
        "chain": "each record's hash and the one before it are carried as "
                 "activity attributes; PROV has no notion of a hash chain, "
                 "so this document does not verify the history -- "
                 "EventLog.read_verified does",
        "payloads": "only the fields the causal assurance graph reads are "
                    "represented; the rest of every payload is not",
        "unread_actions": dict(sorted(g.unread.items()))}
    return doc


def edges(doc: dict) -> set:
    """The graph links a document records: ``(src, dst, kind, seq, note)``."""
    out = set()
    for rel, (s_key, d_key) in {
            "wasDerivedFrom": ("prov:usedEntity", "prov:generatedEntity"),
            "wasInfluencedBy": ("prov:influencer", "prov:influencee"),
            "wasAttributedTo": ("prov:agent", "prov:entity"),
            "hadMember": ("prov:entity", "prov:collection")}.items():
        for r in (doc.get(rel) or {}).values():
            try:
                seq = int(r["qt:activity"].rsplit("/", 1)[1])
                out.add((_node(r[s_key]), _node(r[d_key]),
                         AG.Edge(r["qt:edge"]), seq, r.get("qt:note", "")))
            except (KeyError, ValueError, IndexError) as exc:
                raise ProvExportError(f"{rel}: {exc!r}") from None
    return out


def activities(doc: dict) -> dict:
    """seq -> (action, actor, wall time as ISO) from a document."""
    actor = {r["prov:activity"]: _node(r["prov:agent"])[len("actor:"):]
             for r in (doc.get("wasAssociatedWith") or {}).values()}
    return {a["qt:seq"]: (a["qt:action"], actor.get(qn), a["prov:startTime"])
            for qn, a in (doc.get("activity") or {}).items()}


def problems(doc: dict) -> list:
    """Structural problems: a relation naming an identifier the document
    does not declare in the class the relation requires. Empty for a
    well-formed export."""
    declared = {"entity": set(doc.get("entity") or {}),
                "activity": set(doc.get("activity") or {}),
                "agent": set(doc.get("agent") or {})}
    needs = {"wasAssociatedWith": {"prov:activity": "activity",
                                   "prov:agent": "agent"},
             "wasDerivedFrom": {"prov:generatedEntity": "entity",
                                "prov:usedEntity": "entity",
                                "prov:activity": "activity"},
             "wasInfluencedBy": {"prov:influencee": "entity",
                                 "prov:influencer": "entity"},
             "wasAttributedTo": {"prov:entity": "entity",
                                 "prov:agent": "agent"},
             "hadMember": {"prov:collection": "entity",
                           "prov:entity": "entity"},
             "wasInvalidatedBy": {"prov:entity": "entity",
                                  "prov:activity": "activity"}}
    out = []
    for rel, fields in needs.items():
        for qid, r in (doc.get(rel) or {}).items():
            for f, cls in fields.items():
                if r.get(f) not in declared[cls]:
                    out.append(f"{rel} {qid}: {f} {r.get(f)!r} is no "
                               f"declared {cls}")
    for cls, ids in declared.items():
        out += [f"{cls} {i!r} is not a qt: name" for i in sorted(ids)
                if not i.startswith("qt:")]
    return out
