# Reproducing what this repository claims

This guide is for someone who does not trust the people, the agents or the
CI runs that produced this repository, and wants to check its claims
anyway. Every step names the command, what it establishes, and what it does
not. Nothing here asks you to trust a CI log, an agent's explanation, or a
file the producer generated that a command below does not re-derive.

## 0. What you must trust

- **The commit.** Obtain its hash from a channel you trust, then check it
  out. Everything below is about the tree at that commit.
- **The interpreter and the lock.** Use Python 3.12 and `uv`, with the
  environment `uv.lock` pins (`INSTALL.md`). Steps 1 and 2 need only Python
  3 and its standard library.
- **Pinned external tools,** where a step uses one. Each is fetched and
  checked by sha256 before it runs: TLC for the TLA+ models, the FEniCSx
  lock, and the RO-Crate validator.

## 1. Without the repository: the portable assurance package

`docs/assurance/package/slab-mid-plane/` packages one governed answer: does
the transient slab's mid-plane temperature stay below 1000 K at the
documented point? Copy that directory and `tools/verify_assurance.py`
anywhere, and run:

    python3 -I verify_assurance.py slab-mid-plane \
        --expect-package-digest <sha256 of its SHA256SUMS, from your clone>

**What it establishes.** The verifier imports nothing beyond the standard
library and checks seven levels:

- the files are the listed ones;
- the record agrees with its evidence;
- the event log is one hash chain;
- the history declares the key and role registries the package carries,
  and every record is signed by a key that registry holds for its actor;
- every act the role registry governs was made by a party a grant names,
  for that role, over that scope, at that position;
- the reported decisions are in that chain;
- its own closed-form solution of the slab equations agrees with the
  model's numbers and gives the recorded verdict.

**What it does not establish.** It lists these on every run: physical
validity (no real slab was observed), the registries' origin, the custody
of the keys (every key is a TEST identity, whose secret anyone can derive
from its name), the verdict's authority, the producer's toolchain, and
more. The package's own `README.md` walks through
the reproduction, including corrupting a file to watch the refusal.

## 2. With the repository: re-derive what it records

    uv sync --frozen --all-groups

Each command below re-computes something the tree records, and fails if the
record differs. CI runs every one of them on every push. Run them yourself:
the CI logs are not evidence.

**Scientific results.** All are MODEL_ONLY: no measurement of the world
exists here.

- `uv run python tools/slab_uq.py --check` re-derives the slab's error
  budget. Each contribution says whether it was measured, estimated,
  assumed or not established.
- `uv run python tools/assurance_package.py --check` holds the package above
  to the code as it is, and demands meaningful refusals of corrupted
  copies.

**The governed agent path.** Every agent here is a SCRIPT, and no language
model was run (OD-1 and OD-6 are undecided):

- `uv run python tools/adversarial_eval.py --check` is the offline
  adversarial evaluation of the research loop, with the counterfactual
  cases of the slab and of the RC network, each judged by an analytic
  reference no agent is shown;
- `uv run python tools/integration_demo.py --check` is the twenty-step
  lifecycle and its twelve attacks, each refused;
- `uv run python tools/research_horizon.py --decisions 100` is the
  long-horizon campaign's invariants at 100 scripted decisions;
- `uv run python tools/grounding_experiment.py --check` checks that the
  preregistered experiment is still the registered one.

**Formal models.** These are model checks of the authority state machines,
bounded and stated as such. They are not proofs about the code:

- `uv run python tools/formal_check.py check`;
- for TLC itself:

      JAR="$(uv run python tools/formal_check.py fetch --dest /tmp/tla)"
      uv run python tools/formal_check.py verify --jar "$JAR"

**The repository's own records.** Each of these status files is derived,
never written by hand:

- `uv run python generate_manifest.py --check`
- `uv run python tools/repo_content_audit.py --check`
- `uv run python tools/harness_status.py verify`
- `uv run python tools/readiness.py check`
- `uv run python tools/model_evidence.py verify`
- `uv run python tools/neural.py verify`

**The tests, and whether they test anything.**

    uv run python -m pytest tests -q
    uv run python tools/mutation_matrix.py tools/mutations/<spec>.json

Each mutation specification removes one rule from the code. Its suite must
then fail, and must have passed beforehand.

## 3. What this repository cannot reproduce

These are not hidden behind any step above:

- **A language model's behaviour.** No real model was run (NOT_RUN: OD-1,
  OD-6).
- **A physical experiment.** None occurred, and no hardware result is
  claimed.
- **The large flagship model.** It is a validated development pipeline,
  not a trained model.
- **Machine-checked proofs in Lean.** None was run here.
- **A production deployment.** That includes independent witnesses, a role
  registry, a vetted signature provider and a release trust root. Each is
  an owner action or decision (`docs/audit/AUDIT_REPORT.md`, section 7,
  and `docs/security/owner_actions.md`), and no step above can stand in
  for it.
