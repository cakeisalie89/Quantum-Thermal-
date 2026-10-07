#!/usr/bin/env python3
"""Generate and verify the evidence of the learned-model substrate (NF-1T).

    dataset       run the governed source model over the declared design;
                  write docs/neural/dev/dataset.jsonl and its manifest
    pipeline      train, checkpoint, reload and evaluate the development
                  model on that dataset (and the resume experiment)
    distributed   the parallel checks, on eight SIMULATED CPU devices
    architecture  meta-validate and write the model manifests of the
                  development model and the flagship, and the family table
    audit         the legacy-semantics audit report
    claims        record every document in a fresh authority history (by
                  tools/neural_ledger.py, in its own process) and write the
                  claims and statuses it supports
    all           every step above, in order
    verify        re-derive what can be re-derived and compare it with the
                  committed evidence (the CI step)

Nothing here allocates the flagship. ``verify`` never trains and never
regenerates the dataset: it re-solves the budget, re-validates the flagship
abstractly, re-checks every digest link, reloads the stored checkpoint, and
recomputes the claims, the audit and the status ladder.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scientific_ai.neural import (datasets, family,  # noqa: E402
                                  manifests, solver)
from scientific_ai.neural import source_surface_adsorption as SRC  # noqa

EVID = Path("docs") / "neural"
DEV = EVID / "dev"
F = {
    "dataset": DEV / "dataset.jsonl",
    "dataset_manifest": DEV / "dataset_manifest.json",
    "training": DEV / "training_manifest.json",
    "training_resume_a": DEV / "training_resume_a.json",
    "training_resume_b": DEV / "training_resume_b.json",
    "checkpoint": DEV / "checkpoint_manifest.json",
    "checkpoint_resume_a": DEV / "checkpoint_resume_a.json",
    "checkpoint_resume_b": DEV / "checkpoint_resume_b.json",
    "checkpoint_bytes": DEV / "checkpoint_final.safetensors",
    "evaluation": DEV / "evaluation_report.json",
    "distributed": EVID / "distributed_readiness.json",
    "dev_meta": EVID / "development_meta_validation.json",
    "dev_manifest": EVID / "development_model_manifest.json",
    "flagship_meta": EVID / "flagship_meta_validation.json",
    "flagship_manifest": EVID / "flagship_model_manifest.json",
    "family": EVID / "architecture_family.json",
    "claims": EVID / "claims.json",
}
DATASET_ID = "surface_adsorption_dev_v1"
DESIGN = {"n_in": 3072, "n_ood": 384, "design_seed": 20261006,
          "split_seed": 7}


def write_json(rel: Path, doc) -> None:
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n",
                 encoding="utf-8")


def read_json(rel: Path):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def source_commit() -> dict | str:
    """The commit, when the tracked tree is exactly it; otherwise stated as
    unavailable with the reason."""
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain",
                            "--untracked-files=no"], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
    if head and not dirty:
        return head
    return manifests.unavailable(
        f"generated from a tree that is not a commit (HEAD {head or '?'} "
        "with tracked changes)")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds")


# -- dataset ---------------------------------------------------------------

def cmd_dataset(args) -> int:
    commit = source_commit()
    samples, rejected = SRC.generate(
        dataset_id=DATASET_ID, n_in=args.n_in, n_ood=args.n_ood,
        design_seed=DESIGN["design_seed"], split_seed=DESIGN["split_seed"],
        source_commit=commit, generated_at=_now(), workers=args.workers)
    raw = datasets.to_jsonl(samples)
    from scientific_ai.neural.model import pipeline
    gen = {"source_model": f"{SRC.MODEL_ID}@{SRC.MODEL_VERSION}",
           "regions": SRC.REGIONS, "ood_rule": "T_gas_K > 300 K",
           "design": "per-dimension Latin hypercube, PCG64, values to 12 "
                     "significant digits", "design_seed":
               DESIGN["design_seed"], "split_seed": DESIGN["split_seed"],
           "split_fractions": dict(datasets.DEFAULT_FRACTIONS),
           "n_in_distribution": args.n_in, "n_ood": args.n_ood,
           "exclusion_rules": list(SRC.EXCLUSION_RULES),
           "source_commit": commit}
    man, _ = pipeline.dataset_manifest(
        SRC.SCHEMA, samples, rejected, dataset_id=DATASET_ID,
        generation=gen, storage={"path": str(F["dataset"]),
                                 "format": "jsonl, one canonical sample per "
                                           "line, sample_id order",
                                 "sha256": hashlib.sha256(raw).hexdigest(),
                                 "bytes": len(raw)},
        ood_rule=SRC.in_ood_region)
    (ROOT / F["dataset"]).parent.mkdir(parents=True, exist_ok=True)
    (ROOT / F["dataset"]).write_bytes(raw)
    write_json(F["dataset_manifest"], man)
    print(f"dataset {man['dataset_digest'][:16]}: {man['sample_count']} "
          f"samples {man['splits']}, {man['rejected_sample_count']} "
          f"rejected, leakage "
          f"{'PASS' if man['leakage']['passed'] else 'FAIL'}")
    return 0 if man["leakage"]["passed"] else 1


def load_dataset():
    man = read_json(F["dataset_manifest"])
    raw = (ROOT / F["dataset"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != man["storage"]["sha256"]:
        raise SystemExit("dataset.jsonl does not match its manifest")
    samples = datasets.from_jsonl(raw)
    if datasets.dataset_digest(SRC.SCHEMA, samples) != man["dataset_digest"]:
        raise SystemExit("the samples do not hash to the manifest's "
                         "dataset_digest")
    from scientific_ai.neural.features import Normalization
    norm = Normalization.from_dict(man["normalization_statistics"])
    return man, samples, norm


# -- pipeline --------------------------------------------------------------

def train_config(steps: int):
    from scientific_ai.neural.model.train import TrainConfig
    return TrainConfig(steps=steps, batch_size=64, learning_rate=3e-3,
                       warmup_steps=min(200, steps // 10),
                       checkpoint_interval=steps // 2,
                       log_interval=max(1, steps // 80))


def cmd_pipeline(args) -> int:
    from scientific_ai.neural.model import pipeline
    man, samples, norm = load_dataset()
    cfg = family.development()
    out = pipeline.run(cfg, train_config(args.steps), SRC.SCHEMA, samples,
                       man, norm, source_commit=source_commit(),
                       run_prefix="nf1t-dev",
                       stored_at=str(F["checkpoint_bytes"]))
    (ROOT / F["checkpoint_bytes"]).write_bytes(out["checkpoint_bytes"])
    for name, doc in out["documents"].items():
        write_json(F[name], doc)
    ev = out["documents"]["evaluation"]
    tr = out["documents"]["training"]
    print(f"trained {tr['run_id']}: reproducibility "
          f"{tr['reproducibility']['claimed']}; reload "
          f"{ev['reload']['outputs_equal']}")
    return 0


# -- distributed -----------------------------------------------------------

def cmd_distributed(args) -> int:
    env = dict(os.environ)
    env["XLA_FLAGS"] = "--xla_force_host_platform_device_count=8"
    r = subprocess.run([sys.executable, __file__, "_distributed-inner"],
                       cwd=ROOT, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-4000:])
        return 1
    doc = json.loads(r.stdout.strip().splitlines()[-1])
    write_json(F["distributed"], doc)
    print(f"distributed: {doc['result']} on {doc['execution_profile']['kind']}"
          f" ({doc['execution_profile']['devices']} devices)")
    return 0 if doc["result"] == "PASS" else 1


def _distributed_inner() -> int:
    import jax
    import jax.numpy as jnp
    import numpy as np
    from jax.sharding import Mesh, NamedSharding
    from jax.sharding import PartitionSpec as P

    from scientific_ai.neural.model import (_jax, checkpoint, meta, network,
                                            parallel, train)
    cfg = family.development()
    devs = jax.devices()
    if len(devs) < 8:
        raise SystemExit(f"{len(devs)} devices; the check needs 8")
    params, buffers = meta.materialize(cfg, jax.random.key(0))
    checks = []

    def record(name, passed, **detail):
        checks.append({"check": name, "passed": bool(passed), **detail})

    lp = params["layers"][0]["moe"]
    h = jax.random.normal(jax.random.key(1), (128, cfg.hidden_size))
    mask = jnp.ones((128,), bool)
    y1, _ = network.moe(lp, h, mask, cfg)
    for ep in (8, 4, 2):
        mesh = Mesh(np.array(devs[:ep]), ("expert",))
        y2 = parallel.expert_parallel_moe(lp, h, mask, cfg, mesh)
        diff = float(jnp.max(jnp.abs(y1 - y2)))
        record(f"expert_parallel_equivalence_ep{ep}", diff == 0.0
               or bool(jnp.allclose(y1, y2, rtol=1e-5, atol=1e-6)),
               max_abs_difference=diff, experts=cfg.moe.num_experts,
               devices=ep)

    rng = np.random.Generator(np.random.PCG64(3))
    b, f = 64, 8
    batch = {"feature_ids": np.tile(np.arange(f, dtype=np.int32), (b, 1)),
             "context_ids": np.zeros((b, f), np.int32),
             "validity": np.zeros((b, f), np.int32),
             "dim_exponents": np.zeros((b, f, 7), np.float32),
             "dim_class": np.zeros((b, f), np.int32),
             "values": rng.normal(size=(b, f, 4)).astype(np.float32)}
    y = rng.normal(size=(b, len(cfg.heads))).astype(np.float32)
    jb = {k: jnp.asarray(v) for k, v in batch.items()}
    jy = jnp.asarray(y)

    def rel(a_tree, b_tree):
        num = max(float(jnp.max(jnp.abs(a - c))) for a, c in zip(
            jax.tree_util.tree_leaves(a_tree),
            jax.tree_util.tree_leaves(b_tree)))
        den = max(float(jnp.max(jnp.abs(a)))
                  for a in jax.tree_util.tree_leaves(a_tree))
        return num, num / max(den, 1e-30)

    grad1 = jax.jit(lambda p, x, t: train.accumulated_gradient(
        cfg, p, buffers, x, t, 1))
    _, _, _, _, g_ref = grad1(params, jb, jy)
    mesh = Mesh(np.array(devs[:8]), ("data",))

    def shard(x):
        if x.ndim and x.shape[0] % 8 == 0:
            return jax.device_put(x, NamedSharding(mesh, P("data")))
        return jax.device_put(x, NamedSharding(mesh, P()))

    sp = jax.tree_util.tree_map(shard, params)
    sb = {k: jax.device_put(v, NamedSharding(mesh, P("data")))
          for k, v in jb.items()}
    sy = jax.device_put(jy, NamedSharding(mesh, P("data")))
    l_sh, _, _, _, g_sh = grad1(sp, sb, sy)
    l_ref = grad1(params, jb, jy)[0]
    absd, reld = rel(g_ref, g_sh)
    sharded_leaves = sum(1 for x in jax.tree_util.tree_leaves(sp)
                         if not x.sharding.is_fully_replicated)
    tc = train.TrainConfig(steps=10, batch_size=b, warmup_steps=1)
    step = train.make_step(cfg, tc)
    opt = train.init_optimizer(params)
    p_ref, _, _ = step(params, opt, buffers, jb, jy)
    so = jax.tree_util.tree_map(lambda x: shard(x) if hasattr(x, "ndim")
                                else x, opt)
    p_sh, _, _ = step(sp, so, buffers, sb, sy)
    step_d, _ = rel(p_ref, p_sh)
    record("fsdp_sharded_gradient_equivalence",
           reld <= 1e-5 and sharded_leaves > 0,
           max_abs_gradient_difference=absd,
           max_relative_gradient_difference=reld,
           loss_difference=abs(float(l_ref) - float(l_sh)),
           sharded_leaves=sharded_leaves, devices=8,
           one_adam_step_max_parameter_difference=step_d,
           criterion="gradients agree to 1e-5 of their largest magnitude: "
                     "the sharded and unsharded computations differ only "
                     "in reduction order. Parameters after one Adam step "
                     "are NOT the criterion -- the first Adam step is "
                     "about lr*sign(g), so a near-zero gradient that "
                     "differs by one rounding can move a parameter by up "
                     "to 2*lr; the difference is reported, not hidden")

    cfg0 = solver.with_experts(cfg, cfg.moe.num_experts)
    from dataclasses import replace as _replace
    cfg0 = _replace(cfg0, moe=_replace(cfg0.moe, aux_loss_coef=0.0))
    g_full = train.accumulated_gradient(cfg0, params, buffers, jb, jy,
                                        1)[4]
    g_acc = train.accumulated_gradient(cfg0, params, buffers, jb, jy,
                                       4)[4]
    absa, rela = rel(g_full, g_acc)
    g_full_aux = train.accumulated_gradient(cfg, params, buffers, jb, jy,
                                            1)[4]
    g_acc_aux = train.accumulated_gradient(cfg, params, buffers, jb, jy,
                                           4)[4]
    _, rel_aux = rel(g_full_aux, g_acc_aux)
    record("gradient_accumulation_equivalence", rela <= 1e-5,
           max_abs_gradient_difference=absa,
           max_relative_gradient_difference=rela, micro_batches=4,
           with_load_balancing_term_relative_difference=rel_aux,
           criterion="with the likelihood alone (a mean over samples) the "
                     "accumulated gradient equals the full-batch one to "
                     "1e-5 of its magnitude. The load-balancing term is a "
                     "product of batch statistics and changes under "
                     "accumulation BY DESIGN; that difference is reported "
                     "separately and is not a defect of the mechanics")

    shards, cd = checkpoint.save_sharded(cfg, params, buffers, 8)
    sd = [hashlib.sha256(s).hexdigest() for s in shards]
    p_l, b_l = checkpoint.load_sharded(cfg, shards, shard_digests=sd,
                                       expected_digest=cd)
    same = all(np.array_equal(np.asarray(a), np.asarray(c)) for a, c in zip(
        jax.tree_util.tree_leaves(params), jax.tree_util.tree_leaves(p_l)))
    record("sharded_checkpoint_roundtrip", same, shards=8)

    order = np.random.Generator(np.random.PCG64(9)).permutation(1000)
    parts = [parallel.rank_indices(1000, r, 8, order) for r in range(8)]
    allidx = np.concatenate(parts)
    record("data_parallel_partition", len(set(allidx.tolist())) ==
           allidx.size == 1000 // 8 * 8, ranks=8, samples=1000)

    flag = solver.config_from(family.solve_flagship())
    prof = parallel.ExecutionProfile("B300_CLASS_NODE", 64, 8).validate()
    plan = parallel.ParallelPlan((("data", 8), ("expert", 8)))
    planned = plan.validate(flag, prof, global_batch=1024)
    refused = False
    try:
        parallel.ParallelPlan((("data", 4), ("expert", 16))).validate(
            flag, parallel.ExecutionProfile("B300_CLASS_NODE", 64, 8),
            global_batch=1000)
    except parallel.PlanError:
        refused = True
    record("flagship_plan_validation_symbolic", refused
           and planned["experts_per_expert_rank"] == 8,
           plan=planned["mesh"], experts_per_rank=8,
           executed=False, note="validated arithmetically; nothing ran")

    profile = parallel.ExecutionProfile("SIMULATED_MULTI_DEVICE", 8,
                                        8).validate()
    doc = {"schema": manifests.DISTRIBUTED_REPORT,
           "execution_profile": profile.to_dict(),
           "framework": {k: v for k, v in _jax.versions().items()
                         if k != "devices"},
           "devices": [str(d) for d in devs[:8]],
           "checks": checks,
           "result": "PASS" if all(c["passed"] for c in checks) else "FAIL",
           "limitations": [
               "eight CPU devices of ONE host process: the collectives are "
               "exercised, not an interconnect",
               "no speed, memory, communication-volume or failure-recovery "
               "measurement: DISTRIBUTED_HARDWARE_VALIDATED is not claimed",
               "the flagship plan is validated arithmetically and was not "
               "executed"]}
    print(json.dumps(doc, sort_keys=True))
    return 0


def _remat_loss(p, buffers, batch, y, cfg):
    import jax.numpy as jnp

    from scientific_ai.neural.model import network
    from scientific_ai.neural.model.train import LOG_2PI
    out = network.apply(p, buffers, batch, cfg, remat=True)
    nll = 0.5 * jnp.mean(LOG_2PI + jnp.log(out["var"])
                         + (jnp.asarray(y) - out["mean"]) ** 2 / out["var"])
    aux = sum(st["aux_loss"] for st in out["router"])
    return nll + cfg.moe.aux_loss_coef * aux


# -- architecture ----------------------------------------------------------

def _evidence_docs() -> list:
    out = []
    for key in ("dataset_manifest", "training", "training_resume_a",
                "training_resume_b", "checkpoint", "checkpoint_resume_a",
                "checkpoint_resume_b", "evaluation", "distributed",
                "dev_meta", "flagship_meta"):
        if (ROOT / F[key]).exists():
            out.append(read_json(F[key]))
    return out


def cmd_architecture(args) -> int:
    from scientific_ai.neural import accounting, documents
    from scientific_ai.neural.model import meta
    commit = source_commit()
    dev = family.development()
    fsolve = family.solve_flagship()
    flag = solver.config_from(fsolve)
    write_json(F["dev_meta"], meta.validate(dev))
    write_json(F["flagship_meta"], meta.validate(flag))
    ev = _evidence_docs()
    write_json(F["dev_manifest"], documents.model_manifest(
        dev, solve=None, evidence=ev, source_commit=commit))
    write_json(F["flagship_manifest"], documents.model_manifest(
        flag, solve=fsolve, evidence=ev, family_members=(dev.digest(),),
        source_commit=commit))
    rungs = []
    for name, rec in family.solve_ladder():
        cfg = solver.config_from(rec)
        rep = meta.validate(cfg)
        pc = accounting.count(cfg).to_dict()
        rungs.append({
            "rung": name, "variant": cfg.variant,
            "config_digest": rec["config_digest"],
            "target": rec["target"], "expert_hidden": rec["expert_hidden"],
            "top_k": rec["top_k"],
            "trainable_parameters": pc["trainable_parameters"],
            "active_parameters_per_token":
                pc["estimated_total_active_parameters_per_token"],
            "total_relative_deviation": rec["total_relative_deviation"],
            "active_relative_deviation": rec["active_relative_deviation"],
            "hidden_size": cfg.hidden_size, "num_layers": cfg.num_layers,
            "num_heads": cfg.attention.num_heads,
            "num_experts": cfg.moe.num_experts,
            "meta_validation": rep["result"],
            "meta_counts_equal": rep["counts_equal"],
            "trained": False})
    dpc = accounting.count(dev).to_dict()
    write_json(F["family"], {
        "schema": "scientific-moe-family/1",
        "family": dev.to_dict()["family"],
        "development": {
            "variant": dev.variant, "config_digest": dev.digest(),
            "trainable_parameters": dpc["trainable_parameters"],
            "active_parameters_per_token":
                dpc["estimated_total_active_parameters_per_token"],
            "trained": True},
        "rungs": rungs,
        "note": "every rung is the same architecture; each was solved "
                "against its budget and validated by abstract evaluation; "
                "only the development member was trained"})
    print(f"flagship {flag.digest()[:16]}: "
          f"{read_json(F['flagship_manifest'])['claim_status']['status']}")
    return 0


# -- audit and claims ------------------------------------------------------

def cmd_audit(args) -> int:
    import neural_legacy_audit
    return neural_legacy_audit.main(["--write"])


def ledger_claims() -> dict:
    """The claims document, from every committed document recorded in a
    FRESH authority history -- by ``tools/neural_ledger.py``, in its own
    process, so that no process that generates data or trains imports the
    authority substrate."""
    r = subprocess.run([sys.executable, str(ROOT / "tools" /
                                            "neural_ledger.py")],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("tools/neural_ledger.py failed: "
                           + (r.stderr.strip()[-2000:] or r.stdout[-2000:]))
    return json.loads(r.stdout)


def cmd_claims(args) -> int:
    doc = ledger_claims()
    write_json(F["claims"], {k: v for k, v in doc.items()
                             if k != "history_head"})
    for name, s in doc["subjects"].items():
        held = [c for c, v in s["claims"].items() if v["holds"]]
        print(f"{name}: status {s['status']}; holds {held}")
    print(f"acceptance refused: {doc['acceptance_attempt']['refused']}")
    return 0


def cmd_all(args) -> int:
    for fn in (cmd_dataset, cmd_pipeline, cmd_distributed, cmd_architecture,
               cmd_audit, cmd_claims):
        rc = fn(args)
        if rc:
            return rc
    return 0


# -- verify ----------------------------------------------------------------

def cmd_verify(args) -> int:
    import numpy as np

    from scientific_ai.neural import accounting, documents
    from scientific_ai.neural.model import checkpoint, evaluate, meta
    problems = []

    def need(cond, what):
        if not cond:
            problems.append(what)

    fs = family.solve_flagship()
    flag = solver.config_from(fs)
    man = read_json(F["flagship_manifest"])
    need(man["configuration_digest"] == flag.digest(),
         "the flagship manifest names a configuration the solver does not "
         "produce today")
    pc = accounting.count(flag).to_dict()
    need(man["parameter_accounting"] == pc,
         "the flagship manifest's counts differ from the exact count")
    lo, hi = family.FLAGSHIP_TARGET.total_range
    need(lo <= pc["trainable_parameters"] <= hi, "flagship total out of "
                                                 "range")
    lo, hi = family.FLAGSHIP_TARGET.active_range
    need(lo <= pc["estimated_total_active_parameters_per_token"] <= hi,
         "flagship active count out of range")
    rep = meta.validate(flag)
    need(rep["result"] == "PASS", f"flagship meta validation: "
                                  f"{rep['result']}")
    committed = read_json(F["flagship_meta"])
    need(committed["abstract_by_category"] == rep["abstract_by_category"]
         and committed["counts_equal"] and committed["result"] == "PASS",
         "the committed flagship meta report differs from a fresh one")
    need(manifests.problems(man) == [], "flagship manifest malformed")
    ev = _evidence_docs()
    fresh = documents.model_manifest(
        flag, solve=fs, evidence=ev,
        family_members=(family.development().digest(),),
        source_commit=man["source_commit"])
    need(fresh["claim_status"]["claims"] == man["claim_status"]["claims"],
         "the flagship's claims no longer follow from the evidence")
    held = {c for c, v in man["claim_status"]["claims"].items()
            if v["holds"] and v["scope"] == "SUBJECT"}
    need("LARGE_MODEL_TRAINED" not in held
         and "SCIENTIFIC_PERFORMANCE_ESTABLISHED" not in held
         and "DISTRIBUTED_HARDWARE_VALIDATED" not in held
         and "DEVELOPMENT_MODEL_TRAINED" not in held,
         "the flagship claims a training or validation it does not have")

    dman, samples, norm = load_dataset()
    tman = read_json(F["training"])
    cman = read_json(F["checkpoint"])
    eva = read_json(F["evaluation"])
    raw = (ROOT / F["checkpoint_bytes"]).read_bytes()
    chain = manifests.check_chain(
        dataset=dman, training=tman, checkpoint=cman, evaluation=eva,
        checkpoint_bytes_digest=hashlib.sha256(raw).hexdigest())
    problems += [f"chain: {p}" for p in chain]
    for a, b in (("training_resume_b", "checkpoint_resume_b"),
                 ("training_resume_a", "checkpoint_resume_a")):
        problems += [f"{a}: {p}" for p in manifests.check_chain(
            dataset=dman, training=read_json(F[a]),
            checkpoint=read_json(F[b]))]
    dev = family.development()
    params, buffers, _, _ = checkpoint.load(
        dev, raw, expected_digest=cman["checkpoint_digest"])
    from scientific_ai.neural.features import tokenize
    test = [s for s in samples if s.split == "test"]
    out = evaluate.predict(params, buffers, dev,
                           tokenize(SRC.SCHEMA, norm,
                                    [s.inputs for s in test]))
    need(bool(np.all(np.isfinite(out["mean"])) and np.all(
        np.isfinite(out["var"]))), "reloaded outputs are not finite")
    need(eva["reload"]["outputs_equal"] is True,
         "the evaluation's reload check did not hold")
    claims_doc = ledger_claims()
    committed_claims = read_json(F["claims"])
    need({k: v for k, v in claims_doc.items() if k != "history_head"}
         == committed_claims, "claims.json does not follow from the "
                              "evidence recorded in a fresh history")
    need(claims_doc["acceptance_attempt"]["refused"] is True,
         "a learned record was accepted")
    import neural_legacy_audit
    aud = neural_legacy_audit.audit()
    need(aud["active_neural_semantic_leaks"] == 0 and aud["unclassified"]
         == 0, "legacy semantic leak or unclassified finding")
    need(aud == read_json(neural_legacy_audit.OUT),
         "the committed legacy audit is stale")
    for p in problems:
        print(f"  PROBLEM: {p}")
    print(f"neural evidence: {'PASS' if not problems else 'FAIL'} -- "
          f"flagship {pc['trainable_parameters']:,} trainable, "
          f"{pc['estimated_total_active_parameters_per_token']:,} active; "
          f"meta {rep['result']}; chain "
          f"{'intact' if not chain else 'BROKEN'}")
    return 0 if not problems else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dataset")
    d.add_argument("--n-in", type=int, default=DESIGN["n_in"])
    d.add_argument("--n-ood", type=int, default=DESIGN["n_ood"])
    d.add_argument("--workers", type=int, default=4)
    p = sub.add_parser("pipeline")
    p.add_argument("--steps", type=int, default=4000)
    for name in ("distributed", "_distributed-inner", "architecture",
                 "audit", "claims", "verify"):
        sub.add_parser(name)
    a = sub.add_parser("all")
    a.add_argument("--n-in", type=int, default=DESIGN["n_in"])
    a.add_argument("--n-ood", type=int, default=DESIGN["n_ood"])
    a.add_argument("--workers", type=int, default=4)
    a.add_argument("--steps", type=int, default=4000)
    args = ap.parse_args(argv)
    return {"dataset": cmd_dataset, "pipeline": cmd_pipeline,
            "distributed": cmd_distributed,
            "_distributed-inner": lambda a: _distributed_inner(),
            "architecture": cmd_architecture, "audit": cmd_audit,
            "claims": cmd_claims, "all": cmd_all,
            "verify": cmd_verify}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
