"""The documents a learned model's history is made of, and how they link.

Seven kinds, each a JSON object with a ``schema`` naming it, an exact key
set, and an identity -- ``scientific.identity.digest`` of the document:

    model manifest          what an architecture IS (counts, estimates)
    meta-validation report  that it resolves without allocating it
    dataset manifest        which samples, which splits, which statistics
    training manifest       one training run: what it trained, on what
    checkpoint manifest     one saved state: its bytes and its lineage
    evaluation report       what one checkpoint did on held-out splits
    distributed report      what the parallel plans did on which devices

THE CHAIN (directive s.60)

    source -> dataset -> training run -> checkpoint -> evaluation

:func:`check_chain` refuses a link whose digests disagree: a checkpoint
naming a training run that names a different dataset, an evaluation of a
checkpoint whose bytes are not the ones recorded, a training manifest bound
to one configuration and a checkpoint bound to another. A link is checked
by digest, never by name.

UNAVAILABLE IS A VALUE

A provenance field this run cannot know is written
``{"unavailable": "<why>"}`` -- never omitted, never null, never filled in.
"""
from __future__ import annotations

from scientific.identity import digest, is_digest

MODEL_MANIFEST = "scientific-moe-model-manifest/1"
META_REPORT = "scientific-moe-meta-validation/1"
DATASET_MANIFEST = "scientific-dataset-manifest/1"
TRAINING_MANIFEST = "scientific-moe-training-manifest/1"
CHECKPOINT_MANIFEST = "scientific-moe-checkpoint-manifest/1"
EVALUATION_REPORT = "scientific-moe-evaluation/1"
DISTRIBUTED_REPORT = "scientific-moe-distributed/1"

#: Directive s.34: what a run that starts from a saved state IS.
RESUME_SEMANTICS = ("FRESH_TRAINING", "EXACT_RESUME", "WARM_START",
                    "FINE_TUNE", "CONTINUED_PRETRAINING")
#: Of these, only EXACT_RESUME continues the SAME lineage; every other
#: start from a checkpoint begins a new run that cites its source.
SAME_LINEAGE = ("EXACT_RESUME",)
COMPLETION_STATES = ("COMPLETED", "INTERRUPTED", "FAILED", "RUNNING")
#: Directive s.29, weakest to strongest. A run states the ones its
#: evidence supports and no others.
REPRODUCIBILITY_LEVELS = ("CONFIGURATION_REPRODUCIBLE",
                          "DATASET_REPRODUCIBLE", "SEEDED_EXECUTION",
                          "NUMERICALLY_REPRODUCIBLE",
                          "CHECKPOINT_REPRODUCIBLE", "BITWISE_REPRODUCIBLE")

KEYS = {
    MODEL_MANIFEST: (
        "schema", "schema_version", "architecture_family",
        "architecture_variant", "configuration_digest", "configuration",
        "total_parameters", "trainable_parameters",
        "non_trainable_parameters", "shared_parameters",
        "attention_parameters", "expert_parameters", "router_parameters",
        "embedding_parameters", "output_parameters",
        "uncertainty_head_parameters", "moe_layers",
        "experts_per_moe_layer", "experts_selected_per_token",
        "expert_parameters_per_expert", "shared_active_parameters",
        "expert_active_parameters", "estimated_active_parameters_per_token",
        "routing_profiles", "hidden_size", "num_layers",
        "num_attention_heads", "num_kv_heads", "head_dimension",
        "expert_hidden_size", "parameter_accounting",
        "precision_memory_estimates", "allocation_mode", "solve",
        "claim_status", "source_commit"),
    META_REPORT: (
        "schema", "config_digest", "framework", "method", "allocation_mode",
        "analytic_by_category", "abstract_by_category",
        "abstract_trainable", "abstract_non_trainable", "counts_equal",
        "abstract_dtypes", "forward", "routing", "allocation_guard",
        "real_allocation_refused", "result"),
    DATASET_MANIFEST: (
        "schema", "schema_version", "dataset_id", "dataset_digest",
        "sample_count", "feature_schema", "feature_schema_digest",
        "target_schema", "target_schema_digest", "source_identifiers",
        "splits", "split_digests", "normalization_statistics",
        "rejected_sample_count", "rejections", "exclusion_rules",
        "generation_method", "leakage", "storage"),
    TRAINING_MANIFEST: (
        "schema", "run_id", "source_commit", "model_configuration_digest",
        "parameter_count", "dataset_digest", "feature_schema_digest",
        "target_schema_digest", "normalization_digest",
        "initialization_seed", "data_order_seed", "precision", "optimizer",
        "optimizer_configuration", "scheduler", "scheduler_configuration",
        "batch_size", "gradient_accumulation", "world_size",
        "distributed_topology", "hardware_identity", "framework_versions",
        "backend_versions", "checkpoint_interval", "loss_definition",
        "evaluation_configuration", "start_time", "end_time", "steps",
        "resume_semantics", "parent_checkpoint", "parent_run",
        "completion_state", "loss_history", "router_health",
        "reproducibility", "checkpoints"),
    CHECKPOINT_MANIFEST: (
        "schema", "checkpoint_digest", "checkpoint_format",
        "model_config_digest", "dataset_digest", "training_run_id",
        "training_manifest_digest", "training_step", "epoch", "precision",
        "optimizer_state_presence", "shard_count", "shards",
        "tensor_count", "tensor_index_digest", "parent_checkpoint",
        "source_commit", "data_position", "rng"),
    EVALUATION_REPORT: (
        "schema", "model_config_digest", "checkpoint_digest",
        "checkpoint_manifest_digest", "dataset_digest",
        "normalization_digest", "semantics", "splits", "per_target",
        "calibration", "ood", "constraints", "router", "reload",
        "limitations"),
    DISTRIBUTED_REPORT: (
        "schema", "execution_profile", "framework", "devices", "checks",
        "result", "limitations"),
}


class ManifestError(ValueError):
    pass


def unavailable(why: str) -> dict:
    if not isinstance(why, str) or not why.strip():
        raise ManifestError("an unavailable field must say why")
    return {"unavailable": why}


def is_unavailable(v) -> bool:
    return isinstance(v, dict) and set(v) == {"unavailable"} \
        and isinstance(v["unavailable"], str) and bool(v["unavailable"])


def problems(doc) -> list:
    """Structural problems of one document; [] when it is well formed."""
    if not isinstance(doc, dict):
        return ["not a JSON object"]
    schema = doc.get("schema")
    if schema not in KEYS:
        return [f"unknown schema {schema!r}"]
    out = []
    want = set(KEYS[schema])
    if set(doc) != want:
        missing = sorted(want - set(doc))
        extra = sorted(set(doc) - want)
        out.append(f"{schema}: missing {missing}, unexpected {extra}")
        return out
    try:
        digest(doc)
    except ValueError as exc:
        out.append(f"{schema}: not canonically serialisable: {exc}")
    for key in ("configuration_digest", "config_digest", "dataset_digest",
                "model_configuration_digest", "model_config_digest",
                "checkpoint_digest", "feature_schema_digest",
                "target_schema_digest", "training_manifest_digest",
                "checkpoint_manifest_digest", "normalization_digest",
                "tensor_index_digest"):
        if key in doc and not is_digest(doc[key]):
            out.append(f"{schema}: {key} is not a sha256 digest")
    if schema == TRAINING_MANIFEST:
        if doc["resume_semantics"] not in RESUME_SEMANTICS:
            out.append(f"resume_semantics {doc['resume_semantics']!r}")
        if doc["completion_state"] not in COMPLETION_STATES:
            out.append(f"completion_state {doc['completion_state']!r}")
        fresh = doc["resume_semantics"] == "FRESH_TRAINING"
        if fresh != (doc["parent_checkpoint"] is None):
            out.append("a FRESH_TRAINING run has no parent checkpoint, and "
                       "every other start names one")
        if doc["resume_semantics"] in SAME_LINEAGE and \
                doc["parent_run"] is None:
            out.append("an EXACT_RESUME continues a named run")
        if doc["resume_semantics"] not in SAME_LINEAGE and \
                doc["parent_run"] is not None:
            out.append("only an EXACT_RESUME continues another run's "
                       "lineage; a warm start, fine-tune or continued "
                       "pretraining is a NEW run that cites its source "
                       "checkpoint")
        levels = doc["reproducibility"].get("claimed") if isinstance(
            doc["reproducibility"], dict) else None
        if not isinstance(levels, list) or any(
                lv not in REPRODUCIBILITY_LEVELS for lv in levels):
            out.append("reproducibility.claimed must list known levels")
    if schema == CHECKPOINT_MANIFEST:
        if not isinstance(doc["shard_count"], int) or doc["shard_count"] < 1 \
                or len(doc["shards"]) != doc["shard_count"]:
            out.append("shard_count must equal the number of shards")
        if not isinstance(doc["optimizer_state_presence"], bool):
            out.append("optimizer_state_presence must be a bool")
    if schema == EVALUATION_REPORT:
        if doc["semantics"] != list(PREDICTION_SEMANTICS):
            out.append(f"evaluation semantics must be "
                       f"{list(PREDICTION_SEMANTICS)}")
    if schema == META_REPORT:
        if doc["allocation_mode"] != "meta":
            out.append("a meta-validation report is about a meta "
                       "construction")
    return out


#: What every learned output is, until an authority outside this package
#: says otherwise (directive s.5). Repeated in ``claims``.
PREDICTION_SEMANTICS = ("LEARNED_PREDICTION", "NON_AUTHORITATIVE",
                        "REQUIRES_EXTERNAL_VERIFICATION")


def check_chain(*, dataset: dict, training: dict, checkpoint: dict,
                evaluation: dict | None = None,
                checkpoint_bytes_digest: str | None = None) -> list:
    """Every disagreement along source -> dataset -> run -> checkpoint ->
    evaluation. [] when every link holds."""
    out = []
    for name, doc in (("dataset", dataset), ("training", training),
                      ("checkpoint", checkpoint)) + (
            (("evaluation", evaluation),) if evaluation is not None else ()):
        for p in problems(doc):
            out.append(f"{name}: {p}")
    if out:
        return out
    if training["dataset_digest"] != dataset["dataset_digest"]:
        out.append("the training run names a different dataset")
    for key in ("feature_schema_digest", "target_schema_digest"):
        if training[key] != dataset[key]:
            out.append(f"the training run's {key} is not the dataset's")
    if training["normalization_digest"] != digest(
            dataset["normalization_statistics"]):
        out.append("the training run used normalisation statistics the "
                   "dataset manifest does not record")
    if checkpoint["training_run_id"] != training["run_id"]:
        out.append("the checkpoint names a different training run")
    if checkpoint["training_manifest_digest"] != digest(training):
        out.append("the checkpoint's training manifest digest does not "
                   "match the training manifest")
    if checkpoint["model_config_digest"] != \
            training["model_configuration_digest"]:
        out.append("checkpoint and training run bind different "
                   "configurations")
    if checkpoint["dataset_digest"] != dataset["dataset_digest"]:
        out.append("the checkpoint names a different dataset")
    listed = [c.get("checkpoint_digest") for c in training["checkpoints"]]
    if checkpoint["checkpoint_digest"] not in listed:
        out.append("the training run does not list this checkpoint")
    if checkpoint_bytes_digest is not None and \
            checkpoint_bytes_digest != checkpoint["checkpoint_digest"]:
        out.append("the checkpoint's bytes are not the ones its manifest "
                   "records")
    if evaluation is not None:
        if evaluation["checkpoint_digest"] != checkpoint["checkpoint_digest"]:
            out.append("the evaluation is of a different checkpoint")
        if evaluation["checkpoint_manifest_digest"] != digest(checkpoint):
            out.append("the evaluation cites a different checkpoint "
                       "manifest")
        if evaluation["model_config_digest"] != \
                checkpoint["model_config_digest"]:
            out.append("the evaluation names a different configuration")
        if evaluation["dataset_digest"] != dataset["dataset_digest"]:
            out.append("the evaluation used a different dataset")
        if evaluation["normalization_digest"] != \
                training["normalization_digest"]:
            out.append("the evaluation normalised with different "
                       "statistics than training")
    return out
