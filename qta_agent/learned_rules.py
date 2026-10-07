"""No learned record becomes authority -- not in this tranche (NF-1T).

A learned model's history is recorded in the authority log as ordinary
records (``record.create`` / ``record.depend``) of kinds that begin with
``learned_``: a dataset, an architecture, a meta-validation, a training run,
a checkpoint, an evaluation (``qta_agent.learned_lifecycle``). Such a record
can be PROPOSED, depended on, rejected, revoked and made STALE by the edges
every record has.

What it cannot be is VERIFIED or PROMOTED. For a scientific_result those
edges require an admission policy (``result_rules``): a PASS from an
independent implementation about THIS bundle. No admission policy for a
learned model exists yet -- none says what evaluation, against which
independent reference, would let a learned prediction stand for a governed
calculation -- and an edge with no policy is an edge with no rule. So the
store refuses it on the edge itself, live and on replay, and the second
reader (``reconstruct``) restates the same refusal independently.

Writing a policy is a decision for the owner of this programme, made in a
later tranche with its own evidence; this module is where it would be
enforced.
"""
from __future__ import annotations

KIND_PREFIX = "learned_"
REFUSED_STATES = ("VERIFIED", "PROMOTED")

#: document schema -> the record kind that carries it.
KINDS = {
    "scientific-dataset-manifest/1": "learned_dataset",
    "scientific-moe-model-manifest/1": "learned_architecture",
    "scientific-moe-meta-validation/1": "learned_meta_validation",
    "scientific-moe-training-manifest/1": "learned_training_run",
    "scientific-moe-checkpoint-manifest/1": "learned_checkpoint",
    "scientific-moe-evaluation/1": "learned_evaluation",
    "scientific-moe-distributed/1": "learned_distributed",
}


def is_learned(kind) -> bool:
    return isinstance(kind, str) and kind.startswith(KIND_PREFIX)


def refusal(kind, dst) -> str | None:
    """Why ``kind`` may not move to ``dst``; None when nothing here
    objects (the ordinary edge rules still apply)."""
    value = getattr(dst, "value", dst)
    if is_learned(kind) and value in REFUSED_STATES:
        return (f"a {kind} record cannot become {value}: no admission "
                "policy for learned models exists, and a learned output is "
                "LEARNED_PREDICTION, NON_AUTHORITATIVE, "
                "REQUIRES_EXTERNAL_VERIFICATION")
    return None
