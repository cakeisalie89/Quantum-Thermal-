# Scientific stack — adoption ladder (Stage 10)

Part of the governed scientific-agent framework. An adopted tool is a
checked claim; nothing on this ladder is a measurement or an authority.

`stack.json` (schema 1.0.0) is the machine-readable form of this document and
is checked against the code by `tests/test_stage10_stack.py`. Code is the
authority; both files mirror it and neither overrides it.

## 1. What "adoption" means here

Adding a tool to a governed scientific framework is a governance act, not a
convenience. Every element below is placed on one of three rungs, and the rung
is a claim that can be checked:

| Level | Meaning |
|---|---|
| **ADOPTED** | In use, exercised by CI or the workflow, behaviour verified in this repository. |
| **STAGED** | Interface and acceptance criteria are written and executable, but the tool is not installed, not exercised, and produces nothing authoritative. |
| **DEFERRED** | Deliberately not built. The contract is recorded so later work is implementation, not redesign. |
| **RESOLVED** | Decided by a measured rule: each candidate explicitly adopted or rejected, the decision committed and re-derived on a hosted runner; nothing is in force beyond what the decision says. |

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
| Reproducible container | ADOPTED | `Dockerfile`, `container_verify.sh`, `container-verify.yml` | built and run on a hosted runner, the harness and its demonstration compared with a native run under the equivalence policy; certifies the runs it hosts, never a result |
| pytest + Hypothesis | ADOPTED | `tests/` | software verification only |
| Ruff + mypy + Pydantic | ADOPTED | `pyproject.toml`, `stage7_boundary_models.py`, `stack/registry.py` | trusted-boundary validation; no competing vocabulary |
| HDF5 | ADOPTED | `hdf5_schema.json`, `build_hdf5.py` | representation only; equivalence checked, not assumed |
| RO-Crate | ADOPTED | `ro_crate_tools.py` | metadata packaging; adds no evidence |
| SLSA + Sigstore | ADOPTED¹ | `tools/supply_chain.py`, `supply-chain.yml` | a CI artifact signed with the workflow's OIDC identity and verified against the exact identity; a signature attests origin only, never scientific validity; **no SLSA level claimed, no release published** |
| Governed read-only retrieval | ADOPTED | `stack/rag_index.py` | deterministic offline BM25 over reviewed documents; no generation, no network, no model client, no embedding service |
| ParaView / VTK | ADOPTED | `stack/vtk_export.py` | serializes solved cell values unchanged |
| OpenUSD | ADOPTED | `stack/usd_export.py` | geometry representation; no prim is a solver input |
| SALib | ADOPTED | `sensitivity_3d.py` stays authoritative | cross-check only; disagreement is reported, not resolved |
| OpenMDAO | ADOPTED | `qta_full_sim.py` stays the operating-point authority | exploration only; every result is `NOT_A_RECOMMENDATION` |
| FEniCSx | ADOPTED¹ | the governed producers; FEniCSx is an independent check | executed from a hash-pinned environment as an INDEPENDENT_IMPLEMENTATION check of the transient slab; a verifier, never a producer |
| Selective Rust | **RESOLVED**² | the NumPy references | both kernels **REJECTED** by the measured rule; **no scientific path consumes Rust** |
| FMI 3.0 | ADOPTED¹ | an FMU result is NON_AUTHORITATIVE until checked and reviewed | one generic FMU built, loaded by fmpy, stepped, its state saved, restored and replayed; the legacy solvers are not exported |
| Authority substrate | ADOPTED | `qta_agent` | decides admission and computes no result; no scientific module imports it |
| Generic scientific layer | ADOPTED | `scientific` | models produce, checks verify, the store admits; imports no hardware ontology |
| AI proposal ingress | ADOPTED | `qta_agent/authority.py` | a proposal is non-authoritative data, reaching a decision only through a governed run, an independent check and a distinct reviewer |

¹ has open items — see §4.

² Decided by measurement (`docs/rust_kernel_decisions.json`, re-derived by
the hosted rust-kernels job): `face_conductance` is REJECTED for having no
production call site, `conductivity_power_law` for host-conditional parity,
no call site and a 0.61x workload speed. The bit-parity admission rule stays
in force for any later candidate. The Rust *backend* is not in force: no
solver, gate or canonical output imports `qta_kernels`, the default backend
is NumPy, an explicit Rust selection is refused unless a committed decision
is ADOPTED, and the crate is in neither the container nor `uv.lock`.

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

### Governed read-only retrieval — `stack/rag_index.py`
Governed read-only retrieval over reviewed documents: an offline,
deterministic Okapi BM25 index over the documents the corpus allowlist
admits (`docs/corpus_allowlist.json`, regenerated in the commit that changes
a document, so membership is reviewed). It was once called "read-only RAG";
there is no generative model, no embedding service and no model client in it,
so the name is retired. Retrieval returns **verbatim spans with citations**
(`path:line_start-line_end`) plus the SHA-256 of the source file at index
time, so a retrieved claim can be walked back and a stale index is detectable
rather than silently wrong. There is no generation step: a person -- or an
AI proposer, through `qta_agent/proposals.py`'s context assembly, which
records each hit's path and sha256 -- reads the cited span, and what it reads
is never authority. A test asserts the module imports no network or model
client, and every hit is stamped `RETRIEVED_TEXT_NOT_EVIDENCE`.

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

### FEniCSx — `scientific/checks/fenicsx_slab.py` — ADOPTED as an independent verifier
The harness runs FEniCSx from a hash-pinned conda-forge environment
(`integrations/fenicsx/`, created by `tools/fenicsx_env.py`) as an
INDEPENDENT_IMPLEMENTATION check of the transient slab: P1/P2 elements,
backward Euler, two refinements, admitted by the check contract and a distinct
reviewer, never as a producer. The acceptance campaign
(`tools/fenicsx_acceptance.py`) and the end-to-end demonstration run on a
hosted runner with the runtime REQUIRED. MPI is offered only self and
shared-memory transports, because UCX's network probe aborted MPI_Init on some
hosted runners (D-2026-124). One problem class, the slab, is covered.

What follows is the Stage-10 adapter for the legacy finite-volume backends,
`stack/fem_fenicsx.py`, which stays STAGED for that scope: dolfinx is not
wheel-installable and is in no project environment of the legacy pipeline, so
that adapter is staged. What exists now is the part that must exist *before*
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

**Both verdicts are conditional on the host's SIMD dispatch, and the table
above is this machine's.** The reference side of a bit-parity comparison is
NumPy, and NumPy's `**` loop moves with the CPU. Measured:

| NumPy SIMD in force | `conductivity_power_law` | max ulp | Backend `dispatch()` selects |
|---|---|---|---|
| `X86_V3+X86_V4` (AVX-512) | **REJECTED** | 2 | numpy |
| `X86_V3` only | **ADOPTED** | 0 | rust |

So which code computes thermal conductivity would be decided by the host,
not by the kernel — R59's divergence one level up, in a choice of
implementation rather than a printed digit. Nothing turns on it today
(`rust_kernel.py`'s own record states, and a sweep confirms, that no solver
imports these kernels), and the rule itself is unchanged and correct. What
changed is that `rust_kernel_status.json` now carries the dispatch every
verdict was measured under, so a report read on another machine is read as a
second measurement rather than as a contradiction. D-2026-58.

### The registry itself — `stack/registry.py`
`stack.json` is hand-editable and is read by the tests, which makes it a
trusted boundary in the same sense `stage7_boundary_models.py` uses the term,
so it gets the same treatment: a strict Pydantic model, extras forbidden,
statuses drawn from a closed vocabulary. Two rules are worth naming. The label
and `automatic_gate_effect` must be present and exact, so an edit cannot
quietly drop the claim boundary. And a STAGED, DEFERRED or
ADOPTED_ADMISSION_MECHANISM_ONLY element must list at least one open item —
"not adopted, nothing outstanding" is a contradiction, and rejecting it is
what keeps this ladder honest as it changes. ADOPTED and RESOLVED are
settled and need not. The Stage-10
modules themselves are held to the Stage-7 typing standard
(`disallow_untyped_defs`, `disallow_incomplete_defs`, `warn_unreachable`);
the legacy numerical tree keeps its documented typing debt.

### FMI — `scientific/fmi_boundary.py` — ADOPTED for one generic FMU
`integrations/fmi/thermal_rc2` is a generic two-node thermal RC network in C,
built against hash-checked FMI 3.0 headers. fmpy, from a hash-pinned lock in
its own runtime, validates, instantiates, configures, steps, saves and
restores its state in memory and as bytes, replays to identical outputs and
terminates; P2–P5 hold for it, and its fault twin is REJECTED by the
independent check. The result is NON_AUTHORITATIVE until checked and
reviewed. This runs on a hosted runner (the fmi and end-to-end jobs).

What follows is `stack/fmi_contract.py`, the interface contract for exporting
the *legacy* solvers as FMUs, which stays DEFERRED: the stack says "FMI later"; in a governed project that should mean the
interface is specified now and the blockers are named now. This module emits
an FMI 3.0 **interface contract** — variables, causalities, units,
co-simulation semantics — written as `modelDescription.contract.xml`, never as
`modelDescription.xml` inside a zip, so nothing can be mistaken for or
accidentally packaged as a working FMU. The instantiation token is derived
from the interface content, so it changes when — and only when — the interface
changes. Five prerequisites are open (§4); the load-bearing two are state
serialisation (FMI masters may roll a step back, and the integrator exposes no
serialisable state) and mode-boundary semantics (a communication step
straddling a phase boundary of a composed model would bypass the constraint
that boundary enforces). Neither is a packaging detail.

## 4. Open items

| Element | Open item |
|---|---|
| SLSA / Sigstore | no release is published and no tag created — publication needs the owner's separate authorization; **no SLSA build level claimed** |
| SALib | global vs. local ranking disagreement on the top parameter (§3) — open for human review |
| Selective Rust | `conductivity_power_law`'s parity is host-conditional — 2 ulp under AVX-512, bit-identical without it (D-2026-58) — but its decision is not: REJECTED in every dispatch (no call site, 0.61x). No solver imports either kernel |
| FEniCSx | one problem class (the transient slab); the 2D axisymmetric and 3D thermal models and the legacy backends have no FEniCSx check, and the legacy adapter stays STAGED |
| FMI | the legacy solvers are not exported: `fmi_contract.py`'s FMI-P1 to FMI-P5 stay open for them (the RC2 FMU meets them for itself) |

## 5. What would change a status

A STAGED element becomes ADOPTED when its acceptance criteria pass in an
environment the project can reproduce, and the run is recorded in the
workflow. A DEFERRED element becomes STAGED when every prerequisite is CLOSED.
Nothing on this ladder is authority: `automatic_gate_effect = NONE` for every
element, at every level, and a result becomes authority only through the
governed admission path (`AUTHORITIES.md`).
