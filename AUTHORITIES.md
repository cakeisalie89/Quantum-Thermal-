# QTA Authorities — Single Sources of Truth

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

`authorities.json` (schema 1.0) is the machine-readable registry mapping every
governed concept to its one executable authority, its owner, its schema
version, and its consumers. Rule: code is the authority; documentation mirrors
code and never overrides it; canonical outputs live at the repository root and
are byte-gated against regeneration by the package consistency checker.

| Concept | Authority (owner) |
|---|---|
| Modes & species permissions | `qta_multiphysics/mode_sequence_3d.py` (CANONICAL_ACTIVE / RESIDUAL_ONLY / validators) |
| States, transitions, interlocks, switches/valves/shutters | `qta_multiphysics/machine_fsm.py` (32 states, 38+8 transitions, 18 interlocks over 13 hardware axes) |
| Per-mode device states | `qta_multiphysics/state_machine_3d.py` |
| Units & physical parameters | `qta_multiphysics/config.py` (SI, validated) + provenance JSONs |
| Solver profiles & tolerances | `SolverConfig` + `qta_full_sim.py` profiles (default / --ci / --deep / --heavy-3d) |
| Meshes | `runner_3d.py` (CI 10×10×12; heavy 16×16×20) + `convergence_3d.py` (refined 14×14×18) |
| Seeds & MC counts | `qta_full_sim.py` (42; N=10000/5000; mc_samples=30) + `uncertainty.py` (12345; n=120) |
| Gates (83; PASS=0) | `metrics.py` + `qta_full_sim.py` → `results_gate_table.csv` |
| Schemas | `deep_design_schema.json`, `deep_parameter_schema.json`, ledger vocabulary in `coupling_ledger_3d.py` |
| Canonical output paths | repo root, enumerated by `final_manifest.json`, byte-gated by checker Step 2b |
| Claim-boundary wording | `CLAIMS_BOUNDARY.md` + the MODEL_ONLY/FORECAST_ONLY label + per-record claim fields (checker-enforced) |
| Provenance | `final_manifest.json` + `manifest_hash.txt` (generator `generate_manifest.py`; coverage = every git-tracked file minus the two detached, see `MANIFEST_BOUNDARY.md`. Hashing a file records its bytes, it does not make the file authoritative — this table does that) |
| Cryopanel dynamics (Stage 2) | `qta_multiphysics/cryopanel_dynamics_3d.py` (ASSUMED sticking; PLACEHOLDER capacity) |
| Campaign continuity (Stage 2) | `qta_multiphysics/campaign_state_3d.py` (schema 1.0; separate API) |
| Campaign uncertainty (Stage 3) | `qta_multiphysics/campaign_uncertainty_3d.py` (seed 20260717; verified-source distributions; exclusions recorded) |
| Measurement ingestion (Stage 4) | `qta_multiphysics/measurement_ingest_3d.py` (SYNTHETIC-only; read-only; fail-closed; never a gate input) |
| Hardware governance (Stage 5) | `qta_multiphysics/hardware_governance_3d.py` (quarantine/dossier/audit-chain; human-only review; automatic_gate_effect=NONE) |
| Validation roadmap (Stage 6) | registries + playbooks + schemas (planning only; 25/25 gates; automatic_gate_effect=NONE) |
| Scientific-stack adoption (Stage 10) | `STACK.md` + `stack.json` (schema 1.0.0) — ADOPTED / STAGED / DEFERRED per element; implemented in `qta_multiphysics/stack/` (additive, workspace-only, automatic_gate_effect=NONE) |
| Agent authority substrate | `qta_agent/` — `authority.py` (transition table; invariants I1–I6), `events.py` (append-only hash-chained log + separately witnessed head), `evidence.py` (content-addressed store; a cited digest must resolve), `policy.py` (versioned rules; a decision names the document digest that decided), `scheduler.py` (the durable queue), `secrets.py` (references, not values), `netauth.py` (egress as a bounded grant), `memory.py` (remembered, never evidence), `context.py` (shown, never true), `checkpoint.py` (a cached verification result, never a second truth), `canonical.py` (the one byte representation). Governs how claims *about* the project become canonical, never what the physics says: no module here is imported by the solvers, `qta_full_sim.py`, or `metrics.py`; automatic_gate_effect=NONE; PASS=0 is unaffected |

Competing sources are never resolved silently: see
`authorities.json → competing_sources_record` (two historical entries, both
resolved with evidence and recorded numerical consequences; the unresolved
list is empty).
