"""Scripted agents, and the conditions that decide what they may consult.

SCRIPTED, NOT LEARNED. An agent here is a seeded procedure with an explicit
ERROR MODEL. Nothing in this benchmark calls a language model, and nothing
it measures is a property of one. What it measures is how much each source
of state limits the damage a given error process does -- the error process
is an assumption, stated in :class:`ErrorModel`, and the results hold for
that assumption and no other.

THE AGENT'S OWN BELIEFS come from the narrations (:class:`world.Narration`),
read through the error model:

``miss``     an update is not taken in (attention);
``corrupt``  a value is taken in wrong (transcription, conflation);
``credulity`` an injected message is believed (the agent cannot tell an
             honest narration from an attacker's -- neither can a reader);
``window``   beliefs last touched more than this many steps before the
             question are forgotten (context length);
``guess``    with no belief, the agent asserts a plausible answer anyway.

THE CONDITIONS -- what it consults before answering:

A ``context``      its beliefs, nothing else.
B ``memory``       its memory notes (recall), then its beliefs.
C ``projection``   the PRIMARY projection, loaded at question time; when the
                   primary refuses the history (fail-closed), its beliefs.
D ``reconstruct``  the INDEPENDENT reader's state, taken as it comes: the
                   state, and a value read from the report's bytes.
E ``verified``     its own claim, gated by V(C, S, E): SUPPORTED is
                   asserted, CONTRADICTED is replaced by what S holds,
                   UNKNOWN and UNVERIFIABLE abstain.
F ``authenticated`` E, with S rebuilt by a reader that authenticates every
                   event against the attestations and the registry of TEST
                   keys: an event nobody's key vouches for is not folded,
                   and its subject is UNVERIFIABLE.

ABLATIONS of E: ``no_evidence`` (V reads the report without verifying it),
``no_correct`` (CONTRADICTED abstains instead of correcting),
``no_abstain`` (UNKNOWN and UNVERIFIABLE assert the belief anyway). And of
C: ``fail_abstain`` (when the primary refuses, abstain).
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass

from agent_substrate import principals as pr
from agent_substrate import reconstruct as rc
from agent_substrate.events import EventLog
from agent_substrate.evidence import EvidenceStore
from agent_substrate.memory import MemoryError_, MemoryStore
from agent_substrate.store import AuthorityStore, StoreError

from .claims import (ADMITTED, CONTRADICTED, STATE, SUPPORTED, VALUE, Claim,
                     refused_subjects, verify)
from .world import registry

ABSTAIN = None
STATES = ("UNDER_REVIEW", "VERIFIED", "PROMOTED", "REVOKED")
CONDITIONS = ("A", "B", "C", "C_fail_abstain", "D", "E", "E_no_evidence",
              "E_no_correct", "E_no_abstain", "F")


@dataclass(frozen=True)
class ErrorModel:
    miss: float = 0.10
    corrupt: float = 0.05
    credulity: float = 0.50
    window: int = 400
    guess: float = 0.30

    def to_record(self) -> dict:
        return {"miss": self.miss, "corrupt": self.corrupt,
                "credulity": self.credulity, "window": self.window,
                "guess": self.guess}


class Beliefs:
    """What the agent thinks, built once from the narrations."""

    def __init__(self, narrations, err: ErrorModel, rng: random.Random):
        self.held: dict = {}            # (subject, predicate) -> (value, step)
        for n in narrations:
            if not n.honest and rng.random() >= err.credulity:
                continue
            if n.honest and rng.random() < err.miss:
                continue
            value = n.value
            if n.predicate == VALUE and rng.random() < err.corrupt:
                value = round(value + rng.choice((-1, 1)) * 0.5, 3)
            self.held[(n.subject, n.predicate)] = (value, n.step)
        self.err = err
        self.rng = rng

    def claim(self, subject: str, predicate: str, at_step: int):
        got = self.held.get((subject, predicate))
        if got is not None and at_step - got[1] <= self.err.window:
            return Claim(subject, predicate, got[0])
        if self.rng.random() < self.err.guess:
            value = (self.rng.choice(STATES) if predicate == STATE
                     else round(self.rng.uniform(0.0, 100.0), 3))
            return Claim(subject, predicate, value)
        return ABSTAIN


class _RawEvidence:
    """For the no_evidence ablation only: the report's bytes as they lie on
    disk, whatever they hash to. The evidence store has no such read, on
    purpose; the ablation is what that refusal is worth."""

    def __init__(self, store: EvidenceStore):
        self.store = store

    def contains(self, dg) -> bool:
        return self.store._blob_path(dg).is_file()

    def get(self, dg) -> bytes:
        return self.store._blob_path(dg).read_bytes()


class Sources:
    """What a world offers to be consulted, each read once and shared by
    every agent over that world. Read-only: no agent can change what
    another sees, and none is handed anything the view does not hold."""

    def __init__(self, view):
        self.view = view
        self.evidence = EvidenceStore(view.evidence_root)
        self._state: dict = {}
        self._targets = None
        self._primary = None
        self._memory = None

    def state(self, *, authenticated: bool = False):
        """The INDEPENDENT reader's state -- authenticating every event
        against the attestations, or not."""
        if authenticated not in self._state:
            auth = (pr.Authenticator(
                pr.Attestations(self.view.attestations_path), registry())
                if authenticated else None)
            self._state[authenticated] = rc.reconstruct(
                EventLog(self.view.log_path), evidence=self.evidence,
                authenticator=auth)
        return self._state[authenticated]

    def targets(self) -> dict:
        """seq -> target, for a refusal that names only a seq."""
        if self._targets is None:
            report, events = EventLog(self.view.log_path).read_verified()
            report.raise_if_bad()
            self._targets = {ev.seq: ev.target for ev in events}
        return self._targets

    def primary(self):
        """The primary projection, or the refusal it gives."""
        if self._primary is None:
            try:
                self._primary = AuthorityStore(
                    EventLog(self.view.log_path),
                    evidence=self.evidence).load()
            except StoreError as exc:
                self._primary = exc
        return self._primary

    def memory(self):
        """The memory store, or the refusal it gives."""
        if self._memory is None:
            try:
                self._memory = MemoryStore(
                    EventLog(self.view.log_path)).load()
            except MemoryError_ as exc:
                self._memory = exc
        return self._memory


class Agent:
    """One agent, one condition, over one world."""

    def __init__(self, view, condition: str, err: ErrorModel, seed: int,
                 sources: Sources | None = None):
        if condition not in CONDITIONS:
            raise ValueError(f"unknown condition {condition!r}")
        self.condition = condition
        self.beliefs = Beliefs(view.narrations, err, random.Random(seed))
        self.sources = sources if sources is not None else Sources(view)
        self.evidence = self.sources.evidence

    def state(self):
        return self.sources.state(authenticated=self.condition == "F")

    # -- the answer ---------------------------------------------------------
    def answer(self, subject: str, predicate: str, at_step: int):
        c = self.condition
        if c == "A":
            return self.beliefs.claim(subject, predicate, at_step)
        if c == "B":
            return self._from_memory(subject, predicate, at_step)
        if c in ("C", "C_fail_abstain"):
            return self._from_primary(subject, predicate, at_step)
        if c == "D":
            return self._from_reconstruction(subject, predicate)
        return self._gated(subject, predicate, at_step)

    def _from_memory(self, subject, predicate, at_step):
        """The LATEST note about the subject wins -- the ordinary heuristic,
        and the one a note written later to mislead exploits."""
        m = self.sources.memory()
        if predicate == VALUE and not isinstance(m, Exception):
            notes = []
            for hit in m.recall(subject, k=20):
                words = hit.text.split()
                if words and words[0] == subject:
                    try:
                        notes.append((m.get(hit.memory_id).created_seq,
                                      float(words[-1])))
                    except ValueError:
                        continue
            if notes:
                return Claim(subject, VALUE, max(notes)[1])
        return self.beliefs.claim(subject, predicate, at_step)

    def _from_primary(self, subject, predicate, at_step):
        p = self.sources.primary()
        if isinstance(p, Exception):
            return (ABSTAIN if self.condition == "C_fail_abstain"
                    else self.beliefs.claim(subject, predicate, at_step))
        try:
            rec = p.get(subject)
        except Exception:                 # noqa: BLE001 - unknown record
            return ABSTAIN
        if predicate == STATE:
            return Claim(subject, STATE, rec.state.value)
        if rec.state.value not in ADMITTED:
            return ABSTAIN
        dg = rec.evidence.get("verification_report")
        try:
            doc = json.loads(self.evidence.get(dg))
        except Exception:                 # noqa: BLE001 - does not verify
            return ABSTAIN
        return Claim(subject, VALUE, doc.get("value"))

    def _from_reconstruction(self, subject, predicate):
        rec = self.state().records.get(subject)
        if rec is None:
            return ABSTAIN
        if predicate == STATE:
            return Claim(subject, STATE, rec.get("state"))
        if rec.get("state") not in ADMITTED:
            return ABSTAIN
        dg = (rec.get("evidence") or {}).get("verification_report")
        raw = _RawEvidence(self.evidence)
        if not dg or not raw.contains(dg):
            return ABSTAIN
        try:
            return Claim(subject, VALUE, json.loads(raw.get(dg)).get("value"))
        except ValueError:
            return ABSTAIN

    def _gated(self, subject, predicate, at_step):
        c = self.condition
        claim = self.beliefs.claim(subject, predicate, at_step)
        if claim is ABSTAIN:
            # No belief: the state is asked directly, and what it holds is
            # still checked before it is asserted.
            held = self._from_reconstruction(subject, predicate)
            if held is ABSTAIN:
                return ABSTAIN
            claim = held
        ev = (_RawEvidence(self.evidence) if c == "E_no_evidence"
              else self.evidence)
        v = verify(claim, self.state(), ev,
                   targets=self.sources.targets())
        if v.verdict == SUPPORTED:
            return claim
        if v.verdict == CONTRADICTED:
            return (ABSTAIN if c == "E_no_correct"
                    else Claim(subject, predicate, v.held))
        return claim if c == "E_no_abstain" else ABSTAIN


__all__ = ["ABSTAIN", "Agent", "CONDITIONS", "ErrorModel", "Sources",
           "refused_subjects"]
