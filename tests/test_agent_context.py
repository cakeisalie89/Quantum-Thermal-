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


def test_a_replaced_source_is_recorded_as_an_omission_with_a_pointer():
    """The quiet failure: a summary carried forward as the record."""
    long_text = "the full derivation, at length"
    b = _full(_builder())
    b.add(item_id="s", tier=Tier.RETRIEVED_EVIDENCE, text="in short: fine",
          summary_of=digest_bytes(long_text.encode()),
          summarizes_item="long")
    ctx = b.build(budget_bytes=100_000)
    omitted = ctx.manifest.was_omitted("long")
    assert omitted is not None
    assert omitted.summarized_by == "s"
    assert "replaced by a summary" in omitted.reason
    assert ctx.manifest.answers()["summaries"] == {
        "s": digest_bytes(long_text.encode())}


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
