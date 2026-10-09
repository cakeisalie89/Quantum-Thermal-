# Governed scientific-agent framework

A governed scientific-agent framework with event-sourced memory and
replaceable numerical models.

Agents propose work; governed tools execute it under bounded, recorded
conditions; independent checks verify what the tools produced; and an
authority layer decides -- from evidence, by a party who is none of the
above -- what may be treated as a result. Every one of those steps is an
event in an append-only, hash-chained log, and every reader of that log
re-decides what it means rather than trusting what was written into it.

## Where things stand

`SCIENTIFIC_AI_STATUS.md` is the current status of the Scientific-AI
harness, generated from the committed evidence by `tools/neural.py status`
and checked by `tools/neural.py verify`: the ~1T learned-model architecture
(counted exactly and validated by abstract construction, never allocated or
trained), the development member that was trained and evaluated, what ran
distributed (simulated devices only), and what learned outputs may be
(non-authoritative). The legacy QTA hardware forecast's gate table -- no
gate passes, because nothing was measured -- is reported there in its own
LEGACY_QTA_ONLY section; its PASS count is a fact about that forecast and
not a measure of this software.

## What this is not

It is not a measurement system and it reports no measurement. A simulation
result is not a measurement, independent numerical agreement is not
experimental validation, and a signed artifact is not a correct one: the
full list, each boundary held by named code and tests, is in
`CLAIMS_BOUNDARY.md`. No hardware exists in this repository.

## The architecture, in three parts

**The agent authority substrate** (`qta_agent/`, `AGENT_SUBSTRATE.md`). The
log (`events.py`) is the only writable record; everything else is a
projection of it -- authority records (`authority.py`, `store.py`), durable
tasks (`tasks.py`, `governed_stage10.py`), the scheduler, policy,
capabilities, secrets, egress, memory, context and agents. A second,
independent implementation (`reconstruct.py`) re-reads the same log without
importing the first, and the two are compared.

**The scientific package** (`scientific/`). Standard-library-only interfaces:
`ScientificModel`, `ModelRegistry`, `ResultBundle`, `VerificationResult`,
`Observation`, run identity. A model declares its inputs, runs, and returns
a bundle with invariants and provenance; a check with a different
implementation verifies it; observation kinds keep simulated and measured
values apart by lineage.

**Models** (`scientific/models/`, `scientific/checks/`, and retained physics
in `qta_multiphysics/`). Thermal 1D, thermal 2D axisymmetric and Langmuir
surface capture are behind the interface today, each with an independent
check (a 2D solver reduced, a 3D solver with adiabatic sides, the capture
law integrated by Runge-Kutta with the flux taken in its other form). The
last was extracted from a legacy component model, which now computes
through it. More retained physics is being extracted into the same shape.

## The trust model

Three meanings of "verified" are kept apart: *artifact integrity* (the same
bytes, a digest), *scientific verification* (a bounded check passed, a
`VerificationResult`) and *authority* (evidence judged sufficient, a record's
state). A `scientific_result` reaches VERIFIED only under the admission
policy `scientific_result.admission/1` (`qta_agent/result_rules.py`): a PASS
from an independent implementation about that exact bundle, every invariant
holding, both documents captured by governed tasks, and the check executed by
none of the proposer, the decider and the model's executor. The store decides
it live and again on every replay; the independent reader decides it a
third time in its own code. Evidence that cannot be read makes a result
UNVERIFIABLE, never silently VERIFIED.

## Active and legacy

This framework grew out of QTA, a hardware-era package forecasting a
cryogenic quantum-sensing apparatus. What of it is kept is dispositioned in
`FILE_DISPOSITION.csv`: ACTIVE framework code, TRANSITIONAL code being
rewritten or mined for generic physics, and LEGACY QTA kept for history and
equation extraction. `tools/framework_boundary.py` (a CI step) fails if
anything active imports, opens or takes authority from legacy QTA. The QTA
package's own documentation -- its gate table, machine modes and claims -- is
kept unchanged in `docs/legacy/qta/`, and its pipeline is still checked by the
legacy verifier `package_consistency_check.py`.

## Retained scientific capability

Physics models (thermal, gas and species transport, surface coverage,
radiation, optical, microwave, vibration), Monte Carlo and uncertainty
propagation, sensitivity analysis (SALib), design optimisation (OpenMDAO),
Bayesian experimental design, VTK and OpenUSD export, HDF5 representation,
and read-only retrieval over the governed documents. The harness adds an
independent FEniCSx verifier (ADOPTED, executed on hosted runners, for one
problem class: the transient slab), an FMI 3.0 runtime and boundary (ADOPTED
for one generic FMU built, loaded, stepped and restored through fmpy; the
legacy solvers are not exported), and selective Rust kernels (RESOLVED: both
REJECTED by a measured rule; no Rust backend is active). `STACK.md` and
`stack.json` record each element's state and the evidence for it. None of
them is authority by itself: their outputs become results only through the
governed path above.

## Running it

```
uv sync --frozen --all-groups
uv run python -m pytest tests/ -q
uv run snakemake --cores 1                        # the generic workflow (default target)
uv run snakemake --cores 1 s10_governed_model     # a model run, checked and decided
uv run python tools/audit_log.py verification/stage10/governed_model/task_log.jsonl replay \
    --evidence verification/stage10/governed_model/evidence
uv run python tools/generic_consistency.py verification/stage10/governed_model/task_log.jsonl \
    --evidence verification/stage10/governed_model/evidence
uv run python tools/framework_boundary.py --check
```

The legacy QTA pipeline stays invocable explicitly: `snakemake --cores 1
legacy_qta` (`workflow/legacy_qta.smk`), checked by the legacy verifier
`package_consistency_check.py`.

`INSTALL.md` has the details, `TESTING.md` the test discipline,
`AUTHORITIES.md` which module owns which concept, and
`ARCHITECTURE_CONVERGENCE_PLAN.md` the migration and what is still open.

## Repository map

| Path | What it is |
|---|---|
| `qta_agent/` | the authority substrate: log, projections, governed execution, second reader |
| `scientific/` | model, result, verification and observation interfaces; models and checks |
| `qta_multiphysics/` | retained physics and the stack adapters; legacy QTA orchestration being retired |
| `tools/` | verifiers: framework boundary, claims boundary, identity inventory, mutation harness |
| `tests/` | the suite; `tools/mutations/` holds the mutation specifications CI runs |
| `docs/` | registries, the defect ledger, and `docs/legacy/qta/` |
| `attic/` | earlier delivery bundles, hashed in the manifest and not part of the governed project |
