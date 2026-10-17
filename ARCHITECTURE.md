# Architecture

The Scientific AI Harness has four layers of code and one direction of
dependency between them. The direction is the design: a scientific result
must never depend on an authority verdict, so nothing that computes a result
imports anything that decides one.

```
                 tools/, Snakefile, tests/          (drivers and verifiers)
                   |            |           |
                   v            v           v
        agent_substrate/   scientific_ai/  scientific/
              |                 |              ^
              |                 +--------------+  identity, model, run identity
              +---------------------------------> scientific.workspace (only)
```

| Package | Owns | Imports |
|---|---|---|
| `scientific/` | models, independent checks, the result/verification/observation interfaces, run identity, equivalence, ranking, sensitivity, the workspace guard, integrations (VTK, OpenUSD, OpenMDAO, HDF5, FMI boundary) | NumPy/SciPy and its own modules; never `agent_substrate` or `scientific_ai` |
| `scientific_ai/` | NF-1T: configuration, exact accounting, the governed dataset, training and evaluation of the development member, status | `scientific` (identity, the model interface, run identity, the backend probe); never `agent_substrate` |
| `agent_substrate/` | the event log, every projection of it, authority, governed execution, the second reader | its own modules, plus `scientific.workspace` from three named bridge modules -- nothing else in `scientific` |
| `tools/`, `Snakefile`, `tests/` | drivers, verifiers, generators, the suite | anything; a tool that imports the substrate is listed by name with its reason (`tests/test_agent_substrate_isolation.py`) |

Each arrow is enforced by a test that parses the imports, lazy ones
included: `tests/test_agent_substrate_isolation.py` (the substrate's layering
inside the package, the three bridges and their single target, and that no
scientific module imports the substrate), `tests/test_neural_boundary.py`
(the learned-model substrate reaches no authority) and
`tests/test_typecheck_scope.py` (every package is under the active type
scope).

## How a result is made, checked and decided

1. **A proposal** enters through `agent_substrate/proposals.py`. It is
   non-authoritative data: a receipt on the log, nothing more.
2. **A governed run** (`agent_substrate/governed_model.py` over
   `governed_tasks.py`) executes a model named in `scientific/catalog.py`
   through `scientific/_governed_run.py`, by module string. The tool writes
   only through `scientific/workspace.py`, into `verification/stage10`; the
   result is a `ResultBundle` with its invariants and provenance, captured by
   digest on the log.
3. **An independent check** (`scientific/_governed_check.py` over
   `scientific/checks/`) re-derives the result by a different
   implementation -- a method-of-lines solve, FEniCSx out of process, an
   FMU stepped by fmpy, Runge-Kutta integration of the other form of the
   law -- and records a `VerificationResult`.
4. **A decision** by a reviewer who is none of the proposer, the executor
   and the checker. The store admits a `scientific_result` to VERIFIED only
   under `scientific_result.admission/1` (`agent_substrate/result_rules.py`).
5. **Re-decision.** The store decides again on every replay; the second
   reader (`agent_substrate/reconstruct.py`, which imports none of the
   first) decides a third time; `tools/independent_verify.py` does it in a
   separate process that refuses the primary reducers at import.

No step trusts a field written by an earlier one: a reader re-derives what
the log means. A checkpoint is a cache, never an authority.

## What is identified, and how

Scientific identity (`scientific/identity.py`) digests canonical JSON, and an
implementation's identity is the digest of its module closure's source. The
learned-model dataset pins the closure of the model it was generated from
(`docs/neural/dev/dataset_manifest.json`), so a change to that code is a
change of dataset identity, detected rather than inherited.

## Protocol identifiers

Some identifiers are wire formats rather than names: the event-attestation,
test-identity, key-registry and reference-signature schema strings, the
`stage10.*` event kinds and actors, policy rule ids, and the workspace path
`verification/stage10`. They are recorded in logs that must stay readable,
so they are kept verbatim even where the code that writes them was renamed.
`tools/repo_content_audit.py` declares them and strips them before it looks
for retired vocabulary, so a protocol identifier is never a finding and never
an excuse for one.

## What the layers are checked by

| Concern | Checked by |
|---|---|
| layering and import direction | `tests/test_agent_substrate_isolation.py`, `tests/test_neural_boundary.py` |
| every enforcement point bites | `tools/mutation_matrix.py` over `tools/mutations/*.json`, sharded in CI |
| parsers and trust boundaries | `tools/fuzz_substrate.py` |
| state machines | `tools/model_check.py` |
| no retired content returns | `tools/repo_content_audit.py --check` |
| claims stay inside their boundary | `tools/claims_enforcement.py`, `docs/claims_boundary.json` |
| completion is software completion | `tools/harness_status.py` against `docs/harness_contract.json` |
