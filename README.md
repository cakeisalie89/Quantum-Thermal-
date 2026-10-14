# Scientific AI Harness

A governed scientific-agent harness: event-sourced authority, replaceable
scientific models checked by independent implementations, a learned-model
substrate, and the software assurance that holds them.

Agents propose work; governed tools execute it under bounded, recorded
conditions; independent checks verify what the tools produced; and an
authority layer decides -- from evidence, by a party who is none of the
above -- what may be treated as a result. Every one of those steps is an
event in an append-only, hash-chained log, and every reader of that log
re-decides what it means rather than trusting what was written into it.

## Where things stand

`HARNESS_STATUS.md` is the completion status, derived by
`tools/harness_status.py` from the stack registry, the completion matrix,
the audits re-run and the learned-model evidence against a software
contract (`docs/harness_contract.json`); it is never written by hand.
`SCIENTIFIC_AI_STATUS.md` is the learned-model substrate's status,
generated from its committed evidence by `tools/neural.py status` and
checked by `tools/neural.py verify`: the ~1T architecture (counted exactly
and validated by abstract construction, never allocated or trained), the
development member that was trained and evaluated, what ran distributed
(simulated devices only), and what learned outputs may be
(non-authoritative).

## What this is not

It is not a measurement system and it reports no measurement. A simulation
result is not a measurement, independent numerical agreement is not
experimental validation, a signed artifact is not a correct one, and a
software completion status is not scientific validity: the full list, each
boundary held by named code and tests, is in `CLAIMS_BOUNDARY.md`. No
physical hardware exists in this repository.

## The architecture

`ARCHITECTURE.md` has the layers and the direction of every dependency
between them; in brief:

**The agent authority substrate** (`agent_substrate/`, `AGENT_SUBSTRATE.md`).
The log (`events.py`) is the only writable record; everything else is a
projection of it -- authority records (`authority.py`, `store.py`), durable
tasks (`tasks.py`, `governed_tasks.py`), governed model runs
(`governed_model.py`), the scheduler, policy, capabilities, secrets, egress,
memory, context, agents and the AI proposal ingress. A second, independent
implementation (`reconstruct.py`) re-reads the same log without importing
the first, and the two are compared.

**Scientific computing** (`scientific/`). Interfaces: `ScientificModel`,
`ModelRegistry`, `ResultBundle`, `VerificationResult`, `Observation`, run
identity, equivalence and ranking at declared resolution, sensitivity. A
model declares its inputs, runs, and returns a bundle with invariants and
provenance; a check with a different implementation verifies it. Three
models are admitted (`scientific/catalog.py`): a transient slab (checked by
a method-of-lines solve and, out of process, by FEniCSx; its exact series is
an analytic reference), a two-node thermal RC network (exported as an FMI
3.0 FMU and checked against its closed form), and Langmuir surface capture
(checked by Runge-Kutta integration of the law in its other form).

**Integrations** (`scientific/integrations/`, `integrations/`). VTK and
OpenUSD export of a solved field and its domain, OpenMDAO design-space
exploration, SALib global sensitivity, HDF5 result bundles, RO-Crate
packaging, FEniCSx and FMI runtimes in their own hashed environments. None
of them is authority: their outputs become results only through the
governed path above.

**The learned-model substrate** (`scientific_ai/`, `NEURAL_SUBSTRATE.md`).
NF-1T: a configuration-driven mixture-of-experts family whose ~1T flagship
is counted exactly and meta-validated, with one small member trained on a
governed dataset of the Langmuir model. A learned record can never become
VERIFIED or PROMOTED.

**Software assurance** (`tools/`, `tests/`). Mutation testing of every
enforcement point, fuzzing of every parser and trust boundary, a bounded
model check of the state machines, a long-horizon campaign, an active type
scope, the claims boundary, the completion matrix, and the repository
content audit.

## The trust model

Three meanings of "verified" are kept apart: *artifact integrity* (the same
bytes, a digest), *scientific verification* (a bounded check passed, a
`VerificationResult`) and *authority* (evidence judged sufficient, a record's
state). A `scientific_result` reaches VERIFIED only under the admission
policy `scientific_result.admission/1` (`agent_substrate/result_rules.py`):
a PASS from an independent implementation about that exact bundle, every
invariant holding, both documents captured by governed tasks, and the check
executed by none of the proposer, the decider and the model's executor. The
store decides it live and again on every replay; the independent reader
decides it a third time in its own code. Evidence that cannot be read makes
a result UNVERIFIABLE, never silently VERIFIED.

## The stack

`STACK.md` and `stack.json` record each element's state and the evidence
for it: FEniCSx as an independent verifier (ADOPTED, executed on hosted
runners, for one problem class: the transient slab), FMI 3.0 (ADOPTED, for
one generic FMU built, loaded, stepped and restored through fmpy), OpenMDAO
(ADOPTED, exploration only), SALib, VTK, OpenUSD, HDF5, RO-Crate,
containers, and keyless Sigstore signing of a CI artifact with an SBOM and
provenance (no SLSA level claimed, no release published).

## What was removed

The repository once carried a hardware-theory programme. It is retired and
removed from the tree; git history still holds it.
`docs/repository_inventory.json` classifies every file of the last commit
that carried it, with the rationale and the disposition, and
`tools/repo_content_audit.py` (a CI step) fails if any of it comes back.

## Running it

```
uv sync --frozen --all-groups
uv run python -m pytest tests/ -q
uv run snakemake --cores 1                    # the governed workflow (default target)
uv run snakemake --cores 1 governed_model     # a model run, checked and decided
uv run python tools/audit_log.py verification/stage10/governed_model/task_log.jsonl replay \
    --evidence verification/stage10/governed_model/evidence
uv run python tools/generic_consistency.py verification/stage10/governed_model/task_log.jsonl \
    --evidence verification/stage10/governed_model/evidence
uv run python tools/repo_content_audit.py --check
uv run python tools/harness_status.py verify
```

`INSTALL.md` has the details, `TESTING.md` the test discipline,
`AUTHORITIES.md` which module owns which concept, and
`docs/DEFECT_LEDGER.md` every defect found and how it was closed.

## Repository map

| Path | What it is |
|---|---|
| `agent_substrate/` | the authority substrate: log, projections, governed execution, second reader |
| `scientific/` | model, result, verification and observation interfaces; models, checks and integrations |
| `scientific_ai/` | the learned-model substrate (NF-1T) |
| `integrations/` | FEniCSx, FMI, RO-Crate and supply-chain runtimes and their locks |
| `tools/` | verifiers and generators: audits, status, mutation harness, fuzzing, supply chain |
| `tests/` | the suite; `tools/mutations/` holds the mutation specifications CI runs |
| `docs/` | registries, derived status, the inventory and content audit, the defect ledger |
