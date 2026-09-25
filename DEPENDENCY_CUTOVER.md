# Dependency cutover: where the QTA hardware ontology reaches the core

Phase 0 artefact of the architectural-convergence directive. It answers one
question with evidence: **which QTA-hardware dependencies are reachable from
the agent core, the scientific core and the release core, and in what order
they are cut.** Nothing here has been cut yet; this tranche is Phase 0 and
Phase 1 only.

Companion documents: `ARCHITECTURE_CONVERGENCE_PLAN.md` (the target and the
phase order) and `FILE_DISPOSITION.csv` (one disposition per tracked file).

## 1. Method, and what it can and cannot see

Two measurements, both over every tracked `.py` file (285 modules at
`71b58cb`), neither of them a text search:

1. **Import graph, twice.** Every `import` / `from ... import` node,
   resolved to tracked modules (relative imports against the importing
   package). Taken two ways, because they answer different questions:
   *import-time* edges are the statements that run when a module is
   imported -- module-level statements, plus the parent packages Python
   imports implicitly (`import a.b.c` runs `a/__init__.py` and
   `a/b/__init__.py` first); *lazy* edges are imports inside function
   bodies, which run only on the call path that reaches them. A first pass
   that merged the two reported the whole of `deep_expdesign` as unable to
   import without the apparatus registry. It imports fine; one function
   loads the registry when called. The distinction is the difference
   between a dependency and a call path, and it is kept below.
2. **Data references.** Every string constant in a module that is exactly the
   name of a tracked non-Python file. 533 references. This says a module
   *names* a file (to read it, write it, or check it); it does not by itself
   say which.

**Which modules ARE the hardware ontology was decided by reading each
candidate's docstring and purpose, not by marker counts.** A regex census of
hardware vocabulary (Mode A/B/C/D, BOM, interlock, gate, `BLOCKED`,
`measured_in_this_system`, apparatus identifiers) was taken first and then
used only as evidence to read against: it reports 20 "gate" hits in
`qta_agent/scheduler.py`, every one of which is the job-FSM state
`JobState.BLOCKED` and none of which is a QTA gate. A census is a proxy; the
reading is the classification.

The hardware-ontology module set (HW) used below:

| module | why it is ontology, not physics |
|---|---|
| `qta_multiphysics/machine_fsm.py` | the physical machine FSM: valves, shutter, heat switch, Mode A/B/C/D |
| `qta_multiphysics/hardware_governance_3d.py` | physical hardware governance and reviewer registry |
| `qta_multiphysics/design/` (3 files) | apparatus component registry, interface graph, decision ledger; reads `BOM.csv`, `interlock_table.csv` |
| `qta_multiphysics/mode_sequence_3d.py` | fixed Mode A/B/C/D sequencing |
| `qta_multiphysics/state_machine_3d.py` | mode -> device-state map (shutter, baffle, laser, switch) |
| `qta_multiphysics/stage7_boundary_models.py` | Pydantic validation of QTA identifiers and validation-matrix requests |
| `qta_multiphysics/integrated_layers.py` | feeds new records into the QTA gate table |
| `qta_multiphysics/metrics.py` | assembles the multiphysics gate specifications |
| `build_stage6_playbooks.py`, `build_stage6_registries.py`, `stage6_preservation_check.py`, `manuscript_consistency_check.py` | playbooks, validation registries, PASS=0 preservation, manuscript consistency |

The hardware data set: `BOM.csv`, `rejected_baseline_BOM.csv`,
`hardware_registry.json`, `hardware_reviewers.json`, `design_*`,
`machine_fsm_*`, `interlock_table.csv`, `results_gate_table.csv`,
`experiment_*`, `validation_matrix.csv`, `kill_gate_ranking.csv`,
`mode_transition_acceptance_tests.csv`, `campaign_registry.json`,
`failed_gate_samples.csv`, `monte_carlo_gate_failure_rates.csv`,
`validation_experiment_ranking.csv`, `shielding_stack_register.csv`,
`EXPERIMENT_PLAYBOOKS/`, the manuscript.

**Limits, stated.** The import graph cannot see a dependency expressed
through a subprocess (`qta_full_sim.py` is run, not imported, by
`package_consistency_check.py` and `snakemake_sim_entry.py`) or through a
default value that encodes apparatus semantics (`config.py`'s defaults are
the QTA apparatus: 10 mK fridge, fs laser, diamond NV). Both are listed by
hand in section 3. The data-reference scan cannot see a file name assembled
at run time.

## 2. Reachability, by core

### 2.1 Agent core (`qta_agent/`, 31 modules): no hardware dependency

The import closure of every `qta_agent` module reaches **no HW module and
names no hardware data file.** The only module outside `qta_agent` it
reaches is `qta_multiphysics/stack/workspace.py`, the output-location guard,
which is generic (it refuses writes to the repository root and to tracked
canonical directories).

The coupling that remains is naming and payload, not import:
`governed_stage10.py` (with `_stage10_tool.py`, `_stage10_index_tool.py`) is
the substrate's only production consumer, and what it governs is "a real
Stage-10 run" -- the QTA stack artefacts. That is the thing Phase 2 turns into
a governed `ScientificModel` run. `_stage10_index_tool.py` mentions
`results_gate_table.csv` in its docstring as an example input, and reads
nothing by that name.

### 2.2 Release core: no hardware import, a hard-wired QTA payload

`release_trust.py`, `verify_release.py`, `generate_manifest.py`,
`ro_crate_tools.py`, `build_release_artifacts.py`,
`finalize_release_signing.py`, `release_revision_gate.py`, `build_hdf5.py`,
`build_hdf5_mapping.py`, `validate_hdf5_equivalence.py`: the import closure
is these ten modules and nothing else. But the payload they expect is QTA's:

| module | hardware expectation |
|---|---|
| `generate_manifest.py` | `DERIVED_STATE_FIELDS = ("total_gates", "PASS", "CONDITIONAL", "BLOCKED", ...)`; refuses a manifest whose derived PASS != 0 |
| `build_release_artifacts.py` | `"qta_claims": {"scientific_gate_PASS_count": 0, ...}` in the release index and provenance |
| `build_hdf5.py` | refuses a gate table with any PASS; stamps `scientific_gate_PASS_count = 0` |
| `build_hdf5_mapping.py` | classifies "the 88 governed outputs" |
| `ro_crate_tools.py` | names `experiment_registry.json`, `experiment_gate_coverage.json`, `experiment_matrix_coverage.json`, `campaign_registry.json`, `validation_matrix.csv`, `results_gate_table.csv` |
| `verify_release.py`, `validate_hdf5_equivalence.py` | name `results_gate_table.csv` |

`release_trust.py`, `release_revision_gate.py` and
`finalize_release_signing.py` carry none of it: the trust machinery proper
is already generic. The coupling is in what is released, which is the
directive's "machine-specific release expectations" (Phase 7).

### 2.3 Scientific core: every model imports the gate machinery

**Import-time, measured and then confirmed by importing it:**

```
$ python -c "import sys, qta_multiphysics.thermal_1d; print(...)"
['qta_multiphysics.metrics', 'qta_multiphysics.runner']
```

`qta_multiphysics/__init__.py` does `from .runner import run_all`, and
`runner` imports `metrics`, which assembles the QTA gate specifications. So
**every** module in `qta_multiphysics` -- `thermal_1d`, `grids`, `units`,
every physics model and every stack adapter -- loads the gate machinery the
moment it is imported, because importing any submodule runs the package
`__init__` first. No physics module *uses* it; they carry it. That is the
directive's sentence made literal: a thermal solver that cannot be imported
without the code that knows a QTA gate is BLOCKED. It is the first cut (C1).

Beyond that shared edge, import-time reachability of the rest of the
ontology:

| module | import-time | lazy (call path only) |
|---|---|---|
| `campaign_state_3d` | `machine_fsm`, `mode_sequence_3d` | `state_machine_3d` |
| `campaign_uncertainty_3d`, `cryopanel_dynamics_3d`, `provenance_3d`, `species_accounting_3d` | `mode_sequence_3d` | `state_machine_3d` |
| `falsification_3d`, `nv_eligibility_3d`, `sources_3d` | `mode_sequence_3d`, `state_machine_3d` | -- |
| `runner_3d` | `mode_sequence_3d`, `state_machine_3d` | `machine_fsm` |
| `deep_expdesign/*` (11 modules through the package) | -- | `design`, `design.registry`, `design.validation` |
| `qta_full_sim` | `design/*`, `integrated_layers`, `mode_sequence_3d`, `state_machine_3d` | `machine_fsm` |
| `qta_sim_stages`, `tools/clip_provenance` | -- | all of the above |
| `package_consistency_check` | `machine_fsm` | -- (plus a subprocess run of `qta_full_sim`) |

**The experimental-design layer, precisely.** `deep_expdesign` imports
cleanly. Its end-to-end runner (`run_deep_expdesign`) calls
`design_space.load_interlocks()`, which imports `qta_multiphysics.design` and
reads the apparatus registry for "forbidden simultaneous states", and
`design_space.load_design_families()`, which takes the candidate experiments
from `expdesign.model` -- i.e. from `FIRST_VALIDATION_EXPERIMENTS.md` and
`kill_gate_ranking.csv`. The library pieces (posterior model, calibration,
OOD, transforms, EIG estimators) run without either; the layer as a
capability does not.

**Clear of the rest of the ontology** (beyond the shared `metrics` edge):
every physics model (`thermal_1d`, `thermal_2d_axisymmetric`,
`thermal_3d_transient`, `boundaries_3d`, `laser_source*`,
`optical_absorption*`, `surface_coverage*`, `gas_transport_1d/2d`,
`species_transport_3d`, `radiation_paths*`, `vibration_*`,
`microwave_heating_*`, `material_models`, `materials_3d`, `heat_switch_3d`,
`nv_spin/*`), all numerical infrastructure (`fields`, `grids`, `numerics`,
`units`, `exports`, `mesh_3d`, `convergence_3d`, `energy_accounting_3d`,
`reduction_checks_3d`, `verification*`, `uncertainty`, `sensitivity_3d`),
every stack adapter, `coupled_mode_solver`, and `expdesign/*`.

**Coupled by data or semantics rather than import** (the graph alone would
miss these):

| module | coupling |
|---|---|
| `expdesign/model.py` | parses `FIRST_VALIDATION_EXPERIMENTS.md`, names `kill_gate_ranking.csv` |
| `expdesign/runner.py` | writes `validation_experiment_ranking.csv`, names `experiment_falsification_map.csv` |
| `nv_spin/runner.py` | refuses a Mode D spin calculation until Mode C readiness holds; the NV model and sequences are clear |
| `coupled_mode_solver.py` | the B->C->D chain is hard-wired; the physics it chains is clear |
| `deep_expdesign/likelihood_model.py`, `trainer.py` | the generative model samples a "Mode-D sensing design distribution" |
| `measurement_ingest_3d.py` | QTA-specific quantities; a `SYNTHETIC`-only data class |
| `uncertainty.py` | Monte Carlo over the QTA parameter registry, scored as gate failure rates |
| `config.py` | the defaults are the QTA apparatus |
| `stack/fmi_contract.py` | the deferred FMI contract is written against the machine FSM |

### 2.4 Tools

`tools/clip_provenance.py` imports `integrated_layers` and `runner_3d`, and
so reaches `design`, `machine_fsm`, `metrics`, `mode_sequence_3d` and
`state_machine_3d`. Every other tool's import closure is clear;
`cross_env_semantics.py`, `resolution_inventory.py` and `unit_inventory.py`
operate over the QTA output set by data.

## 3. Dependencies the graph cannot see

* **The byte gate (R59).** `stack-verify.yml` and `agent-substrate.yml` run
  `package_consistency_check.py`, which reruns `qta_full_sim.py` and
  requires 88 outputs byte-identical to the committed ones. That makes the
  QTA output corpus a CI requirement for every change, physics or not.
  Its known red on runners of a different CPU dispatch is floating-point
  digits, never a decision (23 files, 0 decision-bearing tokens at `71b58cb`).
* **Completion model.** `docs/completion_matrix.json` and
  `package_consistency_check.py` encode 83 gates / 0 PASS / 47 CONDITIONAL
  as a framework-level expectation.
* **An undeclared direct dependency (D-2026-74, open).** Two test modules
  (`tests/test_bootstrap_workflow_contract.py`,
  `tests/test_release_workflow_contract.py`) `import yaml`, and
  `pyproject.toml` does not declare PyYAML. It arrives only because
  `snakemake` (workflow group) pulls it through `conda-inject` and `yte`. A
  local environment synced without that group fails collection; CI passes
  because it syncs `--all-groups`. Retiring or rewriting the Snakemake
  workflow (Phase 6) would silently delete a dependency two trust tests rely
  on. The fix is to declare it in the `dev` group, which regenerates
  `uv.lock` and, through it, the SBOM -- deferred to the next tranche so the
  lockfile change is reviewed on its own.

## 4. Cutover order

Each step names what is cut, what replaces it, and the check that says it
stayed cut. None of these has happened.

| # | phase | cut | replaced by | stays cut because |
|---|---|---|---|---|
| C1 | 2 | the package `__init__`'s eager `from .runner import run_all`, which makes every model import `runner` and `metrics` | a lazy entry point; plus `ScientificModel`, `ModelRegistry`, `ResultBundle`, `VerificationResult`, `Observation`, run identity, with thermal 1D adapted as the proving case | a new test that imports each registered model in a fresh interpreter and asserts no HW module is in `sys.modules` -- measured, not inferred from the graph |
| C2 | 3 | `design_space.load_interlocks() -> qta_multiphysics.design` and `load_design_families() -> expdesign.model` on the deep runner's call path | design constraints declared by the model's applicability domain and a typed experiment declaration | the C1 test extended to the runner's call path; `tests/test_deep_*` exercised against a non-QTA toy model as well as the QTA reference |
| C3 | 3 | `expdesign.model` parsing `FIRST_VALIDATION_EXPERIMENTS.md` / `kill_gate_ranking.csv` | candidate experiments as typed `Observation`-producing declarations | data-reference scan: `expdesign` names no hardware data file |
| C4 | 3 | `uncertainty.py`'s gate-failure scoring | generic UQ engine over `ScientificModel` responses; parameter / numerical / model-form / observational / surrogate uncertainty kept separate | the MC determinism and failed-sample tests, rewritten generically, keep their seeds and byte determinism |
| C5 | 4 | Mode-letter phase labels in plugins (`gas_transport_1d`, `species_transport_3d`, `material_models`, `surface_coverage_3d`, `cryopanel_dynamics_3d`) | a declared phase schedule passed as input | the plugin's own physics tests unchanged and green |
| C6 | 4 | `coupled_mode_solver`'s hard-wired B->C->D; `nv_spin/runner`'s Mode C readiness gate | a phase schedule; readiness as a precondition the caller declares | reduction / conservation / convergence tests unchanged |
| C7 | 5 | HW modules and hardware data retire to history; tests protecting only retired hardware invariants retire with a documented reason | -- | C1 test covers the whole tree except `attic/` |
| C8 | 6 | `qta_full_sim.py` / `Snakefile` / `package_consistency_check.py`'s 83-gate and PASS=0 checks | validate -> run -> invariants -> independent verifier -> UQ -> serialize -> provenance -> evidence -> authority | the byte-regeneration verifier kept, over the new declared output set |
| C9 | 7 | `DERIVED_STATE_FIELDS`, `qta_claims`, the 88-output HDF5 classification, the RO-Crate's QTA registries | a release set derived from `ResultBundle`s | `verify_release.py` hostile/offline suites unchanged |
| C10 | 3-7 | declare PyYAML (D-2026-74) before C8 removes `snakemake` from anything | -- | a collection test under a `dev`-only sync |

## 5. Is any retained scientific capability still blocked?

Yes. At `71b58cb` and after this tranche, the retired ontology still blocks:

0. **Every model, at import** -- importing any `qta_multiphysics` module
   loads the gate-spec assembly (C1). Nothing is prevented from running;
   nothing can be imported without it.
1. **Bayesian / deep experimental design** -- the end-to-end deep runner
   reads the apparatus design registry for its interlocks and takes its
   candidates from the QTA validation experiments (C2); `expdesign` draws
   its candidates from the same experiments and the gate kill ranking (C3).
2. **Monte Carlo / UQ outputs** -- the engine is generic, but what it
   reports is gate failure rates over the QTA registry (C4).
3. **Adaptive campaigns and campaign-level UQ** -- through the machine FSM
   (C5-C6).
4. **The coupled multi-phase solve and NV spin orchestration** -- through
   fixed Mode B->C->D semantics, not through their physics (C6).
5. **Release and provenance** -- the release set is the QTA output corpus
   and its claims (C9), and CI's byte gate makes that corpus a requirement
   for every change (C8).

Not blocked beyond (0): every physics model, all numerical infrastructure,
and every stack adapter (FEniCSx, SALib, OpenMDAO, VTK, OpenUSD, Rust,
read-only RAG) reach no ontology module other than through the package
`__init__`. SALib and OpenMDAO are wired to the QTA thermal
ROM rather than to a model contract; that is a Phase 3 rewrite, not a
block.
