"""OpenMDAO wrapper over the thermal ROM for design-space exploration.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

This module exposes the 3D thermal reduced-order model to OpenMDAO as one
``ExplicitComponent`` so that design-space exploration (DOE) and gradient-free
search can run against the *same* solver the rest of the project uses. It adds
no discipline, no surrogate, and no coupling that is not already in the code.

Two governance rules are enforced mechanically rather than by convention:

1. **Only DESIGN-provenance parameters may be design variables.** A knob the
   project can actually turn (``laser.spot_radius_m``) is a design freedom; an
   ASSUMED or LITERATURE_BOUND quantity (``absorbed_fraction``,
   ``absorption_coeff_1_m``, ``kapitza_coeff_W_m2_K4``) is *uncertainty*.
   Optimising over uncertainty would silently convert an assumption into a
   design decision, so :func:`assert_design_variables` rejects it.
2. **No result here is an operating point.** The canonical operating point
   lives in ``best_forecast_operating_point.json`` and is produced by
   ``qta_full_sim.py``. Every record written by this module is labelled
   ``NOT_A_RECOMMENDATION`` and is written into the Stage-10 workspace only;
   ``automatic_gate_effect = NONE``.

OpenMDAO is optional: without it, :func:`component_spec` still describes the
component exactly (names, units, bounds, provenance) so the contract is
testable, and the runners report ``availability = UNAVAILABLE`` instead of
producing numbers by some other route.
"""
from __future__ import annotations

import copy
from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np

from ..config import MultiphysicsConfig, default_config
from . import AUTOMATIC_GATE_EFFECT, LABEL
from .sensitivity_salib import (PARAMETERS, SCREENING_MESH, SCREENING_N_EVAL,
                                parameter_bounds)
from .workspace import (StrPath, guard_output_dir,
                        write_json_deterministic)

UNITS: dict[str, str | None] = {
    "laser.absorbed_fraction": None,               # dimensionless fraction
    "laser.spot_radius_m": "m",
    "laser.absorption_coeff_1_m": "1/m",
    "fridge.kapitza_coeff_W_m2_K4": None}          # W/m^2/K^4 (no OM unit)
OM_VAR: dict[str, str] = {p.name: p.name.replace(".", "_")
                          for p in PARAMETERS}
DESIGN_PROVENANCE = "DESIGN"


def openmdao_available() -> bool:
    try:
        import openmdao.api  # noqa: F401
        return True
    except Exception:                              # pragma: no cover - optional
        return False


def design_parameter_names() -> list[str]:
    """Parameters the project may legitimately optimise over."""
    return [p.name for p in PARAMETERS
            if p.provenance == DESIGN_PROVENANCE]


def uncertain_parameter_names() -> list[str]:
    """Parameters that are uncertainty, not design freedom."""
    return [p.name for p in PARAMETERS
            if p.provenance != DESIGN_PROVENANCE]


def assert_design_variables(names: Iterable[str]) -> None:
    """Fail closed when a non-DESIGN parameter is offered as a design variable."""
    allowed = set(design_parameter_names())
    bad = [n for n in names if n not in allowed]
    if bad:
        raise ValueError(
            "refusing to optimise over non-DESIGN parameters "
            f"{sorted(bad)}: these are ASSUMED / LITERATURE_BOUND "
            "uncertainties, not design freedoms. Explore them with a DOE or "
            "the SALib cross-check instead.")


def component_spec(cfg: MultiphysicsConfig | None = None,
                   fraction: float = 0.10) -> dict:
    """Dependency-free description of the OpenMDAO component's interface."""
    cfg = cfg or default_config()
    bounds = {b["name"]: b for b in parameter_bounds(cfg, fraction)}
    return {
        "label": LABEL,
        "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
        "component": "ThermalROMComp",
        "backend": "qta_multiphysics.thermal_3d_transient.solve_thermal_3d",
        "mesh": {"nx": SCREENING_MESH.nx, "ny": SCREENING_MESH.ny,
                 "nz": SCREENING_MESH.nz, "n_eval": SCREENING_N_EVAL},
        "inputs": [{"om_name": OM_VAR[p.name], "parameter": p.name,
                    "units": UNITS[p.name],
                    "provenance": p.provenance,
                    "role": ("design_variable"
                             if p.provenance == DESIGN_PROVENANCE
                             else "uncertain_input"),
                    "default": bounds[p.name]["nominal"],
                    "lower": bounds[p.name]["low"],
                    "upper": bounds[p.name]["high"]}
                   for p in PARAMETERS],
        "outputs": [
            {"om_name": "probe_rise_K", "units": "K",
             "meaning": "Mode-B NV-probe temperature rise above the fridge "
                        "base temperature (forecast)"},
            {"om_name": "peak_T_K", "units": "K",
             "meaning": "peak temperature anywhere in the resolved domain "
                        "over the transient (forecast)"}],
        "partials": "finite difference (the solver exposes no adjoint)",
        "design_variables_allowed": design_parameter_names(),
        "uncertain_inputs": uncertain_parameter_names(),
    }


def evaluate(values: dict, cfg: MultiphysicsConfig | None = None
             ) -> dict:
    """Run the ROM once for a parameter mapping; returns the component outputs.

    Shared by the OpenMDAO component and by the tests, so what OpenMDAO sees
    and what the tests check cannot diverge.
    """
    from ..thermal_3d_transient import solve_thermal_3d
    c = copy.deepcopy(cfg or default_config())
    for p in PARAMETERS:
        if p.name in values:
            p.set_value(c, float(values[p.name]))
    r = solve_thermal_3d(c, SCREENING_MESH, n_eval=SCREENING_N_EVAL)
    return {"probe_rise_K": float(r.probe_timeseries_K()[-1])
                            - float(c.fridge.T_fridge_K),
            "peak_T_K": float(r.peak_temperature_K())}


def make_component(cfg: MultiphysicsConfig | None = None,
                   fraction: float = 0.10) -> Any:
    """Build the ``ThermalROMComp`` class bound to a configuration.

    Defined inside the factory so that importing this module never requires
    OpenMDAO.
    """
    if not openmdao_available():                   # pragma: no cover - optional
        raise RuntimeError("OpenMDAO is not installed (install the 'uq' "
                           "extra); no component can be built")
    import openmdao.api as om

    spec = component_spec(cfg, fraction)
    base_cfg = cfg or default_config()

    class ThermalROMComp(om.ExplicitComponent):
        """Thermal ROM as a single explicit discipline (forecast-only)."""

        def setup(self) -> None:
            for inp in spec["inputs"]:
                self.add_input(inp["om_name"], val=inp["default"],
                               units=inp["units"])
            self.add_output("probe_rise_K", val=0.0, units="K")
            self.add_output("peak_T_K", val=0.0, units="K")

        def setup_partials(self) -> None:
            self.declare_partials("*", "*", method="fd", step_calc="rel")

        def compute(self, inputs: Any, outputs: Any) -> None:
            values = {p.name: float(inputs[OM_VAR[p.name]][0])
                      for p in PARAMETERS}
            res = evaluate(values, base_cfg)
            outputs["probe_rise_K"] = res["probe_rise_K"]
            outputs["peak_T_K"] = res["peak_T_K"]

    return ThermalROMComp


def latin_hypercube_samples(inputs: Sequence[dict], n_samples: int,
                            seed: int) -> list[list[tuple[str, float]]]:
    """Deterministic Latin-hypercube sample list in OpenMDAO ``ListGenerator``
    form: ``[[(om_name, value), ...], ...]``.

    Built from ``scipy.stats.qmc`` (already a core dependency) rather than an
    extra DOE package, so the sample set is fixed by ``(bounds, n_samples,
    seed)`` and the study is exactly repeatable.
    """
    from scipy.stats import qmc
    lo = np.asarray([i["lower"] for i in inputs], dtype=float)
    hi = np.asarray([i["upper"] for i in inputs], dtype=float)
    unit = qmc.LatinHypercube(d=len(inputs), seed=int(seed)).random(
        int(n_samples))
    scaled = qmc.scale(unit, lo, hi)
    return [[(inputs[k]["om_name"], float(row[k]))
             for k in range(len(inputs))] for row in scaled]


def run_doe(out_dir: StrPath, n_samples: int = 8, fraction: float = 0.10,
            seed: int = 20260819) -> dict:
    """Latin-hypercube DOE over the *uncertain* inputs; reports the envelope.

    This deliberately explores uncertainty rather than optimising it: the
    output is the forecast range of the probe rise implied by the assumed
    parameter box, which is what a reviewer needs in order to disagree with
    the assumptions.
    """
    out = guard_output_dir(out_dir)
    spec = component_spec(fraction=fraction)
    if not openmdao_available():                   # pragma: no cover - optional
        rep = _unavailable("doe", spec)
        write_json_deterministic(out / "openmdao_doe.json", rep)
        return rep
    import openmdao.api as om

    cfg = default_config()
    uncertain = [i for i in spec["inputs"] if i["role"] == "uncertain_input"]
    samples = latin_hypercube_samples(uncertain, int(n_samples), int(seed))
    comp = make_component(cfg, fraction)
    prob = om.Problem(reports=False)
    prob.model.add_subsystem("rom", comp(), promotes=["*"])
    for inp in uncertain:
        prob.model.add_design_var(inp["om_name"], lower=inp["lower"],
                                  upper=inp["upper"])
    prob.model.add_objective("probe_rise_K")
    # ListGenerator over a SciPy Latin hypercube: the sample set is ours and
    # is reproducible from (bounds, n_samples, seed) without pulling in an
    # extra DOE package, so the study repeats exactly on any machine.
    prob.driver = om.DOEDriver(om.ListGenerator(samples))
    # Record every case so the study is read back from the driver's own log
    # rather than re-evaluated: each evaluation is a full 3D solve, and
    # running the sweep twice would both double the cost and open a gap
    # between what the driver did and what the report says it did.
    prob.driver.recording_options["includes"] = ["*"]
    prob.driver.recording_options["record_inputs"] = True
    sql = out / "openmdao_doe_cases.sql"
    prob.driver.add_recorder(om.SqliteRecorder(str(sql)))
    prob.setup()
    prob.run_driver()
    prob.cleanup()

    cases: list[dict] = []
    for case in om.CaseReader(str(sql)).get_cases("driver"):
        row: dict[str, float] = {}
        for spec_p in PARAMETERS:
            om_name = OM_VAR[spec_p.name]
            for source in (case.outputs, case.inputs):
                if source is not None and om_name in source:
                    row[spec_p.name] = float(
                        np.asarray(source[om_name]).ravel()[0])
                    break
        row["probe_rise_K"] = float(
            np.asarray(case.outputs["probe_rise_K"]).ravel()[0])
        row["peak_T_K"] = float(
            np.asarray(case.outputs["peak_T_K"]).ravel()[0])
        cases.append(row)
    rises = [c["probe_rise_K"] for c in cases]

    rep = {
        "label": LABEL,
        "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
        "producer": "qta_multiphysics.stack.mdao_openmdao",
        "availability": "AVAILABLE",
        "study": "latin_hypercube_doe_over_uncertain_inputs",
        "n_samples": int(n_samples), "seed": int(seed),
        "bounds_fraction": float(fraction),
        "component": spec,
        "cases": cases,
        "case_log": "openmdao_doe_cases.sql (OpenMDAO recorder; SQLite, "
                    "not byte-deterministic — excluded from digest checks)",
        "envelope": {"probe_rise_K_min": min(rises) if rises else None,
                     "probe_rise_K_max": max(rises) if rises else None,
                     "probe_rise_K_mean": float(np.mean(rises))
                     if rises else None},
        "status": "NOT_A_RECOMMENDATION",
        "note": "forecast range implied by the ASSUMED parameter box; it is "
                "not an operating point and does not update "
                "best_forecast_operating_point.json",
    }
    write_json_deterministic(out / "openmdao_doe.json", rep)
    return rep


def run_design_exploration(out_dir: StrPath, maxiter: int = 12,
                           fraction: float = 0.10,
                           design_variables: Iterable[str] | None = None
                           ) -> dict:
    """Gradient-free search over DESIGN-provenance variables only.

    Fails closed if asked to search over an ASSUMED or LITERATURE_BOUND
    quantity. The reported point is a *forecast candidate*, never a
    recommendation or a gate input.
    """
    out = guard_output_dir(out_dir)
    dvs = list(design_variables if design_variables is not None
               else design_parameter_names())
    assert_design_variables(dvs)
    spec = component_spec(fraction=fraction)
    if not openmdao_available():                   # pragma: no cover - optional
        rep = _unavailable("design_exploration", spec)
        write_json_deterministic(out / "openmdao_design.json", rep)
        return rep
    import openmdao.api as om

    by_name = {i["parameter"]: i for i in spec["inputs"]}
    comp = make_component(default_config(), fraction)
    prob = om.Problem(reports=False)
    prob.model.add_subsystem("rom", comp(), promotes=["*"])
    for name in dvs:
        inp = by_name[name]
        prob.model.add_design_var(inp["om_name"], lower=inp["lower"],
                                  upper=inp["upper"])
    prob.model.add_objective("probe_rise_K")
    prob.driver = om.ScipyOptimizeDriver(optimizer="COBYLA",
                                         maxiter=int(maxiter), tol=1e-6)
    prob.setup()
    prob.run_driver()
    point = {name: float(prob.get_val(by_name[name]["om_name"])[0])
             for name in dvs}
    rep = {
        "label": LABEL,
        "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
        "producer": "qta_multiphysics.stack.mdao_openmdao",
        "availability": "AVAILABLE",
        "study": "gradient_free_design_exploration",
        "optimizer": "COBYLA", "maxiter": int(maxiter),
        "design_variables": dvs,
        "refused_variables_policy": "non-DESIGN provenance rejected by "
                                    "assert_design_variables()",
        "candidate_point": point,
        "objective_probe_rise_K": float(prob.get_val("probe_rise_K")[0]),
        "peak_T_K": float(prob.get_val("peak_T_K")[0]),
        "component": spec,
        "status": "NOT_A_RECOMMENDATION",
        "note": "a forecast candidate under ASSUMED inputs on a reduced "
                "screening mesh; it is not an operating point, not a "
                "measured result, and creates no gate evidence",
    }
    write_json_deterministic(out / "openmdao_design.json", rep)
    return rep


def _unavailable(study: str, spec: dict) -> dict:  # pragma: no cover - optional
    return {"label": LABEL,
            "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
            "availability": "UNAVAILABLE", "study": study,
            "optional_dependency": "openmdao (install with the 'uq' extra)",
            "component": spec,
            "note": "no design study is reported without OpenMDAO; the "
                    "canonical operating point is unaffected"}
