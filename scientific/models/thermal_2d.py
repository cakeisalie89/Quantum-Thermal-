"""Thermal 2D axisymmetric conduction as a ScientificModel (Phase 4, family 1).

An ADAPTER, as thermal 1D is: ``qta_multiphysics.thermal_2d_axisymmetric.
solve_thermal_2d`` is called as it is, and this module turns its result into
a ResultBundle -- typed inputs, outputs with units, invariants computed from
this run's own arrays, the fields written to an artefact by digest.

THE LATERAL BOUNDARY IS A DECLARED INPUT

The solver's production boundary at r = R is a cold radial contact to bulk
at the fridge temperature; ``lateral_adiabatic`` replaces it with zero normal
flux (interior radial conduction retained). They are different
boundary-value problems, and the difference matters to what can be checked:
the independent check (``scientific.checks.reduction_3d``) solves the same
problem in a 3D Cartesian box whose lateral faces are adiabatic, so it can
only speak to an ``adiabatic`` run. A ``cold_contact`` run is a legitimate
result with NO independent check in this repository, and its check reports
NOT_RUN -- which the authority layer does not accept as support. That is the
honest state of the cold-contact boundary, not a gap in the adapter.

``disable_radial`` is NOT exposed: it turns the 2D domain into a stack of 1D
columns, a reduction fixture that the thermal 1D check uses and that is not
a 2D result.

INVARIANTS, EACH ABLE TO FAIL

* ``solver_converged`` -- the integrator reported success.
* ``finite_field`` -- every entry of the final and per-cell-peak fields is
  finite; counted.
* ``energy_balance`` -- the solver's own finite-volume energy accounting
  (source minus Kapitza and radial-boundary losses minus internal-energy
  change, relative), |r| <= 0.10: the criterion
  ``qta_multiphysics.verification.source_energy_integral_2d`` holds the 2D
  source quadrature to, borrowed and stated as borrowed.
* ``minimum_principle`` -- non-negative sources, an initial field at the
  fridge temperature, and sinks that pull toward it admit no cell below the
  fridge temperature beyond 100 x the integrator's absolute tolerance (the
  tolerance the solver actually used: ``max(atol, 1e-9)``).

MODEL-ONLY / FORECAST-ONLY. Every default is a forecast or assumed value,
not measured in this system; k(T) and Cp(T) are reduced ASSUMED models. This
is laser thermal loading, not a deposition rate.
"""
from __future__ import annotations

import dataclasses
import json
import math

from ..identity import digest, digest_bytes
from ..model import (
    Applicability, CheckSpec, InvariantSpec, ModelBase, Parameter,
    ParameterSchema, run_identity_for,
)
from ..quantity import Quantity, ResolutionClass as RC, UncertaintyClass as UC
from ..result import (
    ArtifactRef, InvariantResult, Output, OutputStatus, ResultBundle,
)
from ..run_identity import environment_digest, environment_record

MODEL_ID = "thermal.conduction_2d_axisymmetric"
MODEL_VERSION = "1.0.0"
FIELD_ARTIFACT = "temperature_field"
FIELD_MAGIC = b"QTSF2\n"
LATERAL_BOUNDARIES = ("cold_contact", "adiabatic")

ENERGY_BALANCE_MAX = 0.10
UNDERSHOOT_ATOL_FACTOR = 100.0
OUTPUT_NAMES = ("nv_layer_peak_T", "nv_layer_mean_T", "max_T",
                "energy_relative_residual")


def configure(params: dict):
    """The solver configuration these parameters mean -- the same mapping as
    ``thermal_1d.configure``, written out rather than imported so the two
    models' implementation identities stay independent (a test holds the two
    to the same configuration). Shared, and stated as shared, with the
    independent check."""
    from qta_multiphysics.config import default_config
    cfg = default_config()
    cfg = dataclasses.replace(cfg, fridge=dataclasses.replace(
        cfg.fridge, T_fridge_K=params["T_fridge_K"],
        T_background_K=params["T_fridge_K"],
        kapitza_coeff_W_m2_K4=params["kapitza_coeff_W_m2_K4"]))
    return cfg.validate()


def window_s(cfg, source_mode: str) -> float:
    """The simulated window: the solver's own default for the mode, stated
    so the check can solve the same window without reading it back from the
    producer."""
    return float(cfg.solver.pulse_window_s if source_mode == "pulse"
                 else cfg.solver.recovery_window_s)


def effective_tolerances(cfg) -> tuple:
    """``(rtol, atol)`` the 2D solver actually integrates with."""
    return max(cfg.solver.rtol, 1e-5), max(cfg.solver.atol, 1e-9)


def _field_bytes(r, z, t, T_final, T_peak) -> bytes:
    """Magic, one JSON header line, then r, z, t, T_final and T_peak (C
    order, shape (n_r, n_z)) as little-endian float64."""
    import numpy as np
    arrays = (r, z, t, T_final, T_peak)
    header = {"layout": ["r_centers_m", "z_centers_m", "t_s", "T_final_K",
                         "T_peak_K"],
              "shapes": [list(np.shape(a)) for a in arrays],
              "dtype": "<f8", "order": "C"}
    out = [FIELD_MAGIC, json.dumps(header, sort_keys=True).encode() + b"\n"]
    for a in arrays:
        out.append(np.ascontiguousarray(a, dtype="<f8").tobytes())
    return b"".join(out)


class Thermal2DModel(ModelBase):
    model_id = MODEL_ID
    model_version = MODEL_VERSION
    implementation_modules = ("scientific.models.thermal_2d",
                              "qta_multiphysics.thermal_2d_axisymmetric")
    parameter_schema = ParameterSchema((
        Parameter("source_mode", "str", choices=("averaged", "pulse"),
                  default="averaged",
                  description="continuous absorbed power, or one fs pulse"),
        Parameter("lateral_boundary", "str", choices=LATERAL_BOUNDARIES,
                  default="cold_contact",
                  description="r = R: cold radial contact to bulk at the "
                              "fridge temperature (production), or zero "
                              "normal flux"),
        Parameter("n_r", "int", unit="1", minimum=8, maximum=400, default=48,
                  description="finite-volume cells in radius"),
        Parameter("n_z", "int", unit="1", minimum=8, maximum=400, default=64,
                  description="finite-volume cells in depth"),
        Parameter("n_eval", "int", unit="1", minimum=2, maximum=2000,
                  default=40, description="output times"),
        Parameter("T_fridge_K", "float", unit="K", minimum=1e-3,
                  maximum=300.0, default=0.010,
                  description="cold-contact temperature (ASSUMED 10 mK)"),
        Parameter("kapitza_coeff_W_m2_K4", "float", unit="W m^-2 K^-4",
                  minimum=1e-6, maximum=1e6, default=50.0,
                  description="Kapitza-radiative sink coefficient (ASSUMED)"),
    ))
    invariants = (
        InvariantSpec("solver_converged", "the integrator reported success",
                      "status == ok"),
        InvariantSpec("finite_field",
                      "every final and peak temperature is finite",
                      "non-finite count == 0"),
        InvariantSpec("energy_balance", "finite-volume energy accounting",
                      f"|relative residual| <= {ENERGY_BALANCE_MAX}"),
        InvariantSpec("minimum_principle",
                      "no cell below the fridge temperature",
                      "undershoot <= 100 x atol"),
    )
    independent_checks = (
        CheckSpec("thermal_2d.reduction_3d_adiabatic_lateral",
                  "the 3D Cartesian transient solver, Gaussian beam, "
                  "adiabatic lateral faces, solves the same problem with a "
                  "different discretization and geometry; its beam-axis "
                  "NV-layer peak must match (adiabatic runs only)",
                  "DIFFERENT_DISCRETIZATION",
                  shared_components=("material models k(T), Cp(T)",
                                     "laser source parameters",
                                     "parameter-to-configuration mapping"),
                  limitations=("agreement of two discretizations of one "
                               "model, not of the model with the world",
                               "a cold_contact run is not checked")),
    )
    applicability = Applicability(
        "axisymmetric r-z conduction in diamond under a Gaussian x "
        "Beer-Lambert laser source, insulated front, Kapitza-radiative back "
        "contact, and a declared lateral boundary",
        bounds={"T_fridge_K": [1e-3, 300.0]},
        excludes=("deposition or growth rates", "off-axis beams",
                  "phase change", "any measured in-system temperature"))

    def run(self, inputs: dict) -> ResultBundle:
        return self.run_with_artifacts(inputs)[0]

    def run_with_artifacts(self, inputs: dict) -> tuple:
        import numpy as np
        from qta_multiphysics.thermal_2d_axisymmetric import solve_thermal_2d

        cfg = configure(inputs)
        t_end = window_s(cfg, inputs["source_mode"])
        r = solve_thermal_2d(
            cfg, source_mode=inputs["source_mode"], t_end=t_end,
            n_r=inputs["n_r"], n_z=inputs["n_z"], n_eval=inputs["n_eval"],
            lateral_adiabatic=inputs["lateral_boundary"] == "adiabatic")
        T_final = np.asarray(r.T_final.values, dtype=float)
        T_peak = np.asarray(r.T_peak.values, dtype=float)
        converged = r.solver_status == "ok"
        both = np.concatenate([T_final.ravel(), T_peak.ravel()])
        nonfinite = int(both.size - np.count_nonzero(np.isfinite(both)))
        Tf = cfg.fridge.T_fridge_K
        rtol, atol = effective_tolerances(cfg)

        def temp(v):
            return Quantity(float(v), "K", resolution=max(atol,
                                                           rtol * abs(v)),
                            resolution_class=RC.SOLVER_TOLERANCE,
                            resolution_basis="solve_ivp rtol/atol as the 2D "
                                             "solver applies them",
                            reporting_digits=7,
                            uncertainty_class=UC.NOT_ASSESSED)

        rel = r.energy_residual()
        undershoot = (float(Tf - np.nanmin(both)) if nonfinite < both.size
                      else float("inf"))

        invariants = (
            InvariantResult("solver_converged", converged,
                            Quantity(1.0 if converged else 0.0, "1",
                                     exact=True),
                            "status == ok", Quantity(1.0, "1", exact=True),
                            f"solve_ivp success flag; message: "
                            f"{str(r.message)[:120]}"),
            InvariantResult("finite_field", nonfinite == 0,
                            Quantity(float(nonfinite), "1", exact=True),
                            "non-finite count == 0",
                            Quantity(0.0, "1", exact=True),
                            "counted over the final and per-cell peak "
                            "fields"),
            InvariantResult(
                "energy_balance",
                math.isfinite(rel) and abs(rel) <= ENERGY_BALANCE_MAX,
                Quantity(abs(rel) if math.isfinite(rel) else 1.0, "1"),
                f"|relative residual| <= {ENERGY_BALANCE_MAX}",
                Quantity(ENERGY_BALANCE_MAX, "1"),
                "the 10% source-energy criterion of qta_multiphysics."
                "verification.source_energy_integral_2d"),
            InvariantResult(
                "minimum_principle",
                math.isfinite(undershoot)
                and undershoot <= UNDERSHOOT_ATOL_FACTOR * atol,
                Quantity(max(undershoot, 0.0) if math.isfinite(undershoot)
                         else 1e300, "K"),
                "T_fridge - min(T) <= 100 x atol",
                Quantity(UNDERSHOOT_ATOL_FACTOR * atol, "K"),
                "non-negative sources, T0 = T_fridge and sinks toward "
                "T_fridge admit no undershoot beyond integration error"),
        )

        if converged and nonfinite == 0:
            outputs = (
                Output("nv_layer_peak_T", OutputStatus.OK,
                       temp(r.nv_layer_max_K())),
                Output("nv_layer_mean_T", OutputStatus.OK,
                       temp(r.nv_layer_mean_K())),
                Output("max_T", OutputStatus.OK, temp(r.max_T_K())),
                Output("energy_relative_residual", OutputStatus.OK,
                       Quantity(float(rel), "1")),
            )
        else:
            why = ("the integrator did not converge" if not converged
                   else f"{nonfinite} non-finite temperatures")
            outputs = tuple(Output(n, OutputStatus.FAILED, reason=why)
                            for n in OUTPUT_NAMES)

        field = _field_bytes(r.grid.r_centers, r.grid.z_centers, r.t,
                             T_final, T_peak)
        art = ArtifactRef(FIELD_ARTIFACT, digest_bytes(field),
                          "application/octet-stream", len(field),
                          "magic QTSF2, a JSON header line, then r, z, t, "
                          "T_final and T_peak as little-endian float64")
        env = environment_record()
        bundle = ResultBundle(
            model_id=self.model_id, model_version=self.model_version,
            implementation_digest=self.implementation_digest(),
            parameter_digest=digest(inputs),
            environment_digest=environment_digest(env),
            parameters=dict(inputs),
            outputs=outputs, invariants=invariants,
            convergence={"solver_status": r.solver_status,
                         "message": str(r.message)[:200],
                         "n_time_points": int(len(r.t)),
                         "n_r": int(r.grid.nr), "n_z": int(r.grid.nz),
                         "t_end_s": t_end,
                         "method": cfg.solver.method,
                         "rtol": rtol, "atol": atol},
            solver_config=dataclasses.asdict(cfg.solver),
            warnings=(("MODEL-ONLY / FORECAST-ONLY: defaults are forecast "
                       "or assumed values, not measured in this system"),),
            artifacts=(art,),
            provenance={"solver": "qta_multiphysics.thermal_2d_axisymmetric."
                                  "solve_thermal_2d",
                        "environment": env,
                        "run_identity":
                            run_identity_for(self, inputs).to_record(),
                        "configuration_digest":
                            digest(dataclasses.asdict(cfg))})
        return bundle, {FIELD_ARTIFACT: field}
