"""Deep experimental-design runner (Stage 1: dataset + fail-closed readiness).

Builds the joint latent space and design space from existing repository sources,
generates the deterministic simulation dataset, and writes the compact, canonical
outputs (schemas, dataset summary, dataset hash) plus a fail-closed
``deep_surrogate_readiness.json``. Until the neural posterior / EIG surrogate is
trained AND validated against the direct nested-Monte-Carlo reference (a later
stage), the readiness state is ``DATASET_GENERATED`` and the surrogate does NOT
control experiment ordering; the engine falls back to direct Monte Carlo.

Nothing here is a measurement, and no physical gate is created or upgraded.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .config import DeepConfig, ci_config, config_for, SCHEMA_VERSION
from .schemas import ReadinessState, SIMULATION_LABELS
from . import parameter_space as _ps
from . import design_space as _ds
from . import dataset as _dset
from . import io as _io
from .readiness import Evidence, readiness_record, compute_state


def _dependency_ok() -> bool:
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
        return True
    except Exception:
        return False


def run_deep_expdesign(*, profile: str = "ci", repo_root: Optional[Path] = None,
                       output_dir: Optional[Path] = None,
                       cfg: Optional[DeepConfig] = None,
                       write_bulk: bool = False) -> dict:
    """Run Stage 1 of the deep layer and write canonical compact outputs."""
    cfg = (cfg or config_for(profile)).validate()
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    out = Path(output_dir) if output_dir else _io.deep_output_dir(root)
    out.mkdir(parents=True, exist_ok=True)

    latents = _ps.build_latent_parameters(root)
    design_vars = _ds.design_variables()
    families = _ds.load_design_families(root)
    interlocks = _ds.load_interlocks(root)

    # deterministic simulation dataset
    ds = _dset.generate_dataset(cfg, repo_root=root)
    summary = _dset.summarize(ds, cfg)
    if write_bulk:
        _dset.persist_bulk(ds, out)

    # ---- fail-closed readiness (model not trained yet) ----
    ev = Evidence(dependency_ok=_dependency_ok(), dataset_generated=True,
                  trained=False, validation_ran=False, thresholds_passed=None,
                  ood_triggered=False,
                  notes="Stage 1: dataset generated; neural posterior/EIG surrogate "
                        "not yet trained or validated. Fail-closed to direct MC.")
    readiness = readiness_record(ev, cfg)

    # ---- write canonical compact outputs ----
    _io.write_json(out / "deep_parameter_schema.json", {
        "schema_version": SCHEMA_VERSION, "labels": dict(SIMULATION_LABELS),
        "latent_parameters": [p.to_dict() for p in latents],
        "trainable": [p.name for p in _ps.trainable_subspace(latents)],
    })
    _io.write_json(out / "deep_design_schema.json", {
        "schema_version": SCHEMA_VERSION,
        "design_variables": [v.to_dict() for v in design_vars],
        "design_families": [f.to_dict() for f in families],
        "forbidden_simultaneous_states": interlocks,
    })
    _io.write_json(out / "deep_training_config.json", cfg.as_dict())
    _io.write_json(out / "deep_dataset_summary.json", summary.to_dict())
    (out / "deep_dataset_hash.txt").write_text(
        f"sha256: {ds.sha256}\n"
        f"schema: {SCHEMA_VERSION}\n"
        f"n_total: {summary.n_total}\n"
        f"source_commit: {ds.source_commit}\n"
        f"labels: SIMULATION_GENERATED FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM\n",
        encoding="utf-8")
    _io.write_json(out / "deep_surrogate_readiness.json", readiness)
    _io.write_json(out / "deep_expdesign_summary.json", {
        "schema_version": SCHEMA_VERSION,
        "stage": "dataset_generated",
        "status": readiness["status"],
        "controls_experiment_ordering": readiness["controls_experiment_ordering"],
        "fallback": readiness["fallback"],
        "n_trainable_latents": len(_ps.trainable_subspace(latents)),
        "n_unknown_latents": sum(1 for p in latents if not p.is_trainable),
        "n_design_families": len(families),
        "dataset_sha256": ds.sha256,
        "labels": dict(SIMULATION_LABELS),
        "reference_estimator": "direct_nested_monte_carlo",
        "no_pass_states_created": True,
        "remaining_not_implemented": [
            "posterior_model (conditional density estimator) training",
            "eig_surrogate training", "calibration (SBC)", "ood detection runtime",
            "direct-MC validation + trust gating", "adaptive posterior-conditioned policy",
        ],
    })

    return {"config": cfg, "dataset": ds, "summary": summary,
            "readiness": readiness, "output_dir": out,
            "state": compute_state(ev)}


def _trust_decision(vr, cal, cfg):
    """Combine EIG-comparison and calibration into a single fail-closed trust bool."""
    th = cfg.thresholds
    calib_ok = (cal.max_coverage_error <= th.max_calibration_error and
                cal.empirical_coverage.get(0.9, 0.0) >= th.min_posterior_coverage_90)
    return bool(vr.thresholds_passed and calib_ok), bool(calib_ok)


def run_deep_expdesign_full(*, profile: str = "ci", repo_root=None, output_dir=None,
                            cfg: Optional[DeepConfig] = None) -> dict:
    """Stage 2: train the posterior, validate vs direct nested-MC EIG, calibrate,
    OOD-check, and emit all deep outputs with REAL content. Fail-closed: the
    surrogate controls ordering only if every trust threshold (incl. calibration)
    passes; otherwise status is TRAINED_NOT_TRUSTED and the engine falls back to
    direct Monte Carlo. No metric is fabricated; no physical gate is created."""
    import time
    import numpy as _np
    import numpy as numpy_v
    import scipy as scipy_v
    import platform
    from . import trainer as _tr, validation as _val, calibration as _cal
    from . import ood as _ood, policy as _pol, inference as _inf
    from . import likelihood_model as _lm
    from .simulator_adapter import observable_names as _obsn

    cfg = (cfg or config_for(profile)).validate()
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    out = Path(output_dir) if output_dir else _io.deep_output_dir(root)
    out.mkdir(parents=True, exist_ok=True)

    # ---- train ----
    t0 = time.time()
    tp = _tr.train_posterior(cfg, repo_root=root)
    train_dur = time.time() - t0
    ds = tp.dataset

    # ---- validate vs direct nested-MC EIG ----
    t1 = time.time()
    bench = _val.benchmark_designs(cfg.n_benchmark_designs)
    vr = _val.validate_against_direct_mc(tp, bench, cfg, seed=0)
    val_dur = time.time() - t1

    # ---- calibration (SBC coverage) ----
    cal = _cal.run_sbc(tp, seed=0)

    # ---- OOD ----
    om = _ood.fit_ood(tp.context_train, quantile=cfg.ood_quantile,
                      maha_factor=cfg.ood_mahalanobis_factor)
    far = _np.tile(om.hi * 5.0 + 5.0, (10, 1))
    oodrep = _ood.ood_report(om, {"train": tp.context_train, "far_synthetic": far})

    # ---- trust decision (fail-closed; folds calibration in) ----
    trust_pass, calib_ok = _trust_decision(vr, cal, cfg)
    ev = Evidence(dependency_ok=_dependency_ok(), dataset_generated=True, trained=True,
                  validation_ran=True, thresholds_passed=trust_pass, ood_triggered=False,
                  notes=("Stage 2: posterior trained and validated against direct "
                         "nested-MC EIG and SBC coverage. Trust requires all EIG "
                         "thresholds AND calibration to pass."))
    metrics = {
        "eig_mean_abs_error_nats": vr.mean_abs_error,
        "eig_max_abs_error_nats": vr.max_abs_error,
        "eig_normalized_error": vr.normalized_error,
        "spearman_rank_corr": vr.spearman,
        "top1_agreement": vr.top1_agreement, "top3_agreement": vr.top3_agreement,
        "repeat_seed_eig_std_nats": vr.repeat_seed_eig_std,
        "calibration_coverage": {str(k): v for k, v in cal.empirical_coverage.items()},
        "max_calibration_error": cal.max_coverage_error,
        "calibration_passed": calib_ok,
        "eig_thresholds_passed": bool(vr.thresholds_passed),
        "final_train_nll": tp.history["nll"][-1],
        "per_threshold": {k: {"value": v[0], "threshold": v[1], "passed": v[2]}
                          for k, v in vr.per_threshold.items()},
    }
    readiness = readiness_record(ev, cfg, metrics=metrics)
    controls = readiness["controls_experiment_ordering"]

    # ---- adaptive policy (uses direct-MC EIG unless surrogate is trusted) ----
    eig_source = "deep_surrogate" if controls else "direct_monte_carlo"
    eig_for_policy = vr.eig_deep if controls else vr.eig_direct
    cands = [_pol.Candidate(experiment_id=bench[i].experiment_id,
                            eig=float(eig_for_policy[i]), criticality=0.5,
                            falsification=0.5, dependency=0.5,
                            cost=bench[i].estimated_cost, duration=bench[i].estimated_duration,
                            risk=bench[i].estimated_risk) for i in range(len(bench))]
    policy_out = _pol.rank_candidates(cands, eig_source=eig_source, controls_ordering=controls)

    # ---- representative posterior query (forecast-only) ----
    names, lo, hi, transforms = _lm.trainable_prior_bounds(root)
    theta_med_t = (lo + hi) / 2.0
    rep_design = bench[0]
    x_rep = _lm.forward_matrix(theta_med_t[None, :], rep_design, names, transforms)[0]
    post = _inf.posterior_query(tp, x_rep, rep_design, n_samples=2000, seed=0)
    samp_std = tp.model.sample(_np.hstack([tp.std_x.transform(x_rep[None, :]),
                               _np.array([_inf._design_norm(rep_design)])]), n=200, seed=0)[0]
    samp_t = samp_std * tp.std_theta.std + tp.std_theta.mean
    samp_nat = _np.column_stack([_lm._tf.from_model(samp_t[:, j], transforms[j])
                                 for j in range(len(names))])

    # ---- write outputs (deterministic) ----
    obs = _obsn()
    _io.write_csv(out / "deep_training_history.csv", ["step", "nll"],
                  list(zip(tp.history["step"], tp.history["nll"])))
    n_params = int(sum(v.size for v in tp.model.p.flat()))
    _io.write_json(out / "deep_model_manifest.json", {
        "schema_version": SCHEMA_VERSION, "backend": "numpy_cpu",
        "architecture": "conditional_mixture_density_network",
        "d_in": tp.model.d_in, "d_out": tp.model.d_out, "n_mixture": tp.model.K,
        "hidden_units": tp.model.H, "n_parameters": n_params,
        "input_layout": "[standardized_observation(%d), normalized_design(%d)]" % (
            len(obs), len(_lm.design_feature_names())),
        "device": "cpu", "dtype": cfg.dtype, "master_seed": cfg.master_seed,
        "deterministic": True,
        "library_versions": {"numpy": numpy_v.__version__, "scipy": scipy_v.__version__,
                             "python": platform.python_version()},
        "weights_committed": False,
        "note": "Weights are not a canonical manifest artifact; regenerate deterministically from seed.",
        "forecast_only": True, "measured_in_this_system": False,
    })
    # non-canonical runtime profile (wall-clock timings are not deterministic)
    _io.write_json(out / "deep_runtime_profile.json", {
        "schema_version": SCHEMA_VERSION, "canonical": False,
        "train_duration_s": round(train_dur, 3), "validation_duration_s": round(val_dur, 3),
        "device": "cpu", "note": "Timings vary run-to-run; excluded from the manifest."})
    _io.write_json(out / "deep_posterior_summary.json", {
        "schema_version": SCHEMA_VERSION,
        "representative_design": rep_design.experiment_id,
        "representative_observation": {obs[k]: float(x_rep[k]) for k in range(len(obs))},
        **post})
    _io.write_csv(out / "deep_posterior_samples.csv", list(names),
                  [list(samp_nat[i]) for i in range(samp_nat.shape[0])])
    cal_rows = []
    for lv in cal.levels:
        cal_rows.append([lv, cal.empirical_coverage[lv], cal.coverage_error[lv]]
                        + cal.per_param_coverage[lv])
    _io.write_csv(out / "deep_posterior_calibration.csv",
                  ["nominal_level", "empirical_coverage", "coverage_error"] + list(names),
                  cal_rows)
    _io.write_csv(out / "deep_eig_validation.csv",
                  ["design", "eig_deep_nats", "eig_direct_mc_nats", "eig_direct_mc_se", "abs_error"],
                  [[vr.designs[i], vr.eig_deep[i], vr.eig_direct[i], vr.eig_direct_se[i],
                    abs(vr.eig_deep[i] - vr.eig_direct[i])] for i in range(len(vr.designs))])
    deep_rank = {d: r for r, d in enumerate(_np.argsort(-_np.array(vr.eig_deep)))}
    dir_rank = {d: r for r, d in enumerate(_np.argsort(-_np.array(vr.eig_direct)))}
    _io.write_csv(out / "deep_ranking_comparison.csv",
                  ["design", "deep_eig_rank", "direct_mc_eig_rank"],
                  [[vr.designs[i], int(_np.where(_np.argsort(-_np.array(vr.eig_deep)) == i)[0][0]),
                    int(_np.where(_np.argsort(-_np.array(vr.eig_direct)) == i)[0][0])]
                   for i in range(len(vr.designs))])
    _io.write_json(out / "deep_ood_report.json", {
        "schema_version": SCHEMA_VERSION, **oodrep, "forecast_only": True,
        "measured_in_this_system": False})
    _io.write_json(out / "deep_adaptive_policy.json", {
        "schema_version": SCHEMA_VERSION, **policy_out, "forecast_only": True,
        "measured_in_this_system": False,
        "note": "EIG source is direct Monte Carlo unless the surrogate is a trusted "
                "VALIDATED_SURROGATE."})
    _io.write_json(out / "deep_surrogate_readiness.json", readiness)

    # schemas + dataset summary (compact, canonical-eligible)
    from . import parameter_space as _ps2
    latents = _ps2.build_latent_parameters(root)
    _io.write_json(out / "deep_training_config.json", cfg.as_dict())
    _io.write_json(out / "deep_expdesign_summary.json", {
        "schema_version": SCHEMA_VERSION, "stage": "trained_and_validated",
        "status": readiness["status"],
        "controls_experiment_ordering": controls,
        "fallback": readiness["fallback"],
        "eig_source_used_by_policy": eig_source,
        "n_trainable_latents": len(names),
        "n_dataset": cfg.n_dataset, "n_train": int(len(ds.split["train"])),
        "n_val": int(len(ds.split["val"])), "n_test": int(len(ds.split["test"])),
        "device": "cpu", "backend": "numpy_cpu",
        "trust_metrics": metrics,
        "reference_estimator": "direct_nested_monte_carlo (matched generative model)",
        "no_pass_states_created": True,
        "labels": dict(SIMULATION_LABELS),
        "claim_boundary": ("Numerical SBI/EIG surrogate validated against a direct "
                           "reference estimator. This is NOT experimental validation "
                           "of the physical architecture."),
    })

    return {"config": cfg, "trained": tp, "validation": vr, "calibration": cal,
            "ood": oodrep, "policy": policy_out, "readiness": readiness,
            "metrics": metrics, "output_dir": out, "trust_pass": trust_pass}


def run_deep_expdesign_full(*, profile: str = "ci", repo_root=None, output_dir=None,
                            cfg: "DeepConfig | None" = None) -> dict:
    """Stage 2: train the posterior, validate vs direct MC, calibrate, OOD-check,
    and emit all deep outputs with an honest (fail-closed) readiness state.

    The learned surrogate controls ordering ONLY if it passes every trust
    threshold; otherwise the readiness is TRAINED_NOT_TRUSTED and the policy uses
    the direct nested-Monte-Carlo EIG. Nothing here creates or upgrades a gate.
    """
    import time as _time
    import hashlib as _hashlib
    import numpy as _np
    from .config import config_for as _cf, BACKEND, BACKEND_REASON, SCHEMA_VERSION
    from . import trainer as _tr
    from . import validation as _val
    from . import calibration as _cal
    from . import ood as _ood
    from . import inference as _inf
    from . import policy as _pol
    from . import likelihood_model as _lm
    from . import transforms as _tf2
    from .eig_surrogate import _design_norm as _dn
    from .readiness import Evidence as _Ev, readiness_record as _rr
    from .schemas import ReadinessState as _RS, SIMULATION_LABELS as _LAB

    if cfg is None:
        # CI-tractable, still-conservative settings; deterministic
        ov = dict(eig_ref_outer=200, eig_ref_inner=200, eig_ref_batches=3,
                  n_repeat_seeds=2) if profile == "ci" else {}
        if profile == "ci":
            ov["n_dataset"] = 256; ov["train_steps"] = 800
        cfg = _cf(profile, **ov)
    cfg.validate()
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    out = Path(output_dir) if output_dir else _io.deep_output_dir(root)
    out.mkdir(parents=True, exist_ok=True)

    # ---- train ----
    t0 = _time.time()
    tp = _tr.train_posterior(cfg, repo_root=root)
    train_secs = _time.time() - t0

    # ---- validate vs direct MC ----
    bench = _val.benchmark_designs(cfg.n_benchmark_designs)
    vr = _val.validate_against_direct_mc(tp, bench, cfg, seed=0)

    # ---- calibration ----
    cal = _cal.run_sbc(tp, seed=0)

    # ---- OOD ----
    om = _ood.fit_ood(tp.context_train, quantile=cfg.ood_quantile,
                      maha_factor=cfg.ood_mahalanobis_factor)
    val_idx = tp.dataset.split["val"][tp.dataset.success[tp.dataset.split["val"]]]
    val_ctx = _np.hstack([tp.std_x.transform(tp.dataset.x[val_idx]),
                          tp.dataset.design_norm[val_idx]]) if len(val_idx) else tp.context_train[:1]
    far = _np.tile(om.hi * 4.0 + 5.0, (16, 1))
    ood_rep = _ood.ood_report(om, {"train": tp.context_train, "validation": val_ctx,
                                   "synthetic_far_ood": far})

    # ---- posterior summary on a representative observation ----
    rep_design = bench[0]
    names, transf = tp.dataset.names, tp.dataset.transforms
    lo, hi = tp.prior_lo, tp.prior_hi
    rng = _np.random.default_rng(cfg.master_seed)
    theta_star_t = lo + 0.5 * (hi - lo)
    x_rep = _lm.forward_matrix(theta_star_t[None, :], rep_design, names, transf)[0]
    x_rep = x_rep + _lm.obs_sigma_vector() * rng.standard_normal(x_rep.shape)
    post = _inf.posterior_query(tp, x_rep, rep_design, n_samples=2000, seed=cfg.master_seed)
    post_samples = tp.model.sample(
        _np.hstack([tp.std_x.transform(x_rep[None, :]), _dn(rep_design)[None, :]]),
        n=200, seed=cfg.master_seed)[0]
    post_samples_t = post_samples * tp.std_theta.std + tp.std_theta.mean
    post_samples_nat = _np.column_stack([
        _tf2.from_model(post_samples_t[:, j], transf[j]) for j in range(len(names))])

    # ---- readiness (fail-closed) ----
    passed = bool(vr.thresholds_passed and cal.max_coverage_error <= cfg.thresholds.max_calibration_error)
    ev = _Ev(dependency_ok=True, dataset_generated=True, trained=True,
             validation_ran=True, thresholds_passed=passed, ood_triggered=False,
             notes=("Stage 2: posterior trained and validated against direct nested-MC. "
                    "Trusted only if every threshold passes; otherwise fail-closed."))
    metrics = {
        "eig_mean_abs_error_nats": vr.mean_abs_error,
        "eig_max_abs_error_nats": vr.max_abs_error,
        "eig_normalized_error": vr.normalized_error,
        "eig_spearman": vr.spearman, "eig_top1_agreement": vr.top1_agreement,
        "eig_top3_agreement": vr.top3_agreement,
        "repeat_seed_eig_std_nats": vr.repeat_seed_eig_std,
        "max_calibration_error": cal.max_coverage_error,
        "final_train_nll": float(tp.history["nll"][-1]),
        "per_threshold": {k: {"value": v[0], "threshold": v[1], "passed": bool(v[2])}
                          for k, v in vr.per_threshold.items()},
    }
    readiness = _rr(ev, cfg, metrics=metrics)
    eig_source = "deep_surrogate" if readiness["controls_experiment_ordering"] else "direct_monte_carlo"

    # ---- adaptive policy (uses direct-MC EIG when surrogate untrusted) ----
    eig_for_policy = vr.eig_deep if readiness["controls_experiment_ordering"] else vr.eig_direct
    cands = [_pol.Candidate(experiment_id=bench[i].experiment_id, eig=float(eig_for_policy[i]),
                            criticality=0.6, falsification=0.5, dependency=0.5,
                            cost=bench[i].estimated_cost, duration=bench[i].estimated_duration,
                            risk=bench[i].estimated_risk) for i in range(len(bench))]
    pol = _pol.rank_candidates(cands, eig_source=eig_source,
                               controls_ordering=readiness["controls_experiment_ordering"])

    # weight hash (training deterministic -> weights deterministic)
    wbytes = b"".join(_np.ascontiguousarray(a).tobytes() for a in tp.model.p.flat())
    w_sha = _hashlib.sha256(wbytes).hexdigest()

    # ---- emit outputs ----
    # schema + dataset outputs (so ROOT carries all directive-required deep outputs)
    from .parameter_space import (build_latent_parameters as _blp,
                                   trainable_subspace as _tss)
    from .design_space import (design_variables as _dv, load_design_families as _ldf,
                               load_interlocks as _li)
    _lat = _blp(root)
    _amort = tp.dataset
    _dhash = _hashlib.sha256(__import__("json").dumps({
        "theta": _np.round(_amort.theta_t, 10).tolist(),
        "x": _np.round(_np.nan_to_num(_amort.x, nan=0.0), 10).tolist(),
        "design": _np.round(_amort.design_norm, 10).tolist(),
        "success": _amort.success.astype(int).tolist(),
    }, sort_keys=True).encode()).hexdigest()
    _io.write_json(out / "deep_parameter_schema.json", {
        "schema_version": SCHEMA_VERSION, "labels": dict(_LAB),
        "latent_parameters": [p.to_dict() for p in _lat],
        "trainable": [p.name for p in _tss(_lat)]})
    _io.write_json(out / "deep_design_schema.json", {
        "schema_version": SCHEMA_VERSION,
        "design_variables": [v.to_dict() for v in _dv()],
        "design_families": [f.to_dict() for f in _ldf(root)],
        "forbidden_simultaneous_states": _li(root)})
    _io.write_json(out / "deep_training_config.json", cfg.as_dict())
    _io.write_json(out / "deep_dataset_summary.json", {
        "schema_version": SCHEMA_VERSION, "n_total": int(_amort.theta_t.shape[0]),
        "n_success": int(_amort.success.sum()), "n_failure": int((~_amort.success).sum()),
        "n_train": int(len(_amort.split["train"])), "n_val": int(len(_amort.split["val"])),
        "n_test": int(len(_amort.split["test"])), "latent_names": list(_amort.names),
        "design_feature_names": _lm.design_feature_names(),
        "observable_names": list(_amort.obs_names), "sampler": cfg.sampler,
        "master_seed": cfg.master_seed, "dataset_sha256": _dhash,
        "amortized_over_design": True, "labels": dict(_LAB)})
    (out / "deep_dataset_hash.txt").write_text(
        f"sha256: {_dhash}\nschema: {SCHEMA_VERSION}\n"
        f"n_total: {int(_amort.theta_t.shape[0])}\n"
        f"labels: SIMULATION_GENERATED FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM\n",
        encoding="utf-8")
    _io.write_csv(out / "deep_training_history.csv", ["step", "nll"],
                  list(zip(tp.history["step"], tp.history["nll"])))
    import numpy as _npv
    import scipy as _spv
    _io.write_json(out / "deep_model_manifest.json", {
        "schema_version": SCHEMA_VERSION, "backend": BACKEND, "backend_reason": BACKEND_REASON,
        "device": "cpu", "dtype": cfg.dtype, "architecture": "conditional_mixture_density_network",
        "input_dim": int(tp.context_train.shape[1]), "output_dim": len(names),
        "n_mixture": cfg.n_mixture, "hidden_units": cfg.hidden_units,
        "n_parameters": int(sum(a.size for a in tp.model.p.flat())),
        "train_steps": cfg.train_steps, "learning_rate": cfg.learning_rate,
        "weight_decay": cfg.weight_decay, "master_seed": cfg.master_seed,
        "library_versions": {"numpy": _npv.__version__, "scipy": _spv.__version__},
        "weight_sha256": w_sha, "deterministic": True, "labels": dict(_LAB),
        "note": "weights deterministic given seed; wall-clock timings are in "
                "deep_runtime_profile.json (non-canonical); not added to the package manifest.",
    })
    # non-canonical runtime profile (wall-clock; excluded from determinism guarantees)
    _io.write_json(out / "deep_runtime_profile.json", {
        "schema_version": SCHEMA_VERSION, "device": "cpu", "dtype": cfg.dtype,
        "training_seconds": round(train_secs, 3), "deterministic_flags": {
            "training": True, "sampling": True, "monte_carlo_seeded": True},
        "note": "wall-clock measurement; nondeterministic by nature.",
    })
    _io.write_json(out / "deep_posterior_summary.json", {
        "schema_version": SCHEMA_VERSION, "design": rep_design.to_dict(),
        "observation": x_rep.tolist(), "true_theta_transformed": theta_star_t.tolist(),
        **post, "labels": dict(_LAB)})
    _io.write_csv(out / "deep_posterior_samples.csv", list(names),
                  post_samples_nat.tolist())
    cal_rows = []
    for lv in cal.levels:
        cal_rows.append([lv, cal.empirical_coverage[lv], cal.coverage_error[lv]]
                        + cal.per_param_coverage[lv])
    _io.write_csv(out / "deep_posterior_calibration.csv",
                  ["nominal_level", "empirical_coverage", "coverage_error"] + list(names),
                  cal_rows)
    _io.write_csv(out / "deep_eig_validation.csv",
                  ["design", "eig_deep_nats", "eig_direct_mc_nats", "eig_direct_se_nats", "abs_error_nats"],
                  [[vr.designs[i], vr.eig_deep[i], vr.eig_direct[i], vr.eig_direct_se[i],
                    abs(vr.eig_deep[i] - vr.eig_direct[i])] for i in range(len(vr.designs))])
    rank_deep = list(_np.argsort(-_np.array(vr.eig_deep)).argsort() + 1)
    rank_dir = list(_np.argsort(-_np.array(vr.eig_direct)).argsort() + 1)
    _io.write_csv(out / "deep_ranking_comparison.csv",
                  ["design", "rank_deep", "rank_direct_mc"],
                  [[vr.designs[i], int(rank_deep[i]), int(rank_dir[i])] for i in range(len(vr.designs))])
    _io.write_json(out / "deep_ood_report.json", {
        "schema_version": SCHEMA_VERSION, **ood_rep, "labels": dict(_LAB)})
    _io.write_json(out / "deep_adaptive_policy.json", {
        "schema_version": SCHEMA_VERSION, **pol, "labels": dict(_LAB),
        "no_pass_states_created": True, "measured_in_this_system": False})
    _io.write_json(out / "deep_surrogate_readiness.json", readiness)
    _io.write_json(out / "deep_expdesign_summary.json", {
        "schema_version": SCHEMA_VERSION, "stage": "trained_and_validated",
        "status": readiness["status"],
        "controls_experiment_ordering": readiness["controls_experiment_ordering"],
        "fallback": readiness["fallback"], "eig_source_for_policy": eig_source,
        "metrics": metrics, "n_benchmark_designs": len(bench),
        "dataset_n": cfg.n_dataset, "device": "cpu", "backend": BACKEND,
        "labels": dict(_LAB), "reference_estimator": "direct_nested_monte_carlo",
        "no_pass_states_created": True,
    })

    return {"config": cfg, "trained": tp, "validation": vr, "calibration": cal,
            "ood": ood_rep, "policy": pol, "readiness": readiness,
            "metrics": metrics, "output_dir": out}
