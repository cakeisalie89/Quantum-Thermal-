#!/usr/bin/env python3
"""Stage-6 registry builder. MODEL-ONLY / FORECAST-ONLY. Planning
infrastructure only -- not experimental evidence. Deterministic."""
import csv
import json

LBL = "MODEL_ONLY FORECAST_ONLY NOT_MEASURED_IN_THIS_SYSTEM"
SV = "1.0.0"   # new Stage-6 record types (compatible extension of the
               # project's MAJOR.MINOR convention; noted in AUTHORITIES)
NONE_EFF = "NONE"
PLAN_NOTE = ("planning infrastructure only; not experimental evidence; no "
             "experiment performed; no hardware claim")

RANK = list(csv.DictReader(open("validation_experiment_ranking.csv")))
EIG = {r["experiment_id"]: {"rank": int(r["rank"]), "score": r["score"],
                            "label": r["label"]} for r in RANK}
MATRIX = list(csv.DictReader(open("validation_matrix.csv")))
OPEN = [r for r in MATRIX if r["status"] in ("UNKNOWN", "ASSUMED")]
GATES = list(csv.DictReader(open("results_gate_table.csv")))
G25 = [r for r in GATES if r["status"] in ("BLOCKED", "UNKNOWN")]

REP = {
    "V1": ("average scans until SE(P_i) <= 1/3 of the margin to each "
           "canonical pressure threshold; n and dwell follow from the "
           "RGA minimum-detectable-partial-pressure and noise density",
           "UNRESOLVED_MISSING_INSTRUMENT_NOISE",
           "RGA model noise density / MDPP spec (a Campaign-1 output)"),
    "V2": ("fit tau_purge per sequence; n_sequences from target "
           "sigma_tau/tau via fit-residual scatter",
           "UNRESOLVED_MISSING_REPEATABILITY",
           "per-scan RGA repeatability (from EXP-V1)"),
    "P1": ("n = (sigma_shot/sigma_target)^2 with sigma_target sized to "
           "resolve the CSV's declared 0.3-0.8 sticking range to +-0.05",
           "UNRESOLVED_MISSING_INSTRUMENT_NOISE",
           "cryo-QCM frequency-noise floor at 4 K"),
    "T1": ("sigma_G/G from thermometer noise and Delta-T by standard "
           "propagation; repeat until SE <= target margin vs the ASSUMED "
           "G=1e-5 W/K comparison",
           "UNRESOLVED_MISSING_INSTRUMENT_NOISE",
           "RuOx thermometer noise at the 10 mK operating point"),
    "T2": ("pulse-ensemble averaging vs detector shot noise; n from "
           "(sigma_shot/sigma_target)^2",
           "UNRESOLVED_MISSING_INSTRUMENT_NOISE",
           "thermoreflectance/pyrometer detector noise floor"),
    "N0": ("power-meter-limited averaging per vendor spec",
           "CONDITIONALLY_RESOLVABLE_DURING_RUN",
           "vendor spec sheet confirmation at run time"),
    "N1": ("photon-shot rule n = (sigma_shot/sigma_target)^2; per-shot "
           "sigma from the measured count rate (adaptive-n, "
           "pre-registered target)",
           "CONDITIONALLY_RESOLVABLE_DURING_RUN",
           "measured count rate during the run itself"),
    "N2": ("photon-shot-limited: sigma_C per readout ~ sqrt(2/N_ph); "
           "n_avg = (sigma_C,shot/sigma_target)^2 with sigma_target = "
           "1/10 of the contrast threshold margin",
           "UNRESOLVED_MISSING_COUNT_RATE",
           "in-cryostat count rate N_ph per readout (from EXP-N1)"),
    "N3": ("CRLB on Rabi-fringe fit; repeats from per-point sigma",
           "UNRESOLVED_MISSING_COUNT_RATE",
           "per-point contrast sigma (from EXP-N2)"),
    "N4": ("CRLB on the decay-constant fit: >=5 delay points/decade over "
           ">=2 decades; repeat until sigma_tauc/tau_c <= 25% (enough to "
           "place tau_c against the 292 us canonical definition and the "
           "27.7 us SUPERSEDED value)",
           "UNRESOLVED_MISSING_COUNT_RATE",
           "per-point sigma from EXP-N2's measured contrast noise"),
    "S1": ("Welch spectral averaging: n_avg segments until in-band "
           "amplitude SE <= 1/3 of the margin to the canonical 1e-10 m "
           "threshold (sensor self-noise measured first)",
           "UNRESOLVED_MISSING_INSTRUMENT_NOISE",
           "each environment sensor's self-noise floor (measured as the "
           "first sub-step of this experiment)"),
}


def exp(eid, title, family, purpose, legacy, plan_alias, execo, camp,
        diff, burden, modes, dstates, fmodes, rspecies, fspecies, meas,
        qty, units, sample, instruments, cal, controls, raw, meta,
        analysis, unc, acc, rej, stop, deps, gates, items, dossier, risk):
    r = REP[eid.split("-")[1]]
    return {
        "experiment_id": eid, "title": title, "version": "1.0",
        "status": "DESIGNED", "purpose": purpose,
        "experiment_family": family,
        "legacy_aliases": legacy, "plan_aliases": plan_alias,
        "eig_rank": (EIG.get(plan_alias[0], {}).get("rank")
                     if plan_alias else None),
        "eig_source": ("validation_experiment_ranking.csv via plan alias "
                       + plan_alias[0]) if plan_alias else
                      "no EXP-A..F alias; not EIG-ranked (consolidated "
                      "infrastructure experiment)",
        "information_value_order": "see eig_preservation block "
                                   "(EIG ranking, kept separate)",
        "execution_order": execo, "campaign": camp,
        "difficulty": diff, "resource_burden": burden,
        "required_modes": modes, "required_device_states": dstates,
        "forbidden_modes": fmodes,
        "required_species": rspecies, "forbidden_species": fspecies,
        "measurement_summary": meas, "measured_quantities": qty,
        "units": units, "sample_requirements": sample,
        "instruments": instruments, "calibration_requirements": cal,
        "controls": controls,
        "repetition_derivation": r[0],
        "repetition_resolution_status": r[1],
        "missing_repetition_inputs": r[2],
        "raw_data_formats": raw, "metadata_requirements": meta,
        "analysis_method": analysis, "uncertainty_method": unc,
        "acceptance_criteria": acc, "rejection_criteria": rej,
        "stop_criteria": stop, "dependencies": deps,
        "gates_served": gates, "matrix_items_served": items,
        "dossier_requirements": dossier, "risk_notes": risk,
        "claim_limitations": PLAN_NOTE,
        "automatic_gate_effect": NONE_EFF,
        "measured_in_this_system": False,
        "provenance_refs": ["Stage-6 assessment", "validation_matrix.csv",
                            "FIRST_VALIDATION_EXPERIMENTS.md",
                            "results_gate_table.csv"],
    }


SIDE = "Stage-5 sidecar metadata per raw_data_standard.md"
EXPS = [
 exp("EXP-V1", "Bakeout + RGA all-species baseline", "vacuum",
     "establish the post-bakeout residual-gas baseline with the "
     "FC-corrected all-species RGA protocol",
     [], [], 1, "Campaign-1", "MED", "MED",
     ["MODE_A"], ["PREP_VACUUM.POST_BAKEOUT_VERIFY",
                  "MODE_A_BASELINE.READY_IDLE"], ["MODE_B", "MODE_D"],
     [], ["C13_CH4 (live)", "He3 (live)", "He4 (live)"],
     "250C/48h bakeout record then species-resolved partial pressures "
     "with FC correction, backgrounds subtracted",
     ["P_H2", "P_CH4", "P_He", "P_H2O", "P_CO", "P_CO2",
      "P_hydrocarbons"], "Pa",
     "none (no diamond required)",
     ["quadrupole RGA", "bakeout thermocouples", "certified leak"],
     "RGA mass+FC gain calibration against certified leak, in-window; "
     "thermocouple traceable calibration",
     ["pre-bake baseline scan", "filament-off background",
      "empty-chamber blank"],
     ["vendor RGA spectra", "CSV export"], SIDE,
     "background-subtracted species fits; drift monitoring",
     "scan-ensemble statistics + calibration systematics stated "
     "separately",
     "stable post-bake spectra, backgrounds subtracted, calibration "
     "in-window, uncertainties stated",
     "FC calibration drift beyond vendor spec; non-stationary background",
     "pressure excursion beyond interlock limits",
     [], ["E01", "E04", "B3", "D10a", "D10b(enables)",
          "A11(instrumented)", "D2(instrumented)",
          "RESIDUAL_SPECIES_MODE_D_CHECK(instrumented)",
          "Shield-CHEM(partial)"],
     ["P_H2-class residual items (per matrix routing)"],
     "hardware records + calibration records + backgrounds; dossier per "
     "gate", "first mover; no sample risk"),
 exp("EXP-V2", "Purge/dosing isolation dynamics", "vacuum",
     "time-resolved purge/valve isolation between growth and sensing "
     "modes (no NV sensing)",
     [], ["EXP-F"], 5, "Campaign-2", "MED", "LOW-additional",
     ["MODE_A", "MODE_B", "MODE_C", "MODE_D"],
     ["MODE_B_PROCESS.B_SOURCE_OFF", "MODE_C_RECOVERY.C_PURGE",
      "MODE_D_SENSE.D_PRECONFIG"], [],
     ["surrogate/calibrated leak injection only"],
     ["C13_CH4 (live at full process rates)", "He3 (live)",
      "He4 (live)"],
     "P_i(t) through simulated B->C->D valve/purge sequences; purge "
     "time-constants and cross-mode isolation ratios",
     ["P_i(t)", "tau_purge", "isolation_ratio"],
     "Pa; 1/s; dimensionless", "none",
     ["EXP-V1 RGA", "calibrated leak"],
     "as EXP-V1 + leak certificate in-window",
     ["no-injection sequence", "valve-closed leak-through scan"],
     ["RGA time-series CSV"], SIDE,
     "exponential decay fits per species",
     "fit-residual statistics; systematic leak-rate uncertainty",
     "tau_purge and isolation ratios with stated uncertainty",
     "unexplained non-exponential contamination signatures",
     "interlock-limit pressures", ["EXP-V1"],
     ["A11", "D1", "D2", "RESIDUAL_SPECIES_MODE_D_CHECK",
      "Shield-CHEM(partial)", "COUPLED_MODE_RECOVERY_CHECK(constituent)",
      "C_to_D_Readiness(constituent)"],
     ["molecular_flux", "purge-class items"],
     "sequence dossiers binding V1 baseline", "valve-actuation wear"),
 exp("EXP-P1", "Cryopanel sticking and capacity", "surface",
     "measure sticking coefficients and areal capacity on panel coupon "
     "and F-diamond witness (closes Stage-2/3 ASSUMED/PLACEHOLDER "
     "parameters)",
     [], [], 4, "Campaign-1-parallel", "MED", "MED",
     ["MODE_A"], ["MODE_A_BASELINE.READY_IDLE"], ["MODE_B", "MODE_D"],
     ["controlled dosing via calibrated line"], ["live process flows"],
     "cryo-QCM uptake + TPD on coupon and F-diamond witness vs coverage",
     ["s_H2", "s_CH4", "areal_capacity"],
     "dimensionless; molecules/m^2 (monolayers)",
     "panel-material coupon + F-terminated diamond witness",
     ["cryo-QCM at 4 K", "TPD stage with mass spec",
      "dosing manometer"],
     "QCM frequency calibration; manometer calibration; TPD mass-scale "
     "calibration",
     ["blank-crystal dose", "temperature-ramp blank",
      "isotope-free background"],
     ["QCM frequency time-series", "TPD spectra"], SIDE,
     "Langmuir-uptake fits vs coverage; TPD integration",
     "frequency-noise statistics + dose systematics",
     "s and capacity with uncertainty and coverage-dependence curve",
     "crystal-mount thermal artifacts",
     "panel contamination beyond regeneration capability", ["EXP-V1"],
     ["Shield-CHEM(partial)", "A11(context)",
      "RESIDUAL_SPECIES_MODE_D_CHECK(context)"],
     ["H2_sticking", "n_s", "surface_coverage_proxy",
      "surface_spin_density(indirect)"],
     "uptake-curve dossiers per species", "witness-sample handling"),
 exp("EXP-T1", "Thermal conductance and recovery", "thermal",
     "step-response G_eff and support load after Ag-sinter fabrication; "
     "pulsed recovery-to-baseline",
     [], ["EXP-E"], 6, "Campaign-2", "MED-HIGH", "HIGH",
     ["MODE_A", "MODE_B"], ["MODE_A_BASELINE.READY_IDLE",
                             "MODE_B_PROCESS.B_GROWTH_ACTIVE(attenuated)"],
     ["MODE_D"], ["attenuated 532 nm only"], ["He3", "He4", "C13_CH4"],
     "calibrated-heater step response + attenuated-pulse recovery at the "
     "probe",
     ["G_eff", "support_load", "t_recovery"], "W/K; W; s",
     "instrumented stage (no diamond growth)",
     ["RuOx thermometer chain", "calibrated heater",
      "photodiode-monitored laser"],
     "RuOx calibration against reference at base; heater power "
     "calibration",
     ["heater-off drift record", "zero-power baseline",
      "closed-shutter laser control"],
     ["thermometry time-series CSV"], SIDE,
     "step-response fits; recovery-curve comparison to the forecast band",
     "thermometer-noise propagation to sigma_G/G",
     "G_eff with uncertainty; recovery curve honestly matching or "
     "failing the forecast",
     "unexplained drift exceeding controls", "base-temperature loss",
     ["fridge commissioning"],
     ["COUPLED_MODE_RECOVERY_CHECK(constituent)",
      "C_to_D_Readiness(constituent)"],
     ["G_eff", "support_thermal_load"],
     "step-response dossiers", "fridge-time contention"),
 exp("EXP-T2", "In-situ surface T and per-pulse yield", "thermal",
     "surface temperature during Mode-B pulses + deposition yield per "
     "pulse",
     [], [], 11, "Campaign-3", "HIGH", "HIGH",
     ["MODE_B"], ["MODE_B_PROCESS.B_GROWTH_ACTIVE"], ["MODE_D"],
     ["C13_CH4 (Mode-B only)"], ["He3", "He4"],
     "thermoreflectance/pyrometry during pulses; QCM-witness or "
     "post-growth ellipsometry/AFM yield",
     ["T_surface(t)", "yield_per_pulse"], "K; nm/pulse",
     "growth witness + diamond substrate",
     ["thermoreflectance or pyrometer", "QCM witness",
      "ellipsometer/AFM (post)"],
     "detector radiometric calibration; QCM calibration",
     ["no-precursor pulses (pure heating)", "no-pulse dosing"],
     ["detector time-series", "post-growth metrology files"], SIDE,
     "pulse-ensemble averaging; yield regression vs pulse count",
     "detector-shot statistics + radiometric systematics",
     "T_surface and yield with uncertainty",
     "window fouling artifacts", "sample damage signatures",
     ["EXP-V1", "EXP-V2", "laser line"], ["A8", "A9"],
     ["A8/A9-class items"], "per-pulse-set dossiers",
     "highest sample risk of the set"),
 exp("EXP-N0", "Bench optical pre-characterization", "nv",
     "532 nm transmission/reflection, PL depth / SIMS, sample screening "
     "(dependency reducer)",
     [], [], 3, "Campaign-1", "LOW", "LOW",
     ["MODE_A(bench analog)"], ["bench (outside machine)"], [],
     [], [],
     "eta_abs and d_NV on candidate samples at 300 K / 4 K bench",
     ["eta_abs", "d_NV"], "dimensionless; m",
     "candidate diamond samples",
     ["bench 532 nm source", "NIST-traceable power meter",
      "PL microscope / SIMS access"],
     "power-meter traceable calibration; SIMS standard",
     ["substrate-only blank", "meter dark reading"],
     ["power-meter CSV", "PL maps", "SIMS profiles"], SIDE,
     "transmission/reflection arithmetic; depth-profile fits",
     "vendor-spec meter uncertainty + repeat statistics",
     "eta_abs and d_NV with uncertainty per sample",
     "surface-contamination artifacts", "sample damage", [],
     [], ["eta_abs", "d_NV"],
     "per-sample screening dossiers", "lowest-risk starter"),
 exp("EXP-N1", "In-cryostat optical collection calibration", "nv",
     "eta_col via calibrated emitter / reference ensemble in the Mode-D "
     "optical path",
     [], [], 7, "Campaign-2", "MED", "HIGH",
     ["MODE_D"], ["MODE_D_SENSE.D_PRECONFIG"], ["MODE_B"],
     [], ["C13_CH4", "He3 (dose off)", "He4 (dose off)"],
     "counts/s vs known emission through the real collection path",
     ["eta_col"], "dimensionless",
     "calibrated emitter or reference NV ensemble",
     ["calibrated emitter", "SPAD/PMT chain"],
     "emitter calibration certificate; detector dead-time "
     "characterization",
     ["dark counts", "misaligned-reference"],
     ["count-rate time-series"], SIDE,
     "ratio to known emission with dead-time correction",
     "photon-shot statistics (adaptive-n to pre-registered target)",
     "eta_col with uncertainty", "alignment drift beyond controls",
     "cryostat window damage", ["EXP-N0", "fridge+optics"],
     ["E11"], ["eta_col"], "calibration dossier",
     "optical-access complexity"),
 exp("EXP-N2", "ODMR contrast and charge state at 10 mK", "nv",
     "C_contr and NV-/NV0 charge stability vs optical power at base "
     "temperature",
     [], ["EXP-A"], 8, "Campaign-2", "HIGH", "HIGH",
     ["MODE_D"], ["MODE_D_SENSE.D_PRECONFIG (dose off)"], ["MODE_B"],
     [], ["C13_CH4", "He3 (dose off)", "He4 (dose off)"],
     "ODMR contrast + power-sweep charge-state plateau at 10 mK",
     ["C_contr", "NV_charge_ratio"], "dimensionless",
     "screened sample from EXP-N0",
     ["ODMR chain (MW + optics)", "SPAD/PMT"],
     "EXP-N1 collection calibration in-window; MW chain via directional "
     "coupler",
     ["off-resonance MW", "power-sweep hysteresis check",
      "room-T reference on the same sample"],
     ["photon-count records", "sweep tables"], SIDE,
     "contrast fits; plateau identification",
     "photon-shot per-readout sigma_C ~ sqrt(2/N_ph)",
     "C_contr with uncertainty + stable charge plateau",
     "unbounded photo-ionization drift", "sample damage signatures",
     ["EXP-N1", "EXP-N0"], ["E12", "D12_G23"],
     ["C_contr_10mK", "charge_stability"],
     "contrast + charge dossiers", "cryostat-time heavy"),
 exp("EXP-N3", "Rabi calibration in cryostat", "nv",
     "Omega_R vs MW power with directional-coupler P_mw",
     [], [], 9, "Campaign-2", "HIGH", "LOW-additional",
     ["MODE_D"], ["MODE_D_SENSE.D_PRECONFIG (dose off)"], ["MODE_B"],
     [], ["C13_CH4", "He3 (dose off)", "He4 (dose off)"],
     "Rabi fringes vs P_mw",
     ["Omega_R", "P_mw"], "rad/s; W",
     "as EXP-N2", ["EXP-N2 chain", "directional coupler"],
     "coupler calibration; EXP-N2 chain in-window",
     ["detuned drive", "power-off"], ["fringe records"], SIDE,
     "Rabi-fringe fits", "CRLB on fringe fit",
     "Omega_R(P_mw) with uncertainty", "chirp/heating artifacts",
     "MW-heating interlock trip", ["EXP-N2"], ["E13"],
     ["P_mw", "Omega_R-class items"], "Rabi dossier",
     "incremental once N2 runs"),
 exp("EXP-N4", "Ramsey/Hahn T2*, T2, tau_c (He-3 vs He-4)", "nv",
     "decay constants and bath correlation time with the decisive "
     "helium-isotope surface-dose comparison; A14 film-absence "
     "monitored",
     [], ["EXP-B", "EXP-C"], 10, "Campaign-2", "HIGH", "HIGH",
     ["MODE_D"], ["MODE_D_SENSE.D_HE_DOSE",
                   "MODE_D_SENSE.D_PRECONFIG"], ["MODE_B"],
     ["He3 (Mode-D dose)", "He4 (Mode-D dose)"], ["C13_CH4"],
     "Ramsey/Hahn/XY8 decays with no-dose, He-4-only and He-3 "
     "conditions",
     ["T2_star", "T2", "tau_c", "isotope_contrast"], "s; s; s; "
     "dimensionless",
     "as EXP-N2",
     ["EXP-N2/N3 chain", "He dosing line"],
     "chains in-window; dosing manometer calibration",
     ["no-dose baseline", "He-4-only", "sequence-order randomization"],
     ["decay records per sequence"], SIDE,
     "filter-function analysis across sequences; exponential/stretched "
     "fits",
     "CRLB on decay-constant fits; >=5 delay points/decade over >=2 "
     "decades",
     "tau_c bound or value with uncertainty + isotope-contrast result "
     "either sign, reported honestly",
     "charge instability per EXP-N2 criteria",
     "EXP-N2 stop conditions", ["EXP-N3"],
     ["E14", "A14(monitored)"],
     ["tau_c", "T2_star", "T2", "surface_spin_density(indirect)"],
     "sequence-family dossiers incl. isotope comparison",
     "the scientific centerpiece; longest fridge occupancy"),
 exp("EXP-S1", "Environment survey: vibration at NV + shield quartet",
     "environment",
     "vibration spectrum at the sensing location (IL-08 path) plus "
     "radiative, RF, optical-scatter and magnetic surveys",
     [], [], 2, "Campaign-1", "MED", "MED",
     ["MODE_D(device states, sources off)"],
     ["MODE_D_SENSE.D_PRECONFIG (all sources off)"], ["MODE_B"],
     [], ["C13_CH4", "He3", "He4"],
     "sensor self-noise floors first, then in-band spectra with "
     "pumps-on/off and shutter open/closed pairs",
     ["a_vib(f)", "P_rad", "P_RF(f)", "P_opt_scatter", "B_noise(f)",
      "B_bias_stability"], "m/sqrt(Hz), m in-band; W; dBm; W; "
     "T/sqrt(Hz); T",
     "none",
     ["geophone/interferometer", "bolometric radiometer",
      "RF spectrum analyzer", "calibrated photodiode", "magnetometer"],
     "each sensor's calibration certificate in-window; self-noise "
     "measured first",
     ["sensor self-noise floors (mandatory first)", "pumps-off/on "
      "pairs", "shutter open/closed pairs"],
     ["spectra + time-series per sensor"], SIDE,
     "Welch spectral estimation; in-band integration",
     "segment-averaging SE; calibration systematics",
     "in-band vibration amplitude with uncertainty vs the 1e-10 m "
     "threshold WHATEVER the outcome (forecast 4.65e-9 m is ABOVE "
     "threshold; honest confirmation keeps IL-08 refusing) + the four "
     "shield surveys with uncertainty",
     "sensor overload/clipping", "none beyond instrument safety",
     [], ["Shield-RAD", "Shield-RF", "Shield-OPT", "Shield-MAG"],
     ["vibration/shield-class items"],
     "per-survey dossiers", "portable instruments; low machine risk"),
]

# ---- EXP-A..F reconciliation (phase 3; EXP-D grounded in the file) ----
AF = [
 {"plan_id": "EXP-A", "relationship": "renamed",
  "maps_to": ["EXP-N2"], "note": "identical scope (ODMR contrast at "
  "10 mK); consolidated id"},
 {"plan_id": "EXP-B", "relationship": "merged",
  "maps_to": ["EXP-N4"], "note": "He-3/He-4 decoherence contrast merged "
  "into the sequence-family experiment"},
 {"plan_id": "EXP-C", "relationship": "merged",
  "maps_to": ["EXP-N4"], "note": "tau_c constraint merged with EXP-B "
  "into EXP-N4"},
 {"plan_id": "EXP-D", "relationship": "decomposed",
  "maps_to": ["EXP-V1", "EXP-P1"], "note": "grounded in "
  "FIRST_VALIDATION_EXPERIMENTS.md: 'H2 partial pressure AND equivalent "
  "surface coverage after bakeout+NEG+cryotrap' -> pressure side EXP-V1, "
  "surface-coverage side EXP-P1"},
 {"plan_id": "EXP-E", "relationship": "renamed",
  "maps_to": ["EXP-T1"], "note": "thermal recovery to sensing baseline"},
 {"plan_id": "EXP-F", "relationship": "renamed",
  "maps_to": ["EXP-V2"], "note": "dosing/purge isolation"},
]

# single permitted design-readiness advance: EXP-V1 (Campaign-1 lead) --
# registry + playbook + raw-data standard + update-request schema +
# governance validators all exist; every other experiment stays DESIGNED
for _e in EXPS:
    if _e["experiment_id"] == "EXP-V1":
        _e["status"] = "PLAYBOOK_READY"

registry = {
    "schema_version": SV, "label": LBL, "note": PLAN_NOTE,
    "experiments": EXPS,
    "exp_af_reconciliation": AF,
    "eig_preservation": {
        "source": "validation_experiment_ranking.csv (deep layer; "
                  "authoritative; NOT recomputed)",
        "ranking": RANK,
        "distinct_from_execution_order": (
            "EIG order (A > C > B > F > D > E) is information value; "
            "execution order (V1 -> {S1,N0,P1} -> V2 -> T1 -> N1 -> N2 "
            "-> N3 -> N4 -> T2) is dependency reality: the high-EIG NV "
            "experiments cannot run first because vacuum, optics, "
            "calibration and noise-floor prerequisites are missing"),
    },
    "automatic_gate_effect": NONE_EFF,
    "measured_in_this_system": False,
}
json.dump(registry, open("experiment_registry.json", "w"), indent=1)

# --------------------- gate coverage (phase 5) ---------------------
GMAP = {
 "E01": ["EXP-V1"], "E04": ["EXP-V1"], "B3": ["EXP-V1"],
 "D10a": ["EXP-V1"], "D10b": ["EXP-V1"],
 "A11": ["EXP-V2", "EXP-V1"], "D1": ["EXP-V2"],
 "D2": ["EXP-V2", "EXP-V1"],
 "A8": ["EXP-T2"], "A9": ["EXP-T2"], "A14": ["EXP-N4"],
 "E11": ["EXP-N1"], "E12": ["EXP-N2"], "D12_G23": ["EXP-N2"],
 "E13": ["EXP-N3"], "E14": ["EXP-N4"],
 "Shield-RAD": ["EXP-S1"], "Shield-RF": ["EXP-S1"],
 "Shield-OPT": ["EXP-S1"], "Shield-MAG": ["EXP-S1"],
 "Shield-CHEM": ["EXP-V2", "EXP-P1"],
 "RESIDUAL_SPECIES_MODE_D_CHECK": ["EXP-V1", "EXP-V2"],
}
COMPOSITE = {
 "C_to_D_Readiness": {"constituents_gates":
      ["E01", "E04", "B3", "A11", "D1", "D2", "D10a", "D10b",
       "RESIDUAL_SPECIES_MODE_D_CHECK", "Shield-RAD", "Shield-RF",
       "Shield-OPT", "Shield-MAG", "Shield-CHEM"],
      "experiments": ["EXP-V1", "EXP-V2", "EXP-P1", "EXP-S1", "EXP-T1"]},
 "COUPLED_MODE_RECOVERY_CHECK": {"constituents_gates":
      ["D1", "D2", "A11", "RESIDUAL_SPECIES_MODE_D_CHECK"],
      "experiments": ["EXP-V2", "EXP-T1"]},
 "RESIDUAL_SPECIES_MODE_D_CHECK": {"constituents_gates":
      ["B3", "A11", "D2"], "experiments": ["EXP-V1", "EXP-V2"]},
}
cov = []
for g in G25:
    gid = g["gate_id"]
    if gid == "RTB_JT_OPTIONAL_COOLING_PLANT":
        cov.append({"gate_id": gid, "current_status": g["status"],
                    "classification": "engineering-evidence",
                    "experiment_ids": [],
                    "evidence_required": "procurement/engineering "
                    "documentation of the optional upstream cooling "
                    "plant; not a measurement experiment",
                    "constituents": [], "dossier_class":
                    "engineering-evidence (optional)",
                    "dependencies": [], "human_review_required": True,
                    "automatic_status_effect": NONE_EFF})
        continue
    if gid in COMPOSITE:
        c = COMPOSITE[gid]
        cov.append({"gate_id": gid, "current_status": g["status"],
                    "classification": "composite",
                    "experiment_ids": c["experiments"],
                    "evidence_required": "closure ONLY through every "
                    "constituent's reviewed evidence; no shortcut "
                    "experiment",
                    "constituents": c["constituents_gates"],
                    "dossier_class": "composite cross-reference dossier",
                    "dependencies": c["experiments"],
                    "human_review_required": True,
                    "automatic_status_effect": NONE_EFF})
        continue
    exps = GMAP.get(gid)
    cov.append({"gate_id": gid, "current_status": g["status"],
                "classification": "direct" if exps else "unresolved",
                "experiment_ids": exps or [],
                "evidence_required": "reviewed measurement dossier(s) "
                "per the playbook(s)" if exps else "UNRESOLVED",
                "constituents": [], "dossier_class":
                "measurement dossier" if exps else "unresolved",
                "dependencies": (exps or []),
                "human_review_required": True,
                "automatic_status_effect": NONE_EFF})
json.dump({"schema_version": SV, "label": LBL, "note": PLAN_NOTE,
           "n_gates": len(cov), "gates": cov,
           "automatic_status_effect": NONE_EFF},
          open("experiment_gate_coverage.json", "w"), indent=1)

# --------------------- matrix coverage (phase 6) ---------------------
OVR = {"tau_c": ["EXP-N4"], "C_contr_10mK": ["EXP-N2"],
       "G_eff": ["EXP-T1"], "support_thermal_load": ["EXP-T1"],
       "eta_abs": ["EXP-N0"], "T2_star": ["EXP-N4"], "T2": ["EXP-N4"],
       "d_NV": ["EXP-N0"], "n_s": ["EXP-P1"],
       "surface_spin_density": ["EXP-P1", "EXP-N4"],
       "charge_stability": ["EXP-N2"], "eta_col": ["EXP-N1"],
       "H2_sticking": ["EXP-P1"], "molecular_flux": ["EXP-V2"],
       "surface_coverage_proxy": ["EXP-P1"], "P_mw": ["EXP-N3"],
       # grounded second-pass routings (required_measurement texts):
       "thermal_recovery_time": ["EXP-T1"],       # pump-probe recovery
       "support_resonant_modes": ["EXP-S1"],      # PSD modal survey
       "eta_abs_D": ["EXP-N0"],                    # 532 nm absorption
       "fluence": ["EXP-T2"],                      # pulse+geometry cal chain
       "peak_intensity": ["EXP-T2"],               # pulse characterization
       "f_rep_D": ["EXP-T2"]}                      # rep rate at sample
ENG_ITEMS = {"Kevlar_clamp_slip_after_cooldown", "Kevlar_creep_relaxation",
             "G10CR_attachment_stability",
             "differential_thermal_contraction",
             "sample_stage_alignment_drift", "shutter_actuator_wear",
             "CTE_mismatch_diamond_Ag_sinter"}
# cooldown-cycle mechanical inspection / bench-rig records: engineering
# evidence class (inspection dossiers), not one of the 11 experiments
KW = [("rga", ["EXP-V1"]), ("bakeout", ["EXP-V1"]), ("neg", ["EXP-V1"]),
      ("ramsey", ["EXP-N4"]), ("hahn", ["EXP-N4"]), ("echo", ["EXP-N4"]),
      ("odmr", ["EXP-N2"]), ("rabi", ["EXP-N3"]),
      ("coupler", ["EXP-N3"]), ("qcm", ["EXP-P1"]), ("tpd", ["EXP-P1"]),
      ("step-response", ["EXP-T1"]), ("thermometry", ["EXP-T1"]),
      ("vibration", ["EXP-S1"]), ("magnet", ["EXP-S1"]),
      ("radiat", ["EXP-S1"]), ("rf", ["EXP-S1"]),
      ("scatter", ["EXP-S1"]), ("sims", ["EXP-N0"]), ("pl ", ["EXP-N0"]),
      ("transmission", ["EXP-N0"]), ("in-cryostat", ["EXP-N1"]),
      ("collection", ["EXP-N1"]), ("flux", ["EXP-V2"]),
      ("purge", ["EXP-V2"]), ("pyromet", ["EXP-T2"]),
      ("deposition", ["EXP-T2"]), ("power sweep", ["EXP-N2"]),
      ("charge", ["EXP-N2"]), ("sticking", ["EXP-P1"]),
      ("coverage", ["EXP-P1"]), ("film", ["EXP-N4"])]
ENG = ("rtb", "jt", "procure", "install", "fabricat", "engineering",
       "vendor", "plant", "module")
items = []
n_unres = 0
rep_status = {e["experiment_id"]: e["repetition_resolution_status"]
              for e in EXPS}
for r in OPEN:
    key = r["item"]; req = (r["required_measurement"] or "").lower()
    exps = OVR.get(key)
    cls = "direct" if exps else None
    if not exps:
        for kw, ee in KW:
            if kw in req or kw in key.lower():
                exps = ee; cls = "indirect"; break
    if not exps and (key in ENG_ITEMS or
                     any(t in req or t in key.lower() for t in ENG)):
        exps, cls = [], "engineering-evidence"
    if exps is None:
        exps, cls = [], "unresolved"; n_unres += 1
    items.append({"matrix_key": key, "current_status": r["status"],
                  "experiment_ids": exps,
                  "measured_quantity": r["required_measurement"] or
                  "(engineering evidence)",
                  "units": r["unit"],
                  "required_uncertainty_or_resolution":
                  "per the serving playbook's uncertainty method"
                  if exps else "n/a",
                  "repetition_resolution_status":
                  (rep_status[exps[0]] if exps else "n/a"),
                  "classification": cls,
                  "update_request_eligible": bool(exps) or
                  cls == "engineering-evidence",
                  "human_review_required": True,
                  "automatic_matrix_effect": NONE_EFF})
json.dump({"schema_version": SV, "label": LBL, "note": PLAN_NOTE,
           "n_items": len(items), "n_unresolved": n_unres,
           "rtb_jt_note": "RTB_JT-class items remain optional "
           "engineering/procurement evidence, not measurement "
           "experiments",
           "items": items, "automatic_matrix_effect": NONE_EFF},
          open("experiment_matrix_coverage.json", "w"), indent=1)

# --------------------- campaign registry (phase 8) ---------------------
camp = {"schema_version": SV, "label": LBL, "note": PLAN_NOTE,
 "campaigns": [{
   "campaign_id": "Campaign-1", "status": "PROPOSED_NOT_PERFORMED",
   "experiments": ["EXP-V1", "EXP-S1", "EXP-N0"],
   "rationale": ["avoids NV cryostat operation",
                 "avoids growth operation",
                 "minimizes diamond-sample risk",
                 "produces independently useful results",
                 "measures the instrument noise floors and calibration "
                 "inputs needed to resolve later repetition counts "
                 "(self-de-risking)"],
   "automatic_gate_effect": NONE_EFF,
   "measured_in_this_system": False}],
 "proposed_execution_sequence": [
   "1. EXP-V1", "2. EXP-S1 + EXP-N0 + EXP-P1 (parallel where practical)",
   "3. EXP-V2", "4. EXP-T1", "5. EXP-N1", "6. EXP-N2", "7. EXP-N3",
   "8. EXP-N4", "9. EXP-T2"],
 "dependency_note": ("EXP-P1 depends on EXP-V1's clean baseline, so its "
                     "parallel slot begins after V1 completes -- a "
                     "genuine dependency correction relative to a naive "
                     "fully-parallel reading; EIG order is preserved "
                     "separately in experiment_registry.json"),
 "automatic_gate_effect": NONE_EFF}
json.dump(camp, open("campaign_registry.json", "w"), indent=1)

print("built: experiment_registry.json ({} exps), "
      "experiment_gate_coverage.json ({} gates), "
      "experiment_matrix_coverage.json ({} items, {} unresolved), "
      "campaign_registry.json".format(
          len(EXPS), len(cov), len(items), n_unres))
