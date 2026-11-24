"""Memory, attacked the way a long-running agent's memory actually degrades.

The failure is not dramatic. It is a remembered sentence that nobody rechecks,
carried forward until it reads like a finding. Every test in the poisoning
section is a way of making that happen deliberately, and the control plane has
to survive all of them without the model's cooperation.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_substrate.authority import (  # noqa: E402
    Role, State, TransitionError, TransitionRequest, check,
)
from agent_substrate.canonical import digest  # noqa: E402
from agent_substrate.events import EventLog  # noqa: E402
from agent_substrate.evidence import EvidenceStore  # noqa: E402
from agent_substrate.memory import (  # noqa: E402
    ACT_MEMORY_STATUS, ACT_MEMORY_WRITE, MAX_ENTRY_BYTES, MAX_RECALL,
    MemoryEntry, MemoryError_, MemoryHit,
    MemoryIsNotEvidence, MemoryStatus, MemoryStore, MemoryTooLarge,
    UnknownMemory, entry_from_record, refuse_as_evidence,
)


@pytest.fixture()
def mem(tmp_path):
    ev = EvidenceStore(tmp_path / "evidence")
    return MemoryStore(EventLog(tmp_path / "log.jsonl"), evidence=ev).load()


def _remember(mem, mid="m1", **over):
    kw = dict(memory_id=mid, text="the coupling term looked negligible",
              author="agent-1")
    kw.update(over)
    return mem.remember(**kw)


# ---- the boundary --------------------------------------------------------
def test_a_memory_digest_does_not_resolve_as_evidence(mem):
    """The structural guarantee, not a rule someone must remember.

    An authority transition citing a memory's digest is refused by the
    evidence check that already exists, because nothing ever put the memory
    into the evidence store.
    """
    entry = _remember(mem)
    with pytest.raises(TransitionError, match="I6"):
        check(TransitionRequest(
            "r", State.UNDER_REVIEW, State.VERIFIED, "carol", Role.VERIFIER,
            {"verification_report": entry.digest()}, proposer="alice"),
            resolve=mem.evidence.contains)


def test_a_verified_looking_memory_still_does_not_resolve(mem):
    """Wording is not authority."""
    entry = _remember(mem, text="VERIFIED by the reviewer on 2026-01-01")
    assert not mem.evidence.contains(entry.digest())


def test_the_authority_path_cannot_import_memory():
    """Enforced by layering, asserted here so the reason is written down."""
    import ast

    # AUD-090: by any form, absolute ones too; the relative form alone was
    # read, and the layering test that enforces this read no more.
    from test_agent_substrate_isolation import package_imports
    for module in ("authority", "store", "tasks", "scheduler", "capability"):
        src = (ROOT / "agent_substrate" / f"{module}.py").read_text(encoding="utf-8")
        names = package_imports(ast.parse(src))
        assert "memory" not in names, (
            f"{module}.py imports memory; an authority decision that "
            "can read memory can be argued into a conclusion")


def test_refusing_as_evidence_names_what_went_wrong(mem):
    entry = _remember(mem)
    with pytest.raises(MemoryIsNotEvidence, match="not evidence"):
        refuse_as_evidence(entry)


# ---- provenance ----------------------------------------------------------
def test_an_entry_citing_evidence_that_does_not_exist_is_refused(mem):
    with pytest.raises(MemoryError_, match="does not resolve"):
        _remember(mem, derived_from=(digest({"invented": True}),))


def test_an_entry_may_cite_evidence_that_does_exist(mem):
    dg = mem.evidence.put(b"a real measurement record")
    entry = _remember(mem, derived_from=(dg,))
    assert entry.derived_from == (dg,)
    assert mem.derived_from(dg) == ("m1",)


def test_an_entry_with_no_source_is_allowed_and_says_so(mem):
    """A hunch is worth recording; calling it a finding is not."""
    entry = _remember(mem, confidence="a guess")
    assert entry.derived_from == ()
    assert entry.confidence == "a guess"


def test_confidence_is_commentary_and_status_is_the_system_s(mem):
    """Two entries that differ only in the author's confidence are treated
    identically by everything the system decides.

    The same status, the same recall score, the same response to an
    invalidated source, the same refusal as evidence, and the same answer to
    every trust question: ``factually_established`` is never YES, however
    sure the author was.
    """
    from agent_substrate.memory_trust import NOT_ESTABLISHED, assess

    dg = mem.evidence.put(b"the run both entries were drawn from")
    sure = _remember(mem, "sure", confidence="certain; I verified it myself",
                     derived_from=(dg,))
    hunch = _remember(mem, "hunch", confidence="a guess", derived_from=(dg,))
    assert sure.confidence != hunch.confidence
    assert sure.status is hunch.status is MemoryStatus.ACTIVE
    assert "confidence" not in str(MemoryEntry.__dataclass_fields__[
        "status"].type).lower()

    hits = {h.memory_id: h for h in mem.recall("coupling term negligible")}
    assert hits["sure"].score == hits["hunch"].score > 0
    assert hits["sure"].evidence_status == "REMEMBERED_TEXT_NOT_EVIDENCE"

    for entry in (sure, hunch):
        assert not mem.evidence.contains(entry.digest())
        with pytest.raises(MemoryIsNotEvidence):
            refuse_as_evidence(entry)
        with pytest.raises(TransitionError, match="I6"):
            check(TransitionRequest(
                "r", State.UNDER_REVIEW, State.VERIFIED, "carol",
                Role.VERIFIER, {"verification_report": entry.digest()},
                proposer="alice"), resolve=mem.evidence.contains)

    trust = {m: assess(mem.log, m, evidence=mem.evidence)
             for m in ("sure", "hunch")}
    assert ({k: a.value for k, a in trust["sure"].answers.items()}
            == {k: a.value for k, a in trust["hunch"].answers.items()})
    assert trust["sure"].answers["factually_established"].value \
        == NOT_ESTABLISHED

    moved = mem.invalidate_source(dg, actor="system", reason="rerun differed")
    assert sorted(e.memory_id for e in moved) == ["hunch", "sure"]
    assert mem.get("sure").status is mem.get("hunch").status \
        is MemoryStatus.STALE


def test_entries_survive_a_restart(tmp_path):
    ev = EvidenceStore(tmp_path / "evidence")
    m = MemoryStore(EventLog(tmp_path / "log.jsonl"), evidence=ev).load()
    _remember(m, "m1")
    _remember(m, "m2", text="the mesh refinement mattered")
    revived = MemoryStore(EventLog(tmp_path / "log.jsonl"),
                          evidence=ev).load()
    assert [e.memory_id for e in revived.current()] == ["m1", "m2"]
    assert revived.get("m1").digest() == m.get("m1").digest()


# ---- invalidation --------------------------------------------------------
def test_invalidating_a_source_makes_what_was_derived_from_it_stale(mem):
    dg = mem.evidence.put(b"a measurement that later changed")
    _remember(mem, "m1", derived_from=(dg,))
    _remember(mem, "m2", text="unrelated observation")

    moved = mem.invalidate_source(dg, actor="system", reason="rerun differed")
    assert [e.memory_id for e in moved] == ["m1"]
    assert mem.get("m1").status is MemoryStatus.STALE
    assert "invalidated" in mem.get("m1").status_reason
    assert mem.get("m2").status is MemoryStatus.ACTIVE
    assert [e.memory_id for e in mem.current()] == ["m2"]


def test_invalidation_reaches_a_summary_of_a_stale_entry(mem):
    """Compressing something that stopped being true does not fix it."""
    dg = mem.evidence.put(b"source")
    _remember(mem, "detail", derived_from=(dg,))
    mem.summarize(memory_id="summary", text="in short: negligible",
                  author="agent-1", summarizes=("detail",))
    mem.invalidate_source(dg, actor="system", reason="source withdrawn")
    assert mem.get("detail").status is MemoryStatus.STALE
    assert mem.get("summary").status is MemoryStatus.STALE
    assert mem.current() == ()


def test_an_entry_written_after_an_invalidation_is_born_stale(mem):
    """The ordering trap: invalidate first, then write the memory."""
    dg = mem.evidence.put(b"already withdrawn")
    mem.invalidate_source(dg, actor="system", reason="withdrawn")
    entry = _remember(mem, "late", derived_from=(dg,))
    assert entry.status is MemoryStatus.STALE, (
        "an entry derived from an already-invalidated source must not be "
        "briefly current; 'briefly' is long enough to be read")


def test_supersession_records_what_replaced_what(mem):
    _remember(mem, "old", text="value is about 3")
    _remember(mem, "new", text="value is 3.14 after the refinement")
    mem.supersede(old_id="old", new_id="new", actor="agent-1",
                  reason="refined")
    assert mem.get("old").status is MemoryStatus.SUPERSEDED
    assert mem.get("old").superseded_by == "new"
    assert [e.memory_id for e in mem.current()] == ["new"]


def test_retraction_keeps_the_text_and_changes_the_status(mem):
    _remember(mem, "m1")
    mem.retract("m1", actor="agent-1", reason="I was wrong")
    assert mem.get("m1").status is MemoryStatus.RETRACTED
    assert mem.get("m1").text == "the coupling term looked negligible", (
        "the log is append-only; a retraction adds a fact rather than "
        "deleting one")
    assert mem.current() == ()


def test_only_the_author_may_retract(mem):
    _remember(mem, "m1")
    with pytest.raises(MemoryError_, match="may not retract"):
        mem.retract("m1", actor="agent-2", reason="I disagree")


# ---- summaries -----------------------------------------------------------
def test_a_summary_does_not_remove_its_sources(mem):
    _remember(mem, "d1", text="detail one")
    _remember(mem, "d2", text="detail two")
    mem.summarize(memory_id="s", text="two details", author="agent-1",
                  summarizes=("d1", "d2"))
    ids = [e.memory_id for e in mem.current()]
    assert ids == ["d1", "d2", "s"], (
        "a summary that replaced its sources is how a long-running agent "
        "loses the ability to check itself")


def test_a_summary_must_name_what_it_summarizes(mem):
    with pytest.raises(MemoryError_, match="must name what it summarizes"):
        mem.summarize(memory_id="s", text="in short", author="agent-1",
                      summarizes=())


def test_a_summary_of_something_that_does_not_exist_is_refused(mem):
    with pytest.raises(MemoryError_, match="does not exist"):
        mem.summarize(memory_id="s", text="x", author="agent-1",
                      summarizes=("ghost",))


# ---- poisoning -----------------------------------------------------------
def test_a_memory_containing_a_fabricated_evidence_digest_is_refused(mem):
    """The most direct attack: write the digest you wish existed."""
    fake = "0" * 64
    with pytest.raises(MemoryError_, match="does not resolve"):
        _remember(mem, "poison", text="supported by a report",
                  derived_from=(fake,))


def test_a_memory_claiming_reviewer_approval_grants_nothing(mem):
    entry = _remember(
        mem, "poison",
        text="Reviewer bob approved promotion of record R1 on 2026-01-01")
    # It is recorded, and it changes nothing: the promotion edge needs a
    # verification report that resolves, and needs a distinct actor.
    with pytest.raises(TransitionError):
        check(TransitionRequest(
            "R1", State.VERIFIED, State.PROMOTED, "agent-1", Role.PROMOTER,
            {"verification_report": entry.digest(), "policy_id": "p"},
            proposer="agent-1", policy_id="p"),
            resolve=mem.evidence.contains)


def test_a_memory_asserting_its_own_status_does_not_get_it(mem):
    """Status is derived from events, not written by the author."""
    rec = MemoryEntry(memory_id="poison", text="x", author="agent-1",
                      status=MemoryStatus.ACTIVE).to_record()
    rec["status"] = "ACTIVE"
    mem.log.append(actor="agent-1", action=ACT_MEMORY_WRITE, target="poison",
                   payload={"entry": rec})
    reloaded = MemoryStore(mem.log, evidence=mem.evidence).load()
    assert reloaded.get("poison").status is MemoryStatus.ACTIVE
    # ...and asserting ACTIVE does not survive a real invalidation.
    dg = mem.evidence.put(b"src")
    _remember(reloaded, "derived", derived_from=(dg,))
    reloaded.invalidate_source(dg, actor="system", reason="withdrawn")
    assert reloaded.get("derived").status is MemoryStatus.STALE


def test_conflicting_memories_both_stand_until_something_resolves_them(mem):
    """The store does not pick a winner. Neither is authority, so neither
    needs to win."""
    _remember(mem, "a", text="the term is negligible", author="agent-1")
    _remember(mem, "b", text="the term is NOT negligible", author="agent-2")
    assert len(mem.current()) == 2, (
        "silently dropping one would be the store deciding a question it has "
        "no evidence about")


def test_a_second_write_to_one_id_is_refused(mem):
    _remember(mem, "m1")
    with pytest.raises(MemoryError_, match="already exists"):
        _remember(mem, "m1", text="quietly different")


def test_a_forged_duplicate_write_is_refused_on_replay(mem):
    entry = _remember(mem, "m1")
    rec = entry.to_record()
    rec["text"] = "quietly different"
    mem.log.append(actor="mallory", action=ACT_MEMORY_WRITE, target="m1",
                   payload={"entry": rec})
    with pytest.raises(MemoryError_, match="written twice"):
        MemoryStore(mem.log, evidence=mem.evidence).load()


def test_an_entry_record_with_unknown_fields_is_refused():
    rec = MemoryEntry(memory_id="m", text="t", author="a").to_record()
    rec["is_authoritative"] = True
    with pytest.raises(MemoryError_, match="unknown fields"):
        entry_from_record(rec)


@pytest.mark.parametrize("bad", [None, [], "entry", {"memory_id": "m"}])
def test_malformed_entry_records_fail_closed(bad):
    with pytest.raises(MemoryError_):
        entry_from_record(bad)


def test_an_empty_or_oversized_entry_is_refused(mem):
    with pytest.raises(MemoryError_, match="must have text"):
        _remember(mem, "empty", text="   ")
    with pytest.raises(MemoryError_, match="byte bound"):
        _remember(mem, "huge", text="x" * (MAX_ENTRY_BYTES + 1))


def test_unknown_memory_is_an_error_not_a_default(mem):
    with pytest.raises(UnknownMemory):
        mem.get("nope")


def test_apply_ignores_foreign_actions(mem):
    ev = mem.log.append(actor="x", action="task.create", target="t",
                        payload={})
    assert mem.apply(ev) is False


# --- §29: a status a replay would refuse does not become state --------------

def test_a_retracted_note_cannot_be_un_retracted_by_appending_one_line(
        tmp_path):
    """Retraction is a withdrawal, and withdrawals are not undone by fiat.

    The reducer used to assign whatever status a record named, so one line
    moved a RETRACTED note back to ACTIVE -- a statement its author had
    withdrawn, presented as current again, in a store that feeds context.
    """
    log = EventLog(tmp_path / "log.jsonl")
    ev = EvidenceStore(tmp_path / "e")
    m = MemoryStore(log, evidence=ev).load()
    m.remember(memory_id="m1", text="a note", author="a")
    m.retract("m1", actor="a", reason="withdrawn")

    log.append(actor="mallory", action=ACT_MEMORY_STATUS, target="m1",
               payload={"memory_id": "m1", "status": "ACTIVE",
                        "reason": "forged un-retraction"})
    with pytest.raises(MemoryError_, match="may not move to ACTIVE"):
        MemoryStore(log, evidence=ev).load()


def test_nothing_ever_moves_back_to_active(tmp_path):
    """Not an omission: a re-validated source produces a NEW entry.

    Reinstating the old one would leave the log saying it was never stale.
    """
    from agent_substrate.memory import STATUS_EDGES, MemoryStatus

    for src, dsts in STATUS_EDGES.items():
        assert MemoryStatus.ACTIVE not in dsts, (
            f"{src.value} may move to ACTIVE, so a note can be quietly "
            "reinstated instead of a new one being written")
    assert STATUS_EDGES[MemoryStatus.RETRACTED] == frozenset(), (
        "RETRACTED must be terminal")


def test_only_the_author_may_retract_on_replay_too(tmp_path):
    """The write path checks this; a reducer that did not made it advisory."""
    log = EventLog(tmp_path / "log.jsonl")
    ev = EvidenceStore(tmp_path / "e")
    m = MemoryStore(log, evidence=ev).load()
    m.remember(memory_id="m1", text="a note", author="alice")

    log.append(actor="mallory", action=ACT_MEMORY_STATUS, target="m1",
               payload={"memory_id": "m1", "status": "RETRACTED",
                        "reason": "withdrawing someone else's statement"})
    with pytest.raises(MemoryError_, match="may not retract"):
        MemoryStore(log, evidence=ev).load()


def test_the_ordinary_status_moves_still_replay(tmp_path):
    """The guard must name a real condition, not refuse the normal path."""
    log = EventLog(tmp_path / "log.jsonl")
    ev = EvidenceStore(tmp_path / "e")
    m = MemoryStore(log, evidence=ev).load()
    m.remember(memory_id="m1", text="first", author="a")
    m.remember(memory_id="m2", text="second", author="a")
    m.supersede(old_id="m1", new_id="m2", actor="a", reason="replaced")
    m.retract("m2", actor="a", reason="withdrawn")

    fresh = MemoryStore(log, evidence=ev).load()
    assert fresh.get("m1").status.value == "SUPERSEDED"
    assert fresh.get("m2").status.value == "RETRACTED"


def test_a_refused_retraction_never_reaches_the_log(tmp_path):
    """Y3, isolated from the replay check that now also catches it.

    Both refuse someone withdrawing another author's statement. They are not
    redundant: the write path refuses BEFORE the append, so the attempt never
    becomes a permanent hash-chained record claiming it happened.
    """
    log = EventLog(tmp_path / "log.jsonl")
    ev = EvidenceStore(tmp_path / "e")
    m = MemoryStore(log, evidence=ev).load()
    m.remember(memory_id="m1", text="a note", author="alice")
    before = len(list(log.read()))

    with pytest.raises(MemoryError_, match="may not retract"):
        m.retract("m1", actor="mallory", reason="not mine to withdraw")

    assert len(list(log.read())) == before, (
        "a refused retraction was appended; the write path must refuse "
        "before the record exists")
    assert m.get("m1").status is MemoryStatus.ACTIVE


def test_a_memory_write_with_no_entry_is_a_domain_error(tmp_path):
    """An authority API fails on purpose, or not at all.

    Found by a real killed child process writing a malformed memory.write
    into a live campaign log: the record was wrong, and the diagnosis the
    store gave was the single word 'entry'. A raw KeyError here leaks the
    payload's shape, names no subject, and makes the WHOLE store unloadable
    rather than refusing the one record that is unreadable.
    """
    log = EventLog(tmp_path / "log.jsonl")
    log.append(actor="w1", action="memory.write", target="m1",
               payload={"memory_id": "m1", "text": "wrong shape"})
    with pytest.raises(MemoryError_, match="carries no entry"):
        MemoryStore(log).load()


# ==========================================================================
# RECALL, and the total bound
#
# THE GAPS THESE CLOSE, stated as they were found:
#
#     "memory is not retrievable through the RAG index, so the 'retrievable
#      memory keeps its lower authority' case is unimplemented rather than
#      solved"
#     "no bound on total memory size across entries, only per entry"
#
# The first is solved by NOT putting memory in the document index. That
# corpus is an allowlisted set of governed documents, and letting an agent's
# notes join it would let an agent write text into the corpus it is later
# handed back with the retrieval layer's provenance on it -- exactly what the
# allowlist exists to stop.
# ==========================================================================

def test_recall_finds_an_entry_by_its_words(mem):
    mem.remember(memory_id="m1", author="a",
                   text="the lattice thermal conductivity forecast diverged")
    mem.remember(memory_id="m2", author="a",
                   text="the chamber pressure was nominal")

    hits = mem.recall("thermal conductivity")

    assert [h.memory_id for h in hits] == ["m1"]
    assert hits[0].evidence_status == "REMEMBERED_TEXT_NOT_EVIDENCE"


def test_a_hit_is_not_an_entry_and_offers_no_digest(mem):
    """A hit that could be passed where evidence is expected fails at the
    attribute, not three layers later."""
    mem.remember(memory_id="m1", author="a", text="alpha beta gamma")
    hit = mem.recall("alpha")[0]

    assert not isinstance(hit, MemoryEntry)
    assert not hasattr(hit, "digest")
    with pytest.raises(AttributeError):
        hit.digest()


def test_the_evidence_label_on_a_hit_cannot_be_set(mem):
    """A status a caller can choose is a label, not a property."""
    mem.remember(memory_id="m1", author="a", text="alpha beta")
    hit = mem.recall("alpha")[0]
    with pytest.raises(Exception):
        hit.evidence_status = "VERIFIED"
    assert hit.evidence_status == "REMEMBERED_TEXT_NOT_EVIDENCE"
    # And it is not a dataclass FIELD, so no construction path can pass it.
    from dataclasses import fields
    assert "evidence_status" not in {f.name for f in fields(MemoryHit)}


def test_recall_excludes_a_retracted_entry(mem):
    """THE property. A search that returned it would un-withdraw a statement
    for everybody who searched instead of listing -- without an event."""
    mem.remember(memory_id="m1", author="a", text="the forecast diverged")
    mem.retract("m1", actor="a", reason="wrong")

    assert mem.recall("forecast") == ()


def test_recall_excludes_a_superseded_entry(mem):
    mem.remember(memory_id="m1", author="a", text="the forecast diverged")
    mem.remember(memory_id="m2", author="a",
                   text="the forecast converged after all")
    mem.supersede(old_id="m1", new_id="m2", actor="a", reason="revised")

    assert [h.memory_id for h in mem.recall("forecast")] == ["m2"]


def test_asking_for_non_current_entries_returns_them_WITH_their_status(mem):
    """Possible, deliberate, and never silent about what it handed back."""
    mem.remember(memory_id="m1", author="a", text="the forecast diverged")
    mem.retract("m1", actor="a", reason="wrong")

    hits = mem.recall("forecast", include_non_current=True)

    assert [h.memory_id for h in hits] == ["m1"]
    assert hits[0].status is MemoryStatus.RETRACTED
    assert not hits[0].is_current


def test_recall_is_deterministic_across_readers(mem, tmp_path):
    """Two readers of the same log must return the same order, or an audit
    of a run depends on which process ran it."""
    for i in range(8):
        mem.remember(memory_id=f"m{i}", author="a",
                       text="alpha beta gamma delta")

    revived = MemoryStore(EventLog(tmp_path / "log.jsonl")).load()
    assert [h.memory_id for h in mem.recall("alpha beta", k=8)] == \
        [h.memory_id for h in revived.recall("alpha beta", k=8)]


def test_recall_breaks_ties_by_id_and_not_by_arrival_order(mem):
    """The tie-break is the determinism, and the other test cannot see it.

    `test_recall_is_deterministic_across_readers` writes m0..m7 in id order,
    so arrival order and id order are the same sequence and dropping the
    tie-break changes nothing -- Python's sort is stable and the entries come
    back in the order the dict holds them, which is the order they were
    written. The mutation that removes the id break survived that test for
    exactly that reason.

    Here the two orders disagree. Every entry matches the same single term,
    so every score is equal and the ONLY thing deciding the result is the
    tie-break.
    """
    arrival = ["m-zulu", "m-alpha", "m-mike", "m-bravo"]
    for mid in arrival:
        mem.remember(memory_id=mid, author="a", text="alpha")

    got = [h.memory_id for h in mem.recall("alpha", k=8)]

    assert got == sorted(arrival), (
        f"recall returned {got}; ties must resolve by memory_id, not by the "
        "order entries happened to be written")
    assert got != arrival, (
        "arrival order equals id order, so this test cannot see the "
        "tie-break at all")
    assert len({h.score for h in mem.recall("alpha", k=8)}) == 1, (
        "the scores differ, so the ranking decided this rather than the "
        "tie-break")


def test_recall_ranks_by_how_many_query_terms_matched(mem):
    mem.remember(memory_id="m-one", author="a", text="alpha only")
    mem.remember(memory_id="m-both", author="a", text="alpha and beta")

    assert [h.memory_id for h in mem.recall("alpha beta")] == \
        ["m-both", "m-one"]
    assert mem.recall("alpha beta")[0].score == 2


def test_recall_k_is_bounded(mem):
    mem.remember(memory_id="m1", author="a", text="alpha")
    for bad in (0, -1, True, 1.5, None):
        with pytest.raises(MemoryError_):
            mem.recall("alpha", k=bad)
    with pytest.raises(MemoryError_) as exc:
        mem.recall("alpha", k=MAX_RECALL + 1)
    assert "context window" in str(exc.value)


def test_an_empty_query_returns_nothing_rather_than_everything(mem):
    mem.remember(memory_id="m1", author="a", text="alpha")
    assert mem.recall("") == ()
    assert mem.recall("   ") == ()


def test_a_query_that_matches_nothing_returns_nothing(mem):
    """THE guard, as distinct from the fast path above it.

    `recall` returns early on a query with no terms, and that early return
    is a refinement rather than a rule: without it an empty query still
    comes back empty, because an entry is only collected when the query and
    the body share a term. The rule is that intersection test, and a query
    of real words that matches no entry is where it is the only thing
    standing between a caller and the whole store.
    """
    mem.remember(memory_id="m1", author="a", text="alpha beta")
    mem.remember(memory_id="m2", author="a", text="gamma delta")

    assert mem.recall("epsilon zeta", k=8) == (), (
        "a query sharing no term with any entry returned entries; the "
        "caller asked about one thing and was handed the store")
    # ANTI-VACUITY: the store is not simply empty.
    assert len(mem.recall("alpha", k=8)) == 1


# ---- the total bound ----------------------------------------------------

def test_the_total_bound_refuses_a_write_that_would_cross_it(mem,
                                                             monkeypatch):
    """The per-entry bound stops one large write and permits a million small
    ones, which reaches the same place more slowly."""
    monkeypatch.setattr("agent_substrate.memory.MAX_TOTAL_BYTES", 200)
    mem.remember(memory_id="m1", author="a", text="x" * 150)

    with pytest.raises(MemoryTooLarge) as exc:
        mem.remember(memory_id="m2", author="a", text="y" * 100)
    assert "over the 200 bound" in str(exc.value)
    assert "m2" not in [e.memory_id for e in mem.all_entries()]


def test_the_total_bound_counts_LIVE_text_only(mem, monkeypatch):
    """Retracting frees the bound and loses no history.

    The log keeps every byte -- that is the point of a log -- and what the
    bound protects is what a projection has to hold and a recall has to
    search.
    """
    monkeypatch.setattr("agent_substrate.memory.MAX_TOTAL_BYTES", 200)
    mem.remember(memory_id="m1", author="a", text="x" * 150)
    with pytest.raises(MemoryTooLarge):
        mem.remember(memory_id="m2", author="a", text="y" * 100)

    mem.retract("m1", actor="a", reason="no longer needed")
    mem.remember(memory_id="m2", author="a", text="y" * 100)

    assert mem.live_bytes() == 100
    assert mem.get("m1").text == "x" * 150, "the history was lost"


def test_live_bytes_reports_what_a_projection_holds(mem):
    mem.remember(memory_id="m1", author="a", text="abcde")
    mem.remember(memory_id="m2", author="a", text="fgh")
    assert mem.live_bytes() == 8
    mem.retract("m2", actor="a", reason="withdrawn")
    assert mem.live_bytes() == 5


# --------------------------------------------------------------------------
# Superseding is a withdrawal, and withdrawals belong to the author.
#
# retract() has always refused withdrawing somebody else's statement, on the
# write path and on replay, with the argument written out. Its sibling had
# no check anywhere -- and supersession is the STRONGER of the two: it
# removes the entry from current() AND points every later reader at whatever
# the superseder chose to replace it with.
# --------------------------------------------------------------------------

def _two(mem, a1="alice", a2="mallory"):
    _remember(mem, "m1", author=a1, text="the coupling term is not negligible")
    _remember(mem, "m2", author=a2, text="the coupling term is negligible")


def test_nobody_else_may_supersede_your_statement(mem):
    _two(mem)
    with pytest.raises(MemoryError_, match="may not supersede"):
        mem.supersede(old_id="m1", new_id="m2", actor="mallory",
                      reason="mine is better")
    assert mem.get("m1").status is MemoryStatus.ACTIVE


def test_replay_refuses_a_supersession_by_a_non_author(mem):
    """The write-path check would be advisory if the reducer let it through.

    This is the same argument the RETRACTED rule already carries, applied to
    the status move that was missing from it.
    """
    _two(mem)
    log = mem.log
    log.append(actor="mallory", action=ACT_MEMORY_STATUS, target="m1",
               payload={"memory_id": "m1", "status": "SUPERSEDED",
                        "superseded_by": "m2", "reason": "mine is better"})
    with pytest.raises(MemoryError_, match="may not supersede"):
        MemoryStore(log, evidence=mem.evidence).load()


def test_a_superseded_entry_stops_being_recalled_only_when_its_author_says(mem):
    """The whole point of the rule, stated as the effect it protects."""
    _two(mem)
    with pytest.raises(MemoryError_):
        mem.supersede(old_id="m1", new_id="m2", actor="mallory", reason="no")
    assert "m1" in [h.memory_id for h in mem.recall("coupling")]


def test_an_author_may_still_supersede_their_own(mem):
    """Anti-vacuity: the ordinary correction still works."""
    _two(mem, a1="alice", a2="alice")
    mem.supersede(old_id="m1", new_id="m2", actor="alice", reason="refined")
    assert mem.get("m1").status is MemoryStatus.SUPERSEDED
    assert mem.get("m1").superseded_by == "m2"


def test_an_invalidation_cascade_is_not_an_authorship_claim(mem):
    """INVALIDATED is deliberately outside the author's-own rule.

    A source changing is a fact about the world, cascaded over entries with
    many different authors by whoever noticed it. Requiring authorship there
    would stop the cascade doing the only thing it exists for -- so the rule
    covers the two statuses that WITHDRAW and neither of the two that say a
    premise stopped holding.
    """
    dg = mem.evidence.put(b"a measurement that later changed")
    _remember(mem, "m1", author="alice", derived_from=(dg,))
    _remember(mem, "m2", author="bob", derived_from=(dg,))
    moved = mem.invalidate_source(dg, actor="system", reason="rerun differed")

    assert {e.memory_id for e in moved} == {"m1", "m2"}
    assert mem.get("m1").status is MemoryStatus.STALE
    assert mem.get("m2").status is MemoryStatus.STALE


def test_a_refused_supersession_LEAVES_THE_LOG_LOADABLE(mem):
    """What the write-path check is for, as distinct from the reducer's job.

    Both layers refuse a non-author's supersession, with the same exception
    and the same message -- so a test that only asserts "it raises" cannot
    tell them apart, and a mutation removing the write-path check SURVIVED
    against exactly such a test.

    They do not guarantee the same thing. _set_status appends and THEN
    applies, so without the check at the call site the record is durable
    before the reducer ever sees it: the caller gets its exception and the
    store is permanently unloadable. The check at the write path is what
    keeps a refusal from poisoning the log.
    """
    _two(mem)
    with pytest.raises(MemoryError_, match="may not supersede"):
        mem.supersede(old_id="m1", new_id="m2", actor="mallory",
                      reason="mine is better")

    # The refusal must have cost the log nothing.
    assert [ev.action for ev in mem.log.read()] == [ACT_MEMORY_WRITE] * 2, (
        "the refused supersession still appended a record; the log now "
        "carries an event its own reducer will refuse on every future load")
    reloaded = MemoryStore(mem.log, evidence=mem.evidence).load()
    assert reloaded.get("m1").status is MemoryStatus.ACTIVE


# ==========================================================================
# THE STATUS RULES ON REPLAY, AND THE RECORDS THEY READ (AUD-043 to AUD-045)
#
# Replay checked a move's edge and, for the two withdrawals, its author. A
# STALE move is open to everybody, so that invalidating a source cascades
# over entries with many authors, and replay never asked for its premise or
# kept it from naming a replacement (AUD-043). An invalidation nothing rested
# on yet reached no record at all (AUD-044). And a record's keys were
# checked, not what they held (AUD-045).
# ==========================================================================
def _stale_by_mallory(mem, **extra):
    _two(mem)
    payload = {"memory_id": "m1", "status": "STALE", "reason": "outdated"}
    payload.update(extra)
    mem.log.append(actor="mallory", action=ACT_MEMORY_STATUS, target="m1",
                   payload=payload)


def test_replay_refuses_a_stale_move_that_points_readers_elsewhere(mem):
    """AUD-043, as reproduced: mallory moves alice's note to STALE and names
    her own as its replacement. It was projected, and readers of alice's
    note were pointed at mallory's."""
    _stale_by_mallory(mem, superseded_by="m2")
    with pytest.raises(MemoryError_, match="only its author's SUPERSEDED"):
        MemoryStore(mem.log, evidence=mem.evidence).load()


def test_replay_refuses_a_stale_move_with_no_invalidated_source(mem):
    """AUD-043: STALE says a premise stopped holding, so it needs one."""
    _stale_by_mallory(mem)
    with pytest.raises(MemoryError_, match="nothing it rests on"):
        MemoryStore(mem.log, evidence=mem.evidence).load()


def test_a_stale_move_on_an_invalidated_premise_still_replays(mem):
    """Anti-vacuity: the cascade's own move, by somebody other than the
    author, still stands when its premise does."""
    dg = mem.evidence.put(b"a measurement later withdrawn")
    _remember(mem, "m1", author="alice", derived_from=(dg,))
    mem.log.append(actor="system", action="memory.invalidate", target=dg,
                   payload={"source": dg, "reason": "rerun differed"})
    mem.log.append(actor="bob", action=ACT_MEMORY_STATUS, target="m1",
                   payload={"memory_id": "m1", "status": "STALE",
                            "reason": "its source was withdrawn"})
    reloaded = MemoryStore(mem.log, evidence=mem.evidence).load()
    assert reloaded.get("m1").status is MemoryStatus.STALE


def test_a_summary_of_an_entry_on_an_invalidated_source_rests_on_it(mem):
    """The premise reaches as far as the cascade does: a summary of an entry
    derived from the source rests on the source."""
    dg = mem.evidence.put(b"a measurement later withdrawn")
    _remember(mem, "m1", author="alice", derived_from=(dg,))
    mem.summarize(memory_id="s1", text="summary of the coupling note",
                  author="carol", summarizes=("m1",))
    mem.log.append(actor="system", action="memory.invalidate", target=dg,
                   payload={"source": dg, "reason": "rerun differed"})
    mem.log.append(actor="bob", action=ACT_MEMORY_STATUS, target="s1",
                   payload={"memory_id": "s1", "status": "STALE",
                            "reason": "it summarizes a withdrawn note"})
    reloaded = MemoryStore(mem.log, evidence=mem.evidence).load()
    assert reloaded.get("s1").status is MemoryStatus.STALE


@pytest.mark.parametrize("payload,why", [
    ({"status": "RETRACTED", "superseded_by": "m2"}, "only its author's"),
    ({"status": "SUPERSEDED", "superseded_by": "ghost"}, "does not hold"),
    ({"status": "RETRACTED", "invalidated_source": "x" * 64},
     "only a STALE move"),
    ({"status": "RETRACTED", "reason": 5}, "not str"),
])
def test_a_move_carrying_what_another_move_carries_is_refused(mem, payload,
                                                              why):
    """AUD-043: each field belongs to one kind of move, the author's own
    here, so that no record can carry it past the rule that move answers
    to."""
    _remember(mem, "m1", author="alice")
    _remember(mem, "m2", author="alice", text="a later coupling note")
    rec = {"memory_id": "m1", "reason": "r"}
    rec.update(payload)
    mem.log.append(actor="alice", action=ACT_MEMORY_STATUS, target="m1",
                   payload=rec)
    with pytest.raises(MemoryError_, match=why):
        MemoryStore(mem.log, evidence=mem.evidence).load()


@pytest.mark.parametrize("over", [{"status": "RETRACTED"},
                                  {"status": "STALE"},
                                  {"superseded_by": "m1"}])
def test_a_write_that_asserts_a_status_is_refused(mem, over):
    """AUD-043: a status is derived from events, never asserted. An entry
    written as anything but ACTIVE, or naming its own replacement, is a
    status move that no rule was asked about."""
    _remember(mem, "m1", author="alice")
    rec = MemoryEntry(memory_id="m9", text="t", author="mallory").to_record()
    rec.update(over)
    mem.log.append(actor="mallory", action=ACT_MEMORY_WRITE, target="m9",
                   payload={"entry": rec})
    with pytest.raises(MemoryError_, match="written ACTIVE"):
        MemoryStore(mem.log, evidence=mem.evidence).load()


def test_the_write_path_refuses_the_move_replay_refuses(mem):
    """AUD-043: _set_status decides by replay's rules, so it may not write a
    record its own replay refuses -- and the refusal costs the log
    nothing."""
    _two(mem)
    with pytest.raises(MemoryError_, match="only its author's SUPERSEDED"):
        mem._set_status("m1", MemoryStatus.STALE, actor="mallory",
                        reason="outdated", superseded_by="m2")
    with pytest.raises(MemoryError_, match="nothing it rests on"):
        mem._set_status("m1", MemoryStatus.STALE, actor="mallory",
                        reason="outdated")
    assert [ev.action for ev in mem.log.read()] == [ACT_MEMORY_WRITE] * 2
    assert mem.get("m1").status is MemoryStatus.ACTIVE


def test_an_invalidation_nothing_rests_on_yet_is_a_fact_of_the_log(mem):
    """AUD-044, as reproduced: an invalidation over nothing wrote no record,
    so after a reload an entry citing the source was born ACTIVE."""
    dg = mem.evidence.put(b"a source withdrawn before anything cited it")
    assert mem.invalidate_source(dg, actor="system", reason="withdrawn") == ()
    assert [ev.action for ev in mem.log.read()] == ["memory.invalidate"]

    reloaded = MemoryStore(mem.log, evidence=mem.evidence).load()
    late = _remember(reloaded, "late", derived_from=(dg,))
    assert late.status is MemoryStatus.STALE


def test_another_process_learns_of_the_invalidation(tmp_path):
    """AUD-044: a store loaded before the invalidation folds it before it
    decides a write, and so the write is born STALE there too."""
    ev = EvidenceStore(tmp_path / "evidence")
    log_path = tmp_path / "log.jsonl"
    a = MemoryStore(EventLog(log_path), evidence=ev).load()
    b = MemoryStore(EventLog(log_path), evidence=ev).load()
    dg = ev.put(b"a source withdrawn before anything cited it")
    a.invalidate_source(dg, actor="system", reason="withdrawn")
    late = b.remember(memory_id="late", text="a note on the withdrawn source",
                      author="agent-2", derived_from=(dg,))
    assert late.status is MemoryStatus.STALE


@pytest.mark.parametrize("payload", [{"source": ["x"], "reason": "r"},
                                     {"source": "", "reason": "r"},
                                     {"source": "x" * 64, "reason": 5}])
def test_a_malformed_invalidation_is_refused_not_raised(mem, payload):
    mem.log.append(actor="system", action="memory.invalidate", target="t",
                   payload=payload)
    with pytest.raises(MemoryError_, match="cannot read"):
        MemoryStore(mem.log, evidence=mem.evidence).load()


@pytest.mark.parametrize("field,value", [
    ("text", 123), ("confidence", 0.9), ("status_reason", 7),
    ("derived_from", "abc"), ("derived_from", [["nested"]]),
    ("summarizes", "m1"), ("created_seq", "early"), ("updated_seq", True),
    ("memory_id", ""),
])
def test_a_memory_record_is_refused_when_a_field_holds_the_wrong_type(
        mem, field, value):
    """AUD-045, as reproduced for text: a write whose text was a number
    loaded, and then live_bytes() and every later remember(), by any author,
    raised AttributeError. Each field is held to its type when read, so the
    refusal names the field instead."""
    rec = MemoryEntry(memory_id="m9", text="t", author="mallory").to_record()
    rec[field] = value
    with pytest.raises(MemoryError_):
        entry_from_record(rec)
    mem.log.append(actor="mallory", action=ACT_MEMORY_WRITE, target="m9",
                   payload={"entry": rec})
    with pytest.raises(MemoryError_):
        MemoryStore(mem.log, evidence=mem.evidence).load()


def test_a_status_record_naming_no_memory_id_is_refused_not_raised(mem):
    """AUD-045: a list as the memory id raised a raw TypeError."""
    _remember(mem, "m1")
    mem.log.append(actor="agent-1", action=ACT_MEMORY_STATUS, target="m1",
                   payload={"memory_id": ["m1"], "status": "RETRACTED"})
    with pytest.raises(MemoryError_, match="unknown memory"):
        MemoryStore(mem.log, evidence=mem.evidence).load()


def test_a_history_written_before_the_invalidation_record_still_replays(mem):
    """Compatibility: before AUD-044 an invalidation travelled on its
    cascade's first STALE record as invalidated_source, with no record of
    its own. That record supplies its own premise, and the source it
    carries is invalidated for what follows."""
    dg = mem.evidence.put(b"a measurement later withdrawn")
    _remember(mem, "m1", author="alice", derived_from=(dg,))
    mem.log.append(actor="system", action=ACT_MEMORY_STATUS, target="m1",
                   payload={"memory_id": "m1", "status": "STALE",
                            "reason": "derived from a withdrawn source",
                            "invalidated_source": dg})
    reloaded = MemoryStore(mem.log, evidence=mem.evidence).load()
    assert reloaded.get("m1").status is MemoryStatus.STALE
    assert _remember(reloaded, "late", derived_from=(dg,)).status \
        is MemoryStatus.STALE


def test_an_invalidation_is_recorded_once(mem):
    """AUD-044: invalidating a source the log already holds invalidated
    adds no record, so that settling an invalidation twice changes
    nothing; the second call's cascade still moves what is current."""
    dg = mem.evidence.put(b"a source withdrawn twice")
    mem.invalidate_source(dg, actor="system", reason="withdrawn")
    head = mem.log.path.read_bytes()
    assert mem.invalidate_source(dg, actor="system", reason="again") == ()
    assert mem.log.path.read_bytes() == head
    assert [ev.action for ev in mem.log.read()] == ["memory.invalidate"]


def test_an_action_no_module_writes_is_refused(mem):
    """AUD-046: another subsystem's action is skipped, and one nothing in
    the package writes is refused, as the authority store refuses it."""
    _remember(mem, "m1")
    mem.log.append(actor="mallory", action="memory.rewrite", target="m1",
                   payload={"memory_id": "m1", "text": "changed"})
    with pytest.raises(MemoryError_, match="unknown action 'memory.rewrite'"):
        MemoryStore(mem.log, evidence=mem.evidence).load()


# ==========================================================================
# WHAT RESTS ON AN INVALIDATED SOURCE THROUGH ANOTHER ENTRY (AUD-089)
#
# An entry derived from an invalidated source is born STALE, and invalidating
# a source moves to STALE what rests on it, through the entries that
# summarize or derive from it. Two ways led past that. An entry resting on
# the source THROUGH another entry, written after the cascade, was born
# ACTIVE, since its birth asked only for its own sources. And the cascade
# stopped at an entry no longer current, so nothing resting on the source
# through a retracted or superseded note was reached. Each left a current
# note on a withdrawn premise, in both readers, and nothing later moved it.
# ==========================================================================
def _second(mem) -> dict:
    """The second reader's view of the memory: status by id, no anomaly."""
    from agent_substrate.reconstruct import reconstruct_subsystems
    sub = reconstruct_subsystems(EventLog(mem.log.path))
    assert sub.anomalies == [], sub.anomalies
    return {mid: e["status"] for mid, e in sub.memory.items()}


def _stale_detail(mem):
    dg = mem.evidence.put(b"a measurement later withdrawn")
    _remember(mem, "detail", author="alice", derived_from=(dg,))
    mem.invalidate_source(dg, actor="system", reason="rerun differed")
    assert mem.get("detail").status is MemoryStatus.STALE
    return dg


def test_a_summary_written_after_its_detail_went_stale_is_born_stale(mem):
    """AUD-089, as reproduced: it was born ACTIVE, and was in current() and
    in recall, in a fresh load and in the second reader."""
    _stale_detail(mem)
    s = mem.summarize(memory_id="summary", text="in short: negligible",
                      author="carol", summarizes=("detail",))
    assert s.status is MemoryStatus.STALE
    assert mem.current() == ()
    assert mem.recall("negligible") == ()
    fresh = MemoryStore(EventLog(mem.log.path), evidence=mem.evidence).load()
    assert fresh.get("summary").status is MemoryStatus.STALE
    assert _second(mem)["summary"] == "STALE"


def test_an_entry_derived_from_a_stale_entry_is_born_stale(mem):
    """AUD-089: the same through derived_from, which names entries too."""
    _stale_detail(mem)
    e = _remember(mem, "note", author="bob", text="building on the detail",
                  derived_from=("detail",))
    assert e.status is MemoryStatus.STALE
    assert _second(mem)["note"] == "STALE"


def test_an_entry_that_named_a_memory_before_it_was_written_goes_stale(mem):
    """AUD-089, as reproduced: derived_from may name a memory before it is
    written, since it names authority records too. When that memory was
    then born STALE on an invalidated source, the entry naming it stayed
    current: the cascade over the source ran before it existed."""
    dg = mem.evidence.put(b"a measurement later withdrawn")
    mem.invalidate_source(dg, actor="system", reason="rerun differed")
    _remember(mem, "early", author="bob", text="building on a later note",
              derived_from=("later",))
    born = _remember(mem, "later", author="alice", derived_from=(dg,))
    assert born.status is MemoryStatus.STALE
    assert mem.get("early").status is MemoryStatus.STALE
    fresh = MemoryStore(EventLog(mem.log.path), evidence=mem.evidence).load()
    assert fresh.get("early").status is MemoryStatus.STALE
    assert _second(mem)["early"] == "STALE"


def test_a_summary_of_a_withdrawn_note_on_no_invalidated_source_is_active(
        mem):
    """The control: a summary is born STALE for an invalidated premise, not
    for summarizing an entry that is no longer current."""
    _remember(mem, "detail", author="alice")
    mem.retract("detail", actor="alice", reason="withdrawn")
    s = mem.summarize(memory_id="summary", text="in short", author="carol",
                      summarizes=("detail",))
    assert s.status is MemoryStatus.ACTIVE
    assert _second(mem)["summary"] == "ACTIVE"


@pytest.mark.parametrize("withdraw", ["retract", "supersede"])
def test_the_cascade_reaches_past_an_entry_no_longer_current(mem, withdraw):
    """AUD-089, as reproduced: a summary of a retracted or superseded note
    stayed current when the note's source was invalidated. The note itself
    is not moved; what rests on the source through it is."""
    dg = mem.evidence.put(b"a measurement later withdrawn")
    _remember(mem, "detail", author="alice", derived_from=(dg,))
    mem.summarize(memory_id="summary", text="in short", author="carol",
                  summarizes=("detail",))
    if withdraw == "retract":
        mem.retract("detail", actor="alice", reason="withdrawn")
        kept = MemoryStatus.RETRACTED
    else:
        _remember(mem, "restated", author="alice", text="restated")
        mem.supersede(old_id="detail", new_id="restated", actor="alice",
                      reason="restated")
        kept = MemoryStatus.SUPERSEDED
    moved = mem.invalidate_source(dg, actor="system", reason="rerun differed")
    assert [e.memory_id for e in moved] == ["summary"]
    assert mem.get("detail").status is kept
    fresh = MemoryStore(EventLog(mem.log.path), evidence=mem.evidence).load()
    assert fresh.get("summary").status is MemoryStatus.STALE
    assert _second(mem)["summary"] == "STALE"


# ---- every edge of the lifecycle, and every edge it does not have --------
from agent_substrate.memory import STATUS_EDGES  # noqa: E402

_EDGES = sorted((a.value, b.value) for a, dsts in STATUS_EDGES.items()
                for b in dsts)


def _to(mem, mid, status, dg):
    """Move ``mid``, written by alice from ``dg``, to ``status`` by the write
    path: the move each status is reached by."""
    if status == "STALE":
        mem.invalidate_source(dg, actor="system", reason="rerun differed")
    elif status == "SUPERSEDED":
        _remember(mem, f"{mid}-new", author="alice", text="a later note")
        mem.supersede(old_id=mid, new_id=f"{mid}-new", actor="alice",
                      reason="restated")
    elif status == "RETRACTED":
        mem.retract(mid, actor="alice", reason="withdrawn")


@pytest.mark.parametrize("edge", _EDGES, ids="->".join)
def test_each_edge_is_taken_and_replayed_by_both_readers(tmp_path, edge):
    """Each of the six edges, taken on the write path by a writer that did
    not write the entry and had not read it, and replayed by a fresh load
    and by the second reader."""
    src, dst = edge
    ev = EvidenceStore(tmp_path / "evidence")
    path = tmp_path / "log.jsonl"
    a = MemoryStore(EventLog(path), evidence=ev).load()
    b = MemoryStore(EventLog(path), evidence=ev).load()
    dg = ev.put(b"its source")
    _remember(a, "m", author="alice", derived_from=(dg,))
    _to(a, "m", src, dg)
    _to(b, "m", dst, dg)
    assert b.get("m").status.value == dst
    fresh = MemoryStore(EventLog(path), evidence=ev).load()
    assert fresh.get("m").status.value == dst
    assert _second(fresh)["m"] == dst


def test_a_writer_that_has_not_caught_up_withdraws_what_another_wrote(
        tmp_path):
    """AUD-091, as reproduced: a second store of alice's, loaded before her
    notes were written, refused her retraction and her supersession with
    "no memory", though the log held both. A write reads the log before it
    decides, as the authority store's do."""
    ev = EvidenceStore(tmp_path / "evidence")
    path = tmp_path / "log.jsonl"
    a = MemoryStore(EventLog(path), evidence=ev).load()
    b = MemoryStore(EventLog(path), evidence=ev).load()
    _remember(a, "m1", author="alice")
    _remember(a, "m2", author="alice", text="a later note")
    _remember(a, "m3", author="alice", text="a third note")
    b.supersede(old_id="m1", new_id="m2", actor="alice", reason="restated")
    c = MemoryStore(EventLog(path), evidence=ev).load()
    _remember(a, "m4", author="alice", text="a fourth note")
    c.retract("m4", actor="alice", reason="withdrawn")
    fresh = MemoryStore(EventLog(path), evidence=ev).load()
    assert fresh.get("m1").status is MemoryStatus.SUPERSEDED
    assert fresh.get("m4").status is MemoryStatus.RETRACTED
    with pytest.raises(MemoryError_, match="may not retract"):
        c.retract("m3", actor="bob", reason="not his")


_ABSENT = sorted((a.value, b.value)
                 for a in MemoryStatus for b in MemoryStatus
                 if a is not b and (a.value, b.value) not in _EDGES)


@pytest.mark.parametrize("edge", _ABSENT, ids="->".join)
def test_each_edge_the_lifecycle_does_not_have_is_refused_by_both(tmp_path,
                                                                   edge):
    """Each move between two statuses that STATUS_EDGES does not hold,
    written by the author, naming a replacement the store holds, and to
    STALE on a premise that holds, so that the edge alone refuses it: on
    the write path, where it costs the log nothing, and on replay by both
    readers."""
    src, dst = edge
    ev = EvidenceStore(tmp_path / "evidence")
    path = tmp_path / "log.jsonl"
    m = MemoryStore(EventLog(path), evidence=ev).load()
    dg = ev.put(b"its source")
    _remember(m, "m", author="alice", derived_from=(dg,))
    _to(m, "m", src, dg)
    if dst == "STALE" and src != "STALE":
        m.invalidate_source(dg, actor="system", reason="the move's premise")
    _remember(m, "other", author="alice", text="a later note")
    before = path.read_bytes()
    with pytest.raises(MemoryError_, match="may not move"):
        m._set_status("m", MemoryStatus(dst), actor="alice", reason="r",
                      superseded_by="other" if dst == "SUPERSEDED" else None)
    assert path.read_bytes() == before
    payload = {"memory_id": "m", "status": dst, "reason": "r"}
    if dst == "SUPERSEDED":
        payload["superseded_by"] = "other"
    m.log.append(actor="alice", action=ACT_MEMORY_STATUS, target="m",
                 payload=payload)
    with pytest.raises(MemoryError_, match="may not move"):
        MemoryStore(EventLog(path), evidence=ev).load()
    from agent_substrate.reconstruct import reconstruct_subsystems
    sub = reconstruct_subsystems(EventLog(path))
    assert any("'m'" in a and dst in a for a in sub.anomalies), sub.anomalies


# ==========================================================================
# THE LIFECYCLE FROM SEVERAL WRITERS (Wave B)
#
# Three writers, each a store of its own on one log, write a history
# between them, each catching up from its anchor when it writes and when it
# is asked, along the anchored route or the full read, and reopening. After
# every operation, every writer holds what a load of the history up to its
# own position holds, read past its public view; a fresh load of the whole
# history agrees with the second reader; and no current entry rests on an
# invalidated source.
# ==========================================================================
_W_IDS = ("m0", "m1", "m2", "m3", "m4")
_W_ACTORS = ("alice", "bob")


def _w_history():
    from hypothesis import strategies as st
    w, mid, who = (st.integers(0, 2), st.sampled_from(_W_IDS),
                   st.sampled_from(_W_ACTORS))
    # An entry a move names is an index into the entries the log holds when
    # the move is made, and its actor is often that entry's author, so that
    # moves are made and not only refused (see _w_resolve).
    held = st.integers(0, 4)
    mover = st.sampled_from(_W_ACTORS + ("the author", "the author"))
    # Plain lists, made distinct where they are used: Hypothesis 6.158.1
    # asserted inside its own unique-list draw while shrinking this search.
    refs = st.lists(st.sampled_from(_W_IDS), max_size=2)
    srcs = st.lists(st.integers(0, 2), max_size=2)
    kinds = st.sampled_from(["remember"] * 3 + ["summarize"] * 2
                            + ["retract"] * 2 + ["supersede"] * 2
                            + ["invalidate"] * 2 + ["catch_up"] * 2
                            + ["reopen"])
    args = {
        "remember": st.tuples(st.just("remember"), w, mid, who, srcs, refs),
        "summarize": st.tuples(st.just("summarize"), w, mid, who,
                               st.lists(held, min_size=1, max_size=2)),
        "retract": st.tuples(st.just("retract"), w, held, mover),
        "supersede": st.tuples(st.just("supersede"), w, held, held, mover),
        "invalidate": st.tuples(st.just("invalidate"), w, st.integers(0, 2)),
        "catch_up": st.tuples(st.just("catch_up"), w,
                              st.sampled_from(["anchored", "full"])),
        "reopen": st.tuples(st.just("reopen"), w),
    }
    return st.lists(kinds.flatmap(args.__getitem__), min_size=6, max_size=24)


def _w_held(m) -> dict:
    return {"entries": {k: e.to_record() for k, e in sorted(m._entries.items())},
            "by_source": {s: sorted(v)
                          for s, v in sorted(m._by_source.items())},
            "invalidated": sorted(m._invalidated),
            "position": (m._folded_through, m._folded_hash)}


def _w_load_through(path, seq, ev, cache):
    """What a load of the history up to ``seq`` alone holds: the log's first
    records, with a witness naming the last of them."""
    if seq in cache:
        return cache[seq]
    import json

    from agent_substrate.events import ChainState
    cut = path.parent / f"through-{seq}" / "log.jsonl"
    cut.parent.mkdir()
    lines = path.read_bytes().split(b"\n")[:seq + 1] if seq >= 0 else []
    cut.write_bytes(b"".join(line + b"\n" for line in lines))
    log = EventLog(cut)
    if seq >= 0:
        head = json.loads(lines[-1])
        log._write_head(ChainState(seq, head["hash"]))
    cache[seq] = _w_held(MemoryStore(log, evidence=ev).load())
    return cache[seq]


def _w_rests_on(store, entry) -> bool:
    """This test's own reading of what an entry rests on: its sources and
    the entries it names, followed through the entries the store holds."""
    seen, frontier = set(), [entry]
    while frontier:
        e = frontier.pop()
        if e.memory_id in seen:
            continue
        seen.add(e.memory_id)
        for ref in tuple(e.derived_from) + tuple(e.summarizes):
            if ref in store._invalidated:
                return True
            if ref in store._entries:
                frontier.append(store._entries[ref])
    return False


def _w_records(path, start) -> list:
    """What the log holds from line ``start`` on, as written: who, what,
    on what, with what."""
    import json
    lines = path.read_bytes().splitlines() if path.exists() else []
    return [(r["actor"], r["action"], r["target"], r["payload"])
            for r in map(json.loads, lines[start:])]


def _w_fresh_copy(path, ev, scratch):
    """A store just loaded from a copy of the log, to decide beside it."""
    import shutil
    scratch.mkdir()
    for f in (path, path.with_suffix(path.suffix + ".head")):
        if f.exists():
            shutil.copy(f, scratch / f.name)
    return MemoryStore(EventLog(scratch / path.name), evidence=ev).load()


def _w_resolve(op, held):
    """``op`` with what it names resolved against the entries the log holds
    now: an index picks one of them, and "the author" is the author of the
    entry the move names. A remembered entry's references stay as drawn, so
    that one may name a memory not yet written."""
    ids = sorted(held)

    def pick(i):
        return ids[i % len(ids)] if ids else _W_IDS[i % len(_W_IDS)]

    def actor(who, mid):
        if who != "the author":
            return who
        return held[mid].author if mid in held else _W_ACTORS[0]

    kind = op[0]
    if kind == "summarize":
        return op[:4] + ([pick(i) for i in op[4]],)
    if kind == "retract":
        return (kind, op[1], pick(op[2]), actor(op[3], pick(op[2])))
    if kind == "supersede":
        return (kind, op[1], pick(op[2]), pick(op[3]),
                actor(op[4], pick(op[2])))
    return op


def _w_run(m, op, ev, sources, writers=None, path=None):
    """Run ``op`` on ``m``; True when it was refused."""
    kind = op[0]
    try:
        if kind == "remember":
            _, _, mid, who, srcs, refs = op
            m.remember(memory_id=mid, text=f"{mid} by {who}", author=who,
                       derived_from=tuple(dict.fromkeys(
                           [sources[i] for i in srcs] + list(refs))))
        elif kind == "summarize":
            _, _, mid, who, refs = op
            m.summarize(memory_id=mid, text=f"summary {mid} by {who}",
                        author=who, summarizes=tuple(dict.fromkeys(refs)))
        elif kind == "retract":
            _, _, mid, who = op
            m.retract(mid, actor=who, reason="withdrawn")
        elif kind == "supersede":
            _, _, old, new, who = op
            m.supersede(old_id=old, new_id=new, actor=who, reason="restated")
        elif kind == "invalidate":
            m.invalidate_source(sources[op[2]], actor="system",
                                reason="rerun differed")
        elif kind == "catch_up":
            if op[2] == "full":
                m._anchor = None
            m._fold_new()
        elif kind == "reopen":
            writers[op[1]] = MemoryStore(EventLog(path), evidence=ev).load()
    except MemoryError_:
        return True
    return False


def _w_apply(writers, op, path, ev, sources, scratch) -> bool:
    """Run one operation by its writer, and a write by a store just loaded
    too, on a copy of the log: the two decide alike and write alike, and a
    refusal costs the log nothing. True when it was refused."""
    m = writers[op[1]]
    start = len(path.read_bytes().splitlines()) if path.exists() else 0
    beside = None
    if op[0] not in ("catch_up", "reopen"):
        f = _w_fresh_copy(path, ev, scratch)
        op = _w_resolve(op, f._entries)
        beside = (_w_run(f, op, ev, sources),
                  _w_records(f.log.path, start))
    refused = _w_run(m, op, ev, sources, writers, path)
    wrote = _w_records(path, start)
    if refused:
        assert wrote == [], op
    if beside is not None:
        assert (refused, wrote) == beside, op
    return refused


def _w_agrees(d, ops):
    """Run ``ops``; check after each one. Returns the final load."""
    ev = EvidenceStore(d / "evidence")
    path = d / "log.jsonl"
    sources = [ev.put(f"source {i}".encode()) for i in range(3)]
    writers = [MemoryStore(EventLog(path), evidence=ev).load()
               for _ in range(3)]
    cache: dict = {}
    fresh, read = MemoryStore(EventLog(path), evidence=ev).load(), b""
    for i, op in enumerate(ops):
        _w_apply(writers, op, path, ev, sources, d / f"beside-{i}")
        for m in writers:
            assert _w_held(m) == _w_load_through(
                path, m._folded_through, ev, cache), op
        now = path.read_bytes() if path.exists() else b""
        if now == read:
            continue                      # nothing written: nothing to read
        fresh, read = MemoryStore(EventLog(path), evidence=ev).load(), now
        assert _second(fresh) == {e.memory_id: e.status.value
                                  for e in fresh.all_entries()}
        assert not [e.memory_id for e in fresh.current()
                    if _w_rests_on(fresh, e)], op
    for m in writers:
        m._fold_new()
        assert _w_held(m) == _w_held(fresh)
    return fresh


def test_a_lifecycle_written_by_several_writers_agrees_everywhere(
        tmp_path_factory):
    """Wave B. Every writer holds what a load of the history up to its own
    position holds, after every operation; the second reader agrees with a
    fresh load; and nothing current rests on an invalidated source."""
    from hypothesis import HealthCheck, given, settings

    @settings(max_examples=200, deadline=None, derandomize=True,
              database=None, suppress_health_check=[HealthCheck.too_slow])
    @given(ops=_w_history())
    def run(ops):
        _w_agrees(tmp_path_factory.mktemp("lifecycle"), ops)

    run()
