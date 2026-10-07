"""A learned model's lifecycle, recorded in the authority history (NF-1T).

The events directive NF-1T s.48 names -- DATASET_REGISTERED,
MODEL_ARCHITECTURE_REGISTERED, TRAINING_STARTED / COMPLETED / RESUMED,
CHECKPOINT_CREATED, EVALUATION_COMPLETED, MODEL_REJECTED,
MODEL_ACCEPTED_FOR_LIMITED_USE, MODEL_INVALIDATED -- are recorded with the
actions every other subsystem's records use, so there is ONE authority log
and no second source of truth:

=============================  ==========================================
event                          how it is recorded
=============================  ==========================================
DATASET_REGISTERED             record.create, kind learned_dataset
MODEL_ARCHITECTURE_REGISTERED  record.create, kind learned_architecture
(meta validation)              record.create, kind learned_meta_validation
TRAINING_STARTED / COMPLETED   record.create, kind learned_training_run;
/ RESUMED                      the manifest's completion_state and
                               resume_semantics say which
CHECKPOINT_CREATED             record.create, kind learned_checkpoint
EVALUATION_COMPLETED           record.create, kind learned_evaluation
MODEL_REJECTED                 record.transition to REJECTED
MODEL_INVALIDATED              record.transition to REVOKED; dependents go
                               STALE through the ordinary cascade
MODEL_ACCEPTED_FOR_LIMITED_    REFUSED: VERIFIED / PROMOTED of a learned
USE                            record (``learned_rules``)
=============================  ==========================================

Each record cites ONE evidence item -- the canonical bytes of its document,
content-addressed -- and depends on the records it was made from. Before a
record is written its links are checked by digest against the documents of
the records it depends on (:data:`LINKS`): a training run must name the
registered dataset's digest and the registered architecture's configuration
digest, a checkpoint the training manifest's digest, an evaluation the
checkpoint manifest's digest and the dataset's. A link that does not hold
is refused and nothing is written. Nothing is ever edited: a later state is
a later record or a later transition.

This module reads JSON documents and compares digests. It imports nothing
from ``scientific`` or ``scientific_ai``.
"""
from __future__ import annotations

import json

from .authority import Role, State
from .canonical import canonical_bytes, digest
from .learned_rules import KINDS


class LedgerError(ValueError):
    pass


def _eq(a, b):
    return a == b


#: kind -> ((upstream kind, check(document, upstream) -> problem or None)).
LINKS = {
    "learned_meta_validation": (
        ("learned_architecture",
         lambda d, u: None if d["config_digest"]
         == u["configuration_digest"] else "meta report is about another "
                                           "configuration"),),
    "learned_training_run": (
        ("learned_dataset",
         lambda d, u: None if d["dataset_digest"] == u["dataset_digest"]
         and d["feature_schema_digest"] == u["feature_schema_digest"]
         and d["target_schema_digest"] == u["target_schema_digest"]
         and d["normalization_digest"] == digest(
             u["normalization_statistics"])
         else "the run names another dataset, schema or normalisation"),
        ("learned_architecture",
         lambda d, u: None if d["model_configuration_digest"]
         == u["configuration_digest"] else "the run trained another "
                                           "configuration")),
    "learned_checkpoint": (
        ("learned_training_run",
         lambda d, u: None if d["training_manifest_digest"] == digest(u)
         and d["training_run_id"] == u["run_id"]
         and d["model_config_digest"] == u["model_configuration_digest"]
         and d["dataset_digest"] == u["dataset_digest"]
         else "the checkpoint does not belong to this training run"),),
    "learned_evaluation": (
        ("learned_checkpoint",
         lambda d, u: None if d["checkpoint_manifest_digest"] == digest(u)
         and d["checkpoint_digest"] == u["checkpoint_digest"]
         else "the evaluation is of another checkpoint"),
        ("learned_dataset",
         lambda d, u: None if d["dataset_digest"] == u["dataset_digest"]
         else "the evaluation used another dataset")),
}


class LearnedLedger:
    """Write and read learned-lifecycle records through ``store``."""

    def __init__(self, store, evidence):
        if evidence is None:
            raise LedgerError("a learned ledger needs an evidence store: a "
                              "record's document must resolve")
        self.store = store
        self.evidence = evidence

    def document(self, record_id: str) -> dict:
        rec = self.store.get(record_id)
        raw = self.evidence.get(rec.evidence["document"])
        return json.loads(raw)

    def register(self, document: dict, *, actor: str,
                 depends_on: tuple = ()) -> str:
        """Record ``document``; returns the record id. Refused, writing
        nothing, when a link to ``depends_on`` does not hold."""
        schema = document.get("schema") if isinstance(document, dict) \
            else None
        if schema not in KINDS:
            raise LedgerError(f"not a learned-lifecycle document: "
                              f"{schema!r}")
        kind = KINDS[document["schema"]]
        ups = {}
        for rid in depends_on:
            rec = self.store.get(rid)
            if rec.state in (State.REVOKED, State.REJECTED, State.STALE):
                raise LedgerError(f"{rid} is {rec.state.value}; nothing new "
                                  "is built on it")
            ups.setdefault(rec.kind, []).append(self.document(rid))
        for up_kind, check in LINKS.get(kind, ()):
            if up_kind not in ups:
                raise LedgerError(f"a {kind} record depends on a {up_kind} "
                                  "record; none was named")
            for up in ups[up_kind]:
                why = check(document, up)
                if why is not None:
                    raise LedgerError(f"{kind}: {why}")
        dg = self.evidence.put(canonical_bytes(document),
                               media_type="application/json")
        rid = f"{kind}:{dg[:16]}"
        self.store.create(record_id=rid, kind=kind, proposer=actor,
                          evidence={"document": dg},
                          depends_on=tuple(depends_on))
        return rid

    def reject(self, record_id: str, *, actor: str, reason_digest: str):
        """MODEL_REJECTED: a reviewer who is not the proposer."""
        return self.store.transition(
            record_id=record_id, dst=State.REJECTED, actor=actor,
            role=Role.VERIFIER, evidence={"rejection_reason": reason_digest})

    def documents(self) -> list:
        """Every learned document this history holds, in record order."""
        out = []
        for rid, rec in sorted(self.store.all_records().items()):
            if rec.kind in KINDS.values():
                out.append(self.document(rid))
        return out
