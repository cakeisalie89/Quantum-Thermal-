"""Context: what was shown, what was not, and the claims not being made.

The tests fall into three groups. The first checks that the manifest can
answer the question it exists for. The second checks that nothing mandatory
can be dropped to make a prompt fit. The third checks the claims the module
explicitly does NOT make, because a module that quietly implies model
reproducibility will be relied on for it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qta_agent.canonical import digest_bytes, is_digest  # noqa: E402
from qta_agent.context import (  # noqa: E402
    ACT_CONTEXT_BUILD, DROP_ORDER, MANDATORY, ContextBudgetError,
    ContextBuilder, ContextError, Tier, manifest_from_record, record_context,
)
from qta_agent.events import EventLog  # noqa: E402
from qta_agent.secrets import SecretStore  # noqa: E402

OWNER = "produce the Stage-10 artifact and do not touch any gate"
POLICY = "policy scheduler.default@2: dispatch allowed, escalation denied"


def _builder(**over):
    kw = dict(task_id="task-1", purpose="propose a Stage-10 run",
              policy_identity="scheduler.default@2",
              policy_digest="a" * 64, at_seq=42)
    kw.update(over)
    return ContextBuilder(**kw)


def _full(b):
    b.add(item_id="owner", tier=Tier.OWNER_INSTRUCTION, text=OWNER)
    b.add(item_id="policy", tier=Tier.SYSTEM_POLICY, text=POLICY,
          source="scheduler.default@2")
    b.add(item_id="state", tier=Tier.TASK_STATE, text="task-1 is QUEUED")
    b.add(item_id="ev", tier=Tier.TASK_EVIDENCE, text="artifact sha256 abc",
          source="b" * 64)
    return b


# ---- the manifest answers the question ----------------------------------
def test_the_manifest_says_what_was_available_and_what_was_shown():
    ctx = _full(_builder()).add(
        item_id="corpus", tier=Tier.RETRIEVED_EVIDENCE,
        text="a governed document", source="c" * 64).build(budget_bytes=4096)
    answers = ctx.manifest.answers()
    assert answers["policy"] == "scheduler.default@2"
    assert answers["policy_digest"] == "a" * 64
    assert answers["evidence_shown"] == sorted(["b" * 64, "c" * 64])
    assert answers["evidence_withheld"] == []


def test_items_are_recorded_by_digest_not_by_text():
    ctx = _full(_builder()).build(budget_bytes=4096)
    rec = ctx.manifest.to_record()
    flat = str(rec)
    assert OWNER not in flat, (
        "a manifest that stored prompts would be a second copy of every "
        "document the agent read, under the log's retention")
    owner = [i for i in ctx.manifest.items if i.item_id == "owner"][0]
    assert owner.content_digest == digest_bytes(OWNER.encode())
    assert is_digest(owner.content_digest)


def test_the_context_repr_is_not_the_prompt():
    ctx = _full(_builder()).build(budget_bytes=4096)
    assert OWNER not in repr(ctx)
    assert "items=4" in repr(ctx)


def test_the_assembled_text_is_available_to_the_caller_only():
    ctx = _full(_builder()).build(budget_bytes=4096)
    assert OWNER in ctx.text() and POLICY in ctx.text()


def test_the_manifest_digest_covers_the_omissions():
    a = _full(_builder()).add(item_id="x", tier=Tier.SCRATCH,
                              text="y" * 500).build(budget_bytes=4096)
    b = _full(_builder()).add(item_id="x", tier=Tier.SCRATCH,
                              text="y" * 500).build(budget_bytes=300)
    assert a.manifest.digest() != b.manifest.digest()


def test_a_manifest_survives_a_round_trip(tmp_path):
    ctx = _full(_builder()).add(item_id="scratch", tier=Tier.SCRATCH,
                                text="z" * 900).build(budget_bytes=260)
    rec = ctx.manifest.to_record()
    assert manifest_from_record(rec).digest() == ctx.manifest.digest()


def test_a_manifest_with_unknown_fields_is_refused():
    ctx = _full(_builder()).build(budget_bytes=4096)
    rec = ctx.manifest.to_record()
    rec["trust_me"] = True
    with pytest.raises(ContextError, match="unknown fields"):
        manifest_from_record(rec)


@pytest.mark.parametrize("bad", [None, [], "manifest", {"task_id": "t"}])
def test_malformed_manifests_fail_closed(bad):
    with pytest.raises(ContextError):
        manifest_from_record(bad)


def test_only_the_manifest_is_appended_to_the_log(tmp_path):
    log = EventLog(tmp_path / "log.jsonl")
    ctx = _full(_builder()).build(budget_bytes=4096)
    record_context(log, ctx.manifest, actor="agent-1")
    (ev,) = [e for e in log.read() if e.action == ACT_CONTEXT_BUILD]
    assert OWNER not in str(ev.payload)
    assert ev.payload["manifest_digest"] == ctx.manifest.digest()


# ---- the budget ----------------------------------------------------------
def test_mandatory_material_is_never_dropped_to_make_room():
    b = _full(_builder())
    with pytest.raises(ContextBudgetError, match="not a smaller decision"):
        b.build(budget_bytes=10)


def test_every_mandatory_tier_is_actually_mandatory():
    assert MANDATORY == {Tier.OWNER_INSTRUCTION, Tier.SYSTEM_POLICY,
                         Tier.TASK_STATE, Tier.TASK_EVIDENCE}
    assert not (MANDATORY & set(DROP_ORDER)), (
        "a tier that is both mandatory and droppable is droppable")


def test_discretionary_material_that_does_not_fit_is_recorded_as_omitted():
    ctx = _full(_builder()).add(
        item_id="corpus", tier=Tier.RETRIEVED_EVIDENCE, text="q" * 4000,
        source="c" * 64).build(budget_bytes=300)
    omitted = ctx.manifest.was_omitted("corpus")
    assert omitted is not None
    assert "did not fit" in omitted.reason
    assert omitted.content_digest == digest_bytes(("q" * 4000).encode())
    assert ctx.manifest.answers()["evidence_withheld"] == [
        digest_bytes(("q" * 4000).encode())]


def test_the_cheapest_tier_is_dropped_first_regardless_of_add_order():
    b = _full(_builder())
    b.add(item_id="scratch", tier=Tier.SCRATCH, text="s" * 200)
    b.add(item_id="corpus", tier=Tier.RETRIEVED_EVIDENCE, text="c" * 200)
    ctx = b.build(budget_bytes=len(OWNER) + len(POLICY) + 60 + 220)
    kept = {i.item_id for i in ctx.manifest.items}
    assert "corpus" in kept and "scratch" not in kept, (
        "dropping by insertion order would make the cut depend on the "
        "caller's loop rather than on what the material is worth")


def test_a_context_that_fits_omits_nothing():
    ctx = _full(_builder()).build(budget_bytes=100_000)
    assert ctx.manifest.omissions == ()
    assert ctx.manifest.used_bytes <= ctx.manifest.budget_bytes


@pytest.mark.parametrize("budget", [0, -1, True, 1.5, "4096"])
def test_the_budget_must_be_a_positive_int(budget):
    with pytest.raises(ContextError, match="positive int"):
        _full(_builder()).build(budget_bytes=budget)


# ---- summaries -----------------------------------------------------------
def test_a_summary_must_name_the_digest_of_what_it_compressed():
    b = _builder()
    with pytest.raises(ContextError, match="assertion occupying a trusted"):
        b.add(item_id="s", tier=Tier.RETRIEVED_EVIDENCE, text="in short",
              summary_of="not-a-digest")


def test_replacing_an_item_with_a_summary_needs_the_source_digest():
    b = _builder()
    with pytest.raises(ContextError, match="names no source digest"):
        b.add(item_id="s", tier=Tier.RETRIEVED_EVIDENCE, text="in short",
              summarizes_item="long")


LONG = "the full derivation, at length, " * 8
SHORT = "in short: fine"


def _summarized(b, *, source=True, summary_of=None, summary_first=False):
    """Add a real source 'long' and a summary 's' of it, in either order."""
    def add_source():
        b.add(item_id="long", tier=Tier.RETRIEVED_EVIDENCE, text=LONG)

    def add_summary():
        b.add(item_id="s", tier=Tier.RETRIEVED_EVIDENCE, text=SHORT,
              summary_of=(summary_of if summary_of is not None
                          else digest_bytes(LONG.encode())),
              summarizes_item="long")
    if source and not summary_first:
        add_source()
    add_summary()
    if source and summary_first:
        add_source()
    return b


def _room_for_the_summary_only():
    """A budget the mandatory material plus the summary fit in, not LONG."""
    base = _full(_builder()).build(budget_bytes=100_000).manifest.used_bytes
    return base + len(SHORT.encode()) + 1


def test_a_replaced_source_is_recorded_as_an_omission_with_a_pointer():
    """The quiet failure: a summary carried forward as the record.

    REWRITTEN (D-2026-72). The previous version added a summary of 'long'
    and never added 'long'; the builder obliged by inventing an omission
    for it -- tier guessed, digest "", length 0 -- and this test asserted
    that invention. The source now exists, is left out for budget while its
    summary is shown, and the omission describes THE SOURCE'S OWN BYTES.
    """
    ctx = _summarized(_full(_builder())).build(
        budget_bytes=_room_for_the_summary_only())
    omitted = ctx.manifest.was_omitted("long")
    assert omitted is not None
    assert omitted.summarized_by == "s"
    assert omitted.content_digest == digest_bytes(LONG.encode())
    assert omitted.byte_len == len(LONG.encode())
    assert omitted.tier is Tier.RETRIEVED_EVIDENCE
    assert ctx.manifest.answers()["summaries"] == {
        "s": digest_bytes(LONG.encode())}


def test_a_summary_of_an_item_that_was_never_added_is_refused():
    """THE defect: provenance for material that does not exist."""
    b = _summarized(_full(_builder()), source=False)
    with pytest.raises(ContextError, match="not in this context"):
        b.build(budget_bytes=100_000)


def test_a_summary_whose_digest_is_not_its_source_is_refused():
    """The pointer names the item; the digest must name its exact bytes."""
    b = _summarized(_full(_builder()),
                    summary_of=digest_bytes(b"some other document"))
    with pytest.raises(ContextError, match="digest to"):
        b.build(budget_bytes=100_000)


def test_a_digest_with_no_source_item_is_refused():
    """summary_of alone vouches for bytes the builder never held."""
    b = _builder()
    with pytest.raises(ContextError, match="names no source item"):
        b.add(item_id="s", tier=Tier.RETRIEVED_EVIDENCE, text=SHORT,
              summary_of=digest_bytes(LONG.encode()))


def test_a_summary_cannot_be_its_own_source():
    """Even with the digest right: a digest of itself proves nothing."""
    b = _builder()
    with pytest.raises(ContextError, match="another, named item"):
        b.add(item_id="s", tier=Tier.RETRIEVED_EVIDENCE, text=SHORT,
              summary_of=digest_bytes(SHORT.encode()), summarizes_item="s")


def test_two_summaries_of_one_source_are_refused():
    b = _summarized(_full(_builder()))
    b.add(item_id="s2", tier=Tier.RETRIEVED_EVIDENCE, text="also short",
          summary_of=digest_bytes(LONG.encode()), summarizes_item="long")
    with pytest.raises(ContextError, match="summarized by both"):
        b.build(budget_bytes=100_000)


def test_a_summary_may_be_added_before_its_source():
    """Control: the check is at build, so order of addition is free."""
    ctx = _summarized(_full(_builder()), summary_first=True).build(
        budget_bytes=_room_for_the_summary_only())
    assert ctx.manifest.was_omitted("long").summarized_by == "s"


def test_when_both_fit_nothing_is_omitted_or_invented():
    """Control: a summary that did not need to replace anything."""
    ctx = _summarized(_full(_builder())).build(budget_bytes=100_000)
    assert ctx.manifest.omissions == ()
    assert {i.item_id for i in ctx.manifest.items} >= {"long", "s"}


def _good_record():
    return _summarized(_full(_builder())).build(
        budget_bytes=_room_for_the_summary_only()).manifest.to_record()


def test_a_real_replacement_survives_a_round_trip():
    """Control for the three refusals below."""
    rec = _good_record()
    assert manifest_from_record(rec).was_omitted("long").summarized_by == "s"


def test_a_manifest_carrying_an_invented_omission_is_refused():
    """What the old builder wrote into logs: digest "", length 0."""
    rec = _good_record()
    rec["omissions"].append({
        "item_id": "phantom", "tier": Tier.RETRIEVED_EVIDENCE.value,
        "content_digest": "", "byte_len": 0,
        "reason": "replaced by a summary; the full text was not shown",
        "summarized_by": "s"})
    with pytest.raises(ContextError, match="existing material has a digest"):
        manifest_from_record(rec)


def test_a_manifest_pointing_at_no_shown_summary_is_refused():
    rec = _good_record()
    rec["omissions"][0]["summarized_by"] = "nobody"
    with pytest.raises(ContextError, match="not a shown summary"):
        manifest_from_record(rec)


def test_a_manifest_whose_summary_names_other_bytes_is_refused():
    """Refused at the SUMMARY now, before the omission is looked at: since
    D-2026-75 the item names its source, so the mismatch is visible there."""
    rec = _good_record()
    for item in rec["items"]:
        if item["item_id"] == "s":
            item["summary_of"] = digest_bytes(b"something else")
    with pytest.raises(ContextError, match="recorded with digest"):
        manifest_from_record(rec)


# ---- D-2026-75: the summary relationship is durable ----------------------
#
# The builder checks a summary against its source at construction time. The
# durable item used to keep only the digest, so a manifest in which BOTH the
# source and its summary were shown -- no omission to lean on -- could not
# prove, once read back, that the digest was the source's. Every case below
# is decided from the serialized record alone.

def _both_shown_record():
    ctx = _summarized(_full(_builder())).build(budget_bytes=100_000)
    assert ctx.manifest.omissions == (), "both must be shown for this case"
    return ctx.manifest.to_record()


def _item(rec, item_id):
    return next(i for i in rec["items"] if i["item_id"] == item_id)


def test_a_both_shown_manifest_round_trips_with_its_source():
    """Control for every refusal below."""
    rec = _both_shown_record()
    assert rec["manifest_version"] == 2
    assert _item(rec, "s")["summarizes_item"] == "long"
    m = manifest_from_record(rec)
    assert m.digest() == manifest_from_record(rec).digest()


def test_a_tampered_digest_on_a_shown_summary_is_refused():
    """THE anti-vacuity case: nothing is omitted, and the reader must still
    see that the digest is not the named source's. Fails against the
    version-1 reader, which had nothing to compare it with."""
    rec = _both_shown_record()
    _item(rec, "s")["summary_of"] = digest_bytes(b"another document")
    with pytest.raises(ContextError, match="recorded with digest"):
        manifest_from_record(rec)


def _two_sources_record():
    """Two sources and two summaries, all shown: room to swap things."""
    b = _full(_builder())
    b.add(item_id="x1", tier=Tier.RETRIEVED_EVIDENCE, text="first source")
    b.add(item_id="x2", tier=Tier.RETRIEVED_EVIDENCE, text="second source")
    b.add(item_id="s1", tier=Tier.RETRIEVED_EVIDENCE, text="short 1",
          summary_of=digest_bytes(b"first source"), summarizes_item="x1")
    b.add(item_id="s2", tier=Tier.RETRIEVED_EVIDENCE, text="short 2",
          summary_of=digest_bytes(b"second source"), summarizes_item="x2")
    return b.build(budget_bytes=100_000).manifest.to_record()


def _tamper(rec, fn):
    fn(rec)
    return rec


@pytest.mark.parametrize("case,edit,needle", [
    ("the source pointer points at nothing",
     lambda r: _item(r, "s1").update(summarizes_item="ghost"),
     "does not record as available"),
    ("the source pointer is removed and the digest kept",
     lambda r: _item(r, "s1").update(summarizes_item=None),
     "half a summary claim"),
    ("the digest is removed and the source pointer kept",
     lambda r: _item(r, "s1").update(summary_of=None),
     "half a summary claim"),
    ("a summary names itself",
     lambda r: _item(r, "s1").update(summarizes_item="s1"),
     "another, named item"),
    ("an empty source identifier",
     lambda r: _item(r, "s1").update(summarizes_item=""),
     "another, named item"),
    ("a non-string source identifier",
     lambda r: _item(r, "s1").update(summarizes_item=7),
     "another, named item"),
    ("a malformed digest",
     lambda r: _item(r, "s1").update(summary_of="not-a-digest"),
     "not a digest"),
    ("the digests are swapped between the two summaries",
     lambda r: (_item(r, "s1").update(summary_of=_item(r, "s2")["summary_of"]),
                _item(r, "s2").update(summary_of=digest_bytes(b"first source"))),
     "recorded with digest"),
    ("the source pointer is swapped and the digest kept",
     lambda r: _item(r, "s1").update(summarizes_item="x2"),
     "recorded with digest"),
    ("two summaries claim one source",
     lambda r: _item(r, "s2").update(summarizes_item="x1",
                                     summary_of=digest_bytes(b"first source")),
     "summarized by both"),
    ("a shown source's digest is forged to match a forged summary",
     lambda r: (_item(r, "x1").update(content_digest=digest_bytes(b"forged")),
                _item(r, "s2").update(summarizes_item="x1")),
     "recorded with digest"),
])
def test_the_reader_refuses_a_forged_summary_relationship(case, edit, needle):
    rec = _tamper(_two_sources_record(), edit)
    with pytest.raises(ContextError, match=needle):
        manifest_from_record(rec)


def test_an_omitted_source_pointing_at_another_summary_is_refused():
    """The omission names a real shown summary -- of something else."""
    rec = _good_record()
    rec["items"].append({"item_id": "other", "tier": "RETRIEVED_EVIDENCE",
                         "content_digest": digest_bytes(b"o"), "byte_len": 1,
                         "source": "", "summary_of": None,
                         "summarizes_item": None})
    rec["items"].append({"item_id": "s9", "tier": "RETRIEVED_EVIDENCE",
                         "content_digest": digest_bytes(b"s9"), "byte_len": 2,
                         "source": "", "summary_of": digest_bytes(b"o"),
                         "summarizes_item": "other"})
    rec["omissions"][0]["summarized_by"] = "s9"
    with pytest.raises(ContextError):
        manifest_from_record(rec)


def test_an_omission_borrowing_a_summary_of_identical_bytes_is_refused():
    """Two sources with the same bytes: a digest cannot tell them apart,
    so only the durable POINTER says which one the summary is of. The
    omitted copy claims a summary that names the shown copy."""
    same = "identical text in two places"
    b = _full(_builder())
    b.add(item_id="kept", tier=Tier.RETRIEVED_EVIDENCE, text=same)
    b.add(item_id="s", tier=Tier.RETRIEVED_EVIDENCE, text="short",
          summary_of=digest_bytes(same.encode()), summarizes_item="kept")
    rec = b.build(budget_bytes=100_000).manifest.to_record()
    rec["omissions"].append({
        "item_id": "dropped", "tier": "RETRIEVED_EVIDENCE",
        "content_digest": digest_bytes(same.encode()),
        "byte_len": len(same), "reason": "did not fit",
        "summarized_by": "s"})
    with pytest.raises(ContextError, match="names 'kept' as its source"):
        manifest_from_record(rec)


def test_an_omitted_source_that_hides_its_summary_is_refused():
    rec = _good_record()
    rec["omissions"][0]["summarized_by"] = None
    with pytest.raises(ContextError, match="does not say so"):
        manifest_from_record(rec)


def test_a_version_1_manifest_claiming_a_summary_is_refused():
    """The explicit compatibility rule: version 1 cannot prove a summary."""
    rec = _both_shown_record()
    del rec["manifest_version"]
    for i in rec["items"]:
        i.pop("summarizes_item")
    with pytest.raises(ContextError, match="version-1 manifest"):
        manifest_from_record(rec)


def test_a_version_1_manifest_without_summaries_still_reads():
    """Control: the compatibility rule refuses what it cannot prove, and
    only that."""
    rec = _full(_builder()).build(budget_bytes=4096).manifest.to_record()
    del rec["manifest_version"]
    for i in rec["items"]:
        i.pop("summarizes_item")
    assert manifest_from_record(rec).manifest_version == 1


@pytest.mark.parametrize("version", [3, 0, "2", True, None])
def test_an_unknown_manifest_version_is_refused(version):
    rec = _both_shown_record()
    rec["manifest_version"] = version
    with pytest.raises(ContextError, match="version"):
        manifest_from_record(rec)


def test_a_version_2_field_in_a_version_1_record_is_refused():
    rec = _both_shown_record()
    del rec["manifest_version"]
    with pytest.raises(ContextError, match="not a version-1 item"):
        manifest_from_record(rec)


# ---- secrets -------------------------------------------------------------
def test_a_context_carrying_a_secret_is_refused():
    store = SecretStore()
    store.register("api-token", "hunter2-super-secret-token-value")
    b = _full(_builder())
    b.add(item_id="leak", tier=Tier.TOOL_RESULT,
          text="the tool printed hunter2-super-secret-token-value")
    with pytest.raises(ContextError, match="registered secret value"):
        b.build(budget_bytes=100_000, redactor=store.redactor())


def test_a_clean_context_builds_with_a_redactor_attached():
    store = SecretStore()
    store.register("api-token", "hunter2-super-secret-token-value")
    ctx = _full(_builder()).build(budget_bytes=100_000,
                                  redactor=store.redactor())
    assert len(ctx.manifest.items) == 4


# ---- validation ----------------------------------------------------------
def test_a_context_must_state_its_task_and_purpose():
    with pytest.raises(ContextError, match="built for a task"):
        ContextBuilder(task_id="", purpose="p")
    with pytest.raises(ContextError, match="state its purpose"):
        ContextBuilder(task_id="t", purpose="")


def test_duplicate_item_ids_are_refused():
    b = _builder().add(item_id="x", tier=Tier.SCRATCH, text="a")
    with pytest.raises(ContextError, match="added twice"):
        b.add(item_id="x", tier=Tier.SCRATCH, text="b")


def test_non_text_material_is_refused():
    with pytest.raises(ContextError, match="must be text"):
        _builder().add(item_id="x", tier=Tier.SCRATCH, text={"a": 1})


def test_a_tier_must_be_a_tier():
    with pytest.raises(ContextError, match="must be a Tier"):
        _builder().add(item_id="x", tier="SCRATCH", text="a")


# ---- the claims NOT being made ------------------------------------------
def test_the_module_separates_the_three_reproducibility_claims():
    """A module that quietly implies model reproducibility is relied on
    for it."""
    import re

    import qta_agent.context as mod
    doc = re.sub(r"\s+", " ", mod.__doc__ or "")
    assert "context reconstruction" in doc
    assert "model-output reproducibility" in doc
    assert "This module does NOT provide that and does not pretend to" in doc
    assert "authoritative workflow replay" in doc


def test_memory_appears_in_context_as_a_lower_tier_not_as_evidence():
    b = _full(_builder())
    b.add(item_id="mem", tier=Tier.MEMORY, text="last time this was fine",
          source="memory:m1")
    ctx = b.build(budget_bytes=100_000)
    assert Tier.MEMORY not in MANDATORY
    assert ctx.manifest.answers()["evidence_shown"] == ["b" * 64], (
        "a remembered statement must not appear in the evidence answer; it "
        "would read to an auditor as something that was checked")
    assert [i.tier for i in ctx.manifest.shown(Tier.MEMORY)] == [Tier.MEMORY]


def test_prior_model_output_is_a_tier_of_its_own():
    """It is untrusted input, and the manifest says which items it was."""
    b = _full(_builder())
    b.add(item_id="prev", tier=Tier.PRIOR_MODEL_OUTPUT,
          text="I concluded the run is safe")
    ctx = b.build(budget_bytes=100_000)
    assert len(ctx.manifest.shown(Tier.PRIOR_MODEL_OUTPUT)) == 1
    assert ctx.manifest.answers()["evidence_shown"] == ["b" * 64]


def test_a_context_item_has_nowhere_to_put_the_text():
    """The structural half of the guarantee.

    ``test_items_are_recorded_by_digest_not_by_text`` checks the behaviour;
    this checks that the behaviour is not an accident of the current
    ``to_record``. A ``ContextItem`` carries a digest and a length and has no
    field the text could be stored in, so a manifest that leaked a prompt
    would require a schema change rather than a slip.
    """
    from qta_agent.context import ContextItem, Omission

    for cls in (ContextItem, Omission):
        fields = set(cls.__dataclass_fields__)
        assert not (fields & {"text", "content", "body", "prompt", "parts"}), (
            f"{cls.__name__} has somewhere to put the text: {sorted(fields)}")
    assert "byte_len" in ContextItem.__dataclass_fields__, (
        "the length is what a reader gets INSTEAD of the text; without it "
        "the manifest cannot describe what was shown at all")
