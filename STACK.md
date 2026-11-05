# Scientific stack — adoption ladder

Part of the Scientific AI Harness. An adopted tool is a checked claim;
nothing on this ladder is a measurement or an authority.

`stack.json` (schema 2.0.0) is the machine-readable form of this document,
validated by a strict model (`tools/stack_registry.py`) and held to this
page and to the code by `tests/test_stack_registry.py`. Code is the
authority; both files mirror it and neither overrides it.

## 1. What "adoption" means here

Adding a tool to a governed scientific harness is a governance act, not a
convenience. Every element below is placed on a rung, and the rung is a
claim that can be checked:

| Level | Meaning |
|---|---|
| **ADOPTED** | In use, exercised by CI or the workflow, behaviour verified in this repository. |
| **STAGED** | Interface and acceptance criteria are written and executable, but the tool is not installed, not exercised, and produces nothing authoritative. |
| **DEFERRED** | Deliberately not built. The contract is recorded so later work is implementation, not redesign. |
| **RESOLVED** | Decided by a measured rule: each candidate explicitly adopted or rejected, the decision committed and re-derived on a hosted runner; nothing is in force beyond what the decision says. |

Five invariants hold for every element, and are enforced in code rather than
asked for in prose:

1. **Additive.** No scientific model imports an integration, and no
   integration's output is a model input.
2. **Workspace-only writes.** Every writer routes its output through
   `scientific/workspace.py`, which permits exactly one subtree,
   `verification/stage10`, and refuses the repository root, the tracked
   tree, a path outside the repository and a symlink that resolves out.
3. **No authority effect.** `authority_effect = NONE` everywhere. No element
   can admit or promote a result, and none may emit a record claiming a
   measurement.
4. **Fail-closed optionality.** An absent optional package reports
   `availability = UNAVAILABLE`; absence is never recorded as a pass and
   nothing substitutes a different numerical result.
5. **Deterministic bytes.** Every artifact is reproducible byte-for-byte from
   the same inputs on the same numerical backend: fixed float formatting,
   sorted keys, no timestamps, no host paths, LF endings.

Verification for the whole workflow: `snakemake --cores 1`, whose final rule
re-checks every entry in `final_manifest.json` and asserts the tracked tree
was untouched.

## 2. The ladder

| Element | Status | Authority in force | Boundary |
|---|---|---|---|
| Python scientific core (NumPy/SciPy) | ADOPTED | `scientific/models/` | the arithmetic the models run on; a result is admitted only through an independent check |
| Snakemake | ADOPTED | `Snakefile` | orders the governed workflow; decides nothing |
| uv | ADOPTED | `uv.lock` + `pyproject.toml` | optional integrations are *extras*, so `uv sync --all-groups` stays lean |
| Reproducible container | ADOPTED | `Dockerfile`, `container_verify.sh`, `container-verify.yml` | built and run on a hosted runner, the harness and its demonstration compared with a native run under the equivalence policy; certifies the runs it hosts, never a result |
| pytest + Hypothesis | ADOPTED | `tests/` | software verification only |
| Ruff + mypy + Pydantic | ADOPTED | `typecheck_active.ini`, `tools/typecheck_scope.py`, `tools/stack_registry.py` | static discipline and trusted-boundary validation; no competing vocabulary |
| HDF5 | ADOPTED | `scientific/hdf5_bundle.py` | representation only; a bundle read back is checked against the one written |
| RO-Crate | ADOPTED | `ro_crate_tools.py` | metadata packaging; adds no evidence |
| SLSA + Sigstore | ADOPTED¹ | `tools/supply_chain.py`, `supply-chain.yml` | a CI artifact signed with the workflow's OIDC identity and verified against the exact identity; a signature attests origin only, never scientific validity; **no SLSA level claimed, no release published** |
| Governed read-only retrieval | ADOPTED | `agent_substrate/rag_index.py` | deterministic offline BM25 over reviewed documents; no generation, no network, no model client |
| ParaView / VTK | ADOPTED | `scientific/integrations/vtk_export.py` | serializes a solved cell field unchanged and reads it back; not evidence |
| OpenUSD | ADOPTED | `scientific/integrations/usd_export.py` | a model's domain and probes as geometry; no prim is a model input |
| SALib | ADOPTED | `scientific/sensitivity.py` | cross-check only; a disagreement is reported, not resolved |
| OpenMDAO | ADOPTED | `scientific/integrations/openmdao_doe.py` | exploration only, over declared in-bounds parameters; every result is `NOT_A_RECOMMENDATION` |
| FEniCSx | ADOPTED¹ | the governed producers; FEniCSx is an independent check | executed from a hash-pinned environment as an INDEPENDENT_IMPLEMENTATION check of the transient slab; a verifier, never a producer |
| FMI 3.0 | ADOPTED | an FMU result is NON_AUTHORITATIVE until checked and reviewed | one generic FMU built, loaded by fmpy, stepped, its state saved, restored and replayed |
| Authority substrate | ADOPTED | `agent_substrate` | decides admission and computes no result; no scientific module imports it |
| Generic scientific layer | ADOPTED | `scientific` | models produce, checks verify, the store admits |
| AI proposal ingress | ADOPTED | `agent_substrate/authority.py` | a proposal is non-authoritative data, reaching a decision only through a governed run, an independent check and a distinct reviewer |

¹ has open items — see §4.

**Retired:** selective Rust kernels. Both candidates were REJECTED by a
measured rule (no production call site; parity that depended on the host's
SIMD dispatch; a 0.61x workload speed), and they served only a solver that
was removed with the hardware programme. `stack.json` records the
retirement and its reason; no Rust scientific backend exists.

## 3. The elements, briefly

### ParaView / VTK — `scientific/integrations/vtk_export.py`
A solved cell-centred field is written as a VTK `RectilinearGrid`: node
coordinates are the cell *faces*, and every value is written as **cell**
data on the cell it was solved on, so nothing is resampled. VTK enumerates
cells x-fastest; the transpose is a named function with its own test.
ASCII `%.9e`; every array is read back and compared at a stated tolerance,
and a re-export reproduces every byte.

### OpenUSD — `scientific/integrations/usd_export.py`
A model's domain and its reported probes as a `.usda` stage. Boxes are
explicit meshes rather than a unit cube under a transform stack, so a
misread `xformOpOrder` cannot silently move geometry; `metersPerUnit` is
declared. `usd-core` is optional and validates the stage where installed;
its absence reads `UNAVAILABLE`, never `VALID`.

### Governed read-only retrieval — `agent_substrate/rag_index.py`
An offline, deterministic Okapi BM25 index over the documents the corpus
allowlist admits (`docs/corpus_allowlist.json`, regenerated in the commit
that changes a document, so membership is reviewed). Retrieval returns
**verbatim spans with citations** plus the SHA-256 of the source at index
time, so a stale index is detectable rather than silently wrong. There is no
generation step, and every hit is stamped `RETRIEVED_TEXT_NOT_EVIDENCE`.

### SALib — `scientific/sensitivity.py`
A one-at-a-time ranking at the base point and SALib's Sobol total effects
over a declared domain, compared at declared resolution
(`scientific/ranking.py`). A disagreement is `METHODS_DISAGREE` and
`REQUIRES_HUMAN_INTERPRETATION`; nothing chooses between them.

### OpenMDAO — `scientific/integrations/openmdao_doe.py`
A seeded Latin hypercube through OpenMDAO's DOE driver over a model's
declared, numeric, in-bounds parameters only; every case is re-run directly
and must agree byte for byte. The report is
`EXPLORATION_ONLY NOT_A_RECOMMENDATION`.

### FEniCSx — `scientific/checks/fenicsx_slab.py`
Run from a hash-pinned conda-forge environment (`integrations/fenicsx/`,
created by `tools/fenicsx_env.py`) as an INDEPENDENT_IMPLEMENTATION check of
the transient slab: P1/P2 elements, backward Euler, two refinements,
admitted by the check contract and a distinct reviewer, never as a
producer. The acceptance campaign (`tools/fenicsx_acceptance.py`) and the
end-to-end demonstration run on a hosted runner with the runtime REQUIRED.
MPI is offered only self and shared-memory transports (D-2026-124).

### FMI — `scientific/fmi_boundary.py`
`integrations/fmi/thermal_rc2` is a generic two-node thermal RC network in
C, built against hash-checked FMI 3.0 headers. fmpy, from a hash-pinned lock
in its own runtime, validates, instantiates, configures, steps, saves and
restores its state, replays to identical outputs and terminates; its fault
twin is REJECTED by the independent check. A unit crosses the boundary by
its definition, not its name (D-2026-128).

### The registry itself — `tools/stack_registry.py`
`stack.json` is hand-editable and read by the tests and the derived
status, which makes it a trusted boundary: a strict Pydantic model, extras
forbidden, statuses from a closed vocabulary, `authority_effect` exactly
`NONE`, and an element that is not settled must list what is outstanding.
A retired element says why, and is not also live.

## 4. Open items

| Element | Open item |
|---|---|
| SLSA / Sigstore | no release is published and no tag created — publication needs the owner's separate authorization; **no SLSA build level claimed** |
| FEniCSx | one problem class (the transient slab); the two-node RC network and the Langmuir capture model have no FEniCSx check |

## 5. What would change a status

A STAGED element becomes ADOPTED when its acceptance criteria pass in an
environment the project can reproduce, and the run is recorded in the
workflow. A DEFERRED element becomes STAGED when every prerequisite is
CLOSED. Nothing on this ladder is authority: `authority_effect = NONE` for
every element, at every level, and a result becomes authority only through
the governed admission path (`AUTHORITIES.md`).
