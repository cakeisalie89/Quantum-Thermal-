# Authorities -- single sources of truth

`authorities.json` is the machine-readable registry: every governed concept of
the framework, the one executable authority for it, what that authority means
and does not mean, and the tests that hold it. Code is the authority;
documentation mirrors it and never overrides it. A concept with two sources
is a conflict to resolve, not a choice (`authorities.json ->
competing_sources_record`).

| Concept | Authority | What it answers |
|---|---|---|
| event history | `qta_agent/events.py` | what happened, in order, tamper-evident: every other state below is a projection of it |
| authority fsm | `qta_agent/authority.py` | which claims are PROPOSED, VERIFIED, PROMOTED or withdrawn, and who moved them |
| scientific admission | `qta_agent/result_rules.py` | that a scientific_result's evidence -- a PASS from an independent implementation about this exact bundle, captured by governed tasks -- supports VERIFIED or PRO |
| task fsm | `qta_agent/tasks.py` | whether a governed tool run was executed under lease and verified by an actor other than its executor |
| scheduler fsm | `qta_agent/scheduler.py` | which work is ready, running, finished or failed, across process deaths |
| memory fsm | `qta_agent/memory.py` | what an agent was told and still holds |
| evidence store | `qta_agent/evidence.py` | the exact bytes a cited digest names (artifact integrity) |
| model registry | `scientific/registry.py` | which models exist, at which version, with which declared inputs |
| scientific result schema | `scientific/result.py` | what a model run produced, with its invariants, provenance and run identity |
| verification schema | `scientific/verification.py` | what a bounded check established, and how independent it was |
| observation schema | `scientific/observation.py` | what kind of thing a value is -- raw, processed, synthetic, simulated, derived, calibrated -- and what it may be derived from |
| run identity | `scientific/run_identity.py` | what counts as 'the same run' for reuse |
| framework boundary | `docs/framework_boundary.json + tools/framework_boundary.py` | that nothing active imports, opens or takes authority from legacy QTA |
| claims boundary | `CLAIMS_BOUNDARY.md (prose) + docs/claims_boundary.json + tools/claims_enforcement.py` | which kinds of result may never be presented as which others |
| release trust policy | `RELEASE_TRUST_ENFORCEMENT.md + verify_release.py` | who built which bytes, and that they are the bytes released |
| provenance records | `final_manifest.json (every git-tracked file except the two detached ones, per-file SHA-256) + manifest_hash.txt (detached SHA-256 of the manifest); generator generate_manifest.py (reads the git index). The file count is not restated here: it is whatever the git index holds, and generate_manifest.py --check is the authority on membership.` | these bytes were present at this SHA-256 |
| scientific stack adoption | `STACK.md (narrative) + stack.json (machine-readable, schema 1.0.0)` | additive and workspace-only: no stack module is imported by the solvers or qta_full_sim.py, every writer refuses the canonical tree, automatic_gate_effect=NONE, |
| agent authority substrate | `qta_agent/authority.py` | Enforcement is verified by mutation testing rather than by coverage: each check is deleted in turn and the suite must fail. tools/mutation_matrix.py exits non-z |

## Legacy

The QTA package's authorities -- modes and species, legal transitions states interlocks, device states per mode, units and physical parameters, solver profiles and tolerances, meshes, seeds, gates, schemas, canonical output paths, claim boundary wording, cryopanel dynamics, campaign continuity, campaign uncertainty, measurement ingestion, hardware governance, validation roadmap -- are in
`authorities.json -> legacy_authorities`, unchanged, and are authority for
nothing active (`docs/framework_boundary.json`). Their table as it was is
`docs/legacy/qta/AUTHORITIES.md`.
