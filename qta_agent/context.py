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

#: Durable manifest schema. 2 records, on every summary item, WHICH item it
#: summarizes (D-2026-75). Version 1 recorded only the digest, so a manifest
#: in which the source and its summary were both shown could not prove, once
#: read back, that the digest was the source's: nothing named the source. A
#: version-1 record is still readable -- but only if it claims no summary at
#: all, because one that does cannot prove the claim.
MANIFEST_VERSION = 2


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
    #: WHICH item it compressed. Durable alongside the digest, so a reader
    #: holding only the manifest can check that the digest is that item's --
    #: the pair is the claim; either half alone is not.
    summarizes_item: str | None = None

    def to_record(self) -> dict:
        return {"item_id": self.item_id, "tier": self.tier.value,
                "content_digest": self.content_digest,
                "byte_len": self.byte_len, "source": self.source,
                "summary_of": self.summary_of,
                "summarizes_item": self.summarizes_item}


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
    manifest_version: int = MANIFEST_VERSION

    def to_record(self) -> dict:
        return {"manifest_version": self.manifest_version,
                "task_id": self.task_id, "purpose": self.purpose,
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
        if summary_of is not None and summarizes_item is None:
            # A digest with no source item is a claim about bytes this
            # builder was never given, so nothing here can check it. It
            # would stand in the manifest as provenance all the same.
            raise ContextError(
                f"context item {item_id!r} claims to summarize "
                f"{summary_of[:12]} but names no source item; the builder "
                "can only vouch for a digest of bytes it holds")
        if summarizes_item is not None and (
                not isinstance(summarizes_item, str) or not summarizes_item
                or summarizes_item == item_id):
            raise ContextError(
                f"context item {item_id!r} names {summarizes_item!r} as its "
                "source; a summary's source must be another, named item")
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

        self._check_summary_provenance()

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
                        byte_len=n, source=p.source, summary_of=p.summary_of,
                        summarizes_item=p.summarizes_item)
            for p, n in chosen)
        summarized_by = {p.summarizes_item: p.item_id
                         for p, _ in chosen if p.summarizes_item}
        # A source that was left out while its summary was shown carries a
        # pointer to that summary: the manifest must say the full text was
        # available and not shown, or the summary silently becomes the
        # record. Every source is a real item (checked above), so every
        # source is either shown or in this list -- there is no third case,
        # and in particular no omission to invent for a source nobody gave.
        #
        # There used to be one. A summary naming an item that was never
        # added produced an Omission for it anyway: tier guessed as
        # RETRIEVED_EVIDENCE, content digest "", length 0 -- a record,
        # written into the durable manifest, of material that never existed.
        omissions = [
            Omission(item_id=p.item_id, tier=p.tier,
                     content_digest=digest_bytes(p.text.encode("utf-8")),
                     byte_len=n, reason=why,
                     summarized_by=summarized_by.get(p.item_id))
            for p, n, why in omitted]
        omissions.sort(key=lambda o: o.item_id)

        manifest = ContextManifest(
            task_id=self.task_id, purpose=self.purpose, items=items,
            omissions=tuple(omissions), budget_bytes=budget_bytes,
            used_bytes=used, policy_identity=self.policy_identity,
            policy_digest=self.policy_digest, at_seq=self.at_seq)
        return Context(manifest=manifest,
                       parts=tuple(p.text for p, _ in chosen))

    def _check_summary_provenance(self) -> None:
        """Every summary names a real source and the digest of its bytes.

        Checked at build rather than at add, because a summary may be added
        before the item it compresses. Three things, each a way a summary
        could fabricate provenance:

        * the source exists -- no phantom item;
        * ``summary_of`` is the digest of the source's EXACT bytes, not a
          digest of something else that happens to be well-formed;
        * one source, one summary -- two would leave "which one replaced
          it" to dict order.
        """
        by_id = {p.item_id: p for p in self._pending}
        summarized: dict = {}
        for p in self._pending:
            if p.summarizes_item is None:
                continue
            src = by_id.get(p.summarizes_item)
            if src is None:
                raise ContextError(
                    f"context item {p.item_id!r} summarizes "
                    f"{p.summarizes_item!r}, which is not in this context. "
                    "A summary of something that was never provided is "
                    "provenance for material that does not exist.")
            actual = digest_bytes(src.text.encode("utf-8"))
            if p.summary_of != actual:
                raise ContextError(
                    f"context item {p.item_id!r} claims to summarize "
                    f"{p.summarizes_item!r} with digest "
                    f"{p.summary_of[:12]}, but those bytes digest to "
                    f"{actual[:12]}. The pointer names the item; the digest "
                    "must name its exact content.")
            if p.summarizes_item in summarized:
                raise ContextError(
                    f"{p.summarizes_item!r} is summarized by both "
                    f"{summarized[p.summarizes_item]!r} and {p.item_id!r}; "
                    "the manifest could not say which replaced it")
            summarized[p.summarizes_item] = p.item_id


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


#: The keys each durable record may carry, per manifest version. Anything
#: else is refused rather than ignored: a field this reader does not know is
#: a claim it cannot check.
_ITEM_KEYS = {
    1: {"item_id", "tier", "content_digest", "byte_len", "source",
        "summary_of"},
    2: {"item_id", "tier", "content_digest", "byte_len", "source",
        "summary_of", "summarizes_item"},
}
_OMISSION_KEYS = {"item_id", "tier", "content_digest", "byte_len", "reason",
                  "summarized_by"}
_MANIFEST_KEYS = {"task_id", "purpose", "items", "omissions", "budget_bytes",
                  "used_bytes", "policy_identity", "policy_digest", "at_seq"}


def manifest_from_record(rec: dict) -> ContextManifest:
    """Rebuild a manifest from a log payload, validating its shape.

    VERSIONING, EXPLICITLY. A record with no ``manifest_version`` is version
    1 and is read by version-1 rules. Anything newer than this reader is
    refused. See :data:`MANIFEST_VERSION` for what version 1 cannot prove.
    """
    if not isinstance(rec, dict):
        raise ContextError(f"context manifest is {type(rec).__name__}")
    version = rec.get("manifest_version", 1)
    if (not isinstance(version, int) or isinstance(version, bool)
            or version not in _ITEM_KEYS):
        raise ContextError(
            f"context manifest version {version!r} is not one this reader "
            f"understands (1..{MANIFEST_VERSION})")
    known = _MANIFEST_KEYS | ({"manifest_version"} if version >= 2
                              else set())
    unknown = set(rec) - known
    if unknown:
        raise ContextError(
            f"context manifest carries unknown fields {sorted(unknown)}; "
            "refusing to read a manifest this version does not fully "
            "understand")
    try:
        for kind, rows, allowed in (("item", rec["items"],
                                     _ITEM_KEYS[version]),
                                    ("omission", rec["omissions"],
                                     _OMISSION_KEYS)):
            for r in rows:
                if not isinstance(r, dict) or set(r) - allowed:
                    raise ContextError(
                        f"manifest {kind} is not a version-{version} "
                        f"{kind} record: {r!r:.120}")
        items = tuple(
            ContextItem(item_id=i["item_id"], tier=Tier(i["tier"]),
                        content_digest=i["content_digest"],
                        byte_len=i["byte_len"], source=i.get("source", ""),
                        summary_of=i.get("summary_of"),
                        summarizes_item=i.get("summarizes_item"))
            for i in rec["items"])
        omissions = tuple(
            Omission(item_id=o["item_id"], tier=Tier(o["tier"]),
                     content_digest=o["content_digest"],
                     byte_len=o["byte_len"], reason=o["reason"],
                     summarized_by=o.get("summarized_by"))
            for o in rec["omissions"])
        _check_manifest_provenance(items, omissions, version=version)
        return ContextManifest(
            task_id=rec["task_id"], purpose=rec["purpose"], items=items,
            omissions=omissions, budget_bytes=rec["budget_bytes"],
            used_bytes=rec["used_bytes"],
            policy_identity=rec.get("policy_identity", ""),
            policy_digest=rec.get("policy_digest", ""),
            at_seq=rec.get("at_seq", -1), manifest_version=version)
    except (KeyError, TypeError, ValueError) as exc:
        raise ContextError(f"context manifest is malformed: {exc}") from exc


def _check_manifest_provenance(items: tuple, omissions: tuple, *,
                               version: int = MANIFEST_VERSION) -> None:
    """A manifest read back must not carry provenance nobody could have had.

    Stated here independently of the builder, because manifests are durable
    and are read back from logs this version did not write. Everything is
    decided from the manifest alone:

    * every item is identified once, across shown and omitted, and every
      digest is a digest -- an omission is material that EXISTED;
    * a summary names BOTH halves of its claim -- the source item and the
      digest -- and the source exists in what was available (shown or
      omitted) with exactly that content digest, is not the summary itself,
      and is claimed by no other summary;
    * an omission that says a summary replaced it names a shown summary of
      exactly its bytes that names IT as the source; and an omitted source a
      shown summary claims must say so.

    Version 1 recorded the digest without the source, so it cannot prove any
    summary claim: a version-1 manifest carrying one is refused.
    """
    ids = [x.item_id for x in items] + [o.item_id for o in omissions]
    for x in ids:
        if not isinstance(x, str) or not x:
            raise ContextError(f"manifest item id {x!r} is not a name")
    if len(ids) != len(set(ids)):
        raise ContextError(
            "a manifest item id appears more than once across shown and "
            "omitted material, so no claim about it has one referent")
    for x in items:
        if not is_digest(x.content_digest):
            raise ContextError(
                f"manifest item {x.item_id!r} has content digest "
                f"{x.content_digest!r}, which is not a digest")
    available = {x.item_id: x.content_digest for x in items}
    available.update({o.item_id: o.content_digest for o in omissions})
    shown = {i.item_id: i for i in items}
    if version < 2 and (any(i.summary_of is not None for i in items)
                        or any(o.summarized_by is not None
                               for o in omissions)):
        raise ContextError(
            "a version-1 manifest records a summary but not which item it "
            "summarizes, so the claim cannot be checked; refusing it rather "
            "than trusting it")
    claimed: dict = {}
    for i in items:
        if i.summary_of is None and i.summarizes_item is None:
            continue
        if i.summary_of is None or i.summarizes_item is None:
            raise ContextError(
                f"manifest item {i.item_id!r} carries half a summary claim "
                f"(source {i.summarizes_item!r}, digest {i.summary_of!r}); "
                "the pair is the claim")
        if not is_digest(i.summary_of):
            raise ContextError(
                f"manifest item {i.item_id!r} summarizes "
                f"{i.summary_of!r}, which is not a digest")
        src = i.summarizes_item
        if not isinstance(src, str) or not src or src == i.item_id:
            raise ContextError(
                f"manifest item {i.item_id!r} names {src!r} as its source; "
                "a summary's source must be another, named item")
        if src not in available:
            raise ContextError(
                f"manifest item {i.item_id!r} summarizes {src!r}, which the "
                "manifest does not record as available material")
        if available[src] != i.summary_of:
            raise ContextError(
                f"manifest item {i.item_id!r} claims to summarize {src!r} "
                f"with digest {i.summary_of[:12]}, but {src!r} is recorded "
                f"with digest {str(available[src])[:12]}")
        if src in claimed:
            raise ContextError(
                f"{src!r} is summarized by both {claimed[src]!r} and "
                f"{i.item_id!r}")
        claimed[src] = i.item_id
    for o in omissions:
        if not is_digest(o.content_digest):
            raise ContextError(
                f"manifest omission {o.item_id!r} has content digest "
                f"{o.content_digest!r}: an omission records material that "
                "existed, and existing material has a digest")
        if o.summarized_by is None:
            if o.item_id in claimed:
                raise ContextError(
                    f"manifest omission {o.item_id!r} is claimed by shown "
                    f"summary {claimed[o.item_id]!r} and does not say so")
            continue
        by = shown.get(o.summarized_by)
        if by is None or by.summary_of != o.content_digest:
            raise ContextError(
                f"manifest omission {o.item_id!r} says it was replaced by "
                f"{o.summarized_by!r}, which is not a shown summary of its "
                "exact content")
        if by.summarizes_item != o.item_id:
            raise ContextError(
                f"manifest omission {o.item_id!r} says it was replaced by "
                f"{o.summarized_by!r}, which names {by.summarizes_item!r} as "
                "its source")
