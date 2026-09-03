# CP-M12 — controlled source reconstruction from raw mutation evidence

## Classification

`CONTROLLED_RECONSTRUCTION_FROM_EXACT_MUTATION_PAYLOADS`

This is not claimed to be the historical `7216562` Git object because that object/tree was not independently supplied. The reconstruction does, however, use raw exported Claude tool payloads rather than prose summaries.

## Starting source boundary

The reconstruction uses the authenticated 437-file CP-M11 P5 source state restored from `QTA_CP_M11_BOUNDARY_RECOVERY_INPUTS.zip`.

Verified starting source boundary:

- HEAD: `09b39da3a91c55a13dc2ff7c2c02e2c14b6c42f1`
- HEAD tree: `8e6fdd890db04d36abbb2d8d365f5ac7a21e33dd`
- staged source tree: `654295a72603f48af88800f5b13c6c1fc9e1f009`
- tracked files: `437`
- staged paths: `37`
- additions / modifications: `12 / 25`
- staged-diff SHA-256: `bb987d341e38b8ba273c6cee17c88954dbbe55665ebc5f6498be40317215124e`

This source content matches the historical CP-M12 directive's expected P5 predecessor in tracked-file topology, but its Git metadata is a later recovery lineage. Therefore it is a controlled reconstruction base rather than proof of historical commit identity.

## Raw evidence used

`QTA_CLAUDE_EXPORT_MESSAGE_INDEX_V2.json` preserves the CP-M12 owner directive and raw non-thinking tool blocks at:

- directive: `conversations[5].chat_messages[331]`
- first implementation message: `conversations[5].chat_messages[332]`
- continuation/fix/closure message: `conversations[5].chat_messages[334]`

The two implementation messages contain 15 visible tool-use blocks total. Relevant mutation evidence includes:

- complete `create_file` payload for `qta_multiphysics/chemistry/schema.py`;
- complete `create_file` payload for `qta_multiphysics/chemistry/scenarios.py`;
- exact `printf` creation of `qta_multiphysics/chemistry/__init__.py`;
- complete `create_file` payload for `build_p4_chemistry.py`;
- complete `create_file` payload for `tests/test_chemistry_scaffolding.py`;
- exact Python source-to-source corrections to the tests;
- exact Python source-to-source corrections to `scenarios.py` and `build_p4_chemistry.py` after mypy findings;
- exact deterministic write for `p5_p13_clarification.json`;
- exact authority/document/ledger mutations recorded in the same CP-M12 correction command;
- deterministic P4 artifact generation;
- historical verification commands and outputs.

## Replayed source behavior

The raw source writes and deterministic patch bodies were replayed in their original semantic order into an isolated copy of the authenticated 437-file source state.

Local replay result:

- `python build_p4_chemistry.py`: PASS
  - 8 records;
  - 0 numeric chemistry values;
  - 11 hooks;
  - 3/7 temperature scenarios resolved;
  - thresholds for `0.012`, `0.16`, `0.21` eV generated.
- `python -m pytest tests/test_chemistry_scaffolding.py -q`: **31 passed**.
- deterministic P5 regeneration: 5/5 benchmarks passed; 3 newly executable; Knudsen remained `NOT_EVALUABLE`.
- RO-Crate local validation after regeneration: VALID / zero reported problems.

The host did not have the historical uv-managed Python/cache available; `uv sync --frozen` attempted to download the required standalone Python and failed because the analysis container has no DNS/network path. Therefore historical uv/ruff/mypy/full-suite execution was not re-claimed in this replay. System Python was used only to exercise the reconstructed source where its installed dependencies were sufficient.

## Strong topology match

After replaying the CP-M12 source mutations and deterministic P4 outputs, the isolated source contains exactly:

- **454 tracked files**

This matches the raw historical CP-M12 closure output and journal, which reported:

`CP-M12 7216562 COMMITTED | tracked 454`

The historical tool result also records:

- chemistry suite initially 2 failed / 28 passed;
- exact test corrections;
- final chemistry suite 31 passed;
- final mypy: success after two exact corrections;
- Ruff: all checks passed;
- core/layers/three_d staged runs successful;
- 88/88 governed-output comparison;
- package consistency 102/0;
- selected pytest 210 passed;
- Stage-6 preservation green;
- scientific PASS 0;
- readiness expected fail-closed;
- historical detached manifest hash beginning `befda37feef5...`.

The controlled replay's regenerated manifest hash differs from the historical reported hash, so **historical byte/tree identity is not claimed**. That difference is expected to be investigated through reconstruction of the exact earlier Stage-9→CP-M11 historical predecessor rather than hidden by editing toward the historical hash.

## Recovered CP-M12 scientific/epistemic semantics

The exact CP-M12 clarification artifact states:

- P5 made 3 parameters executable:
  - `mp_stick_CH4`
  - `mp_Edes_CH4`
  - `mp_Edes_He4`
- P13 screened count increased from 12 to 14, not 15;
- `mp_Edes_He4` has a valid `surface_coverage.CoverageSpec.E_des_J` argument path;
- its fully coupled interpretation remains `BLOCKED` because no governed helium transport-derived flux and no governed initial coverage exist;
- `theta0 = 1.0` is a diagnostic reference monolayer condition, not a modeled helium-delivery prediction;
- classification: `executable_without_sufficient_interpretation`.

The chemistry package deliberately implements scaffolding and authority semantics rather than unsupported reaction kinetics. The exact tool output reports 8 chemistry records with zero numeric chemistry values and 11 hook records.

## Comparison with current `main`

Live current-main code search found no `qta_multiphysics/chemistry/schema.py` and no `ChemStatus` implementation. `p5_p13_clarification.json` is also absent from current-main code search.

However, CP-M12 cannot be treated as wholly missing: current `main` still carries `mp_Edes_He4` through multiple evolved paths, including `qta_full_sim.py`, parameter/source-gap registries, assumed-parameter records, Monte Carlo registry, and parameter-semantic tests. Therefore recovery must port the **missing interpretation/authority layer** without overwriting stronger current implementation or pretending the old module layout is still canonical.

## Integration decision

Do **not** copy the historical CP-M12 package directly into current `main` yet. Its original implementation imports historical `qta_multiphysics.materials.schema.MatStatus`, while current `main` has evolved away from that historical package topology. Direct insertion would be architecture regression risk.

Next integration step:

1. reconstruct CP-M13→CP-M26, especially the later surface-temperature, coupled-mode, interpretation-guard, G-062B and experiment-requirement semantics;
2. determine the final historical form of the chemistry/interpretation authority layer;
3. compare that final semantic layer against current-main modules;
4. port only still-missing governed semantics into current architecture;
5. add current-architecture tests proving executable chemistry cannot silently become physically interpretable/validated.

Scientific PASS remains zero. No reaction kinetics, experimental validation, or hardware authority are created by this reconstruction.
