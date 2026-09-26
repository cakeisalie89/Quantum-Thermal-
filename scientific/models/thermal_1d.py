"""Thermal 1D conduction as a ScientificModel: the Phase-2 proving case.

An ADAPTER. ``qta_multiphysics.thermal_1d.solve_thermal_1d`` is called as it
is -- no line of the solver changes -- and this module turns its result into
a ResultBundle: typed inputs in, outputs with units out, the model's
invariants computed from this run's own arrays, the temperature field written
to an artefact referenced by digest.

WHAT IS INVARIANT, AND WHY EACH IS A REAL CHECK

* ``solver_converged`` -- the integrator reported success. A truncated
  trajectory is not a result (``numerics.require_converged`` refuses one
  everywhere else in the package; here it is recorded, not raised, so a
  failed run still produces a bundle that SAYS it failed).
* ``finite_field`` -- every temperature is finite; counted, not assumed.
* ``energy_balance`` -- integrated source energy minus boundary loss minus
  internal-energy change, relative to the larger of the two: the solver's
  own finite-volume energy accounting, taken from the solution arrays.
  Criterion |r| <= 0.05, the source-energy discretization criterion
  ``verification.source_energy_integral_1d`` is held to; a tighter bound
  would need the quadrature error of the 120-point time series stated, and
  is not claimed.
* ``minimum_principle`` -- with a non-negative source, an initial field at
  the fridge temperature and a sink that pulls toward it, no cell may fall
  below the fridge temperature. Measured as the largest undershoot, against
  the integrator's absolute tolerance (``SolverConfig.atol``) times 100.

NOT an invariant here: the sign of the Kapitza boundary law. Configuration
validation already refuses a non-positive coefficient, so as a property of a
run it could never fail -- a check that cannot fail is not a check. The QTA
verification suite keeps it as a configuration check (``kapitza_sign_check``).

The 2D-to-1D reduction is NOT here: it is an independent check, with its
own implementation digest, in ``scientific.checks.reduction_2d``.

MODEL-ONLY / FORECAST-ONLY. Every default is a forecast or assumed value,
not measured in this system. Nothing here is hardware-validated.
"""
from __future__ import annotations

import dataclasses
import json
import math

from ..identity import digest, digest_bytes
from ..model import (
    Applicability, CheckSpec, InvariantSpec, ModelBase, Parameter,
    ParameterSchema,
)
from ..quantity import Quantity, ResolutionClass as RC, UncertaintyClass as UC
from ..result import (
    ArtifactRef, InvariantResult, Output, OutputStatus, ResultBundle,
)
from ..run_identity import environment_digest, environment_record

MODEL_ID = "thermal.conduction_1d"
MODEL_VERSION = "1.0.0"
FIELD_ARTIFACT = "temperature_field"
FIELD_MAGIC = b"QTSF1\n"

ENERGY_BALANCE_MAX = 0.05
UNDERSHOOT_ATOL_FACTOR = 100.0


def configure(params: dict):
    """The solver configuration these parameters mean. Shared, and stated as
    shared, with the independent check: both must solve the same problem."""
    from qta_multiphysics.config import default_config
    cfg = default_config()
    cfg = dataclasses.replace(cfg, fridge=dataclasses.replace(
        cfg.fridge, T_fridge_K=params["T_fridge_K"],
        T_background_K=params["T_fridge_K"],
        kapitza_coeff_W_m2_K4=params["kapitza_coeff_W_m2_K4"]))
    return cfg.validate()


def _field_bytes(z, t, T) -> bytes:
    """A self-describing, deterministic binary: magic, one JSON header line,
    then z, t and T (C order) as little-endian float64."""
    import numpy as np
    header = {"layout": ["z_centers_m", "t_s", "T_K"],
              "shapes": [list(z.shape), list(t.shape), list(T.shape)],
              "dtype": "<f8", "order": "C"}
    out = [FIELD_MAGIC, json.dumps(header, sort_keys=True).encode() + b"\n"]
    for a in (z, t, T):
        out.append(np.ascontiguousarray(a, dtype="<f8").tobytes())
    return b"".join(out)


class Thermal1DModel(ModelBase):
    model_id = MODEL_ID
    model_version = MODEL_VERSION
    implementation_modules = ("scientific.models.thermal_1d",
                              "qta_multiphysics.thermal_1d")
    parameter_schema = ParameterSchema((
        Parameter("source_mode", "str", choices=("averaged", "pulse"),
                  default="averaged",
                  description="continuous absorbed power, or one fs pulse"),
        Parameter("n_cells", "int", unit="1", minimum=10, maximum=2000,
                  default=200, description="finite-volume cells in depth"),
        Parameter("n_eval", "int", unit="1", minimum=2, maximum=2000,
                  default=120, description="output times"),
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
        InvariantSpec("finite_field", "every temperature is finite",
                      "non-finite count == 0"),
        InvariantSpec("energy_balance", "finite-volume energy accounting",
                      f"|relative residual| <= {ENERGY_BALANCE_MAX}"),
        InvariantSpec("minimum_principle",
                      "no cell below the fridge temperature",
                      "undershoot <= 100 x atol"),
    )
    independent_checks = (
        CheckSpec("thermal_1d.reduction_2d_radial_disabled",
                  "the 2D axisymmetric solver with radial transport off "
                  "solves the same depth problem with a different "
                  "discretization; its axis column must match",
                  "DIFFERENT_DISCRETIZATION",
                  shared_components=("material models k(T), Cp(T)",
                                     "laser source term",
                                     "parameter-to-configuration mapping"),
                  limitations=("agreement of two discretizations of one "
                               "model, not of the model with the world",)),
    )
    applicability = Applicability(
        "1D depth conduction in diamond under a Beer-Lambert laser source "
        "with an insulated front and a Kapitza-radiative back contact; "
        "lateral spreading neglected",
        bounds={"T_fridge_K": [1e-3, 300.0]},
        excludes=("deposition or growth rates", "radial transport",
                  "phase change", "any measured in-system temperature"))

    def run(self, inputs: dict) -> ResultBundle:
        return self.run_with_artifacts(inputs)[0]

    def run_with_artifacts(self, inputs: dict) -> tuple:
        import numpy as np
        from qta_multiphysics.thermal_1d import solve_thermal_1d

        cfg = configure(inputs)
        r = solve_thermal_1d(cfg, source_mode=inputs["source_mode"],
                             n_cells=inputs["n_cells"],
                             n_eval=inputs["n_eval"])
        T = np.asarray(r.T, dtype=float)
        converged = r.solver_status == "ok"
        nonfinite = int(np.size(T) - np.count_nonzero(np.isfinite(T)))
        Tf = cfg.fridge.T_fridge_K
        atol, rtol = cfg.solver.atol, cfg.solver.rtol
        def temp(v):
            return Quantity(float(v), "K", resolution=max(atol,
                                                           rtol * abs(v)),
                            resolution_class=RC.SOLVER_TOLERANCE,
                            resolution_basis="solve_ivp rtol/atol "
                                             "(SolverConfig)",
                            reporting_digits=7,
                            uncertainty_class=UC.NOT_ASSESSED)

        rel = r.energy_residual()
        undershoot = float(Tf - np.nanmin(T)) if nonfinite < T.size else \
            float("inf")

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
                            "counted over the whole (n, Nt) field"),
            InvariantResult(
                "energy_balance",
                math.isfinite(rel) and abs(rel) <= ENERGY_BALANCE_MAX,
                Quantity(abs(rel) if math.isfinite(rel) else 1.0, "1"),
                f"|relative residual| <= {ENERGY_BALANCE_MAX}",
                Quantity(ENERGY_BALANCE_MAX, "1"),
                "the 5% source-energy discretization criterion of "
                "qta_multiphysics.verification.source_energy_integral_1d"),
            InvariantResult(
                "minimum_principle",
                math.isfinite(undershoot)
                and undershoot <= UNDERSHOOT_ATOL_FACTOR * atol,
                Quantity(max(undershoot, 0.0) if math.isfinite(undershoot)
                         else 1e300, "K"),
                "T_fridge - min(T) <= 100 x atol",
                Quantity(UNDERSHOOT_ATOL_FACTOR * atol, "K"),
                "a non-negative source, T0 = T_fridge and a sink toward "
                "T_fridge admit no undershoot beyond integration error"),
        )

        if converged and nonfinite == 0:
            outputs = (
                Output("nv_layer_peak_T", OutputStatus.OK,
                       temp(r.nv_layer_temperature_K())),
                Output("nv_layer_final_T", OutputStatus.OK,
                       temp(r.nv_layer_temperature_final_K())),
                Output("hotspot_T", OutputStatus.OK,
                       temp(r.hotspot_temperature_K())),
                Output("energy_relative_residual", OutputStatus.OK,
                       Quantity(float(rel), "1")),
            )
        else:
            why = ("the integrator did not converge" if not converged
                   else f"{nonfinite} non-finite temperatures")
            outputs = tuple(Output(n, OutputStatus.FAILED, reason=why)
                            for n in ("nv_layer_peak_T", "nv_layer_final_T",
                                      "hotspot_T",
                                      "energy_relative_residual"))

        field = _field_bytes(r.grid.centers, r.t, T)
        art = ArtifactRef(FIELD_ARTIFACT, digest_bytes(field),
                          "application/octet-stream", len(field),
                          "magic QTSF1, a JSON header line, then z, t, T "
                          "as little-endian float64")
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
                         "n_cells": int(r.grid.n),
                         "method": cfg.solver.method,
                         "rtol": rtol, "atol": atol},
            solver_config=dataclasses.asdict(cfg.solver),
            warnings=(("MODEL-ONLY / FORECAST-ONLY: defaults are forecast "
                       "or assumed values, not measured in this system"),),
            artifacts=(art,),
            provenance={"solver": "qta_multiphysics.thermal_1d."
                                  "solve_thermal_1d",
                        "environment": env,
                        "configuration_digest":
                            digest(dataclasses.asdict(cfg))})
        return bundle, {FIELD_ARTIFACT: field}
