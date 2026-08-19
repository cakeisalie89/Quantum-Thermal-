"""Global (variance-based) sensitivity cross-check via SALib.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

``qta_multiphysics.sensitivity_3d`` is and remains the sensitivity authority:
a deterministic one-at-a-time (OAT) +10% ranking on the CI mesh, whose output
is a canonical, byte-gated file. OAT is a *local* measure and cannot see
interaction effects, so this module adds a **cross-check**, not a replacement:
it runs a Sobol (or Morris) analysis over the same four uncertain inputs, on
the same response, and reports whether the global ranking agrees with the
canonical local one.

The rules that keep this a cross-check:

* The response function is imported from ``sensitivity_3d`` itself rather
  than re-implemented, so the two rankings cannot drift apart silently.
* Disagreement is *reported*, never resolved. If the Sobol ranking differs
  from the OAT ranking, the report says so and the canonical ranking stands;
  a disagreement is a finding for a human, not an automatic override
  (``automatic_gate_effect = NONE``).
* SALib is optional. Without it the module reports ``UNAVAILABLE`` and names
  the in-repo authority; it never silently substitutes a different estimator.
* Sensitivities are properties of THIS model under THESE assumptions. They
  are never experimental importance and never gate evidence.

Cost note: one response evaluation is a full 3D transient solve (~4 s on the
CI mesh, ~0.9 s on the reduced screening mesh), and Sobol needs
``N * (D + 2)`` of them. The screening mesh is therefore the default here and
the analysis is opt-in; the canonical OAT ranking still runs on the CI mesh.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable

import numpy as np
import numpy.typing as npt

from ..config import MultiphysicsConfig, default_config
from ..mesh_3d import Grid3DConfig
from ..sensitivity_3d import CI as CI_MESH
from ..sensitivity_3d import STEP as OAT_STEP
from ..sensitivity_3d import _rise as canonical_probe_rise_K
from ..sensitivity_3d import sensitivity_rows as canonical_oat_rows
from . import AUTOMATIC_GATE_EFFECT, LABEL
from .workspace import (StrPath, guard_output_dir,
                        write_json_deterministic)

# Reduced screening mesh: coarser than the CI mesh (10x10x12) so that the
# hundreds of evaluations a variance-based method needs stay tractable. It
# reproduces the CI-mesh probe rise to within a few percent, which is
# adequate for *ranking* but never for a reported value.
SCREENING_MESH = Grid3DConfig(nx=6, ny=6, nz=8)
SCREENING_N_EVAL = 5
DEFAULT_BOUNDS_FRACTION = OAT_STEP        # +-10%, matching the OAT step
DEFAULT_SEED = 20260819

@dataclass(frozen=True)
class ParameterSpec:
    """One uncertain model input, with where it lives and what it may be.

    A typed record rather than a dict: ``provenance`` decides whether the
    parameter may ever be a design variable (see
    ``mdao_openmdao.assert_design_variables``), and ``clamp`` is the
    physically admissible range that sampling bounds are trimmed to.
    """
    name: str
    section: str
    field: str
    provenance: str
    clamp_low: float | None = None
    clamp_high: float | None = None

    def value(self, cfg: MultiphysicsConfig) -> float:
        return float(getattr(getattr(cfg, self.section), self.field))

    def set_value(self, cfg: MultiphysicsConfig, value: float) -> None:
        setattr(getattr(cfg, self.section), self.field, float(value))


# The same four uncertain inputs the canonical OAT ranking perturbs, in the
# same order, with the same provenance classes.
PARAMETERS: tuple[ParameterSpec, ...] = (
    ParameterSpec("laser.absorbed_fraction", "laser", "absorbed_fraction",
                  "ASSUMED", 0.0, 1.0),
    ParameterSpec("laser.spot_radius_m", "laser", "spot_radius_m",
                  "DESIGN", 0.0, None),
    ParameterSpec("laser.absorption_coeff_1_m", "laser",
                  "absorption_coeff_1_m", "LITERATURE_BOUND/ASSUMED",
                  0.0, None),
    ParameterSpec("fridge.kapitza_coeff_W_m2_K4", "fridge",
                  "kapitza_coeff_W_m2_K4", "ASSUMED", 0.0, None),
)


def salib_available() -> bool:
    try:
        import SALib  # noqa: F401
        return True
    except Exception:                              # pragma: no cover - optional
        return False


def _apply(cfg: MultiphysicsConfig,
           values: npt.ArrayLike) -> MultiphysicsConfig:
    c = copy.deepcopy(cfg)
    for spec, v in zip(PARAMETERS, np.asarray(values, dtype=float)):
        spec.set_value(c, float(v))
    return c


def parameter_bounds(cfg: MultiphysicsConfig | None = None,
                     fraction: float = DEFAULT_BOUNDS_FRACTION
                     ) -> list[dict]:
    """Symmetric +-``fraction`` bounds around the nominal configuration.

    Bounds are clamped to each parameter's physically admissible range;
    clamping is recorded so a narrowed interval can never pass unnoticed.
    """
    cfg = cfg or default_config()
    out: list[dict] = []
    for spec in PARAMETERS:
        nom = spec.value(cfg)
        lo, hi = nom * (1.0 - fraction), nom * (1.0 + fraction)
        clamped = False
        if spec.clamp_low is not None and lo < spec.clamp_low:
            lo, clamped = spec.clamp_low, True
        if spec.clamp_high is not None and hi > spec.clamp_high:
            hi, clamped = spec.clamp_high, True
        out.append({"name": spec.name, "nominal": nom, "low": lo,
                    "high": hi, "clamped": clamped,
                    "provenance": spec.provenance})
    return out


def salib_problem(cfg: MultiphysicsConfig | None = None,
                  fraction: float = DEFAULT_BOUNDS_FRACTION) -> dict:
    """SALib problem definition for the four uncertain thermal inputs."""
    bounds = parameter_bounds(cfg, fraction)
    return {"num_vars": len(bounds),
            "names": [b["name"] for b in bounds],
            "bounds": [[b["low"], b["high"]] for b in bounds]}


def screening_response_K(cfg: MultiphysicsConfig) -> float:
    """Mode-B NV-probe temperature rise on the reduced screening mesh."""
    from ..thermal_3d_transient import solve_thermal_3d
    r = solve_thermal_3d(cfg, SCREENING_MESH, n_eval=SCREENING_N_EVAL)
    return float(r.probe_timeseries_K()[-1]) - float(cfg.fridge.T_fridge_K)


def ci_response_K(cfg: MultiphysicsConfig) -> float:
    """CI-mesh response — the exact function the canonical OAT ranking uses."""
    return float(canonical_probe_rise_K(cfg))


def sobol_indices(model_fn: Callable[[np.ndarray], float] | None = None,
                  cfg: MultiphysicsConfig | None = None,
                  n_base: int = 16, fraction: float = DEFAULT_BOUNDS_FRACTION,
                  seed: int = DEFAULT_SEED,
                  problem: dict | None = None) -> dict:
    """Sobol first-order and total-effect indices, or a fail-closed report.

    ``model_fn`` maps a parameter vector to a scalar response; the default
    evaluates the 3D solver on the screening mesh. Tests inject a cheap
    analytic function to exercise the wiring without paying for solves.
    ``calc_second_order`` is off: the cost is ``n_base * (D + 2)`` solves and
    total-effect indices already carry the interaction information the OAT
    ranking is blind to.
    """
    if not salib_available():                      # pragma: no cover - optional
        return unavailable_report("sobol")
    from SALib.analyze import sobol as sobol_analyze
    from SALib.sample import sobol as sobol_sample

    cfg = cfg or default_config()
    prob = problem or salib_problem(cfg, fraction)
    if model_fn is None:
        def model_fn(x: np.ndarray) -> float:
            return screening_response_K(_apply(cfg, x))

    X = sobol_sample.sample(prob, int(n_base), calc_second_order=False,
                            seed=int(seed))
    Y = np.asarray([float(model_fn(row)) for row in X], dtype=float)
    if not np.all(np.isfinite(Y)):
        raise ValueError("non-finite model response in the Sobol sample; "
                         "refusing to report indices")
    Si = sobol_analyze.analyze(prob, Y, calc_second_order=False,
                               print_to_console=False, seed=int(seed))

    rows = []
    for k, name in enumerate(prob["names"]):
        rows.append({"parameter": name,
                     "S1": float(Si["S1"][k]),
                     "S1_conf": float(Si["S1_conf"][k]),
                     "ST": float(Si["ST"][k]),
                     "ST_conf": float(Si["ST_conf"][k])})
    rows.sort(key=lambda r: (-abs(r["ST"]), r["parameter"]))
    for rank, r in enumerate(rows, 1):
        r["rank_by_ST"] = rank
    return {"availability": "AVAILABLE", "method": "sobol",
            "calc_second_order": False, "n_base": int(n_base),
            "n_model_evaluations": int(X.shape[0]), "seed": int(seed),
            "bounds_fraction": float(fraction),
            "response": "Mode-B NV-probe temperature rise [K]",
            "rows": rows, "label": LABEL,
            "automatic_gate_effect": AUTOMATIC_GATE_EFFECT}


def morris_indices(model_fn: Callable[[np.ndarray], float] | None = None,
                   cfg: MultiphysicsConfig | None = None,
                   n_trajectories: int = 8,
                   fraction: float = DEFAULT_BOUNDS_FRACTION,
                   seed: int = DEFAULT_SEED,
                   problem: dict | None = None) -> dict:
    """Morris elementary-effects screening — the cheap ``(D + 1) * r`` option."""
    if not salib_available():                      # pragma: no cover - optional
        return unavailable_report("morris")
    from SALib.analyze import morris as morris_analyze
    from SALib.sample import morris as morris_sample

    cfg = cfg or default_config()
    prob = problem or salib_problem(cfg, fraction)
    if model_fn is None:
        def model_fn(x: np.ndarray) -> float:
            return screening_response_K(_apply(cfg, x))

    X = morris_sample.sample(prob, int(n_trajectories), num_levels=4,
                             seed=int(seed))
    Y = np.asarray([float(model_fn(row)) for row in X], dtype=float)
    Si = morris_analyze.analyze(prob, X, Y, num_levels=4,
                                print_to_console=False, seed=int(seed))
    rows = [{"parameter": name,
             "mu_star": float(Si["mu_star"][k]),
             "mu": float(Si["mu"][k]),
             "sigma": float(Si["sigma"][k])}
            for k, name in enumerate(Si["names"])]
    rows.sort(key=lambda r: (-abs(r["mu_star"]), r["parameter"]))
    for rank, r in enumerate(rows, 1):
        r["rank_by_mu_star"] = rank
    return {"availability": "AVAILABLE", "method": "morris",
            "n_trajectories": int(n_trajectories),
            "n_model_evaluations": int(X.shape[0]), "seed": int(seed),
            "bounds_fraction": float(fraction),
            "response": "Mode-B NV-probe temperature rise [K]",
            "rows": rows, "label": LABEL,
            "automatic_gate_effect": AUTOMATIC_GATE_EFFECT}


def unavailable_report(method: str) -> dict:
    """Fail-closed report: no indices, and the in-repo authority named."""
    return {"availability": "UNAVAILABLE", "method": method,
            "optional_dependency": "SALib (install with the 'uq' extra)",
            "authority_in_force": "qta_multiphysics.sensitivity_3d "
                                  "(deterministic OAT ranking, CI mesh)",
            "rows": [], "label": LABEL,
            "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
            "note": "no global sensitivity is reported without SALib; the "
                    "canonical local ranking is unaffected"}


def canonical_ranking() -> list[str]:
    """Parameter names in canonical OAT rank order (rank 1 first)."""
    return [r["parameter"] for r in canonical_oat_rows()]


def ranking_agreement(global_rows: list[dict],
                      oat_ranking: list[str] | None = None
                      ) -> dict:
    """Compare a global (SALib) ranking with the canonical OAT ranking.

    Reports exact agreement, the top-1 agreement that actually matters for
    experiment prioritisation, and Kendall's tau-b. Disagreement is a
    reported finding: the canonical ranking is not modified either way.
    """
    key = "rank_by_ST" if global_rows and "rank_by_ST" in global_rows[0] \
        else "rank_by_mu_star"
    g_order = [r["parameter"] for r in sorted(global_rows,
                                              key=lambda r: r[key])]
    o_order = list(oat_ranking if oat_ranking is not None
                   else canonical_ranking())
    if sorted(g_order) != sorted(o_order):
        return {"comparable": False,
                "reason": "parameter sets differ",
                "global_only": sorted(set(g_order) - set(o_order)),
                "canonical_only": sorted(set(o_order) - set(g_order))}

    pos_g = {p: i for i, p in enumerate(g_order)}
    pos_o = {p: i for i, p in enumerate(o_order)}
    conc = disc = 0
    for i, a in enumerate(o_order):
        for b in o_order[i + 1:]:
            s = ((pos_g[a] - pos_g[b]) * (pos_o[a] - pos_o[b]))
            conc += s > 0
            disc += s < 0
    total = conc + disc
    return {"comparable": True,
            "global_order": g_order, "canonical_oat_order": o_order,
            "identical_order": g_order == o_order,
            "top1_agrees": g_order[0] == o_order[0],
            "kendall_tau_b": (conc - disc) / total if total else 1.0,
            "resolution_policy": "canonical OAT ranking stands; a "
                                 "disagreement is reported for human review "
                                 "and never applied automatically",
            "automatic_gate_effect": AUTOMATIC_GATE_EFFECT}


def run_cross_check(out_dir: StrPath, method: str = "sobol",
                    n_base: int = 16,
                    n_trajectories: int = 8,
                    fraction: float = DEFAULT_BOUNDS_FRACTION,
                    seed: int = DEFAULT_SEED,
                    model_fn: Callable[[np.ndarray], float] | None = None
                    ) -> dict:
    """Run the cross-check and write its report into a guarded workspace."""
    out = guard_output_dir(out_dir)
    cfg = default_config()
    if method == "sobol":
        res = sobol_indices(model_fn=model_fn, cfg=cfg, n_base=n_base,
                            fraction=fraction, seed=seed)
    elif method == "morris":
        res = morris_indices(model_fn=model_fn, cfg=cfg,
                             n_trajectories=n_trajectories,
                             fraction=fraction, seed=seed)
    else:
        raise ValueError(f"unknown method '{method}' (sobol | morris)")

    report = {
        "label": LABEL,
        "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
        "producer": "qta_multiphysics.stack.sensitivity_salib",
        "role": "CROSS_CHECK_ONLY",
        "authority": "qta_multiphysics.sensitivity_3d (OAT, CI mesh) — "
                     "canonical; this report never overrides it",
        "screening_mesh": {"nx": SCREENING_MESH.nx, "ny": SCREENING_MESH.ny,
                           "nz": SCREENING_MESH.nz,
                           "n_eval": SCREENING_N_EVAL,
                           "note": "coarser than the CI mesh (10x10x12); "
                                   "adequate for ranking, never for a "
                                   "reported value"},
        "canonical_mesh": {"nx": CI_MESH.nx, "ny": CI_MESH.ny,
                           "nz": CI_MESH.nz},
        "parameter_bounds": parameter_bounds(cfg, fraction),
        "analysis": res,
        "note": "model sensitivity of THIS forecast model under THESE "
                "assumptions; never experimental importance, never gate "
                "evidence",
    }
    if res.get("availability") == "AVAILABLE" and model_fn is None:
        report["agreement_with_canonical"] = ranking_agreement(res["rows"])
    write_json_deterministic(out / f"salib_{method}_cross_check.json", report)
    return report
