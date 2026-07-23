#!/usr/bin/env python3
"""Render the eleven EXPERIMENT_PLAYBOOKS/*.md from experiment_registry.json
(single source of truth) plus per-experiment procedures. Deterministic.
MODEL-ONLY / FORECAST-ONLY -- planning infrastructure, not evidence."""
import json
from pathlib import Path

REG = json.load(open("experiment_registry.json"))
E = {e["experiment_id"]: e for e in REG["experiments"]}

PROC = {
 "EXP-V1": ["Verify interlock chain green and IVC sealed; record chamber "
            "state.", "Execute the 250 C / 48 h bakeout per E01 with "
            "thermocouple logging.", "Cool to operating configuration; "
            "record NEG/cryotrap conditioning steps.",
            "Acquire filament-off background, then pre-analysis blank.",
            "Run the FC-corrected all-species RGA protocol; log dwell and "
            "averaging.", "Repeat scans per the repetition rule until the "
            "SE criterion is met or the missing noise-floor datum is "
            "itself measured and recorded.",
            "Export vendor spectra + CSV; write sidecar metadata; hash "
            "and archive raw files."],
 "EXP-V2": ["Confirm EXP-V1 baseline in force.",
            "Walk device states B_SOURCE_OFF -> C_PURGE -> D_PRECONFIG "
            "with the calibrated leak as surrogate injection.",
            "Record P_i(t) through each transition; repeat the "
            "no-injection control sequence.",
            "Acquire valve-closed leak-through scans.",
            "Fit exponential decays per species; log residuals.",
            "Repeat sequences per the repetition rule.",
            "Export, hash, archive."],
 "EXP-P1": ["Mount blank crystal; record temperature-ramp blank and "
            "isotope-free background.",
            "Mount panel-material coupon; dose H2 per manometer schedule; "
            "record QCM uptake.",
            "Repeat for CH4 dosing within Mode-A safe limits (no growth).",
            "Mount F-diamond witness; repeat dosing series.",
            "Run TPD ramps with mass-spec logging.",
            "Fit Langmuir uptake vs coverage; integrate TPD.",
            "Export frequency series + spectra; hash; archive."],
 "EXP-T1": ["Record heater-off drift and zero-power baseline at base.",
            "Apply calibrated heater steps; log probe response to "
            "steady state.",
            "Fit step responses for G_eff and support load.",
            "Apply attenuated 532 nm pulses (closed-shutter control "
            "first); record recovery to baseline.",
            "Compare recovery to the forecast band; report honestly "
            "either way.", "Repeat per the repetition rule.",
            "Export thermometry series; hash; archive."],
 "EXP-T2": ["Verify EXP-V1/V2 clean baseline and laser-line calibration.",
            "Run no-precursor pulses (pure heating control) with "
            "thermoreflectance/pyrometry logging.",
            "Run no-pulse dosing control.",
            "Run pulsed growth sets in B_GROWTH_ACTIVE; log surface-T "
            "traces per pulse ensemble.",
            "Measure per-pulse yield via QCM witness and post-growth "
            "ellipsometry/AFM.",
            "Repeat ensembles per the repetition rule; watch stop "
            "criteria continuously.", "Export, hash, archive."],
 "EXP-N0": ["Record power-meter dark reading and substrate-only blank.",
            "Measure 532 nm transmission/reflection per candidate "
            "sample (300 K, then 4 K bench stage).",
            "Acquire PL maps; schedule SIMS depth profiles.",
            "Screen and rank samples; record eta_abs and d_NV with "
            "uncertainty.", "Export meter CSV + maps + profiles; hash; "
            "archive."],
 "EXP-N1": ["Install calibrated emitter/reference in the Mode-D optical "
            "path (D_PRECONFIG, dose off).",
            "Record dark counts and the misaligned-reference control.",
            "Acquire counts vs known emission; apply dead-time "
            "correction.",
            "Average adaptively to the pre-registered SE target "
            "(conditionally resolvable during run).",
            "Report eta_col with uncertainty; export; hash; archive."],
 "EXP-N2": ["Confirm EXP-N1 calibration in-window; sample from EXP-N0 "
            "screening.", "Record room-T reference on the same sample.",
            "At 10 mK, D_PRECONFIG dose-off: run the optical power sweep "
            "up and down (hysteresis check); identify the charge "
            "plateau.", "Acquire ODMR with off-resonance control "
            "interleaved.",
            "Average per the photon-shot rule once N_ph is measured "
            "(missing datum resolved by EXP-N1/this run).",
            "Report C_contr and charge stability; export; hash; "
            "archive."],
 "EXP-N3": ["Confirm EXP-N2 chain in-window; log directional-coupler "
            "P_mw.", "Acquire Rabi fringes vs P_mw with detuned-drive "
            "and power-off controls.",
            "Fit fringes; derive Omega_R(P_mw) with CRLB-based "
            "uncertainty.", "Repeat per rule; export; hash; archive."],
 "EXP-N4": ["Confirm EXP-N3 calibration; record no-dose baseline "
            "sequences (Ramsey/Hahn/XY8).",
            "Dose He-4 per canonical window; repeat the sequence "
            "family.", "Dose He-3; repeat; randomize sequence order "
            "across repeats.", "Monitor film-absence signatures (A14) "
            "throughout.",
            ">=5 delay points/decade over >=2 decades; repeat until "
            "sigma_tauc/tau_c <= 25% (per-point sigma from EXP-N2).",
            "Filter-function analysis across sequences; report T2*, T2, "
            "tau_c and the isotope contrast honestly either sign.",
            "Export decay records; hash; archive."],
 "EXP-S1": ["FIRST: record every sensor's self-noise floor (mandatory "
            "control; resolves this experiment's own repetition "
            "inputs).", "Mount vibration sensor at the NV sensing "
            "location; acquire pumps-off/pumps-on spectral pairs.",
            "Welch-average per the rule until the in-band SE criterion "
            "vs the 1e-10 m threshold is met.",
            "Acquire radiative-load, RF-leakage, optical-scatter and "
            "magnetic surveys with shutter open/closed pairs.",
            "Report every survey with uncertainty -- including an honest "
            "confirmation of the forecast exceedance if that is the "
            "outcome.", "Export spectra; hash; archive."],
}

SAFETY = ("All machine interlocks (IL-01..IL-14, FSM-IL-15..18) remain "
          "authoritative; any interlock trip is an immediate stop; no "
          "procedure step may bypass a guard, valve, shutter or "
          "device-state rule; species policy absolute (methane only in "
          "Mode B; helium only in Mode D; no simultaneous processing and "
          "sensing).")
REVIEW = ("Human review per Stage-5 governance: HARDWARE_UNVERIFIED "
          "records quarantine with deficiencies; only complete "
          "HARDWARE_REVIEWED records with a valid ACCEPT_AS_EVIDENCE "
          "review enter dossiers; tools never author or promote reviews.")

TPL = """# {eid} -- {title}

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Planning infrastructure
only -- this playbook is not experimental evidence; no experiment has
been performed. PASS remains zero.

## 1. Experiment purpose
{purpose}

## 2. Scientific question
{question}

## 3. Gates served
{gates}

## 4. Matrix items served
{items}

## 5. Required machine mode
{modes}

## 6. Exact device state
{dstates}

## 7. Sample or witness requirements
{sample}

## 8. Instrument requirements
{instruments}

## 9. Calibration chain
{cal}

## 10. Positive and negative controls
{controls}

## 11. Prerequisites
{deps}

## 12. Step-by-step measurement procedure
{steps}

## 13. Quantities and units
{qty} [{units}]

## 14. Repetition-count derivation
{repder}

## 15. Repetition-resolution status
{repstat}

## 16. Exact missing datum when unresolved
{repmiss}

## 17. Raw-data format
{raw}

## 18. Sidecar metadata requirements
{meta}

## 19. Analysis method
{analysis}

## 20. Uncertainty treatment
{unc}

## 21. Acceptance criteria
{acc}

## 22. Rejection criteria
{rej}

## 23. Stop criteria
{stop}

## 24. Interlock and safety conditions
{safety}

## 25. Dependencies
{deps}

## 26. Expected dossier outputs
{dossier}

## 27. Human review requirements
{review}

## 28. Claim limitations
{claims}

## 29. Automatic gate effect
{auto} -- no measurement, dossier, or review produced under this playbook
can change a gate, a matrix status, or a parameter; matrix changes travel
only through human-authored update requests reviewed under Stage-5
governance.

## 30. Version and provenance
version {ver}; sources: {prov}; forbidden modes: {fmodes}; required
species: {rspec}; forbidden species: {fspec}; EIG provenance: {eig}.
"""

QUESTION = {
 "EXP-V1": "Does the conditioned chamber reach the canonical residual "
           "targets, species-resolved, with defensible calibration?",
 "EXP-V2": "How fast and how completely do purge/valve sequences isolate "
           "processing from sensing?",
 "EXP-P1": "What are the actual sticking coefficients and areal "
           "capacities the Stage-2/3 models assume?",
 "EXP-T1": "What is the real thermal conductance and recovery behaviour "
           "of the fabricated link?",
 "EXP-T2": "What surface temperature and per-pulse yield does Mode-B "
           "processing actually produce?",
 "EXP-N0": "Which candidate samples have the optical properties the "
           "architecture assumes?",
 "EXP-N1": "What fraction of NV emission does the real cryostat path "
           "collect?",
 "EXP-N2": "Is the NV charge state stable, and what ODMR contrast exists "
           "at 10 mK on this sample?",
 "EXP-N3": "What Rabi frequency does the delivered microwave power "
           "produce?",
 "EXP-N4": "What are T2*, T2 and tau_c on this surface, and does the "
           "He-3 vs He-4 comparison show the predicted isotope effect?",
 "EXP-S1": "What are the true environmental loads -- vibration at the NV "
           "location foremost -- against the Mode-D budgets?",
}

outdir = Path("EXPERIMENT_PLAYBOOKS")
outdir.mkdir(exist_ok=True)
for eid, e in E.items():
    md = TPL.format(
        eid=eid, title=e["title"], purpose=e["purpose"],
        question=QUESTION[eid],
        gates=", ".join(e["gates_served"]) or "(none directly; see "
        "matrix items)",
        items=", ".join(e["matrix_items_served"]),
        modes=", ".join(e["required_modes"]),
        dstates="; ".join(e["required_device_states"]),
        sample=e["sample_requirements"],
        instruments="; ".join(e["instruments"]),
        cal=e["calibration_requirements"],
        controls="; ".join(e["controls"]),
        deps=", ".join(e["dependencies"]) or "none",
        steps="\n".join(f"{i+1}. {s}" for i, s in enumerate(PROC[eid])),
        qty=", ".join(e["measured_quantities"]), units=e["units"],
        repder=e["repetition_derivation"],
        repstat=e["repetition_resolution_status"],
        repmiss=e["missing_repetition_inputs"],
        raw="; ".join(e["raw_data_formats"]),
        meta=e["metadata_requirements"],
        analysis=e["analysis_method"], unc=e["uncertainty_method"],
        acc=e["acceptance_criteria"], rej=e["rejection_criteria"],
        stop=e["stop_criteria"], safety=SAFETY,
        dossier=e["dossier_requirements"], review=REVIEW,
        claims=e["claim_limitations"],
        auto=e["automatic_gate_effect"], ver=e["version"],
        prov="; ".join(e["provenance_refs"]),
        fmodes=", ".join(e["forbidden_modes"]) or "none",
        rspec=", ".join(e["required_species"]) or "none",
        fspec=", ".join(e["forbidden_species"]) or "none",
        eig=e["eig_source"])
    (outdir / f"{eid}.md").write_text(md)
print("rendered", len(E), "playbooks; sections per file:",
      (outdir / "EXP-V1.md").read_text().count("\n## "))
