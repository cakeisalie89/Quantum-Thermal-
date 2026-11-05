"""Trust labels: what is known of a piece of content, and how (Assurance C).

ONE RULE

No transformation of content grants it a property its inputs did not have,
and none keeps one: a summary, a paraphrase, a retrieval hit, a message
relayed between agents, a compressed context, an index entry, an
explanation, a citation of a citation, or the agreement of many agents
starts with NO established property at all. A property is added only by a
check registered in :mod:`agent_substrate.evidence_terms` as establishing
it, against a reference a reader can follow (a log position, a digest), and
only when the properties that check requires are already there.

WHAT A LABEL IS, AND IS NOT

A label is a DERIVED description, computed from what the system already
records -- a context item's tier and summary provenance, a memory entry's
sources and status, a retrieval hit's corpus digest, a proposal's envelope.
It is not stored, so no existing record, manifest or digest changes, and it
is not authority: the authority store, the gateway and the research ledger
decide from the log and the evidence store, never from a label. What the
label adds is one place where the flow rule is stated and checked, and the
vocabulary the noninterference tests use to say what may not influence what.

WHAT IT DOES NOT CLAIM

Complete information-flow security. Python does not confine a value to its
label; a caller that reads raw text past its label is not stopped here. The
claim tested is narrower: the system's AUTHORITY outcomes do not change when
untrusted content does (tests/test_noninterference.py).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from .evidence_terms import CHECKS, Property, Strength, implied_by


class Origin(str, Enum):
    """Where content came from. An origin is not a property: it says who
    is answerable for the content, not what has been checked about it."""

    OWNER = "OWNER"                  # an instruction from an owner
    POLICY = "POLICY"                # a published policy document
    RECORD = "RECORD"                # state replayed from the verified log
    EVIDENCE = "EVIDENCE"            # bytes in the evidence store, by digest
    TOOL_OUTPUT = "TOOL_OUTPUT"      # a governed tool's captured output
    RETRIEVED = "RETRIEVED"          # a hit from the governed corpus
    MODEL_OUTPUT = "MODEL_OUTPUT"    # what a model said
    AGENT_MESSAGE = "AGENT_MESSAGE"  # what one agent told another
    MEMORY = "MEMORY"                # a remembered statement
    EXTERNAL = "EXTERNAL"            # anything else from outside


class Transform(str, Enum):
    """Ways content is made from other content. Every one of them starts
    the result with no established property (see the module docstring)."""

    QUOTE = "QUOTE"
    SUMMARIZE = "SUMMARIZE"
    COMPRESS = "COMPRESS"
    PARAPHRASE = "PARAPHRASE"
    RETRIEVE = "RETRIEVE"
    RELAY = "RELAY"
    CITE = "CITE"
    EMBED = "EMBED"
    AGREE = "AGREE"
    EXPLAIN = "EXPLAIN"


class TrustError(Exception):
    """A label refused: an upgrade with no registered check behind it, or
    content short of what a use requires. Fail closed."""


#: The registered checks, by name, and what each establishes.
_CHECKS = {c.target: c for c in CHECKS}


@dataclass(frozen=True)
class Proof:
    """One property, the check that established it, and where to look."""

    prop: Property
    check: str
    ref: str


@dataclass(frozen=True)
class Label:
    """What is known of one piece of content."""

    origin: Origin
    #: who or what produced it: an agent id, a policy identity, a record id
    source: str
    #: the content's digest, when it has bytes of its own
    digest: str = ""
    proofs: frozenset = frozenset()
    #: the labels of what it was made from, and how, oldest first
    derived_from: tuple = ()
    transforms: tuple = ()
    #: set when the content stops being current: the reference that says so
    retracted_by: str = ""
    superseded_by: str = ""

    @property
    def properties(self) -> frozenset:
        return frozenset(p.prop for p in self.proofs)

    @property
    def current(self) -> bool:
        return not (self.retracted_by or self.superseded_by)

    @property
    def derived(self) -> bool:
        return bool(self.transforms)


def derive(transform: Transform, *inputs: Label, by: Origin, source: str,
           digest: str = "") -> Label:
    """A label for content ``by`` made from ``inputs`` by ``transform``.

    It starts with NO proofs, whatever the inputs carried and however many
    of them there are: agreement among ten untrusted inputs is ten untrusted
    inputs. Its origin is the least answerable of the inputs' and of
    whoever transformed them -- a model's summary of an owner's instruction
    is the model's, not the owner's -- and it carries the inputs, so what it
    rests on is never lost."""
    if not inputs:
        raise TrustError(f"{Transform(transform).value} of nothing")
    origin = min([Origin(by), *(i.origin for i in inputs)],
                 key=_ANSWERABILITY.index)
    return Label(origin=origin, source=source, digest=digest,
                 derived_from=tuple(inputs),
                 transforms=(Transform(transform),))


#: Least answerable first: the order derive() takes the minimum in.
_ANSWERABILITY = [Origin.EXTERNAL, Origin.MODEL_OUTPUT, Origin.AGENT_MESSAGE,
                  Origin.MEMORY, Origin.RETRIEVED, Origin.TOOL_OUTPUT,
                  Origin.EVIDENCE, Origin.RECORD, Origin.POLICY, Origin.OWNER]


def establish(label: Label, prop: Property, *, check: str,
              ref: str) -> Label:
    """``label`` with ``prop`` established by ``check``, citing ``ref``.

    The ONE way a property is added. ``check`` must be registered in
    evidence_terms.CHECKS as establishing ``prop`` (directly or by
    implication), DETERMINISTIC (an assessed judgement grants nothing), its
    required properties must already be on the label, and ``ref`` must say
    where the check's answer is recorded."""
    c = _CHECKS.get(check)
    if c is None:
        raise TrustError(f"{check!r} is not a registered check; only a "
                         "registered check establishes a property")
    if prop not in implied_by(c.establishes):
        raise TrustError(f"{check} establishes {c.establishes.value}, not "
                         f"{Property(prop).value}")
    if c.strength is not Strength.DETERMINISTIC:
        raise TrustError(f"{check} is {c.strength.value}; only a "
                         "deterministic check grants a property")
    missing = sorted(p.value for p in c.requires
                     if p not in label.properties)
    if missing:
        raise TrustError(f"{check} requires {missing} first")
    if not isinstance(ref, str) or not ref.strip():
        raise TrustError("an established property cites where its check's "
                         "answer is recorded")
    if not label.current:
        raise TrustError("a retracted or superseded label gains nothing")
    new = {Proof(p, check, ref) for p in implied_by(Property(prop))}
    return replace(label, proofs=label.proofs | frozenset(new))


def require(label: Label, *props: Property) -> None:
    """Refuse ``label`` for a use needing ``props``."""
    if not label.current:
        raise TrustError(f"{label.source}: not current (retracted by "
                         f"{label.retracted_by or label.superseded_by})")
    missing = sorted(Property(p).value for p in props
                     if Property(p) not in label.properties)
    if missing:
        raise TrustError(f"{label.source} ({label.origin.value}"
                         f"{', derived' if label.derived else ''}) lacks "
                         f"{missing}")


def retract(label: Label, *, by: str) -> Label:
    """``label`` no longer current: ``by`` says where that is recorded."""
    if not by:
        raise TrustError("a retraction cites where it is recorded")
    return replace(label, retracted_by=by)


def supersede(label: Label, *, by: str) -> Label:
    if not by:
        raise TrustError("a supersession names what supersedes")
    return replace(label, superseded_by=by)


# ------------------------------------------------- what the system records

#: A context item's tier says where it came from. Nothing about a tier is a
#: property: an owner's instruction is answerable to the owner, which is
#: not the same as anything about it having been checked.
TIER_ORIGIN = {
    "OWNER_INSTRUCTION": Origin.OWNER,
    "SYSTEM_POLICY": Origin.POLICY,
    "TASK_STATE": Origin.RECORD,
    "TASK_EVIDENCE": Origin.EVIDENCE,
    "RETRIEVED_EVIDENCE": Origin.RETRIEVED,
    "TOOL_RESULT": Origin.TOOL_OUTPUT,
    "PRIOR_MODEL_OUTPUT": Origin.MODEL_OUTPUT,
    "SCRATCH": Origin.MODEL_OUTPUT,
    "MEMORY": Origin.MEMORY,
}


def label_of_item(item, *, summarizes=None) -> Label:
    """The label of a context item (``agent_substrate.context.ContextItem``).

    A summary is DERIVED from the item it compresses, whatever tier it is
    shown in: ``summarizes`` is that item, when the caller has it; without
    it the summary is derived from an unknown, which is EXTERNAL."""
    tier = getattr(item.tier, "value", item.tier)
    base = Label(origin=TIER_ORIGIN[tier], source=item.source or item.item_id,
                 digest=item.content_digest)
    if item.summary_of is None:
        return base
    src = label_of_item(summarizes) if summarizes is not None else Label(
        origin=Origin.EXTERNAL, source=item.summarizes_item or "",
        digest=item.summary_of)
    # Who wrote a summary a manifest shows is not recorded; it is treated as
    # the least answerable writer that could have: a model.
    return derive(Transform.SUMMARIZE, src, by=Origin.MODEL_OUTPUT,
                  source=item.item_id, digest=item.content_digest)


def label_of_memory(entry) -> Label:
    """The label of a memory entry (``agent_substrate.memory.MemoryEntry``).

    What is remembered is a statement made FROM its sources, not the
    sources: it is derived (a paraphrase, or a summary of other entries)
    and holds no property of theirs. Its confidence is not read at all."""
    sources = tuple(Label(origin=Origin.EVIDENCE, source=d, digest=d)
                    for d in entry.derived_from) + tuple(
        Label(origin=Origin.MEMORY, source=m) for m in entry.summarizes)
    how = Transform.SUMMARIZE if entry.summarizes else Transform.PARAPHRASE
    label = derive(how, *sources, by=Origin.MEMORY, source=entry.author,
                   digest=entry.digest()) if sources else Label(
        origin=Origin.MEMORY, source=entry.author, digest=entry.digest())
    status = getattr(entry.status, "value", entry.status)
    if status == "SUPERSEDED":
        return supersede(label, by=entry.superseded_by or entry.memory_id)
    if status in ("RETRACTED", "STALE"):
        return retract(label, by=f"{entry.memory_id}@{entry.updated_seq}")
    return label


def label_of_proposal(envelope) -> Label:
    """The label of a received proposal (``proposals.ProposalEnvelope``).

    A model's output, or an agent's: whichever, it is answerable to the
    agent that proposed it and establishes nothing about the world. Its
    citations exist (the envelope parses only if they are well formed);
    that they verify is the ingress's check, recorded at receipt."""
    label = Label(origin=Origin.MODEL_OUTPUT, source=envelope.agent_id,
                  digest=envelope.digest())
    if envelope.citations:
        label = establish(label, Property.CITATION_EXISTS,
                          check="agent_substrate.proposals:"
                                "ProposalEnvelope.parse",
                          ref=envelope.proposal_id)
    return label
