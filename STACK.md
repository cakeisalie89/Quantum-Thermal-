# QTA Scientific Stack — Adoption Ladder (Stage 10)

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

`stack.json` (schema 1.0.0) is the machine-readable form of this document and
is checked against the code by `tests/test_stage10_stack.py`. Code is the
authority; both files mirror it and neither overrides it.

## 1. What "adoption" means here

Adding a tool to a forecast-only project is a governance act, not a
convenience. Every element below is placed on one of three rungs, and the rung
is a claim that can be checked:

| Level | Meaning |
|---|---|
| **ADOPTED** | In use, exercised by CI or the workflow, behaviour verified in this repository. |
| **STAGED** | Interface and acceptance criteria are written and executable, but the tool is not installed, not exercised, and produces nothing authoritative. |
| **DEFERRED** | Deliberately not built. The contract is recorded so later work is implementation, not redesign. |

Five invariants hold for every element outside the numerical core, and are
enforced in code rather than asked for in prose:

1. **Additive.** No Stage-10 module is imported by the solvers, the gate
   logic, the Monte-Carlo layer, or `qta_full_sim.py`. Deleting
   `qta_multiphysics/stack/` changes no canonical output byte.
2. **Workspace-only writes.** Every Stage-10 writer routes its output
   directory through `stack.workspace.guard_output_dir`, which fails closed on
   the repository root, on any governed directory, and on any path outside the
   repository. Canonical outputs stay byte-gated.
3. **No gate effect.** `automatic_gate_effect = NONE` everywhere. No adapter
   can create, promote, or demote a gate, and none may emit a record with
   `measured_in_this_system = true`.
4. **Fail-closed optionality.** An absent optional package reports
   `availability = UNAVAILABLE` and names the in-repo authority that remains
   in force. It never silently substitutes a different numerical result, and
   absence is never recorded as a pass.
5. **Deterministic bytes.** Every artifact is reproducible byte-for-byte from
   the same inputs: fixed float formatting, sorted keys, no timestamps, no
   host paths, LF endings.

Verification for the whole stage: `snakemake --cores 1 s10_full`, whose final
rule re-checks every entry in `final_manifest.json` and asserts the canonical
tree was untouched.

## 2. The ladder

| Element | Status | Authority in force | Boundary |
|---|---|---|---|
| Python scientific core (NumPy/SciPy/QuTiP) | ADOPTED | `qta_multiphysics/`, `qta_full_sim.py` | the numerical authority; everything else is additive to it |
| Snakemake | ADOPTED | `Snakefile` | wraps authoritative commands; rewrites no solver |
| uv | ADOPTED | `uv.lock` + `pyproject.toml` | Stage-10 packages are *extras*, so `uv sync --all-groups` stays lean |
| Reproducible container | ADOPTED¹ | `Dockerfile`, `container_verify.sh` | fixed locale/TZ/hash seed/single-threaded BLAS |
| pytest + Hypothesis | ADOPTED | `tests/` | software verification only |
| Ruff + mypy + Pydantic | ADOPTED | `pyproject.toml`, `stage7_boundary_models.py`, `stack/registry.py` | trusted-boundary validation; no competing vocabulary |
| HDF5 | ADOPTED | `hdf5_schema.json`, `build_hdf5.py` | representation only; equivalence checked, not assumed |
| RO-Crate | ADOPTED | `ro_crate_tools.py` | metadata packaging; adds no evidence |
| SLSA + Sigstore | **STAGED**¹ | `RELEASE_POLICY.md`, `verify_release.py` | a signature attests origin only, never scientific validity |
| Read-only RAG | ADOPTED | `stack/rag_index.py` | retrieval only; no generation, no network, no model client |
| ParaView / VTK | ADOPTED | `stack/vtk_export.py` | serializes solved cell values unchanged |
| OpenUSD | ADOPTED | `stack/usd_export.py` | geometry representation; no prim is a solver input |
| SALib | ADOPTED | `sensitivity_3d.py` stays authoritative | cross-check only; disagreement is reported, not resolved |
| OpenMDAO | ADOPTED | `qta_full_sim.py` stays the operating-point authority | exploration only; every result is `NOT_A_RECOMMENDATION` |
| FEniCSx | **STAGED** | finite-volume backends | no FEM result is comparable until the acceptance criteria pass |
| Selective Rust | ADOPTED | the NumPy references | bit-for-bit parity or no adoption |
| FMI 3.0 | **DEFERRED** | none — no FMU exists | interface contract only; no binary, no compliance claim |

¹ has open items — see §4.

## 3. What Stage 10 added, and what it found

### ParaView / VTK — `stack/vtk_export.py`
The 3D mesh is genuinely rectilinear and graded, so it maps exactly onto a VTK
`RectilinearGrid`: node coordinates are the finite-volume *faces* and every
solver value is written as **cell** data on the cell it was solved on. Nothing
is resampled to points, so ParaView shows the finite-volume state itself.
ASCII `%.9e` — the same precision the project's CSV exports use — so a `.vtr`
and its CSV counterpart agree digit for digit, and a re-export reproduces
every byte. Ordering is the one real trap (VTK enumerates cells x-fastest; the
solver arrays are z-fastest), so the transpose is a named function with its
own property test rather than an inline `.ravel()`.

### OpenUSD — `stack/usd_export.py`
The resolved domain, the NV layer, the beam axis, the probe cell, and the
forecast hotspots as a `.usda` stage. Boxes are explicit 8-point meshes rather
than a unit cube plus a transform stack, so a misread `xformOpOrder` cannot
silently move geometry. The near field is micrometre-scale, so the stage
declares `metersPerUnit = 1e-06` and coordinates are micrometres; canonical SI
values ride along on `qta:` attributes. Verified to open in OpenUSD 26.8 with
all expected prims; `usd-core` is optional and its absence reads
`UNAVAILABLE`, never `VALID`.

### Read-only RAG — `stack/rag_index.py`
An offline Okapi BM25 index over the project's own governed documents (42
files, 1601 chunks). Retrieval returns **verbatim spans with citations**
(`path:line_start-line_end`) plus the SHA-256 of the source file at index
time, so a retrieved claim can be walked back and a stale index is detectable
rather than silently wrong. There is no generation step: the "G" in RAG is a
human reading the cited span. A test asserts the module imports no network or
model client, and every hit is stamped `RETRIEVED_TEXT_NOT_EVIDENCE`.

### SALib — `stack/sensitivity_salib.py` — **and its first finding**
`sensitivity_3d.py` (deterministic one-at-a-time +10% on the CI mesh) remains
the sensitivity authority. OAT is *local* and cannot see interactions, so
Sobol runs as a cross-check over the same four inputs and the same response
function — imported from `sensitivity_3d`, not re-implemented, so the two
cannot drift apart.

The first cross-check **disagrees**, and the disagreement stands as an open
finding rather than being resolved automatically:

| Rank | Canonical OAT (local) | Sobol total-effect (global) |
|---|---|---|
| 1 | `laser.absorbed_fraction` | `laser.spot_radius_m` |
| 2 | `laser.spot_radius_m` | `laser.absorbed_fraction` |
| 3 | `laser.absorption_coeff_1_m` | `laser.absorption_coeff_1_m` |
| 4 | `fridge.kapitza_coeff_W_m2_K4` | `fridge.kapitza_coeff_W_m2_K4` |

Kendall tau-b = 0.667; the bottom two agree, the top two swap. Read plainly:
over a ±10% box, the *global* variance in the forecast NV-probe rise is
dominated by spot radius, while a local one-sided perturbation ranks absorbed
fraction first. That is what a global method is for. The canonical ranking is
unchanged, and nothing about this promotes any gate — sensitivities are
properties of this model under these assumptions and are never experimental
importance. (Run on the reduced 6×6×8 screening mesh, which reproduces the CI
probe rise to ~2%: adequate for *ranking*, never for a reported value.)

### OpenMDAO — `stack/mdao_openmdao.py`
The thermal ROM as one `ExplicitComponent`, with a rule enforced in code:
**only DESIGN-provenance parameters may be design variables.**
`laser.spot_radius_m` is a knob the project can actually turn; the ASSUMED and
LITERATURE_BOUND quantities are *uncertainty*, and optimising over them would
quietly convert an assumption into a design decision, so
`assert_design_variables` refuses. Uncertainty gets a Latin-hypercube DOE
(sampled with SciPy so no extra DOE package is needed and the sweep is fixed
by bounds/samples/seed) and the report is a forecast *envelope*, which is what
a reviewer needs in order to disagree with the assumptions. Every record is
`NOT_A_RECOMMENDATION` and never touches
`best_forecast_operating_point.json`.

### FEniCSx — `stack/fem_fenicsx.py` — STAGED
dolfinx is not wheel-installable and is in no project environment, so the
adapter is staged. What exists now is the part that must exist *before*
adoption is discussable: four written acceptance criteria (manufactured-solution
convergence, reduction to `thermal_1d`, energy conservation, determinism) and a
solver-agnostic harness that runs today. CI proves the harness both reads zero
error for an exact solver and *detects* order — a second-order finite-volume
reference converges at 2.0000 on 20/40/80/160 cells. A harness that has only
ever seen an exact answer has never been tested.

### Selective Rust — `rust/qta_kernels` + `stack/rust_kernel.py` — **and its verdicts**
Rewriting numerics in a second language is a reproducibility risk before it is
a speed win: in a project whose outputs are compared by SHA-256, a last-ulp
disagreement is a broken build. So admission is mechanical — a kernel is
adopted **only if bit-for-bit identical** to the NumPy reference on a fixed
4096-value test vector — and it is per kernel, re-proved at process start, with
the Rust path off unless `QTA_RUST_KERNELS=1`. The crate is built on demand
(`maturin build --release`), is not in the container, and is not in `uv.lock`.

Both candidate kernels were built and checked; the rule did its job:

| Kernel | Max ulp difference | Verdict | Backend in force |
|---|---|---|---|
| `face_conductance` — `A / (dL/kL + dR/kR)` | 0 | **ADOPTED** | rust (when enabled) |
| `conductivity_power_law` — `k0 * (T/T_ref)**n` | 2 | **REJECTED** | numpy |

Pure division and addition reproduce exactly; `powf` against NumPy's `**`
does not (max relative difference 3.8e-16 — numerically negligible, and still
not adoption). "Close enough" is the standard this project cannot use.

### The registry itself — `stack/registry.py`
`stack.json` is hand-editable and is read by the tests, which makes it a
trusted boundary in the same sense `stage7_boundary_models.py` uses the term,
so it gets the same treatment: a strict Pydantic model, extras forbidden,
statuses drawn from a closed vocabulary. Two rules are worth naming. The label
and `automatic_gate_effect` must be present and exact, so an edit cannot
quietly drop the claim boundary. And a STAGED or DEFERRED element must list at
least one open item — "not adopted, nothing outstanding" is a contradiction,
and rejecting it is what keeps this ladder honest as it changes. The Stage-10
modules themselves are held to the Stage-7 typing standard
(`disallow_untyped_defs`, `disallow_incomplete_defs`, `warn_unreachable`);
the legacy numerical tree keeps its documented typing debt.

### FMI — `stack/fmi_contract.py` — DEFERRED
The stack says "FMI later"; in a governed project that should mean the
interface is specified now and the blockers are named now. This module emits
an FMI 3.0 **interface contract** — variables, causalities, units,
co-simulation semantics — written as `modelDescription.contract.xml`, never as
`modelDescription.xml` inside a zip, so nothing can be mistaken for or
accidentally packaged as a working FMU. The instantiation token is derived
from the interface content, so it changes when — and only when — the interface
changes. Five prerequisites are open (§4); the load-bearing two are state
serialisation (FMI masters may roll a step back, and the integrator exposes no
serialisable state) and mode-boundary semantics (a communication step
straddling a Mode B/C/D transition would bypass an interlock the FSM
enforces). Neither is a packaging detail.

## 4. Open items

| Element | Open item |
|---|---|
| Container | base-image digest still `UNRESOLVED`; pin at first pull (`container_verification.md`) |
| SLSA / Sigstore | neither `release.yml` nor `stack-verify.yml` has run on a hosted runner; their actions are still tag-pinned, and policy #3 requires commit-SHA pins before the first real run; no SLSA level claimed |
| SALib | global vs. local ranking disagreement on the top parameter (§3) — open for human review |
| Selective Rust | `conductivity_power_law` rejected on a 2-ulp `powf` difference; NumPy stays in force |
| FEniCSx | dolfinx unavailable; acceptance criteria 2–4 cannot run until a build exists |
| FMI | FMI-P1 state serialisation, FMI-P2 mode-boundary steps, FMI-P3 step-size independence, FMI-P4 claim-boundary survival, FMI-P5 unit round-trip |

## 5. What would change a status

A STAGED element becomes ADOPTED when its acceptance criteria pass in an
environment the project can reproduce, and the run is recorded in the
workflow. A DEFERRED element becomes STAGED when every prerequisite is CLOSED.
Nothing on this ladder can change a gate: `automatic_gate_effect = NONE` for
every element, at every level, and the scientific PASS count remains zero.
