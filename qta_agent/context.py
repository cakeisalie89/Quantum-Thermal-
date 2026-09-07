"""Context: what the agent was shown, recorded apart from what is true.

THREE DIFFERENT CLAIMS, KEPT APART

People say "reproducible" about agent runs and mean one of three things. They
have different costs and different guarantees, and conflating them is how a
system ends up claiming the strongest one while implementing the weakest:

context reconstruction
    Given the log, say WHAT WAS AVAILABLE when an action was proposed: which
    policy version, which evidence digests, which task state, what was left
    out and why. This module provides that, and it is the useful one during an
    incident.

model-output reproducibility
    Given the same context, get the same tokens back. This module does NOT
    provide that and does not pretend to: sampling, model version, and
    provider-side changes are not captured here, and capturing a prompt does
    not capture them.

authoritative workflow replay
    Rebuild the authority state from the log. That is
    :mod:`qta_agent.reconstruct`, it does not involve a model at all, and it
    is the only one of the three that decides anything.

THE PROMPT IS NOT STATE

A model context is a VIEW assembled for one decision. It is derived from
authoritative state and never becomes it. The practical failure this prevents
is the quiet one: a summary written into the context, carried forward, and
eventually treated as the thing it summarized. Here a summary must name the
digest of its source, and the source stays in the manifest as an omission with
a pointer -- so "what did that summary compress" is answerable rather than
lost.

BUDGET WITHOUT SILENT LOSS

Context is finite and evidence is not. Dropping the overflow is the obvious
implementation and the wrong one, because the thing dropped is invisible
afterwards. So:

  * mandatory tiers (owner instruction, policy, authority state, the task's
    own evidence) are never dropped -- if they do not fit, the build FAILS,
    because a decision made without the policy in force is not a smaller
    decision, it is a different one;
  * discretionary material that does not fit is recorded in the manifest as an
    omission, with its digest, so an auditor can see what was not shown.

NO SECRETS IN CONTEXT

A context that carries a credential has copied it into whatever the model
provider logs. :meth:`ContextBuilder.build` refuses when a redactor recognises
a registered secret value in any item.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .canonical import digest, digest_bytes, is_digest

ACT_CONTEXT_BUILD = "context.build"

#: Refused above this. A context nobody can inspect is not auditable.
MAX_ITEMS = 4096


class ContextError(Exception):
    """Base class. Every failure here is fail-closed."""


class ContextBudgetError(ContextError):
    """The mandatory material does not fit. Never resolved by dropping it."""


class Tier(str, Enum):
    """What KIND of thing an item is. Decides whether it may be dropped."""

    #: What the owner asked for. Never dropped, never summarized away.
    OWNER_INSTRUCTION = "OWNER_INSTRUCTION"
    #: The policy in force, by identity and digest.
    SYSTEM_POLICY = "SYSTEM_POLICY"
    #: The authority/task state the decision is about.
    TASK_STATE = "TASK_STATE"
    #: Evidence this task itself produced or must account for.
    TASK_EVIDENCE = "TASK_EVIDENCE"
    #: Retrieved from the governed corpus. Relevant, not mandatory.
    RETRIEVED_EVIDENCE = "RETRIEVED_EVIDENCE"
    #: Output of a tool this task ran.
    TOOL_RESULT = "TOOL_RESULT"
    #: What the model said last time. Untrusted input, kept as such.
    PRIOR_MODEL_OUTPUT = "PRIOR_MODEL_OUTPUT"
    #: Working notes. First to go.
    SCRATCH = "SCRATCH"
    #: A remembered statement. Present so it can be SEEN to be lower
    #: authority, not so it can be treated as higher.
    MEMORY = "MEMORY"


#: Tiers that may never be omitted. A decision missing any of these is not a
#: smaller decision; it is a different one, and it should fail loudly.
MANDATORY: frozenset = frozenset({
    Tier.OWNER_INSTRUCTION, Tier.SYSTEM_POLICY, Tier.TASK_STATE,
    Tier.TASK_EVIDENCE,
})

#: Drop order for discretionary material, least valuable first. Explicit so
#: that "what got cut" is a property of the design rather than of dict order.
DROP_ORDER: tuple = (Tier.SCRATCH, Tier.PRIOR_MODEL_OUTPUT, Tier.MEMORY,
                     Tier.RETRIEVED_EVIDENCE, Tier.TOOL_RESULT)


@dataclass(frozen=True)
class ContextItem:
    """One thing shown to the model, described by identity rather than text."""

    item_id: str
    tier: Tier
    #: Digest of the exact bytes shown. The manifest carries this, not the
    #: text: a manifest that stored prompts would be a second copy of every
    #: document the agent ever read, with a different retention policy.
    content_digest: str
    byte_len: int
    #: Where it came from: an evidence digest, a policy identity, a record id.
    source: str = ""
    #: When this item summarizes something, the digest of what it compressed.
    #: A summary without one is refused: it is an assertion in a trusted slot.
    summary_of: str | None = None

    def to_record(self) -> dict:
        return {"item_id": self.item_id, "tier": self.tier.value,
                "content_digest": self.content_digest,
                "byte_len": self.byte_len, "source": self.source,
                "summary_of": self.summary_of}


@dataclass(frozen=True)
class Omission:
    """Something that was NOT shown, and why. The point of the manifest."""

    item_id: str
    tier: Tier
    content_digest: str
    byte_len: int
    reason: str
    #: The item that summarizes it, when one was shown in its place.
    summarized_by: str | None = None

    def to_record(self) -> dict:
        return {"item_id": self.item_id, "tier": self.tier.value,
                "content_digest": self.content_digest,
                "byte_len": self.byte_len, "reason": self.reason,
                "summarized_by": self.summarized_by}


@dataclass(frozen=True)
class ContextManifest:
    """What was available, what was shown, and what was left out."""

    task_id: str
    purpose: str
    items: tuple = ()
    omissions: tuple = ()
    budget_bytes: int = 0
    used_bytes: int = 0
    #: Identity of the policy in force, e.g. ``scheduler.default@2``.
    policy_identity: str = ""
    policy_digest: str = ""
    at_seq: int = -1

    def to_record(self) -> dict:
        return {"task_id": self.task_id, "purpose": self.purpose,
                "items": [i.to_record() for i in self.items],
                "omissions": [o.to_record() for o in self.omissions],
                "budget_bytes": self.budget_bytes,
                "used_bytes": self.used_bytes,
                "policy_identity": self.policy_identity,
                "policy_digest": self.policy_digest, "at_seq": self.at_seq}

    def digest(self) -> str:
        return digest(self.to_record())

    def shown(self, tier: Tier) -> tuple:
        return tuple(i for i in self.items if i.tier is tier)

    def was_omitted(self, item_id: str) -> Omission | None:
        for o in self.omissions:
            if o.item_id == item_id:
                return o
        return None

    def answers(self) -> dict:
        """The question this manifest exists to answer, as data.

        'What evidence and policy was available when this action was
        proposed?' -- and, just as importantly, what was available and not
        shown.
        """
        return {
            "policy": self.policy_identity,
            "policy_digest": self.policy_digest,
            "evidence_shown": sorted(
                i.source or i.content_digest for i in self.items
                if i.tier in (Tier.TASK_EVIDENCE, Tier.RETRIEVED_EVIDENCE)),
            "evidence_withheld": sorted(
                o.content_digest for o in self.omissions
                if o.tier in (Tier.TASK_EVIDENCE, Tier.RETRIEVED_EVIDENCE)),
            "summaries": {i.item_id: i.summary_of for i in self.items
                          if i.summary_of},
        }


@dataclass
class _Pending:
    item_id: str
    tier: Tier
    text: str
    source: str
    summary_of: str | None
    summarizes_item: str | None


@dataclass(frozen=True)
class Context:
    """The assembled view. Held in memory; never the authority for anything."""

    manifest: ContextManifest
    parts: tuple = field(default_factory=tuple)

    def text(self) -> str:
        return "\n\n".join(self.parts)

    def __repr__(self) -> str:
        # Not the content: a context repr in a log would be the prompt, and
        # the prompt is the thing this module keeps out of durable storage.
        return (f"<Context task={self.manifest.task_id!r} "
                f"items={len(self.manifest.items)} "
                f"omitted={len(self.manifest.omissions)}>")


class ContextBuilder:
    """Assembles a context explicitly, and records what it left out."""

    def __init__(self, *, task_id: str, purpose: str,
                 policy_identity: str = "", policy_digest: str = "",
                 at_seq: int = -1):
        if not task_id:
            raise ContextError("a context must be built for a task")
        if not purpose:
            raise ContextError(
                "a context must state its purpose; 'what was this assembled "
                "for' is the first question asked of it afterwards")
        self.task_id = task_id
        self.purpose = purpose
        self.policy_identity = policy_identity
        self.policy_digest = policy_digest
        self.at_seq = at_seq
        self._pending: list = []
        self._ids: set = set()

    def add(self, *, item_id: str, tier: Tier, text: str, source: str = "",
            summary_of: str | None = None,
            summarizes_item: str | None = None) -> "ContextBuilder":
        """Add material. Summaries must name what they compress."""
        if not isinstance(tier, Tier):
            raise ContextError(f"tier must be a Tier, got {tier!r}")
        if not isinstance(item_id, str) or not item_id:
            raise ContextError("item_id must be a non-empty str")
        if item_id in self._ids:
            raise ContextError(
                f"context item {item_id!r} added twice; two items with one id "
                "make the manifest unable to say which was shown")
        if not isinstance(text, str):
            raise ContextError(
                f"context item {item_id!r} must be text; the manifest records "
                "a digest of the exact bytes shown, and there are none "
                f"for {type(text).__name__}")
        if len(self._pending) >= MAX_ITEMS:
            raise ContextError(
                f"context would exceed {MAX_ITEMS} items")
        if summary_of is not None and not is_digest(summary_of):
            raise ContextError(
                f"context item {item_id!r} claims to summarize "
                f"{summary_of!r}, which is not a digest. A summary that "
                "cannot name what it compressed is an assertion occupying a "
                "trusted slot.")
        if summarizes_item is not None and summary_of is None:
            raise ContextError(
                f"context item {item_id!r} replaces {summarizes_item!r} but "
                "names no source digest")
        self._ids.add(item_id)
        self._pending.append(_Pending(item_id, tier, text, source,
                                      summary_of, summarizes_item))
        return self

    def build(self, *, budget_bytes: int, redactor=None) -> Context:
        """Assemble within the budget. Mandatory material is never dropped."""
        if (not isinstance(budget_bytes, int)
                or isinstance(budget_bytes, bool) or budget_bytes <= 0):
            raise ContextError("budget_bytes must be a positive int")

        if redactor is not None:
            for p in self._pending:
                if redactor.contains_secret(p.text):
                    raise ContextError(
                        f"refusing to build a context: item {p.item_id!r} "
                        "contains a registered secret value. A context "
                        "carrying a credential has copied it into whatever "
                        "the model provider retains.")

        sized = [(p, len(p.text.encode("utf-8"))) for p in self._pending]
        mandatory = [(p, n) for p, n in sized if p.tier in MANDATORY]
        discretionary = [(p, n) for p, n in sized if p.tier not in MANDATORY]

        need = sum(n for _, n in mandatory)
        if need > budget_bytes:
            raise ContextBudgetError(
                f"mandatory context is {need} bytes and the budget is "
                f"{budget_bytes}. Refusing to drop the owner's instruction, "
                "the policy in force, the task state or the task's own "
                "evidence: a decision made without them is not a smaller "
                "decision, it is a different one.")

        # Discretionary material is dropped in a declared order, and the
        # LEAST valuable tier goes first regardless of the order it was added
        # in. Dropping by insertion order would make the cut depend on the
        # caller's loop rather than on what the material is worth.
        rank = {t: i for i, t in enumerate(DROP_ORDER)}
        keepable = sorted(
            discretionary,
            key=lambda pair: (rank.get(pair[0].tier, len(DROP_ORDER)),
                              pair[0].item_id))
        used = need
        kept: list = []
        omitted: list = []
        # Highest value first: reverse the drop order.
        for p, n in reversed(keepable):
            if used + n <= budget_bytes:
                kept.append((p, n))
                used += n
            else:
                omitted.append((p, n, "did not fit within the context budget"))

        chosen = mandatory + kept
        chosen.sort(key=lambda pair: (_tier_order(pair[0].tier),
                                      pair[0].item_id))

        items = tuple(
            ContextItem(item_id=p.item_id, tier=p.tier,
                        content_digest=digest_bytes(p.text.encode("utf-8")),
                        byte_len=n, source=p.source, summary_of=p.summary_of)
            for p, n in chosen)
        summarized_by = {p.summarizes_item: p.item_id
                         for p, _ in chosen if p.summarizes_item}
        omissions = [
            Omission(item_id=p.item_id, tier=p.tier,
                     content_digest=digest_bytes(p.text.encode("utf-8")),
                     byte_len=n, reason=why,
                     summarized_by=summarized_by.get(p.item_id))
            for p, n, why in omitted]
        # A source replaced by a summary is an omission even though it fitted:
        # the manifest must say the full text was available and not shown, or
        # the summary silently becomes the record.
        shown_ids = {p.item_id for p, _ in chosen}
        for replaced, by in sorted(summarized_by.items()):
            if replaced in shown_ids or replaced is None:
                continue
            if any(o.item_id == replaced for o in omissions):
                continue
            src = next((p for p, _ in sized if p.item_id == replaced), None)
            omissions.append(Omission(
                item_id=replaced,
                tier=src.tier if src else Tier.RETRIEVED_EVIDENCE,
                content_digest=(digest_bytes(src.text.encode("utf-8"))
                                if src else ""),
                byte_len=len(src.text.encode("utf-8")) if src else 0,
                reason="replaced by a summary; the full text was not shown",
                summarized_by=by))
        omissions.sort(key=lambda o: o.item_id)

        manifest = ContextManifest(
            task_id=self.task_id, purpose=self.purpose, items=items,
            omissions=tuple(omissions), budget_bytes=budget_bytes,
            used_bytes=used, policy_identity=self.policy_identity,
            policy_digest=self.policy_digest, at_seq=self.at_seq)
        return Context(manifest=manifest,
                       parts=tuple(p.text for p, _ in chosen))


def _tier_order(tier: Tier) -> int:
    """Presentation order: mandatory material first, scratch last."""
    order = (Tier.OWNER_INSTRUCTION, Tier.SYSTEM_POLICY, Tier.TASK_STATE,
             Tier.TASK_EVIDENCE, Tier.TOOL_RESULT, Tier.RETRIEVED_EVIDENCE,
             Tier.MEMORY, Tier.PRIOR_MODEL_OUTPUT, Tier.SCRATCH)
    return order.index(tier)


def record_context(log, manifest: ContextManifest, *, actor: str):
    """Append the MANIFEST, never the context itself.

    The manifest is digests and identities. Storing the assembled text would
    put every document the agent ever read into the authority log, under the
    log's retention rather than the corpus's -- and would make the log the
    place a leaked prompt lives forever.
    """
    return log.append(actor=actor, action=ACT_CONTEXT_BUILD,
                      target=manifest.task_id,
                      payload={"manifest": manifest.to_record(),
                               "manifest_digest": manifest.digest()})


def manifest_from_record(rec: dict) -> ContextManifest:
    """Rebuild a manifest from a log payload, validating its shape."""
    if not isinstance(rec, dict):
        raise ContextError(f"context manifest is {type(rec).__name__}")
    known = {"task_id", "purpose", "items", "omissions", "budget_bytes",
             "used_bytes", "policy_identity", "policy_digest", "at_seq"}
    unknown = set(rec) - known
    if unknown:
        raise ContextError(
            f"context manifest carries unknown fields {sorted(unknown)}; "
            "refusing to read a manifest this version does not fully "
            "understand")
    try:
        items = tuple(
            ContextItem(item_id=i["item_id"], tier=Tier(i["tier"]),
                        content_digest=i["content_digest"],
                        byte_len=i["byte_len"], source=i.get("source", ""),
                        summary_of=i.get("summary_of"))
            for i in rec["items"])
        omissions = tuple(
            Omission(item_id=o["item_id"], tier=Tier(o["tier"]),
                     content_digest=o["content_digest"],
                     byte_len=o["byte_len"], reason=o["reason"],
                     summarized_by=o.get("summarized_by"))
            for o in rec["omissions"])
        return ContextManifest(
            task_id=rec["task_id"], purpose=rec["purpose"], items=items,
            omissions=omissions, budget_bytes=rec["budget_bytes"],
            used_bytes=rec["used_bytes"],
            policy_identity=rec.get("policy_identity", ""),
            policy_digest=rec.get("policy_digest", ""),
            at_seq=rec.get("at_seq", -1))
    except (KeyError, TypeError, ValueError) as exc:
        raise ContextError(f"context manifest is malformed: {exc}") from exc
