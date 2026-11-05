# Authorities -- single sources of truth

`authorities.json` is the machine-readable registry: every governed concept of
the harness, the one executable authority for it, what that authority means
and does not mean, and the tests that hold it. Code is the authority;
documentation mirrors it and never overrides it. A concept with two sources
is a conflict to resolve, not a choice (`authorities.json ->
competing_sources_record`, empty today).

| Concept | Authority | What it answers |
|---|---|---|
| event history | `agent_substrate/events.py` | what happened, in order, tamper-evident: every other state below is a projection of it |
| authority fsm | `agent_substrate/authority.py` | which claims are PROPOSED, VERIFIED, PROMOTED or withdrawn, and who moved them |
| scientific admission | `agent_substrate/result_rules.py` | that a scientific_result's evidence -- a PASS from an independent implementation about this exact bundle, captured by governed tasks -- supports VERIFIED or PRO |
| task fsm | `agent_substrate/tasks.py` | whether a governed tool run was executed under lease and verified by an actor other than its executor |
| scheduler fsm | `agent_substrate/scheduler.py` | which work is ready, running, finished or failed, across process deaths |
| memory fsm | `agent_substrate/memory.py` | what an agent was told and still holds |
| evidence store | `agent_substrate/evidence.py` | the exact bytes a cited digest names (artifact integrity) |
| model registry | `scientific/registry.py` | which models exist, at which version, with which declared inputs |
| scientific result schema | `scientific/result.py` | what a model run produced, with its invariants, provenance and run identity |
| verification schema | `scientific/verification.py` | what a bounded check established, and how independent it was |
| observation schema | `scientific/observation.py` | what kind of thing a value is -- raw, processed, synthetic, simulated, derived, calibrated -- and what it may be derived from |
| run identity | `scientific/run_identity.py` | what counts as 'the same run' for reuse: a RESOLVED backend, identical in every recorded part (D-2026-94) |
| claims boundary | `CLAIMS_BOUNDARY.md (prose) + docs/claims_boundary.json + tools/claims_enforcement.py` | which kinds of result may never be presented as which others |
| release trust policy | `docs/supply_chain_ci_policy.json + tools/supply_chain.py` | who built which bytes in CI, and that they are the bytes signed |
| provenance records | `final_manifest.json (every git-tracked file except the two detached ones, per-file SHA-256) + manifest_hash.txt (detached SHA-256 of the manifest); generator generate_manifest.py (reads the git index). The file count is not restated here: it is whatever the git index holds, and generate_manifest.py --check is the authority on membership.` | these bytes were present at this SHA-256 |
| scientific stack adoption | `STACK.md (narrative) + stack.json (machine-readable, schema 2.0.0, validated by tools/stack_registry.py)` | additive and workspace-only: no scientific model imports an integration, every writer refuses to write outside verification/, authority_effect=NONE, and the tre |
| agent authority substrate | `agent_substrate/authority.py` | Enforcement is verified by mutation testing rather than by coverage: each check is deleted in turn and the suite must fail. tools/mutation_matrix.py exits non-z |
| repository content | `tools/repo_content_audit.py + docs/repo_content_audit.json` | that no tracked file carries the retired hardware programme -- its paths, imports, gate dependencies or vocabulary -- and that every baseline file is classified |
