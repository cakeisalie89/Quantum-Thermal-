"""Evaluation of a learned model, kept apart from any authority over it.

An evaluation report says what one checkpoint DID on held-out data. It does
not accept the model, does not mark any prediction verified, and carries
the semantics every learned output has here: LEARNED_PREDICTION,
NON_AUTHORITATIVE, REQUIRES_EXTERNAL_VERIFICATION.

WHAT IS REPORTED (per target, per split -- never one global score)

* in each target's TRANSFORMED space (log10 or linear): MAE, RMSE, median
  absolute error, p90 / p99 absolute error;
* in physical units: median and p90 relative error;
* the Gaussian NLL, and CALIBRATION: the fraction of truths inside the
  central 50 / 80 / 90 / 95 % predicted intervals against the nominal
  fraction, and the mean absolute miscalibration -- the variance head is
  calibrated only as far as these numbers show, and nothing else here
  calls it calibrated;
* confidence versus error: the Spearman correlation between predicted
  standard deviation and absolute error;
* the standardised residual's mean, deviation and quantiles;
* OOD: the same metrics on the extrapolation split, the ratio of OOD to
  in-distribution RMSE per target, the input-OOD classes of each split, and
  how well a Mahalanobis distance on the model's pooled representation
  separates test from OOD (AUROC);
* constraint violations on test and OOD predictions (``constraints``);
* router health on the test split: expert usage, the largest load share,
  unused experts, entropy, dropped choices -- flagged, not averaged away;
* the reload check: the checkpoint's bytes re-hash to its digest and the
  reloaded parameters reproduce the evaluated outputs exactly.

EPISTEMIC UNCERTAINTY IS NOT ASSESSED: one trained model gives no spread
over models. The report says so.
"""
from __future__ import annotations

import math

import numpy as np

from .. import constraints as C
from ..features import (FeatureSchema, Normalization, decode_targets,
                        encode_targets, inverse_transform, tokenize)
from ..manifests import PREDICTION_SEMANTICS
from ..ood import InputOOD
from . import network
from ._jax import require

NOMINAL = (0.5, 0.8, 0.9, 0.95)
#: Two-sided standard-normal quantiles for NOMINAL.
_Z = {0.5: 0.6744897501960817, 0.8: 1.2815515655446004,
      0.9: 1.6448536269514722, 0.95: 1.959963984540054}
#: Router flags: a reporting threshold, not a tuned one -- one expert
#: taking more than half of a layer's choices on held-out data.
COLLAPSE_SHARE = 0.5


def predict(params, buffers, cfg, tokens: dict, *, top_k=None) -> dict:
    jax, jnp = require()
    fn = jax.jit(lambda p, b, x: network.apply(p, b, x, cfg, top_k=top_k))
    out = fn(params, buffers, {k: jnp.asarray(v) for k, v in tokens.items()})
    return {"mean": np.asarray(out["mean"], np.float64),
            "var": np.asarray(out["var"], np.float64),
            "pooled": np.asarray(out["pooled"], np.float64),
            "router": [{k: np.asarray(v) for k, v in st.items()}
                       for st in out["router"]]}


def _spearman(a, b) -> float | None:
    if len(a) < 3:
        return None
    ra = np.argsort(np.argsort(a))
    rb = np.argsort(np.argsort(b))
    c = np.corrcoef(ra, rb)[0, 1]
    return None if not np.isfinite(c) else float(c)


def target_metrics(schema: FeatureSchema, norm: Normalization, y_z, mean_z,
                   var_z) -> dict:
    out = {}
    for j, t in enumerate(schema.targets):
        sc, loc = norm.target_scale[j], norm.target_mean[j]
        truth_t = y_z[:, j] * sc + loc            # transformed space
        pred_t = mean_z[:, j] * sc + loc
        err = np.abs(pred_t - truth_t)
        std_z = np.sqrt(var_z[:, j])
        resid = (y_z[:, j] - mean_z[:, j]) / std_z
        truth = inverse_transform(t.transform, truth_t)
        pred = inverse_transform(t.transform, pred_t)
        rel = np.abs(pred - truth) / np.maximum(np.abs(truth), 1e-300)
        cover = {}
        for p in NOMINAL:
            cover[str(p)] = float(np.mean(np.abs(resid) <= _Z[p]))
        nll = 0.5 * (math.log(2 * math.pi) + np.log(var_z[:, j])
                     + (y_z[:, j] - mean_z[:, j]) ** 2 / var_z[:, j])
        out[t.name] = {
            "space": t.transform, "n": int(len(err)),
            "mae": float(np.mean(err)),
            "rmse": float(np.sqrt(np.mean(err ** 2))),
            "median_ae": float(np.median(err)),
            "p90_ae": float(np.quantile(err, 0.9)),
            "p99_ae": float(np.quantile(err, 0.99)),
            "median_relative_error": float(np.median(rel)),
            "p90_relative_error": float(np.quantile(rel, 0.9)),
            "nll_standardised": float(np.mean(nll)),
            "interval_coverage": cover,
            "mean_abs_miscalibration": float(np.mean(
                [abs(cover[str(p)] - p) for p in NOMINAL])),
            "std_vs_error_spearman": _spearman(std_z, err),
            "standardised_residual": {
                "mean": float(np.mean(resid)), "std": float(np.std(resid)),
                "q05": float(np.quantile(resid, 0.05)),
                "q50": float(np.quantile(resid, 0.5)),
                "q95": float(np.quantile(resid, 0.95))},
        }
    return out


def _auroc(neg, pos) -> float | None:
    if not len(neg) or not len(pos):
        return None
    scores = np.concatenate([neg, pos])
    ranks = np.argsort(np.argsort(scores)) + 1
    rp = ranks[len(neg):].sum()
    return float((rp - len(pos) * (len(pos) + 1) / 2)
                 / (len(pos) * len(neg)))


def router_health(router: list, cfg) -> list:
    out = []
    for i, st in enumerate(router):
        counts = st["expert_counts"].astype(np.float64)
        sel = st["selected"]
        kept = st["kept"]
        total = counts.sum()
        share = counts / total if total else counts
        idx_ok = bool(np.all((sel >= 0) & (sel < cfg.moe.num_experts)))
        uniq = all(len(set(row.tolist())) == len(row) for row in sel)
        out.append({
            "layer": cfg.moe_layer_indices[i],
            "choices": int(sel.size), "kept": int(kept.sum()),
            "dropped": float(st["dropped"]),
            "expert_counts": counts.tolist(),
            "max_load_share": float(share.max()) if total else None,
            "unused_experts": int(np.sum(counts == 0)),
            "entropy": float(st["entropy"]),
            "indices_in_range": idx_ok, "indices_unique_per_token": uniq,
            "selected_per_token": int(sel.shape[1]),
            "router_finite": bool(st["finite"]),
            "collapse_flag": bool(total and share.max() > COLLAPSE_SHARE),
        })
    return out


def evaluate(cfg, params, buffers, schema: FeatureSchema,
             norm: Normalization, splits: dict, *,
             input_ood: InputOOD) -> dict:
    """``splits``: name -> list of Samples. Returns the report body
    (everything but the binding fields, which the caller fills)."""
    preds, toks, ys = {}, {}, {}
    for name, members in splits.items():
        if not members:
            continue
        toks[name] = tokenize(schema, norm, [s.inputs for s in members])
        ys[name] = encode_targets(schema, norm,
                                  [s.targets for s in members])
        preds[name] = predict(params, buffers, cfg, toks[name])
    per_target = {name: target_metrics(schema, norm, ys[name],
                                       preds[name]["mean"],
                                       preds[name]["var"])
                  for name in preds}
    ood = {}
    if "test" in per_target and "ood" in per_target:
        ood["rmse_ratio_ood_over_test"] = {
            t: per_target["ood"][t]["rmse"] / per_target["test"][t]["rmse"]
            for t in schema.target_names}
        ood["coverage90_test_vs_ood"] = {
            t: [per_target["test"][t]["interval_coverage"]["0.9"],
                per_target["ood"][t]["interval_coverage"]["0.9"]]
            for t in schema.target_names}
        tr = preds.get("train")
        if tr is not None:
            mu = tr["pooled"].mean(axis=0)
            cov = np.cov(tr["pooled"], rowvar=False) + 1e-6 * np.eye(
                tr["pooled"].shape[1])
            prec = np.linalg.inv(cov)

            def d2(x):
                c = x - mu
                return np.einsum("ni,ij,nj->n", c, prec, c)

            ood["representation_mahalanobis_auroc_test_vs_ood"] = _auroc(
                d2(preds["test"]["pooled"]), d2(preds["ood"]["pooled"]))
        classes = {}
        for name in ("test", "ood"):
            counts: dict = {}
            for s in splits[name]:
                c = input_ood.assess(schema, norm, s.inputs)["class"]
                counts[c] = counts.get(c, 0) + 1
            classes[name] = counts
        ood["input_classes"] = classes
    cons = {}
    for name in ("test", "ood"):
        if name not in preds:
            continue
        phys = decode_targets(schema, norm, preds[name]["mean"])
        rows = [(s.sample_id, s.inputs,
                 {t: float(phys[i, j]) for j, t in
                  enumerate(schema.target_names)})
                for i, s in enumerate(splits[name])]
        cons[name] = C.evaluate(C.surface_adsorption(), rows)
    router = router_health(preds["test"]["router"], cfg) \
        if "test" in preds and cfg.moe is not None else []
    return {"per_target": per_target, "ood": ood, "constraints": cons,
            "router": router, "splits": {k: len(v) for k, v in
                                         splits.items()},
            "semantics": list(PREDICTION_SEMANTICS),
            "_test_outputs": preds.get("test")}


def outputs_equal(a: dict, b: dict) -> bool:
    return all(np.array_equal(a[k], b[k]) for k in ("mean", "var",
                                                    "pooled"))
