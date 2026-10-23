"""The W3C PROV export (Assurance J): read-only, versioned, and a faithful
rendering of the fields it represents.

The history is the Gate C objective run end to end -- a fixture model's
proposals, a governed model run, an independent check, a reviewer's
verification, research records and a conclusion -- with analysts' notes,
one superseded. Its export must be well formed, read back to exactly the
causal graph and the events it came from, keep a model's output out of
the derivations, attribute verification to who did it, disclose what it
does not represent, and change nothing.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_research_loop as RL  # noqa: E402

from agent_substrate import assurance_graph as AG  # noqa: E402
from agent_substrate import prov_export as PX  # noqa: E402
from agent_substrate import research as R  # noqa: E402
from agent_substrate import research_loop as L  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.memory import MemoryStore  # noqa: E402


@pytest.fixture(scope="module")
def world():
    w = RL.World("prov-export", L.scripted_agent(
        hypothesis=RL.hypothesis(), run=RL.SLAB["request"]))
    assert w.controller().run("slab-q").status == "CONCLUDED"
    led = R.ResearchLedger(w.log).load()
    (obs,) = led.of("slab-q", "observation")
    bundle = led.get(obs).body["result_bundle"]
    mem = MemoryStore(w.log, evidence=EvidenceStore(w.base / "evidence"))
    mem = mem.load()
    mem.remember(memory_id="note-1", author="analyst",
                 derived_from=(bundle,), text="T_at_half came out low")
    mem.remember(memory_id="note-2", author="analyst",
                 derived_from=(bundle,), text="T_at_half came out low, "
                 "with the resolution counted")
    mem.supersede(old_id="note-1", new_id="note-2", actor="analyst",
                  reason="the second note is the better statement")
    yield w
    w.clean()


@pytest.fixture(scope="module")
def doc(world):
    return PX.export_log(world.log, origin="gate-c")


def _events(world):
    report, events = world.log.read_verified()
    report.raise_if_bad()
    return events


def test_the_export_is_well_formed(doc):
    assert PX.problems(doc) == []
    assert doc["qt:history"]["schema"] == PX.SCHEMA
    json.dumps(doc)                                  # plain JSON throughout


def test_it_reads_back_to_the_graph_and_the_events_it_came_from(world, doc):
    events = _events(world)
    g = AG.build(events)
    assert PX.edges(doc) == {(x.src, x.dst, x.kind, x.seq, x.note)
                             for x in g.links}
    assert PX.activities(doc) == {
        e.seq: (e.action, e.actor, PX._time(e.wall_time)) for e in events}
    head = events[-1]
    assert (doc["qt:history"]["head_seq"], doc["qt:history"]["head_hash"],
            doc["qt:history"]["events"]) == (head.seq, head.hash,
                                              len(events))


def test_every_kind_the_history_holds_is_represented(doc):
    kinds = {v["prov:type"] for v in doc["entity"].values()}
    assert {"qt:record", "qt:observation", "qt:hypothesis", "qt:objective",
            "qt:memory", "qt:context", "qt:proposal", "qt:inference",
            "qt:task", "qt:bytes", "qt:policy"} <= kinds
    for rel in ("wasAssociatedWith", "wasDerivedFrom", "wasInfluencedBy",
                "wasAttributedTo", "hadMember", "wasInvalidatedBy"):
        assert doc[rel], rel


def test_a_model_s_output_is_never_a_derivation(doc):
    derived = {r["qt:edge"] for r in doc["wasDerivedFrom"].values()}
    assert not derived & {e.value for e in AG.EXPOSES}
    outputs = [r for r in doc["wasInfluencedBy"].values()
               if r["qt:edge"] == "MODEL_OUTPUT"]
    assert outputs and all(r["prov:influencer"].startswith("qt:inference/")
                           and r["prov:influencee"].startswith("qt:proposal/")
                           for r in outputs)


def test_verification_is_attributed_to_who_did_it(world, doc):
    verified = [r for r in doc["wasAttributedTo"].values()
                if r["qt:edge"] == "SCIENTIFIC_VERIFICATION"]
    assert verified
    events = _events(world)
    for r in verified:
        seq = int(r["qt:activity"].rsplit("/", 1)[1])
        assert r["prov:agent"] == f"qt:actor/{events[seq].actor}"
        assert events[seq].payload.get("dst") == "VERIFIED"


def test_a_superseded_memory_is_invalidated_by_the_activity_that_did_it(doc):
    (inv,) = [r for r in doc["wasInvalidatedBy"].values()
              if r["prov:entity"] == "qt:memory/note-1"]
    assert doc["activity"][inv["prov:activity"]]["qt:action"] == \
        "memory.status"
    assert doc["entity"]["qt:memory/note-1"]["qt:state"] == "SUPERSEDED"


def test_what_is_not_represented_is_said(world, doc):
    g = AG.build(_events(world))
    gap = doc["qt:notRepresented"]
    assert gap["unread_actions"] == dict(sorted(g.unread.items()))
    assert gap["unread_actions"], "the Gate C history has actions the " \
                                  "graph does not read"
    assert "does not verify the history" in gap["chain"]


def test_exporting_changes_nothing_and_is_deterministic(world):
    before = world.log.path.read_bytes()
    a = PX.export_log(world.log, origin="gate-c")
    b = PX.export_log(world.log, origin="gate-c")
    assert world.log.path.read_bytes() == before
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_a_history_that_does_not_verify_is_not_exported(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    for i in range(3):
        log.append(actor="writer", action="note.fixture", target=f"t{i}",
                   payload={"i": i})
    lines = log.path.read_text(encoding="utf-8").splitlines(keepends=True)
    rec = json.loads(lines[1])
    rec["payload"] = {"i": "rewritten"}
    lines[1] = json.dumps(rec, sort_keys=True) + "\n"
    log.path.write_text("".join(lines), encoding="utf-8")
    with pytest.raises(PX.ProvExportError, match="does not verify"):
        PX.export_log(log)


def test_an_edited_document_no_longer_reads_back_to_the_history(world, doc):
    """The round trip is the check: a document whose relations were
    edited -- a model's output re-typed as evidence, or a relation pointed
    at something never declared -- is told apart from the export."""
    g = AG.build(_events(world))
    want = {(x.src, x.dst, x.kind, x.seq, x.note) for x in g.links}
    forged = json.loads(json.dumps(doc))
    qid, rel = next((k, r) for k, r in forged["wasInfluencedBy"].items()
                    if r["qt:edge"] == "MODEL_OUTPUT")
    del forged["wasInfluencedBy"][qid]
    forged["wasDerivedFrom"][qid] = {
        "prov:generatedEntity": rel["prov:influencee"],
        "prov:usedEntity": rel["prov:influencer"],
        "prov:activity": rel["qt:activity"], "qt:edge": "DIRECT_EVIDENCE",
        "qt:activity": rel["qt:activity"]}
    assert PX.problems(forged) == []                 # well formed, and wrong
    assert PX.edges(forged) != want
    dangling = json.loads(json.dumps(doc))
    next(iter(dangling["wasDerivedFrom"].values()))["prov:usedEntity"] = \
        "qt:record/nobody"
    assert PX.problems(dangling)


def test_nothing_in_the_package_reads_it():
    """An export, not an input: nothing that decides imports it."""
    users = []
    for path in sorted((ROOT / "agent_substrate").glob("*.py")):
        if path.name == "prov_export.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in getattr(node, "names", ())] + \
                [getattr(node, "module", "") or ""]
            if any("prov_export" in n for n in names):
                users.append(path.name)
    assert users == []


@settings(max_examples=300, deadline=None, derandomize=True, database=None)
@given(st.sampled_from(["record", "memory", "bytes", "proposal", "actor"]),
       st.text(min_size=1, max_size=20))
def test_any_identifier_survives_the_name_it_is_given(kind, ident):
    """Ids are the history's, and may hold anything: '/', '%', '#', spaces,
    non-ASCII. Each maps to one qt: name and back."""
    qn = PX._qn(f"{kind}:{ident}")
    assert qn.startswith(f"qt:{kind}/")
    assert all(c.isascii() and not c.isspace() for c in qn)
    assert PX._node(qn) == f"{kind}:{ident}"
