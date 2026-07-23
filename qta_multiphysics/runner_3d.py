"""Output runner for the additive 3D transient layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

Generates the 14 canonical 3D outputs from code (never hand-written), at the
reduced CI-safe resolution used by the tests and the package consistency
check. Outputs are deterministic and byte-identical across runs and profiles.
A heavy/higher-resolution mode exists behind ``heavy=True`` (the
``--heavy-3d`` pipeline flag): it writes to ``<outdir>/heavy_3d`` (gitignored),
is never required by the default run, CI, the package consistency check, or
manifest verification, and is clearly labelled.
"""
from __future__ import annotations

from pathlib import Path

from .config import default_config
from .mesh_3d import Grid3DConfig, StructuredGrid3D
from .thermal_3d_transient import solve_thermal_3d
from .energy_accounting_3d import energy_accounting_rows
from .mode_sequence_3d import run_mode_sequence_3d
from .verification_3d import run_verification_3d
from . import materials_3d, cryo_stack_3d, species_transport_3d
from .surface_coverage_3d import nv_plane_coverage_rows
from .radiation_paths_3d import budget_rows as radiation_budget_rows
from .vibration_paths_3d import budget_rows as vibration_budget_rows
from .microwave_heating_3d import budget_rows as microwave_budget_rows
from .coupling_ledger_3d import coupling_ledger, ledger_summary
from .species_accounting_3d import species_accounting_rows
from .falsification_3d import falsification_report
from .convergence_3d import convergence_report
from .sensitivity_3d import sensitivity_rows
from .nv_eligibility_3d import nv_eligibility_forecast
from .provenance_3d import assumptions_record, provenance_record
from .sources_3d import laser_deposition_summary, optical_collection_load
from .state_machine_3d import device_state, all_device_states
from .exports import write_rows_csv, write_json

LABEL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"

#: reduced CI mesh (default everywhere; identical across profiles)
CI_MESH = Grid3DConfig(nx=10, ny=10, nz=12)
#: heavy/higher-resolution mesh -- OPT-IN ONLY (--heavy-3d), never CI/checker.
#: 16x16x20 is the verified tier on a CPU sandbox (~1 min/solve, energy
#: closure ~1.5e-5); larger meshes are user-configurable via Grid3DConfig but
#: 3D sparse-LU cost scales steeply with cell count.
HEAVY_MESH = Grid3DConfig(nx=16, ny=16, nz=20)


def run_3d_all(outdir, heavy: bool = False, verbose: bool = True) -> dict:
    """Generate the canonical 3D outputs into ``outdir`` (reduced CI mesh).

    With ``heavy=True`` an ADDITIONAL higher-resolution pass writes into
    ``outdir/heavy_3d`` (gitignored; optional; clearly labelled); the canonical
    reduced outputs are always produced and are unaffected by the heavy pass.
    """
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    cfg = default_config()
    g3 = CI_MESH

    def out(name):
        return outdir / name

    # ---- mesh summary ----
    grid = StructuredGrid3D(cfg, g3)
    mesh_summary = dict(grid.summary())
    mesh_summary.update({
        "resolution_tier": "REDUCED_CI",
        "heavy_mesh_available": {"nx": HEAVY_MESH.nx, "ny": HEAVY_MESH.ny,
                                 "nz": HEAVY_MESH.nz,
                                 "note": "opt-in via --heavy-3d; never required "
                                         "by CI or the package consistency check"},
        "label": LABEL,
    })
    write_json(out("mesh_3d_summary.json"), mesh_summary)

    # ---- numerical verification (includes the two reduction checks) ----
    vrep = run_verification_3d(cfg, g3)
    write_json(out("thermal_3d_verification_report.json"), vrep)
    red = [c for c in vrep["checks"] if c["check"].startswith("reduction_")]
    write_json(out("thermal_3d_reduction_check.json"), {
        "checks": red,
        "meaning": "like-for-like reduced-geometry consistency of the 3D model "
                   "against the canonical 1D and 2D backends; numerical only",
        "label": LABEL,
    })

    # ---- Mode B -> C -> D sequence on the 3D core ----
    seq = run_mode_sequence_3d(cfg, g3, n_eval_b=13, n_eval_c=25)
    write_rows_csv(out("mode_recovery_3d_timeline.csv"), seq.timeline_rows())
    write_rows_csv(out("thermal_3d_probe_timeseries.csv"),
                   seq.tB.probe_timeseries_rows())
    write_rows_csv(out("thermal_3d_hotspots.csv"), seq.tB.hotspot_rows())
    write_rows_csv(out("thermal_3d_energy_accounting.csv"),
                   energy_accounting_rows(seq.tB))

    # ---- forecast-only multiphysics hooks ----
    crows, _ = cryo_stack_3d.budget_rows()
    write_rows_csv(out("cryo_stack_3d_budget.csv"), crows)
    write_json(out("species_transport_3d_summary.json"),
               species_transport_3d.summary())
    write_rows_csv(out("surface_coverage_3d_summary.csv"),
                   nv_plane_coverage_rows(cfg, seq.tC))
    rrows, _ = radiation_budget_rows()
    write_rows_csv(out("radiation_paths_3d_budget.csv"), rrows)
    vrows, _ = vibration_budget_rows()
    write_rows_csv(out("vibration_paths_3d_budget.csv"), vrows)
    mrows, _ = microwave_budget_rows()
    write_rows_csv(out("microwave_heating_3d_budget.csv"), mrows)

    # ---- coupled layer: ledger, accounting, falsification, forecasts ----
    led = coupling_ledger()
    write_json(out("coupling_ledger_3d.json"), {
        "couplings": led,
        "summary": ledger_summary(led),
        "mode_device_states": [s.as_row() for s in all_device_states()],
        "channel_activations_this_run": list(seq.channel_meta),
        "label": LABEL})
    sp_rows = species_accounting_rows(cfg)
    write_rows_csv(out("species_accounting_3d.csv"), sp_rows)
    from .campaign_state_3d import (build_campaign, attach_energy_ledger,
                                    mass_bookkeeping_rows, SCHEMA_VERSION,
                                    DEFAULT_CYCLES)
    from .vibration_transfer import vibration_transfer as _vt0
    _, _vm0 = _vt0()
    campaign = build_campaign(cfg, seq, sp_rows, _vm0, DEFAULT_CYCLES)
    attach_energy_ledger(campaign, seq)
    fals = falsification_report(cfg, seq, vrep, sp_rows, campaign=campaign)
    write_json(out("falsification_report_3d.json"), fals)
    conv = convergence_report(cfg)
    write_json(out("convergence_report_3d.json"), conv)
    write_rows_csv(out("sensitivity_ranking_3d.csv"), sensitivity_rows(cfg))
    elig = nv_eligibility_forecast(cfg, seq, sp_rows)
    write_json(out("nv_eligibility_3d.json"), elig)
    write_json(out("assumptions_3d.json"), assumptions_record(cfg))
    write_json(out("provenance_3d.json"), provenance_record())
    dep = laser_deposition_summary(cfg, grid, seq.tB.laser, device_state("MODE_B"))
    dep["optical_collection_path"] = optical_collection_load()
    write_json(out("laser_3d_deposition_summary.json"), dep)

    # ---- machine FSM: the operational controller (zero extra solves) ----
    from .machine_fsm import (run_nominal_lifecycle, state_rows,
                              transition_rows, interlock_rows,
                              mermaid_diagram)
    from .vibration_transfer import vibration_transfer as _vt
    _, _vm = _vt()
    fsm_trace, fsm_summary = run_nominal_lifecycle(cfg, seq, sp_rows, _vm)
    write_rows_csv(out("machine_fsm_states.csv"), state_rows())
    write_rows_csv(out("machine_fsm_transitions.csv"), transition_rows())
    write_rows_csv(out("machine_fsm_interlocks.csv"), interlock_rows())
    write_rows_csv(out("machine_fsm_lifecycle_trace.csv"), fsm_trace)
    write_json(out("machine_fsm_summary.json"), fsm_summary)
    (out("machine_fsm_diagram.mmd")).write_text(mermaid_diagram())

    # ---- campaign continuity outputs (separate API; zero extra solves) ----
    write_rows_csv(out("cryopanel_loading_3d.csv"), campaign.panel_rows)
    write_rows_csv(out("energy_ledger_cumulative_3d.csv"),
                   campaign.energy_rows)
    write_rows_csv(out("machine_fsm_campaign_trace.csv"),
                   campaign.trace_rows)
    from .measurement_ingest_3d import (ingest_and_compare as _ing,
                                        comparison_rows as _ing_rows)
    ing = _ing()
    write_json(out("measurement_comparison_3d.json"), ing)
    write_rows_csv(out("measurement_comparison_rows.csv"), _ing_rows(ing))
    from .campaign_uncertainty_3d import (propagate as _unc_propagate,
                                          quantile_rows as _unc_rows)
    unc = _unc_propagate(cfg, n_cycles=DEFAULT_CYCLES)
    write_json(out("campaign_uncertainty_3d.json"), unc)
    write_rows_csv(out("campaign_uncertainty_quantiles.csv"),
                   _unc_rows(unc))
    write_json(out("campaign_state_3d.json"), {
        "schema_version": SCHEMA_VERSION,
        "n_cycles": campaign.n_cycles,
        "cycles": campaign.cycle_rows,
        "cumulative_energy": {k: (f"{v:.9e}" if isinstance(v, float) else v)
                              for k, v in campaign.cumulative.items()},
        "mass_bookkeeping": mass_bookkeeping_rows(campaign),
        "identical_cycle_approx_delta_K":
            f"{campaign.approx_delta_K:.3e}",
        "n_sensing_refusals": campaign.n_refused,
        "final_state": campaign.final_state,
        "measured_in_this_system": False,
        "can_PASS_now": "NO",
        "label": LABEL})

    # ---- readiness record (fail-closed, forecast-only, never PASS) ----
    mats = materials_3d.summary(cfg, grid)
    readiness = {
        "layer": "thermal_3d_transient + coupled forecast channels",
        "status": "FORECAST_ONLY_IMPLEMENTED",
        "resolution_tier": "REDUCED_CI",
        "all_numerical_checks_within_declared_tolerances":
            bool(vrep["all_within_declared_tolerances"]),
        "mode_sequence_status": seq.status,
        "mode_c_recovery_time_s": seq.recovery_time_s,
        "mode_d_hold_probe_K": (float(seq.tD_hold.probe_timeseries_K()[-1])
                                if seq.tD_hold is not None else None),
        "coupling_ledger_summary": ledger_summary(led),
        "nv_eligibility_forecast": elig["eligibility_forecast"],
        "nv_eligibility_blocking_reasons": elig["blocking_reasons"],
        "campaign_continuity": {
            "schema_version": SCHEMA_VERSION,
            "n_cycles": campaign.n_cycles,
            "panel_CH4_final_ML": f"{max(pp.monolayer_fraction for pp in campaign.panels if pp.species=='C13_CH4'):.3e}",
            "cumulative_energy_rel_residual":
                campaign.energy_rows[-1]["cumulative_rel_residual"],
            "sensing_refusals_per_campaign": campaign.n_refused,
            "carried_states": ["temperature (identical-cycle, delta "
                               f"{campaign.approx_delta_K:.1e} K)",
                               "coverage residual", "device state",
                               "cryopanel loading", "cumulative energy"],
            "not_carried": [],
            "validation_roadmap": (lambda _r, _g, _m, _c: {
                "schema_version": _r["schema_version"],
                "n_experiments": len(_r["experiments"]),
                "playbook_ready": [e["experiment_id"] for e in
                                   _r["experiments"]
                                   if e["status"] == "PLAYBOOK_READY"],
                "gates_covered": _g["n_gates"],
                "matrix_items": _m["n_items"],
                "matrix_unresolved": _m["n_unresolved"],
                "campaign_1": _c["campaigns"][0]["status"],
                "automatic_gate_effect": "NONE",
            })(*[__import__("json").loads(
                __import__("pathlib").Path(f).read_text())
                for f in ("experiment_registry.json",
                          "experiment_gate_coverage.json",
                          "experiment_matrix_coverage.json",
                          "campaign_registry.json")]),
            "hardware_governance": __import__(
                "qta_multiphysics.hardware_governance_3d",
                fromlist=["governance_summary"]).governance_summary(),
            "measurement_ingestion": {
                "schema_version": ing["schema_version"],
                "status": ing["ingestion_status"],
                "n_accepted": ing["n_accepted"],
                "n_rejected": ing["n_rejected"],
                "band_coverage": ing["coverage"],
                "synthetic_only": True,
                "hardware_refused_by_design": True,
                "read_only": "comparison context only; not validation; "
                             "never a gate input",
            },
            "uncertainty_propagation": {
                "schema_version": unc["schema_version"],
                "seed": unc["seed"], "n_members": unc["n_members"],
                "varied": [d["parameter"] for d in unc["distributions"]],
                "n_exclusions": len(unc["exclusions"]),
                "panel_CH4_ML_cycle3_band": unc["cycles"][-1]["panel_CH4_ML"],
                "composition": "thermal quantiles MARGINAL (not joint)",
            },
        },
        "falsification": {"n_conditions": fals["n_conditions"],
                          "n_falsified_in_model": fals["n_falsified_in_model"],
                          "n_not_implemented_bounds":
                              fals["n_not_implemented_bounds"]},
        "convergence": {"mesh_status": conv["mesh_check"]["status"],
                        "time_status":
                            conv["time_integration_check"]["status"]},
        "materials": mats,
        "not_implemented": [
            "per-region material property sets (materials_3d)",
            "C-13 methane 3D deposition footprint (species_transport_3d)",
            "3D radiative view factors (radiation_paths_3d)",
            "3D modal analysis (vibration_paths_3d)",
            "3D microwave field/SAR map (microwave_heating_3d)",
            "collection-path optical heat load (sources_3d)",
            "support-conduction numerics (SCAFFOLD registry only)",
            "residual-coverage and hotspot-damage falsification bounds",
            "LN2 precooling / standalone JT / nuclear demagnetization / "
            "gas-gap heat switch (cryo_stack_3d registry entries)",
        ],
        "measured_in_this_system": False,
        "can_PASS_now": "NO",
        "hardware_validated": False,
        "external_solver": "none (NumPy/SciPy only; no COMSOL)",
        "claim_boundary": "additive forecast-only / benchmark-numerical / "
                          "derived-check validation layer; not hardware "
                          "validation; introduces no PASS gates",
        "label": LABEL,
    }
    write_json(out("thermal_3d_readiness.json"), readiness)

    if heavy:
        hdir = outdir / "heavy_3d"
        hdir.mkdir(parents=True, exist_ok=True)
        rh = solve_thermal_3d(cfg, HEAVY_MESH, n_eval=13)
        write_rows_csv(hdir / "thermal_3d_probe_timeseries_heavy.csv",
                       rh.probe_timeseries_rows())
        write_rows_csv(hdir / "thermal_3d_hotspots_heavy.csv", rh.hotspot_rows())
        write_rows_csv(hdir / "thermal_3d_energy_accounting_heavy.csv",
                       energy_accounting_rows(rh))
        write_json(hdir / "heavy_3d_note.json", {
            "resolution_tier": "HEAVY_OPT_IN",
            "mesh": {"nx": HEAVY_MESH.nx, "ny": HEAVY_MESH.ny, "nz": HEAVY_MESH.nz},
            "note": "optional higher-resolution pass; gitignored; never required "
                    "by CI, the default run, the package consistency check, or "
                    "manifest verification", "label": LABEL})
        if verbose:
            print(f"[3D heavy pass complete -> {hdir} (opt-in; gitignored)]")

    if verbose:
        print("[3D layer: reduced CI outputs written (forecast-only; "
              f"verification within tolerances = {vrep['all_within_declared_tolerances']}; "
              "no PASS gate states)]")
    return {"verification": vrep, "readiness": readiness, "sequence_status": seq.status}
