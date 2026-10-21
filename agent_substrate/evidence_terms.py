"""What a check establishes, named once, so no check is described as another.

Nine properties are easy to run together and are kept apart here:

``CITATION_EXISTS``
    A reference is present and well-formed. It names something; nothing has
    looked it up.
``CITED_BYTES_VERIFY``
    The named bytes are held and hash to the reference. The citation is not
    forged and the content has not changed since it was cited. Nothing more:
    the bytes may be about something else entirely.
``CITATION_RELEVANT``
    The cited bytes concern the claim's subject.
``SEMANTICALLY_SUPPORTED``
    What the cited content says supports what the claim says.
``SCIENTIFICALLY_VERIFIED``
    A result passed the verification its admission policy defines, within
    that policy's stated tolerances and assumptions.
``DECISION_AUTHORIZED``
    A transition passed the gates -- edge, role, separation of duties, cited
    evidence -- under the policy in force.
``CURRENT_WORLD``
    The claim holds of the external world now. A record of a measurement is
    evidence about the time it was made.
``HISTORY_INTACT``
    The record chain and its head witness verify. Consistency, not truth: a
    history rewritten together with its witness is intact.
``HISTORY_RECORDS``
    The verified history records the claimed state, as projected by readers
    that agree. The log saying so is not the world being so.

Only one implication holds between them: verified bytes were cited
(``CITED_BYTES_VERIFY`` implies ``CITATION_EXISTS``). Every other pair is
independent, and :data:`CHECKS` says, for each check in this repository that
answers one of these questions, which one it answers and which it is most
often mistaken for. ``tests/test_evidence_terms.py`` holds the registry to
the code, and ``docs/EVIDENCE_TERMS.md`` carries the rendered table.

A check is DETERMINISTIC when it is a computation over bytes and records,
ASSESSED when it is a fallible judgement (a model's, or a heuristic's), and
OBSERVED when it is a measurement of the world. Only a DETERMINISTIC check
may be relied on by an authority decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Property(str, Enum):
    CITATION_EXISTS = "CITATION_EXISTS"
    CITED_BYTES_VERIFY = "CITED_BYTES_VERIFY"
    CITATION_RELEVANT = "CITATION_RELEVANT"
    SEMANTICALLY_SUPPORTED = "SEMANTICALLY_SUPPORTED"
    SCIENTIFICALLY_VERIFIED = "SCIENTIFICALLY_VERIFIED"
    DECISION_AUTHORIZED = "DECISION_AUTHORIZED"
    CURRENT_WORLD = "CURRENT_WORLD"
    HISTORY_INTACT = "HISTORY_INTACT"
    HISTORY_RECORDS = "HISTORY_RECORDS"


class Strength(str, Enum):
    DETERMINISTIC = "DETERMINISTIC"
    ASSESSED = "ASSESSED"
    OBSERVED = "OBSERVED"


#: The only implications. Anything not reachable from here is independent.
IMPLIES: dict = {
    Property.CITED_BYTES_VERIFY: frozenset({Property.CITATION_EXISTS}),
}


def implied_by(prop: Property) -> frozenset:
    """``prop`` and everything establishing it establishes."""
    out, frontier = {prop}, [prop]
    while frontier:
        for nxt in IMPLIES.get(frontier.pop(), ()):
            if nxt not in out:
                out.add(nxt)
                frontier.append(nxt)
    return frozenset(out)


@dataclass(frozen=True)
class Check:
    """One check and the one property it establishes.

    ``target`` is ``module:attribute`` (dotted within the module), or
    ``module:function#key`` for one answer of a function that answers
    several questions under named keys.
    """

    target: str
    establishes: Property
    strength: Strength
    scope: str
    #: what this check is most often mistaken for establishing
    not_: tuple
    #: properties the check requires before it answers, if any
    requires: tuple = ()


P, S = Property, Strength
CHECKS: tuple = (
    Check("agent_substrate.proposals:ProposalEnvelope.parse",
          P.CITATION_EXISTS, S.DETERMINISTIC,
          "every citation in a proposal names a path, a line range and a "
          "digest, in an exact key set",
          (P.CITED_BYTES_VERIFY, P.SEMANTICALLY_SUPPORTED)),
    Check("agent_substrate.proposals:ProposalIngress.receive",
          P.CITED_BYTES_VERIFY, S.DETERMINISTIC,
          "each cited source still hashes to the digest the proposal "
          "cites; a changed source is refused as a stale context",
          (P.CITATION_RELEVANT, P.SEMANTICALLY_SUPPORTED)),
    Check("agent_substrate.evidence:EvidenceStore.contains",
          P.CITED_BYTES_VERIFY, S.DETERMINISTIC,
          "the store holds bytes that re-hash to the digest (verify=True)",
          (P.CITATION_RELEVANT, P.SEMANTICALLY_SUPPORTED)),
    Check("agent_substrate.evidence:EvidenceStore.get",
          P.CITED_BYTES_VERIFY, S.DETERMINISTIC,
          "the returned bytes re-hash to the digest asked for",
          (P.CITATION_RELEVANT, P.SEMANTICALLY_SUPPORTED)),
    Check("agent_substrate.memory_trust:assess#evidence_supported",
          P.CITED_BYTES_VERIFY, S.DETERMINISTIC,
          "every digest a memory cites resolves and re-hashes. The key's "
          "name is historical: it does not say the evidence supports the "
          "remembered text",
          (P.CITATION_RELEVANT, P.SEMANTICALLY_SUPPORTED, P.CURRENT_WORLD)),
    Check("agent_substrate.memory_trust:assess#hash_consistent",
          P.HISTORY_INTACT, S.DETERMINISTIC,
          "the chain verifies against its head witness",
          (P.CURRENT_WORLD,)),
    Check("agent_substrate.memory_trust:assess#consistently_reconstructed",
          P.HISTORY_RECORDS, S.DETERMINISTIC,
          "the primary projection and the independent reader agree on the "
          "entry", (P.SEMANTICALLY_SUPPORTED, P.CURRENT_WORLD),
          (P.HISTORY_INTACT,)),
    Check("agent_substrate.events:EventLog.read_verified",
          P.HISTORY_INTACT, S.DETERMINISTIC,
          "every record links to the one before and the tail matches the "
          "head witness", (P.HISTORY_RECORDS, P.CURRENT_WORLD)),
    Check("agent_substrate.separate_verify:verify_in_separate_process",
          P.HISTORY_RECORDS, S.DETERMINISTIC,
          "a second implementation, in another process, rebuilds every "
          "authority state and agrees", (P.CURRENT_WORLD,),
          (P.HISTORY_INTACT,)),
    Check("agent_substrate.authority:check",
          P.DECISION_AUTHORIZED, S.DETERMINISTIC,
          "the move is an edge, the role may take it, the actor is not the "
          "proposer where the edge demands a second actor, and every "
          "evidence key it requires is a digest that, given a resolver, "
          "resolves",
          (P.SCIENTIFICALLY_VERIFIED, P.SEMANTICALLY_SUPPORTED,
           P.CURRENT_WORLD),
          (P.CITED_BYTES_VERIFY,)),
    Check("agent_substrate.result_rules:admission_problems",
          P.SCIENTIFICALLY_VERIFIED, S.DETERMINISTIC,
          "a result bundle and its verification report satisfy the named "
          "admission policy", (P.CURRENT_WORLD, P.SEMANTICALLY_SUPPORTED),
          (P.CITED_BYTES_VERIFY,)),
    Check("agent_substrate.inference:prompt_problems",
          P.CITED_BYTES_VERIFY, S.DETERMINISTIC,
          "every item a prompt shows re-hashes to the content digest the "
          "recorded context manifest gives it, in the manifest's order",
          (P.CITATION_RELEVANT, P.SEMANTICALLY_SUPPORTED)),
    Check("agent_substrate.inference:InferenceGateway.binding_problems",
          P.HISTORY_RECORDS, S.DETERMINISTIC,
          "a /2 proposal is what the recorded inference's response said, "
          "asked by the agent that proposes it, for the context it names. "
          "That the model's answer is right is not part of it",
          (P.SEMANTICALLY_SUPPORTED, P.SCIENTIFICALLY_VERIFIED,
           P.CURRENT_WORLD),
          (P.CITED_BYTES_VERIFY,)),
    Check("tools.grounding_bench.claims:verify",
          P.HISTORY_RECORDS, S.DETERMINISTIC,
          "a structured claim matches the state reconstructed from the "
          "verified history. Its SUPPORTED verdict means the history records "
          "it", (P.SEMANTICALLY_SUPPORTED, P.CURRENT_WORLD),
          (P.HISTORY_INTACT,)),
)


def establishing(prop: Property) -> tuple:
    """The registered checks that establish ``prop``, directly or by
    implication."""
    return tuple(c for c in CHECKS if prop in implied_by(c.establishes))


def render_table() -> str:
    """The Markdown table ``docs/EVIDENCE_TERMS.md`` carries."""
    lines = ["| Check | Establishes | Strength | Requires | Not to be read "
             "as |", "|---|---|---|---|---|"]
    for c in CHECKS:
        lines.append(
            f"| `{c.target}` | {c.establishes.value} | {c.strength.value} | "
            f"{', '.join(p.value for p in c.requires) or '-'} | "
            f"{', '.join(p.value for p in c.not_)} |")
    for prop in Property:
        if not establishing(prop):
            lines.append(f"| *(none)* | {prop.value} | - | - | "
                         "no check in this repository establishes it |")
    return "\n".join(lines) + "\n"
