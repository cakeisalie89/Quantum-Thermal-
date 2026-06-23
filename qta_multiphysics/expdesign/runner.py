"""Top-level Bayesian experimental-design runner + deterministic outputs."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from .model import load_candidates
from .engine import (
    compute_all_eig, rank_experiments, adaptive_policy, DeepEIGSurrogate,
    select_estimator_source, W_EIG, W_CRIT, W_FALS, W_DEP,
)

_FLOAT = "{:.10e}"


def _fmt(x):
    if x is None:
        return ""
    if isinstance(x, float):
        if math.isnan(x):
            return "nan"
        return _FLOAT.format(x)
    return str(x)


def _write_csv(path: Path, header, rows):
    lines = [",".join(header)]
    for r in rows:
        lines.append(",".join('"' + str(v).replace('"', "'") + '"'
                              if ("," in str(v) or ";" in str(v)) else _fmt(v)
                              for v in r))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_json(path: Path, obj):
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
                    encoding="utf-8")


def run_expdesign_layer(*, n_outer: int = 600, n_inner: int = 600, n_batches: int = 8,
                        seed: int = 42, profile: str = "standard",
                        output_dir: Optional[Path] = None, write_outputs: bool = True,
                        repo_root: Optional[Path] = None) -> dict:
    cands = load_candidates(repo_root)
    eigs = compute_all_eig(cands, n_outer=n_outer, n_inner=n_inner, seed=seed,
                           n_batches=n_batches)
    ranking = rank_experiments(cands, eigs)
    policy = adaptive_policy(cands, eigs, ranking)
    surrogate = DeepEIGSurrogate()
    estimator_source = select_estimator_source(surrogate)  # -> direct_monte_carlo

    if output_dir is None:
        output_dir = Path(__file__).resolve().parents[1] / "outputs"
    output_dir = Path(output_dir)

    if write_outputs:
        output_dir.mkdir(parents=True, exist_ok=True)
        by_id = {c.experiment_id: c for c in cands}

        # 1. candidates
        ch = ["experiment_id", "label", "parameter", "prior", "required_hardware",
              "required_mode", "gates_affected", "predecessors", "cost", "duration",
              "risk", "criticality", "falsification_value", "safety_note"]
        crows = []
        for c in sorted(cands, key=lambda x: x.experiment_id):
            r = c.to_row()
            crows.append([r["experiment_id"], r["label"], r["parameter"], r["prior"],
                          r["required_hardware"], r["required_mode"], r["gates_affected"],
                          r["predecessors"], r["cost"], r["duration"], r["risk"],
                          r["criticality"], r["falsification_value"], r["safety_note"]])
        _write_csv(output_dir / "experimental_design_candidates.csv", ch, crows)

        # 2. expected information gain
        eh = ["experiment_id", "eig_nats", "eig_se", "prior_entropy_nats",
              "expected_posterior_entropy_nats", "n_outer", "n_inner", "n_batches", "method"]
        erows = []
        for eid in sorted(eigs):
            e = eigs[eid]
            erows.append([eid, e["eig_nats"], e["eig_se"], e["prior_entropy_nats"],
                          e["expected_posterior_entropy_nats"], e["n_outer"], e["n_inner"],
                          e["n_batches"], e["method"]])
        _write_csv(output_dir / "expected_information_gain.csv", eh, erows)

        # 3. ranking
        rh = ["rank", "experiment_id", "label", "score", "eig_component",
              "criticality_component", "falsification_component", "dependency_component",
              "cost_penalty", "source"]
        rrows = [[r["rank"], r["experiment_id"], r["label"], r["score"],
                  r["eig_component"], r["criticality_component"], r["falsification_component"],
                  r["dependency_component"], r["cost_penalty"], r["source"]] for r in ranking]
        _write_csv(output_dir / "validation_experiment_ranking.csv", rh, rrows)

        # 4. adaptive policy
        _write_json(output_dir / "adaptive_experiment_policy.json", policy)

        # 5. summary
        summary = {
            "_note": "Bayesian experimental-design summary. EIG = mutual information "
                     "I(theta;data), nested-MC estimate with batched SE. Ranking blends "
                     "EIG, gate criticality, falsification value, dependencies, and cost/"
                     "duration/risk - not gate count. Forecast-only; no measured results.",
            "forecast_only": True,
            "profile": profile,
            "estimator_source": estimator_source,
            "ranking_weights": {"eig": W_EIG, "criticality": W_CRIT,
                                "falsification": W_FALS, "dependency": W_DEP},
            "monte_carlo": {"n_outer": n_outer, "n_inner": n_inner,
                            "n_batches": n_batches, "seed": seed},
            "ranking": [{"rank": r["rank"], "experiment_id": r["experiment_id"],
                         "score": r["score"]} for r in ranking],
            "top_experiment": ranking[0]["experiment_id"],
            "human_priority_was": ["EXP-A", "EXP-B", "EXP-C", "EXP-D", "EXP-E", "EXP-F"],
            "ranking_differs_from_human_priority": (
                [r["experiment_id"] for r in ranking]
                != ["EXP-A", "EXP-B", "EXP-C", "EXP-D", "EXP-E", "EXP-F"]),
        }
        _write_json(output_dir / "bayesian_design_summary.json", summary)

        # 6. deep surrogate readiness
        _write_json(output_dir / "deep_surrogate_readiness.json", surrogate.readiness())

        # 7. falsification map
        fh = ["experiment_id", "parameter", "transform", "falsification_value",
              "falsify_threshold_transformed", "kill_condition"]
        frows = []
        for c in sorted(cands, key=lambda x: x.experiment_id):
            pm = c.parameter
            frows.append([c.experiment_id, pm.name, pm.transform, c.falsification_value,
                          ("" if pm.falsify_below_transformed is None
                           else pm.falsify_below_transformed), c.kill_condition])
        _write_csv(output_dir / "experiment_falsification_map.csv", fh, frows)

    return {
        "candidates": cands, "eigs": eigs, "ranking": ranking, "policy": policy,
        "surrogate_readiness": surrogate.readiness(), "estimator_source": estimator_source,
    }


def ci_run(**kw):
    return run_expdesign_layer(n_outer=200, n_inner=200, n_batches=4, profile="ci", **kw)


def standard_run(**kw):
    return run_expdesign_layer(profile="standard", **kw)
