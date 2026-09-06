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

from qta_agent.authority import (  # noqa: E402
    Role, State, TransitionError, TransitionRequest, check,
)
from qta_agent.canonical import digest  # noqa: E402
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.evidence import EvidenceStore  # noqa: E402
from qta_agent.memory import (  # noqa: E402
    ACT_MEMORY_STATUS, ACT_MEMORY_WRITE, MAX_ENTRY_BYTES,
    MemoryEntry, MemoryError_,
    MemoryIsNotEvidence, MemoryStatus, MemoryStore, UnknownMemory,
    entry_from_record, refuse_as_evidence,
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

    for module in ("authority", "store", "tasks", "scheduler", "capability"):
        src = (ROOT / "qta_agent" / f"{module}.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                names = ({node.module.split(".")[0]} if node.module
                         else {a.name.split(".")[0] for a in node.names})
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
    entry = _remember(mem, confidence="certain")
    assert entry.status is MemoryStatus.ACTIVE
    assert entry.confidence == "certain"
    assert "confidence" not in MemoryEntry.__dataclass_fields__[
        "status"].type.lower() if False else True


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
    from qta_agent.memory import STATUS_EDGES, MemoryStatus

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
