"""What a scientific result must show before its authority record is VERIFIED.

The authority edges check roles, separation of duties and that cited
evidence EXISTS. For most records that is the whole rule. For a record of
kind ``scientific_result`` it is not enough: an existing report can say FAIL,
can be about a different bundle, or can come from the producer's own code,
and every one of those still resolves in the evidence store. So the store
reads the two documents the record cites -- the ResultBundle
(``result_bundle``) and the VerificationResult (``verification_report``) --
and refuses the edge into VERIFIED or PROMOTED unless:

* the report is about THIS bundle (its ``subject_digest`` is the canonical
  digest of the cited bundle record);
* the report's verdict is PASS;
* it is an INDEPENDENT_IMPLEMENTATION check, and names this bundle's
  implementation as the producer;
* the verifying code is not the producer's (a different implementation
  digest, which is what "independent" means in a checkable form);
* the bundle carries at least one invariant and every one holds.

The rule reads JSON. It deliberately does not import ``scientific``: the
authority layer must not depend on the numerics it governs, so the rule is
stated here over the records' fields, and ``tests/test_agent_result_rules.py``
holds it to documents the ``scientific`` package actually writes.

It is enforced where the transition is decided (``AuthorityStore``), not
re-derived on replay: replay folds decisions the log already recorded, and
must not depend on an evidence store that an archival policy may have pruned
-- the same reason :func:`~qta_agent.authority.check` makes resolution
optional on replay.
"""
from __future__ import annotations

import json

from .canonical import digest, is_digest

#: The record kind this rule governs.
KIND = "scientific_result"


def verification_problems(bundle: dict, report: dict) -> list:
    """Every reason ``report`` does not support ``bundle``; [] when it does."""
    problems = []
    if not isinstance(bundle, dict) or not isinstance(report, dict):
        return ["the cited bundle or report is not a JSON object"]
    bundle_digest = digest(bundle)
    if report.get("subject_digest") != bundle_digest:
        problems.append("the report is about a different bundle")
    if report.get("status") != "PASS":
        problems.append(f"the check reported {report.get('status')}")
    if report.get("check_type") != "INDEPENDENT_IMPLEMENTATION":
        problems.append("the report is not an independent check")
    producer = bundle.get("implementation_digest")
    if report.get("producer_implementation_digest") != producer:
        problems.append("the report names another producer")
    verifier = report.get("verifier_implementation_digest")
    if not is_digest(verifier) or verifier == producer:
        problems.append("the check ran the producer's own code")
    invariants = bundle.get("invariants") or []
    bad = [i.get("invariant_id") for i in invariants
           if not isinstance(i, dict) or i.get("holds") is not True]
    if not invariants or bad:
        problems.append(f"invariants not holding: {bad or 'none run'}")
    return problems


def record_problems(evidence: dict, fetch) -> list:
    """Apply the rule to a record's cited evidence. ``fetch(digest)`` returns
    the stored bytes; anything it cannot return is a problem, not a pass."""
    docs = {}
    for key in ("result_bundle", "verification_report"):
        sha = evidence.get(key)
        if not is_digest(sha):
            return [f"the record cites no {key}"]
        try:
            docs[key] = json.loads(fetch(sha))
        except Exception as exc:                     # noqa: BLE001
            return [f"{key} {sha[:12]} cannot be read as JSON "
                    f"({type(exc).__name__})"]
    return verification_problems(docs["result_bundle"],
                                 docs["verification_report"])
