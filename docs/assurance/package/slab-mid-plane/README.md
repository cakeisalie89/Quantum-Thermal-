# Assurance package: the slab's mid-plane temperature

A portable package of evidence for one answer: whether the transient slab's
temperature at x = L/2 stays below 1000 K at the documented point. It is
evidence and authorizes nothing (NON_AUTHORITATIVE), and it is a statement
about a model (MODEL_ONLY): no temperature of a real slab was observed.

The record is `assurance_record.json`. The run's event log is
`history/log.jsonl`. Each piece of evidence is `evidence/<sha256>`, named by
its own digest, and the record says which is which. `SHA256SUMS` lists
every other file.

## Checking it

    python3 -I verify_assurance.py PATH/TO/THIS/DIRECTORY \
        [--expect-package-digest SHA256] [--expect-head SEQ:SHA256]

The verifier imports the Python standard library and nothing else. Obtain
it independently of this package: a package cannot vouch for its own
checker. It reports five levels (INTEGRITY, CONSISTENCY, HISTORY, DECISION,
RECHECK) and exits 0 only when every one holds and every expectation you
supplied matched. At RECHECK it solves the slab equations itself, in closed
form, and holds the package's numbers to its own.

## What you must trust

- the verifier, tools/verify_assurance.py, obtained independently of this package, from the repository at a commit you trust; this package does not carry it
- a Python 3 interpreter and its standard library
- for the package's origin: the sha256 of SHA256SUMS, obtained from a channel independent of the package, such as the repository's history at a commit you trust; the package is not signed, because signing needs an identity only the owner can provision
- for the history: a head (seq and hash) from a witness independent of the producer; none is offered here

## What no offline check establishes

The verifier lists these on every run. Without the two expectations, a
package anyone assembled consistently passes every level: consistency is
not origin.

- PACKAGE_ORIGIN: that this is the package that was published: a package anyone assembled consistently passes every level. Supply --expect-package-digest, obtained independently of the package.
- HISTORY_ANCHORED: that the history is the producer's whole history and not a consistent rewrite: the chain binds its records to one another, not to anything outside. Supply --expect-head from an independent witness.
- PRODUCER_EXECUTION: that the producer's processes did what the log records: the log is the producer's statement, chained and unsigned.
- MODEL_IMPLEMENTATION: that the code at the stated implementation digest computed the bundle: re-running it needs the producer's source.
- ROLE_ENTITLEMENT: that the deciding parties were entitled to decide: the producing run carried no role registry, and none is provisioned for production.
- FORMAL_PROOF: any machine-checked proof: the package carries none.
- PHYSICAL_VALIDITY: that the equations describe any real slab: no temperature of one was observed.
- INPUT_DISTRIBUTIONS: that the declared input spreads are right: they are declared, not measured.
- PRODUCER_TOOLCHAIN: the producer's interpreter, libraries and machine: the bundle states them, and nothing here can see them.
- WALL_TIMES: when anything happened: the times in the log are the producer's clock.
- HISTORY_SEMANTICS: what the history's other records mean: the verifier reads the objective, the hypothesis, the result's creation and transitions, the captured evidence, the observation and the hypothesis's statuses. Every other record is chained and not interpreted.

## Reproducing it

1. Obtain the package: the directory docs/assurance/package/slab-mid-plane of the repository at a commit you trust, and tools/verify_assurance.py from that commit or another you trust.
2. Verify its integrity and origin: python3 -I verify_assurance.py PACKAGE --expect-package-digest DIGEST, with DIGEST the sha256 of SHA256SUMS as your trusted channel gives it.
3. Identify its dependencies: verifying needs only Python 3 and its standard library. Re-running the model needs the repository at a commit whose model closure digests to the record's implementation_digest, with its locked environment (uv sync).
4. Run the independent check: the verifier's RECHECK level solves the stated equations in closed form and reports what it measured.
5. Inspect the assumptions: the record's mathematical_statement and assumptions, the budget's contributions and the check's limitations (evidence files named in the record).
6. Confirm the authority decision: the verifier's DECISION level; with the repository, python tools/assurance_package.py --check also re-runs the model at the recorded inputs.
7. Corrupt an artifact and observe the refusal: change one byte of any file of a copy and run the verifier again; it refuses at INTEGRITY and names the file.
