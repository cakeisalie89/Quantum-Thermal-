"""What a scientific result must show before its authority record is VERIFIED.

The authority edges check roles, separation of duties and that cited
evidence EXISTS. For most records that is the whole rule. For a record of
kind ``scientific_result`` it is not enough: an existing report can say FAIL,
can be about a different bundle, can come from the producer's own code, or
can be a well-formed document nobody's governed verification ever produced --
and every one of those still resolves in the evidence store. So the store
reads the two documents the record cites -- the ResultBundle
(``result_bundle``) and the VerificationResult (``verification_report``) --
and refuses the edge into VERIFIED or PROMOTED unless the ADMISSION POLICY the
transition names is satisfied.

THREE MEANINGS OF "VERIFIED", KEPT APART

* *Artifact integrity* -- a digest: the same declared bytes. The evidence
  store establishes it on every read.
* *Scientific verification* -- a VerificationResult: a bounded numerical
  check passed. A document, produced by a governed verification task.
* *Authority* -- the record's state: evidence judged sufficient for a
  declared purpose, by a reviewer who is none of the parties above.

This module decides the second feeds the third. It never infers the second
from the first.

ADMISSION POLICY ``scientific_result.admission/1``

* the bundle and the report parse EXACTLY: a JSON object with precisely the
  key set the scientific package writes (restated here as data, because this
  layer must not import the numerics it governs);
* the report is about THIS bundle (``subject_digest`` is the canonical digest
  of the cited bundle record), and says PASS;
* it is an INDEPENDENT_IMPLEMENTATION check that establishes
  INDEPENDENT_NUMERICAL_AGREEMENT, declares an independence class, and names
  this bundle's implementation as the producer;
* the verifying code is not the producer's (a different implementation
  digest);
* the bundle is a SIMULATION_RESULT carrying at least one invariant, and
  every one holds -- numerical verification admits a simulation result and
  nothing else: it cannot establish experimental validation;
* ORIGIN: the report is an artefact of a VERIFIED governed verification task
  of an admitted verifier tool, the bundle an artefact of a VERIFIED governed
  model run, both completed before the transition, and the verification
  executed by an actor who neither proposed the result, ran the model, nor
  decides the transition. A well-formed PASS written into the evidence store
  by hand satisfies every content rule above and fails this one.

Policies are named and versioned. A transition records the policy it was
admitted under; replay resolves THAT policy, and a name this code does not
know is refused rather than reinterpreted under whatever the current rule is.
Changing the rule means a new name, and moving old authority onto it is an
explicit migration, not an upgrade that happens to the history.

EVIDENCE RETENTION

Evidence required to establish current scientific authority must remain
available and content-verifiable for as long as that authority is claimed as
reconstructible. Replay distinguishes the two ways it can fail:

* evidence that resolves and does NOT support the transition: the transition
  is refused, as any unauthorized transition is. Presence in the log is not
  authority.
* evidence that does not resolve here (archived, lost, or a reader with no
  evidence store or no view of governed execution): the record keeps the
  position the log walked it to, and its admission is ``UNVERIFIABLE``. It
  is not canonical and it is not reusable. It is never silently VERIFIED.

The rule reads JSON. It deliberately does not import ``scientific``.
``tests/test_agent_result_rules.py`` holds it to documents the scientific
package actually writes, and :mod:`qta_agent.reconstruct` restates it in its
own code, so a weakened rule here is not reproduced by the second reader.
"""
from __future__ import annotations

import json

from .canonical import digest, is_digest

#: The record kind this rule governs.
KIND = "scientific_result"

#: The admission policies this code can decide under, by name. A durable
#: transition names one; replay refuses a name that is not here.
POLICY_V1 = "scientific_result.admission/1"
CURRENT_POLICY = POLICY_V1

#: The admission outcome of a scientific_result record in VERIFIED or
#: PROMOTED. Empty for every other record.
ADMITTED = "ADMITTED"
UNVERIFIABLE = "UNVERIFIABLE"

#: Exactly the fields ``scientific.result.ResultBundle.to_record`` and
#: ``scientific.verification.VerificationResult.to_record`` write. A
#: conformance test holds these to the scientific package.
BUNDLE_KEYS = frozenset({
    "artifacts", "convergence", "environment_digest", "implementation_digest",
    "invariants", "model_id", "model_version", "observation_kind", "outputs",
    "parameter_digest", "parameters", "provenance", "schema_version", "seeds",
    "solver_config", "warnings"})
REPORT_KEYS = frozenset({
    "check_id", "check_type", "criterion", "criterion_derivation",
    "establishes", "evidence", "independence", "limitations", "measured",
    "observations", "producer_implementation_digest", "shared_components",
    "status", "subject_digest", "subject_model", "threshold", "verifier_id",
    "verifier_implementation_digest"})


class EvidenceUnavailable(Exception):
    """What the policy needs cannot be read here. Not a refusal: the answer
    is UNVERIFIABLE, and the reason is the message."""


def verification_problems(bundle: dict, report: dict) -> list:
    """Every content reason ``report`` does not support ``bundle``; [] when
    it does. Origin is not decided here -- see :func:`admission_problems`."""
    if not isinstance(bundle, dict) or not isinstance(report, dict):
        return ["the cited bundle or report is not a JSON object"]
    problems = []
    if set(bundle) != BUNDLE_KEYS:
        problems.append("the cited bundle is not a ResultBundle record "
                        f"(fields {sorted(set(bundle) ^ BUNDLE_KEYS)})")
    if set(report) != REPORT_KEYS:
        problems.append("the cited report is not a VerificationResult record "
                        f"(fields {sorted(set(report) ^ REPORT_KEYS)})")
    bundle_digest = digest(bundle)
    if report.get("subject_digest") != bundle_digest:
        problems.append("the report is about a different bundle")
    if report.get("status") != "PASS":
        problems.append(f"the check reported {report.get('status')}")
    if report.get("check_type") != "INDEPENDENT_IMPLEMENTATION":
        problems.append("the report is not an independent check")
    if report.get("establishes") != "INDEPENDENT_NUMERICAL_AGREEMENT":
        problems.append(
            f"the report claims to establish {report.get('establishes')}; "
            "numerical verification admits INDEPENDENT_NUMERICAL_AGREEMENT "
            "and cannot establish experimental validation")
    if report.get("independence") in (None, "NONE"):
        problems.append("the report declares no independence")
    producer = bundle.get("implementation_digest")
    if report.get("producer_implementation_digest") != producer:
        problems.append("the report names another producer")
    verifier = report.get("verifier_implementation_digest")
    if not is_digest(verifier) or verifier == producer:
        problems.append("the check ran the producer's own code")
    if bundle.get("observation_kind") != "SIMULATION_RESULT":
        problems.append(
            f"the bundle is a {bundle.get('observation_kind')}, and this "
            "policy admits simulation results only")
    invariants = bundle.get("invariants") or []
    bad = [i.get("invariant_id") if isinstance(i, dict) else i
           for i in invariants
           if not isinstance(i, dict) or i.get("holds") is not True]
    if not invariants or bad:
        problems.append(f"invariants not holding: {bad or 'none run'}")
    return problems


def _read(evidence: dict, fetch) -> dict:
    docs = {}
    for key in ("result_bundle", "verification_report"):
        sha = evidence.get(key)
        if not is_digest(sha):
            docs[key] = None
            continue
        try:
            raw = fetch(sha)
        except Exception as exc:                     # noqa: BLE001
            raise EvidenceUnavailable(
                f"{key} {sha[:12]} does not resolve here "
                f"({type(exc).__name__})") from None
        try:
            docs[key] = json.loads(raw)
        except ValueError as exc:
            docs[key] = exc
    return docs


def record_problems(evidence: dict, fetch) -> list:
    """The content rule applied to a record's cited evidence. ``fetch(digest)``
    returns the stored bytes; bytes that are not JSON are a problem, not a
    pass. Raises :class:`EvidenceUnavailable` when a cited digest does not
    resolve."""
    docs = _read(evidence, fetch)
    for key in ("result_bundle", "verification_report"):
        if docs[key] is None:
            return [f"the record cites no {key}"]
        if isinstance(docs[key], Exception):
            sha = evidence[key]
            return [f"{key} {sha[:12]} cannot be read as JSON "
                    f"({type(docs[key]).__name__})"]
    return verification_problems(docs["result_bundle"],
                                 docs["verification_report"])


def admission_problems(policy: str | None, evidence: dict, *, fetch,
                       origins, proposer: str, actor: str,
                       before_seq: int | None) -> list:
    """Every reason a transition into VERIFIED or PROMOTED under ``policy``
    is not admissible; [] when it is.

    Raises :class:`EvidenceUnavailable` when the decision cannot be made
    here: no evidence store (``fetch`` is None), a cited digest that does not
    resolve, or no view of governed execution (``origins`` is None).
    ``before_seq`` is the position the transition occupies (None: the head),
    so origin is judged against what had happened by then.
    """
    if policy != POLICY_V1:
        return [f"admission policy {policy!r} is not one this code can "
                "decide under; old authority is not reinterpreted under a "
                "newer rule"]
    if fetch is None:
        raise EvidenceUnavailable("no evidence store is attached, so the "
                                  "cited bundle and report cannot be read")
    problems = record_problems(evidence, fetch)
    if problems:
        return problems
    if origins is None:
        raise EvidenceUnavailable(
            "no view of governed execution is attached, so the report's "
            "origin cannot be established")
    return list(origins.problems(
        report_sha=evidence["verification_report"],
        bundle_sha=evidence["result_bundle"], proposer=proposer,
        actor=actor, before_seq=before_seq))
