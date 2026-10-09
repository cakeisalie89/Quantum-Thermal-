"""The development pipeline, end to end (directive s.30), and its documents.

    governed samples -> dataset manifest -> tokens -> forward -> loss
      -> backward -> optimizer -> checkpoint -> reload -> inference
      -> evaluation -> training / checkpoint / evaluation manifests

:func:`run` is that line for one configuration. It returns every document
and the bytes of the checkpoint it keeps; it writes nothing itself (the
tool decides where documents live). It also runs the RESUME EXPERIMENT the
reproducibility claims rest on:

* the MAIN run trains from step 0 to N;
* run A trains, from the same seeds, from 0 to N/2 and stops there by
  design (``INTERRUPTED``), keeping its optimizer state;
* run B is an EXACT_RESUME of A from that checkpoint to N.

A at N/2 against MAIN at N/2 -- two fresh runs -- and B at N against MAIN
at N -- an interrupted run and an uninterrupted one -- are compared by
tensor digest. Equal digests are what the claimed reproducibility levels
mean, and their scope is THIS backend, one process; nothing is claimed
across hosts, devices or frameworks.
"""
from __future__ import annotations

import datetime
import hashlib
from typing import Any

import numpy as np

from scientific.identity import digest

from .. import accounting, datasets, features, manifests
from ..config import ModelConfig
from ..ood import InputOOD
from . import checkpoint as ckpt
from . import evaluate as ev
from . import meta, train
from ._jax import require, versions

LOSS_DEFINITION = (
    "mean over samples and targets of the Gaussian negative "
    "log-likelihood 0.5*(log(2*pi) + log(var) + (y - mean)^2/var) in each "
    "target's standardised transform space, plus moe.aux_loss_coef times "
    "the sum over MoE layers of num_experts * sum_i f_i * P_i")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")


def hardware_identity() -> dict:
    from scientific.run_identity import _read_cpuinfo, cpu_record
    return {"cpu": cpu_record(_read_cpuinfo()), "accelerators": [],
            "note": "the CPU as a numeric kernel sees it (SIMD flags, "
                    "vendor, family, model); no host name"}


def dataset_manifest(schema, samples, rejected, *, dataset_id: str,
                     generation: dict, storage: dict, ood_rule) -> tuple:
    """``(manifest, normalization)`` for governed samples."""
    datasets.check_samples(schema, samples)
    norm = datasets.fit_normalization(schema, samples)
    leak = datasets.leakage_report(schema, samples, norm, ood_rule=ood_rule)
    members = datasets.split_members(samples)
    sources = sorted({
        f"{s.provenance['generator_version']['model_id']}@"
        f"{s.provenance['generator_version']['model_version']}#"
        f"{s.provenance['generator_version']['implementation_digest']}"
        for s in samples})
    doc = {
        "schema": manifests.DATASET_MANIFEST,
        "schema_version": "scientific-dataset/1",
        "dataset_id": dataset_id,
        "dataset_digest": datasets.dataset_digest(schema, samples),
        "sample_count": len(samples),
        "feature_schema": schema.feature_schema_dict(),
        "feature_schema_digest": schema.feature_digest(),
        "target_schema": schema.target_schema_dict(),
        "target_schema_digest": schema.target_digest(),
        "source_identifiers": sources,
        "splits": {k: len(v) for k, v in members.items()},
        "split_digests": datasets.split_digests(samples),
        "normalization_statistics": norm.to_dict(),
        "rejected_sample_count": len(rejected),
        "rejections": rejected[:50],
        "exclusion_rules": list(generation.get("exclusion_rules", ())),
        "generation_method": {**generation,
                              "provenance_digest":
                                  datasets.provenance_digest(samples)},
        "leakage": leak,
        "storage": storage,
    }
    return doc, norm


def _split_tokens(schema, norm, samples, split):
    members = [s for s in samples if s.split == split]
    toks = features.tokenize(schema, norm, [s.inputs for s in members])
    y = features.encode_targets(schema, norm, [s.targets for s in members])
    return members, toks, y


def _buffers_from(norm, cfg, schema, buffers):
    _, jnp = require()
    if len(schema.features) > cfg.feature_vocab_size:
        raise ValueError("the schema has more features than the "
                         "configuration's identity table")
    # the TRAINING split's (location, scale) per feature identity: the
    # tokenizer standardises with exactly these, and the checkpoint carries
    # them so a reloaded model reads inputs as it was trained to; identity
    # rows the schema does not use stay (0, 1)
    fn = np.tile(np.array([[0.0, 1.0]], np.float32),
                 (cfg.feature_vocab_size, 1))
    fn[:len(schema.features)] = np.array(
        list(zip(norm.feature_mean, norm.feature_scale)), np.float32)
    tn = np.array(list(zip(norm.target_mean, norm.target_scale)),
                  np.float32)
    if tn.shape[0] != len(cfg.heads) or \
            [h.name for h in cfg.heads] != list(schema.target_names):
        raise ValueError("the configuration's heads are not the schema's "
                         "targets, in order")
    return {**buffers, "feature_norm": jnp.asarray(fn),
            "target_norm": jnp.asarray(tn)}


def _router_summary(stats: list) -> dict:
    """``stats``: the router statistics recorded at logged steps (not an
    authority log)."""
    if not stats:
        return {"moe": False}
    last = stats[-1]
    return {"moe": True, "logged_steps": len(stats),
            "final_max_load_fraction": last["max_load_fraction"],
            "final_entropy": last["entropy"],
            "total_dropped_logged": float(sum(sum(r["dropped"])
                                              for r in stats)),
            "max_load_fraction_seen": max(max(r["max_load_fraction"])
                                          for r in stats),
            "final_expert_counts": last["expert_counts"]}


def _training_manifest(cfg, tc, dman, norm, *, run_id, source_commit,
                       start, stop, resume, parent_run, parent_ckpt,
                       completion, history, router_log, checkpoints,
                       started, ended, reproducibility) -> dict:
    return {
        "schema": manifests.TRAINING_MANIFEST, "run_id": run_id,
        "source_commit": source_commit,
        "model_configuration_digest": cfg.digest(),
        "parameter_count": accounting.count(cfg).trainable_parameters,
        "dataset_digest": dman["dataset_digest"],
        "feature_schema_digest": dman["feature_schema_digest"],
        "target_schema_digest": dman["target_schema_digest"],
        "normalization_digest": digest(dman["normalization_statistics"]),
        "initialization_seed": tc.init_seed,
        "data_order_seed": tc.data_order_seed,
        "precision": cfg.precision.to_dict(),
        "optimizer": "adam", "optimizer_configuration": tc.optimizer_dict(),
        "scheduler": "linear_warmup_cosine",
        "scheduler_configuration": tc.scheduler_dict(),
        "batch_size": tc.batch_size,
        "gradient_accumulation": tc.gradient_accumulation,
        "world_size": 1,
        "distributed_topology": {"kind": "single_process", "mesh": None},
        "hardware_identity": hardware_identity(),
        "framework_versions": versions(),
        "backend_versions": {"xla_backend": versions()["backend"],
                             "jaxlib": versions().get("jaxlib")},
        "checkpoint_interval": tc.checkpoint_interval,
        "loss_definition": LOSS_DEFINITION,
        "evaluation_configuration": {
            "splits": ["train", "validation", "test", "ood"],
            "nominal_intervals": list(ev.NOMINAL),
            "metrics": ["mae", "rmse", "median_ae", "p90_ae", "p99_ae",
                        "median_relative_error", "nll_standardised",
                        "interval_coverage", "std_vs_error_spearman"],
            "selection": "none: the schedule is fixed before training; "
                         "no checkpoint is chosen on test or OOD data"},
        "start_time": started, "end_time": ended,
        "steps": {"start": start, "stop": stop, "planned": tc.steps},
        "resume_semantics": resume, "parent_checkpoint": parent_ckpt,
        "parent_run": parent_run, "completion_state": completion,
        "loss_history": history,
        "router_health": _router_summary(router_log),
        "reproducibility": reproducibility,
        "checkpoints": checkpoints,
    }


def _checkpoint_manifest(cfg, tman, dman, raw: bytes, *, step: int,
                         tc, n_train: int, with_opt: bool, parent,
                         stored_at, source_commit) -> dict:
    pos = train.position(step, n_train, tc)
    return {
        "schema": manifests.CHECKPOINT_MANIFEST,
        "checkpoint_digest": hashlib.sha256(raw).hexdigest(),
        "checkpoint_format": ckpt.FORMAT,
        "model_config_digest": cfg.digest(),
        "dataset_digest": dman["dataset_digest"],
        "training_run_id": tman["run_id"],
        "training_manifest_digest": digest(tman),
        "training_step": step, "epoch": pos["epoch"],
        "precision": cfg.precision.to_dict(),
        "optimizer_state_presence": with_opt,
        "shard_count": 1,
        "shards": [{"index": 0, "digest": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw), "stored_at": stored_at}],
        "tensor_count": len(ckpt.tensor_index(raw)),
        "tensor_index_digest": digest(ckpt.tensor_index(raw)),
        "parent_checkpoint": parent, "source_commit": source_commit,
        "data_position": pos,
        "rng": {"initialization_seed": tc.init_seed,
                "data_order_seed": tc.data_order_seed,
                "data_order": "PCG64(data_order_seed + epoch) permutation"},
    }


def run(cfg: ModelConfig, tc: train.TrainConfig, schema, samples, dman,
        norm, *, source_commit, run_prefix: str, stored_at: str,
        resume_experiment: bool = True) -> dict:
    jax, _ = require()
    cfg.validate()
    tc.validate()
    tr, toks, y = _split_tokens(schema, norm, samples, "train")
    n_train = len(tr)
    key = jax.random.key(tc.init_seed)
    p0, b0 = meta.materialize(cfg, key)
    b0 = _buffers_from(norm, cfg, schema, b0)
    tag = cfg.digest()[:12]
    main_id = f"{run_prefix}-{tag}-main"
    mid = tc.steps // 2
    kept: dict = {}

    def keep(store, name):
        def cb(step, params, opt):
            store[(name, step)] = (params, opt)
        return cb

    started = _now()
    res = train.run(cfg, tc, toks, y, params=p0, buffers=b0,
                    on_checkpoint=keep(kept, "main"))
    ended = _now()
    p_final = res["params"]
    final_raw = ckpt.save(cfg, p_final, b0, metadata={
        "run_id": main_id, "step": str(tc.steps)})
    repro: dict[str, Any] = {
        "claimed": ["CONFIGURATION_REPRODUCIBLE", "DATASET_REPRODUCIBLE",
                    "SEEDED_EXECUTION"],
        "scope": "this backend (the hardware_identity and "
                 "framework_versions recorded here), one process, CPU",
        "evidence": {}, "not_claimed": [
            "any agreement across hosts, CPUs, devices, frameworks or "
            "framework versions",
            "multi-device or GPU determinism"]}
    docs = {}
    if resume_experiment:
        a_id = f"{run_prefix}-{tag}-resume-a"
        b_id = f"{run_prefix}-{tag}-resume-b"
        sa = _now()
        ra = train.run(cfg, tc, toks, y, params=p0, buffers=b0,
                       stop_step=mid)
        ea = _now()
        a_raw = ckpt.save(cfg, ra["params"], b0, opt_state=ra["opt"],
                          metadata={"run_id": a_id, "step": str(mid)})
        a_digest = hashlib.sha256(a_raw).hexdigest()
        pa, ba, oa, _ = ckpt.load(cfg, a_raw, expected_digest=a_digest,
                                  with_optimizer=True)
        sb = _now()
        rb = train.run(cfg, tc, toks, y, params=pa, buffers=ba, opt=oa,
                       start_step=mid)
        eb = _now()
        b_raw = ckpt.save(cfg, rb["params"], b0,
                          metadata={"run_id": b_id, "step": str(tc.steps)})
        main_mid = kept.get(("main", mid))
        fresh_equal = main_mid is not None and ckpt.tensor_digest(
            main_mid[0], b0, main_mid[1]) == ckpt.tensor_digest(
            ra["params"], b0, ra["opt"])
        resume_equal = ckpt.tensor_digest(p_final, b0) == \
            ckpt.tensor_digest(rb["params"], b0)
        repro["evidence"] = {
            "fresh_runs_bitwise_equal_at_step": mid if fresh_equal else None,
            "fresh_run_tensor_digests_equal": fresh_equal,
            "exact_resume_bitwise_equal_to_uninterrupted": resume_equal,
            "resume_runs": [a_id, b_id]}
        if fresh_equal:
            repro["claimed"] += ["NUMERICALLY_REPRODUCIBLE",
                                 "BITWISE_REPRODUCIBLE"]
        if resume_equal:
            repro["claimed"].append("CHECKPOINT_REPRODUCIBLE")
        ra_hist, rb_hist = ra["history"], rb["history"]
        man_a = _training_manifest(
            cfg, tc, dman, norm, run_id=a_id, source_commit=source_commit,
            start=0, stop=mid, resume="FRESH_TRAINING", parent_run=None,
            parent_ckpt=None, completion="INTERRUPTED", history=ra_hist,
            router_log=ra["router_log"],
            checkpoints=[{"step": mid, "checkpoint_digest": a_digest,
                          "stored": False}],
            started=sa, ended=ea, reproducibility=repro)
        man_b = _training_manifest(
            cfg, tc, dman, norm, run_id=b_id, source_commit=source_commit,
            start=mid, stop=tc.steps, resume="EXACT_RESUME", parent_run=a_id,
            parent_ckpt=a_digest, completion="COMPLETED", history=rb_hist,
            router_log=rb["router_log"],
            checkpoints=[{"step": tc.steps, "checkpoint_digest":
                          hashlib.sha256(b_raw).hexdigest(),
                          "stored": False}],
            started=sb, ended=eb, reproducibility=repro)
        unstored = manifests.unavailable(
            "not retained: a lineage-experiment checkpoint; its digest is "
            "its identity")
        docs["training_resume_a"] = man_a
        docs["training_resume_b"] = man_b
        docs["checkpoint_resume_a"] = _checkpoint_manifest(
            cfg, man_a, dman, a_raw, step=mid, tc=tc, n_train=n_train,
            with_opt=True, parent=None, stored_at=unstored,
            source_commit=source_commit)
        docs["checkpoint_resume_b"] = _checkpoint_manifest(
            cfg, man_b, dman, b_raw, step=tc.steps, tc=tc, n_train=n_train,
            with_opt=False, parent=a_digest, stored_at=unstored,
            source_commit=source_commit)
    final_digest = hashlib.sha256(final_raw).hexdigest()
    man = _training_manifest(
        cfg, tc, dman, norm, run_id=main_id, source_commit=source_commit,
        start=0, stop=tc.steps, resume="FRESH_TRAINING", parent_run=None,
        parent_ckpt=None, completion="COMPLETED", history=res["history"],
        router_log=res["router_log"],
        checkpoints=[{"step": tc.steps, "checkpoint_digest": final_digest,
                      "stored": stored_at}],
        started=started, ended=ended, reproducibility=repro)
    cman = _checkpoint_manifest(
        cfg, man, dman, final_raw, step=tc.steps, tc=tc, n_train=n_train,
        with_opt=False, parent=None, stored_at=stored_at,
        source_commit=source_commit)

    # reload and evaluate
    splits = datasets.split_members(samples)
    iood = InputOOD.fit(schema, norm, [s.inputs for s in splits["train"]])
    body = ev.evaluate(cfg, p_final, b0, schema, norm, splits,
                       input_ood=iood)
    pr, br, _, _ = ckpt.load(cfg, final_raw, expected_digest=final_digest)
    _, toks_t, _ = _split_tokens(schema, norm, samples, "test")
    reloaded = ev.predict(pr, br, cfg, toks_t)
    reload = {"digest_verified": True,
              "outputs_equal": ev.outputs_equal(body["_test_outputs"],
                                                reloaded),
              "compared": "test-split mean, variance and pooled "
                          "representation, bitwise, in-memory parameters "
                          "against the reloaded checkpoint"}
    evaluation = {
        "schema": manifests.EVALUATION_REPORT,
        "model_config_digest": cfg.digest(),
        "checkpoint_digest": final_digest,
        "checkpoint_manifest_digest": digest(cman),
        "dataset_digest": dman["dataset_digest"],
        "normalization_digest": digest(dman["normalization_statistics"]),
        "semantics": body["semantics"], "splits": body["splits"],
        "per_target": body["per_target"],
        "calibration": {
            "method": "central Gaussian intervals of the predicted "
                      "variance; coverage against nominal",
            "nominal": list(ev.NOMINAL),
            "aleatoric": "the variance head (heteroscedastic Gaussian)",
            "epistemic": "NOT_ASSESSED: one trained model gives no spread "
                         "over models"},
        "ood": {**body["ood"], "input_model": iood.to_dict()},
        "constraints": body["constraints"], "router": body["router"],
        "reload": reload,
        "limitations": [
            "a development model of one closed-form law: its accuracy says "
            "nothing about any other member of the family",
            "the in-distribution test split interpolates; extrapolation is "
            "the OOD split only, along one declared axis (temperature)",
            "epistemic uncertainty is not assessed",
            "not compared against measurement: the targets are simulation "
            "results of a governed model"],
    }
    docs.update({"training": man, "checkpoint": cman,
                 "evaluation": evaluation})
    return {"documents": docs, "checkpoint_bytes": final_raw,
            "params": p_final, "buffers": b0}
