# Test execution contract

The harness's tests are `tests/`, run with `uv run python -m pytest tests/ -q`.
The mutation specifications in `tools/mutations/` run in CI's sharded matrix,
and the verifiers -- the repository content audit, the forensic inventory,
the claims boundary, the completion matrix, the identity inventory, the
workflow contract, the derived status -- are CI steps of their own
(`.github/workflows/agent-substrate.yml`).

## What a green suite means

Software verification only. A passing suite says the code does what its
tests say; it never turns a simulation into a measurement, and a check that
passes is not experimental validation (`CLAIMS_BOUNDARY.md`).

## Every module collects on its own

`tests/` has no `__init__.py`; `conftest.py` at the repository root puts the
root on `sys.path` before anything is collected, and
`tools/test_isolation.py` (a CI step) collects every test module alone, so a
suite whose green depends on collection order is caught (D-2026-63).

## Order independence

The suite is run both forward and in reverse file order, and both must
collect and pass the same number of tests. A difference means a test is
leaking state into another.

```sh
uv run python -m pytest tests/ -q
uv run python -m pytest $(ls tests/test_*.py | sort -r) -q
```

## A test must not damage what it proves cannot be damaged

Tests that try to write a tracked file through a guard restore it in a
`finally`, because under mutation testing the guard is removed and the write
succeeds -- that is how the mutation is killed. Probe files are written
under `verification/` or `tmp_path`, never into the tracked tree, and the
mutation harness refuses a run that leaves a tracked file changed.

## Required runtimes turn a skip into a failure

Several suites skip when an optional runtime is absent. CI sets the
variables that make the runtime REQUIRED where it is installed, so a job
that synced the wrong environment cannot go green by skipping:
`HARNESS_NEURAL_REQUIRED=1` (JAX), `HARNESS_UQ_REQUIRED=1` and
`HARNESS_VIZ_REQUIRED=1` (SALib, OpenMDAO, usd-core),
`HARNESS_INTEGRATIONS_REQUIRED=fenicsx|fmi`, and `HARNESS_HISTORY_REQUIRED=1`
(the full-suite job, which fetches history) for the tests that check a
record against the commit it names.

## Mutation testing

`tools/mutation_matrix.py <spec>` applies each mutation in a specification
to its source, runs the named suites, and requires a failure; it refuses to
start against a red or non-deterministic baseline, refuses an anchor that
no longer matches exactly once, and refuses a run that does not restore the
source byte for byte. A surviving mutation fails the run.
`tools/mutation_shards.py` plans the specifications into the shards CI runs,
every specification in exactly one.

## Long-running evidence

`tests/test_agent_long_horizon.py` runs at a default scale with the suite;
CI raises it with `HARNESS_HORIZON_CYCLES`. The bounded model check
(`tools/model_check.py`) and the fuzz campaign (`tools/fuzz_substrate.py`)
are CI steps with stated floors, so an empty campaign cannot read as a clean
one.
