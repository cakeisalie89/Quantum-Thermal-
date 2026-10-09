"""AI proposes: the ingress where a proposal enters, and nothing more.

"AI proposes, the harness decides" was true of the authority substrate and
nowhere written as a software boundary. This module is that boundary. A
proposal is INPUT. It is not evidence, it is not verified, and it is not
authority -- and nothing on this side of the boundary can make it any of
those:

* the ENVELOPE (:class:`ProposalEnvelope`) is provider-neutral and closed:
  an exact key set, a fixed ``authority: NON_AUTHORITATIVE``, a content-
  addressed ``proposal_id``, and no field a proposer could use to state a
  verdict, a state, a signature or a destination for its own output. A
  record that names one is refused, not ignored;
* the SOURCES are adapters that produce raw records and are trusted for
  nothing: a recorded fixture (what CI uses -- no provider, no network, no
  secret) and an external client adapter around any callable a caller
  supplies. Whatever provider or model identity a record carries is
  recorded as CLAIMED, never checked here and never relied on;
* RECEIVING appends one ``proposal.receive`` event, written by the ingress
  under its own identity, before anything runs -- so a crash after receipt
  loses nothing, and a resubmission finds the proposal already there;
* SUBMITTING a MODEL_RUN proposal hands its requested model and parameters
  to the governed model path (``qta_agent.governed_model``) with the
  proposal's id as the idempotency key: the run is then policy-checked,
  capability-bound, executed in a bounded subprocess, re-executed by a
  separate verifier, independently checked by a different executor and
  decided by a reviewer who is none of them. The proposer's agent is the
  submitter, so the store's separation of duties holds it apart from every
  verifying role. Where the output lands is chosen by the ingress, never by
  the proposal;
* CONTEXT is assembled from governed read-only retrieval over reviewed
  documents (injected by the caller; this module imports no retrieval and
  no client), every span stamped RETRIEVED_TEXT_NOT_EVIDENCE and cited by
  path, line range and source SHA-256. A proposal citing a span whose
  source has changed since is refused as stale. Retrieved text informs a
  proposal; it is never cited by an authority decision, which traces only
  to governed evidence.

What a proposal may ask for: a model run, a simulation plan, a retry, a
diagnostic, or a candidate code patch represented by digest outside
authority. What it may never do directly -- append an authority transition,
change a VERIFIED state, sign as a verifier, mark its own output correct,
write a canonical artefact outside governed execution -- it has no handle to
do: the ingress holds the log, the AI holds nothing.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .canonical import canonical_bytes, digest
from .events import EventLogError

SCHEMA = "proposal-envelope/1"
ACT_PROPOSAL_RECEIVE = "proposal.receive"
AUTHORITY = "NON_AUTHORITATIVE"
EVIDENCE_STATUS = "RETRIEVED_TEXT_NOT_EVIDENCE"
INGRESS_ID = "proposal-ingress"
#: the one external model a proposal may name; run against the FMU the
#: ingress's caller admits, never one the proposal points at
FMU_MODEL = "fmi.thermal_rc2"
MAX_RECORD_BYTES = 64 * 1024
MAX_DEPTH = 8

KINDS = ("MODEL_RUN", "SIMULATION_PLAN", "RETRY", "DIAGNOSTIC", "CODE_PATCH")
ADAPTERS = ("recorded-fixture", "external-client")
#: request keys, per kind: closed sets. Nothing here names an output
#: location, a state, a verdict or a signature.
REQUEST_KEYS = {
    "MODEL_RUN": {"model_id", "model_version", "parameters"},
    "SIMULATION_PLAN": {"steps"},
    "RETRY": {"retry_of"},
    "DIAGNOSTIC": {"question", "subject"},
    "CODE_PATCH": {"patch_sha256", "files", "summary"},
}
FIELDS = ("schema", "proposal_id", "kind", "agent_id", "source", "context",
          "request", "rationale_sha256", "parent_proposal_id", "iteration",
          "created_by", "authority")
SOURCE_KEYS = {"adapter", "provider", "model"}
CONTEXT_KEYS = {"digest", "retrieved"}
CITATION_KEYS = {"path", "line_start", "line_end", "source_sha256"}
CREATED_KEYS = {"adapter_version", "input_digest"}
#: words that, as a KEY anywhere in a proposal, would be a claim about
#: authority or about where an output goes. Refused wherever they appear.
FORBIDDEN_KEYS = frozenset({
    "state", "status", "verdict", "verified", "promoted", "approved",
    "decision", "transition", "signature", "signed", "evidence",
    "verification", "authority_state", "out_dir", "output_path",
    "write_path", "canonical"})
#: what a credential looks like. A proposal carrying one is refused: no
#: provider credential belongs in a proposal or in this repository.
_CREDENTIAL = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}|\bAKIA[0-9A-Z]{16}\b|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bxox[abpr]-[A-Za-z0-9-]{10,}|"
    r"\bAIza[0-9A-Za-z_-]{30,})")
_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")
_ID = re.compile(r"\Aprop-[0-9a-f]{64}\Z")
_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._@/-]{0,127}\Z")


class ProposalRefused(ValueError):
    pass


def _walk(obj, where: str, depth: int = 0) -> None:
    """JSON only, bounded, no forbidden key, no credential."""
    if depth > MAX_DEPTH:
        raise ProposalRefused(f"{where}: nested deeper than {MAX_DEPTH}")
    if isinstance(obj, dict):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise ProposalRefused(f"{where}: a non-string key")
            if k.lower() in FORBIDDEN_KEYS:
                raise ProposalRefused(
                    f"{where}.{k}: a proposal cannot carry {k!r} -- it is "
                    "input, and states no verdict, state, signature or "
                    "output location")
            _walk(v, f"{where}.{k}", depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _walk(v, f"{where}[{i}]", depth + 1)
    elif isinstance(obj, str):
        if _CREDENTIAL.search(obj):
            raise ProposalRefused(f"{where}: carries what looks like a "
                                  "credential; refused, and not stored")
    elif isinstance(obj, float):
        if not math.isfinite(obj):
            raise ProposalRefused(f"{where}: {obj!r} is not a JSON number")
    elif not (obj is None or isinstance(obj, (bool, int))):
        raise ProposalRefused(f"{where}: {type(obj).__name__} is not JSON")


def body_of(rec: dict) -> dict:
    """The record minus its id: what the id is the digest of."""
    return {k: v for k, v in rec.items() if k != "proposal_id"}


def proposal_id_of(rec: dict) -> str:
    return "prop-" + digest(body_of(rec))


def _exact(obj, keys, where: str) -> dict:
    if not isinstance(obj, dict):
        raise ProposalRefused(f"{where} must be an object")
    extra, missing = set(obj) - set(keys), set(keys) - set(obj)
    if extra or missing:
        raise ProposalRefused(f"{where}: unknown {sorted(extra)}, missing "
                              f"{sorted(missing)}")
    return obj


def _request(kind: str, req) -> None:
    _exact(req, REQUEST_KEYS[kind], "request")
    if kind == "MODEL_RUN":
        for k in ("model_id", "model_version"):
            if not isinstance(req[k], str) or not _NAME.match(req[k]):
                raise ProposalRefused(f"request.{k} is not a name")
        if not isinstance(req["parameters"], dict):
            raise ProposalRefused("request.parameters must be an object")
    elif kind == "SIMULATION_PLAN":
        steps = req["steps"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
            raise ProposalRefused("request.steps: 1..32 model runs")
        for i, st in enumerate(steps):
            if not isinstance(st, dict):
                raise ProposalRefused(f"request.steps[{i}] is not a run")
            _request("MODEL_RUN", st)
    elif kind == "RETRY":
        if not isinstance(req["retry_of"], str) or \
                not _ID.match(req["retry_of"]):
            raise ProposalRefused("request.retry_of is not a proposal id")
    elif kind == "DIAGNOSTIC":
        for k in ("question", "subject"):
            if not isinstance(req[k], str) or not 0 < len(req[k]) <= 2000:
                raise ProposalRefused(f"request.{k}: 1..2000 characters")
    elif kind == "CODE_PATCH":
        if not isinstance(req["patch_sha256"], str) or \
                not _DIGEST.match(req["patch_sha256"]):
            raise ProposalRefused("request.patch_sha256 is not a digest: "
                                  "a patch is represented outside "
                                  "authority, by digest only")
        if not isinstance(req["files"], list) or not all(
                isinstance(f, str) and f and not f.startswith("/")
                and ".." not in f.split("/") for f in req["files"]):
            raise ProposalRefused("request.files: relative paths")
        if not isinstance(req["summary"], str) or len(req["summary"]) > 2000:
            raise ProposalRefused("request.summary: at most 2000 characters")


@dataclass(frozen=True)
class ProposalEnvelope:
    record: dict

    @property
    def proposal_id(self) -> str:
        return self.record["proposal_id"]

    @property
    def kind(self) -> str:
        return self.record["kind"]

    @property
    def agent_id(self) -> str:
        return self.record["agent_id"]

    @property
    def request(self) -> dict:
        return self.record["request"]

    @property
    def citations(self) -> tuple:
        return tuple(self.record["context"]["retrieved"])

    def digest(self) -> str:
        return digest(self.record)

    @classmethod
    def parse(cls, rec) -> "ProposalEnvelope":
        """Validate a raw record. Every refusal says what and where."""
        if not isinstance(rec, dict):
            raise ProposalRefused("a proposal is a JSON object")
        try:
            size = len(canonical_bytes(rec))
        except (TypeError, ValueError) as exc:
            raise ProposalRefused(f"not canonical JSON: {exc}") from exc
        if size > MAX_RECORD_BYTES:
            raise ProposalRefused(f"{size} bytes exceeds {MAX_RECORD_BYTES}")
        _walk(rec, "proposal")
        _exact(rec, FIELDS, "proposal")
        if rec["schema"] != SCHEMA:
            raise ProposalRefused(f"schema {rec['schema']!r} is not {SCHEMA}")
        if rec["authority"] != AUTHORITY:
            raise ProposalRefused(f"authority must be {AUTHORITY}: a "
                                  "proposal is never authoritative")
        if rec["kind"] not in KINDS:
            raise ProposalRefused(f"kind {rec['kind']!r} is not one of "
                                  f"{KINDS}")
        if not isinstance(rec["agent_id"], str) or \
                not _NAME.match(rec["agent_id"]):
            raise ProposalRefused("agent_id is not a name")
        src = _exact(rec["source"], SOURCE_KEYS, "source")
        if src["adapter"] not in ADAPTERS:
            raise ProposalRefused(f"source.adapter {src['adapter']!r} is "
                                  f"not one of {ADAPTERS}")
        for k in ("provider", "model"):
            if src[k] is not None and (not isinstance(src[k], str)
                                       or not _NAME.match(src[k])):
                raise ProposalRefused(f"source.{k} is not a name or null")
        ctx = _exact(rec["context"], CONTEXT_KEYS, "context")
        if not isinstance(ctx["digest"], str) or \
                not _DIGEST.match(ctx["digest"]):
            raise ProposalRefused("context.digest is not a digest")
        if not isinstance(ctx["retrieved"], list) or \
                len(ctx["retrieved"]) > 64:
            raise ProposalRefused("context.retrieved: at most 64 citations")
        for i, c in enumerate(ctx["retrieved"]):
            _exact(c, CITATION_KEYS, f"context.retrieved[{i}]")
            if not isinstance(c["path"], str) or c["path"].startswith("/") \
                    or ".." in c["path"].split("/"):
                raise ProposalRefused(f"context.retrieved[{i}].path is not "
                                      "a relative path")
            if not (isinstance(c["line_start"], int)
                    and isinstance(c["line_end"], int)
                    and 1 <= c["line_start"] <= c["line_end"]):
                raise ProposalRefused(f"context.retrieved[{i}]: line range")
            if not isinstance(c["source_sha256"], str) or \
                    not _DIGEST.match(c["source_sha256"]):
                raise ProposalRefused(f"context.retrieved[{i}]."
                                      "source_sha256 is not a digest")
        if not isinstance(rec["rationale_sha256"], str) or \
                not _DIGEST.match(rec["rationale_sha256"]):
            raise ProposalRefused("rationale_sha256 is not a digest: the "
                                  "rationale is carried by digest")
        parent = rec["parent_proposal_id"]
        if parent is not None and (not isinstance(parent, str)
                                   or not _ID.match(parent)):
            raise ProposalRefused("parent_proposal_id is not a proposal id")
        it = rec["iteration"]
        if isinstance(it, bool) or not isinstance(it, int) or \
                not 0 <= it <= 10_000:
            raise ProposalRefused("iteration: 0..10000")
        if (parent is None) != (it == 0):
            raise ProposalRefused("iteration 0 has no parent, and only "
                                  "iteration 0")
        cb = _exact(rec["created_by"], CREATED_KEYS, "created_by")
        if not isinstance(cb["adapter_version"], str) or \
                not _NAME.match(cb["adapter_version"]):
            raise ProposalRefused("created_by.adapter_version is not a name")
        if not isinstance(cb["input_digest"], str) or \
                not _DIGEST.match(cb["input_digest"]):
            raise ProposalRefused("created_by.input_digest is not a digest")
        _request(rec["kind"], rec["request"])
        if not isinstance(rec["proposal_id"], str) or \
                rec["proposal_id"] != proposal_id_of(rec):
            raise ProposalRefused("proposal_id is not the digest of the "
                                  "proposal's content")
        return cls(json.loads(canonical_bytes(rec)))


def seal(body: dict) -> dict:
    """A record from its body: the id is computed, never chosen."""
    rec = dict(body)
    rec.pop("proposal_id", None)
    rec["proposal_id"] = proposal_id_of(rec)
    return rec


# --------------------------------------------------------------- adapters
#
# An adapter yields RESPONSES -- what a proposer said: its kind, its
# request, its rationale, and the provider and model it CLAIMS to be. The
# envelope is built around the context the proposer was actually given, so
# a recorded response replays against today's reviewed sources (and a
# fixture cannot go stale just because a document was edited). Nothing an
# adapter returns is trusted: :func:`envelope` and the parse refuse it the
# same way whichever adapter it came from.

RESPONSE_KEYS = {"kind", "request", "rationale", "provider", "model"}


class RecordedFixtureAdapter:
    """Responses replayed from JSON lines: deterministic, offline, no
    provider, no secret -- and exactly as untrusted as any other.

    Handed the fixture's TEXT, not a path: the substrate reads files only
    through its governed readers, and a recorded response is the caller's
    data to supply, as a live client's would be."""

    name = "recorded-fixture"
    version = "1"

    def __init__(self, text: str, source: str = "fixture"):
        self.text = text
        self.source = source

    def responses(self, context: dict) -> Iterable[dict]:
        for n, line in enumerate(self.text.splitlines(), 1):
            if line.strip():
                try:
                    yield json.loads(line)
                except ValueError as exc:
                    raise ProposalRefused(f"{self.source}:{n}: not "
                                          f"JSON: {exc}") from exc


class ExternalClientAdapter:
    """Any client, behind one callable: ``respond(context) -> response``.

    No provider is imported or named here. A caller wires its own client
    (and its own credential, outside this repository); what comes back is
    held to the same envelope as a fixture's response and trusted for
    nothing. The provider and model it reports are recorded as claims."""

    name = "external-client"
    version = "1"

    def __init__(self, respond: Callable[[dict], Any]):
        self._respond = respond

    def responses(self, context: dict) -> Iterable[dict]:
        out = self._respond({k: context[k] for k in ("query", "hits",
                                                     "evidence_status")})
        yield from (out if isinstance(out, list) else [out])


def envelope(response, *, context: dict, agent_id: str, adapter,
             parent_proposal_id: str | None = None,
             iteration: int = 0) -> dict:
    """A sealed envelope from one untrusted response and its context."""
    if not isinstance(response, dict) or set(response) != RESPONSE_KEYS:
        raise ProposalRefused(
            f"a response has exactly {sorted(RESPONSE_KEYS)}")
    rationale = response["rationale"]
    if not isinstance(rationale, str) or len(rationale) > 20_000:
        raise ProposalRefused("rationale: text, at most 20000 characters")
    _walk(response, "response")
    rec = {"schema": SCHEMA, "kind": response["kind"], "agent_id": agent_id,
           "source": {"adapter": adapter.name,
                      "provider": response["provider"],
                      "model": response["model"]},
           "context": {"digest": context["digest"],
                       "retrieved": citations(context)},
           "request": response["request"],
           "rationale_sha256": digest(rationale),
           "parent_proposal_id": parent_proposal_id,
           "iteration": iteration,
           "created_by": {"adapter_version": f"{adapter.name}-"
                                             f"{adapter.version}",
                          "input_digest": digest({"context": context[
                              "digest"], "response": response})},
           "authority": AUTHORITY}
    sealed = seal(rec)
    ProposalEnvelope.parse(sealed)
    return sealed


# ---------------------------------------------------------------- context

def assemble_context(query: str, *, retrieve: Callable, k: int = 5) -> dict:
    """Governed read-only retrieval over reviewed documents, as a proposal
    context: cited spans, each stamped NOT_EVIDENCE, and their digest.

    ``retrieve`` is injected (``qta_multiphysics.stack.rag_index.retrieve``
    in production): deterministic BM25 over an allowlisted corpus, no
    network, no model client, no embedding service."""
    res = retrieve(query, k=k)
    hits = []
    for h in res.get("hits", []):
        if h.get("evidence_status") != EVIDENCE_STATUS:
            raise ProposalRefused("a retrieved span not stamped "
                                  f"{EVIDENCE_STATUS}")
        hits.append({"path": h["path"], "line_start": h["line_start"],
                     "line_end": h["line_end"],
                     "source_sha256": h["source_sha256"],
                     "text_sha256": digest(h["text"]),
                     "text": h["text"],
                     "evidence_status": EVIDENCE_STATUS})
    ctx = {"query": query, "k": k, "hits": hits,
           "generation": "NONE", "evidence_status": EVIDENCE_STATUS}
    ctx["digest"] = digest({k_: v for k_, v in ctx.items()})
    return ctx


def citations(ctx: dict) -> list:
    return [{k: h[k] for k in sorted(CITATION_KEYS)} for h in ctx["hits"]]


# ---------------------------------------------------------------- ingress

def received(log) -> dict:
    """proposal_id -> what was received, projected from the log.

    Refuses a history in which one id names two different envelopes, or a
    receipt whose stated digest is not its envelope's."""
    out: dict = {}
    # The report is the verdict: a broken chain, or a history that requires
    # authentication read without an authenticator, refuses here as it does
    # in every other reader -- never an empty projection.
    report, events = log.read_verified()
    report.raise_if_bad()
    return _fold_receipts(events, out)


def _fold_receipts(events, out: dict) -> dict:
    """Fold the proposal.receive records among ``events`` into ``out``. The
    one reading of a receipt: :func:`received` over a whole verified read,
    and an ingress catching up over what it has not yet seen."""
    for ev in events:
        if ev.action != ACT_PROPOSAL_RECEIVE:
            continue
        p = ev.payload if isinstance(ev.payload, dict) else {}
        env = ProposalEnvelope.parse(p.get("envelope"))
        if p.get("envelope_digest") != env.digest():
            raise ProposalRefused(f"seq {ev.seq}: receipt digest mismatch")
        if ev.target != env.proposal_id:
            raise ProposalRefused(f"seq {ev.seq}: receipt target is not "
                                  "the proposal id")
        # One id cannot name two contents: the parse holds the id to the
        # digest of the content, so a second receipt of an id is the same
        # envelope and the first stands.
        out.setdefault(env.proposal_id, {
            "digest": env.digest(), "agent_id": env.agent_id,
            "kind": env.kind, "seq": ev.seq, "envelope": env})
    return out


@dataclass(frozen=True)
class Receipt:
    envelope: ProposalEnvelope
    seq: int
    duplicate: bool


class _AlreadyReceived(Exception):
    """Raised inside a decided append: the receipt is already in the log,
    so nothing is written and the existing one is the answer."""

    def __init__(self, have: dict):
        super().__init__(have.get("seq"))
        self.have = have


class ProposalIngress:
    """Receives proposals into the log and submits them to governance."""

    def __init__(self, log, *, actor: str = INGRESS_ID,
                 source_digest: Callable[[str], str | None] | None = None):
        self.log = log
        self.actor = actor
        #: path -> the SHA-256 of the reviewed source now, or None. Used to
        #: refuse a proposal citing a span whose source has changed.
        self.source_digest = source_digest
        self._received: dict = {}
        self._folded_through = -1
        self._anchor: Any = None

    def _catch_up(self) -> dict:
        """The receipts, folded in O(new) since this ingress last read.

        Every receipt used to re-read and re-verify the whole log to ask
        whether its proposal was already there: quadratic over a long
        history, the class the long-horizon campaign exists to catch, found
        reading this path when proposals were added to that campaign
        (D-2026-119). Anchored as the scheduler, the store and the
        idempotency ledger are; a whole verified read when there is no
        anchor or the anchor no longer describes the bytes at its offset.
        """
        if self._anchor is not None:
            try:
                events, self._anchor = self.log.advance(self._anchor)
            except EventLogError:
                self._anchor = None
            else:
                _fold_receipts([ev for ev in events
                                if ev.seq > self._folded_through],
                               self._received)
                if events:
                    self._folded_through = max(self._folded_through,
                                               events[-1].seq)
                return self._received
        report, events = self.log.read_verified()
        report.raise_if_bad()
        self._received = _fold_receipts(events, {})
        self._folded_through = events[-1].seq if events else -1
        self._anchor = (self.log.anchor_at(self._folded_through)
                        if self._folded_through >= 0 else None)
        return self._received

    def receive(self, raw) -> Receipt:
        env = ProposalEnvelope.parse(raw)
        if env.agent_id == self.actor:
            raise ProposalRefused("a proposing agent cannot be the ingress "
                                  "that receives it")
        if self.source_digest is not None:
            for c in env.citations:
                now = self.source_digest(c["path"])
                if now != c["source_sha256"]:
                    raise ProposalRefused(
                        f"cites {c['path']}:{c['line_start']}-"
                        f"{c['line_end']} at {c['source_sha256'][:12]}, "
                        f"which is now {str(now)[:12]}: a stale context")
        def decide(head_seq: int) -> dict:
            # Decided under the writer lock, against the head the receipt is
            # written onto: two ingress processes receiving one proposal at
            # once append ONE receipt between them. Checked before the lock
            # and appended after it, both did (D-2026-115).
            have = self._catch_up().get(env.proposal_id)
            if have is not None:
                raise _AlreadyReceived(have)
            return {"actor": self.actor, "action": ACT_PROPOSAL_RECEIVE,
                    "target": env.proposal_id,
                    "payload": {"envelope": env.record,
                                "envelope_digest": env.digest(),
                                "received_by": self.actor,
                                "authority": AUTHORITY}}

        try:
            ev = self.log.append_decided(decide)
        except _AlreadyReceived as already:
            return Receipt(env, already.have["seq"], True)
        _fold_receipts([ev], self._received)
        self._folded_through = max(self._folded_through, ev.seq)
        return Receipt(env, ev.seq, False)

    def pending(self, submitted: Callable[[str], bool]) -> list:
        """Received MODEL_RUN proposals not yet submitted: what recovery
        resubmits, under the same idempotency key."""
        return [r["envelope"] for pid, r in sorted(self._catch_up().items())
                if r["kind"] == "MODEL_RUN" and not submitted(pid)]

    def submit(self, proposal_id: str, runs, *, out_dir: str,
               fmu: dict | None = None):
        """Hand a received MODEL_RUN proposal to the governed model path.

        The proposal's agent is the submitter; the output location is the
        ingress's choice; the proposal id is the idempotency key, so a
        resubmission after a crash returns the first task, finished where it
        stopped (D-2026-110). A proposal for
        the FMU is run against the FMU the CALLER names in ``fmu`` (path,
        digests, build record, runtime): a proposal names a model, never a
        binary or a path."""
        rec = self._catch_up().get(proposal_id)
        if rec is None:
            raise ProposalRefused(f"{proposal_id} was never received; only "
                                  "a recorded proposal is submitted")
        env = rec["envelope"]
        if env.kind != "MODEL_RUN":
            raise ProposalRefused(f"{proposal_id} is {env.kind}; only "
                                  "MODEL_RUN is executed, the rest is "
                                  "recorded for a person")
        req = env.request
        if req["model_id"] == FMU_MODEL:
            if fmu is None:
                raise ProposalRefused("an FMU proposal needs the FMU the "
                                      "ingress has admitted")
            return runs.propose_fmu(parameters=req["parameters"],
                                    out_dir=out_dir, submitter=env.agent_id,
                                    idempotency_key=proposal_id, **fmu)
        return runs.propose(model_id=req["model_id"],
                            model_version=req["model_version"],
                            parameters=req["parameters"], out_dir=out_dir,
                            submitter=env.agent_id, reuse=False,
                            idempotency_key=proposal_id)
