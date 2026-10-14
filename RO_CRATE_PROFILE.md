# RO-Crate profile

Specification: **RO-Crate 1.1** (`https://w3id.org/ro/crate/1.1`),
hand-built as deterministic JSON-LD by `ro_crate_tools.py` with an in-repo
structural validator; the RO-Crate community's validator (roc-validator,
profile ro-crate-1.1, from a hash-pinned lock) runs beside it on a hosted
runner (`tools/ro_crate_conformance.py`), and a disagreement fails.

Crate root `ro-crate/ro-crate-metadata.json`: the Scientific AI Harness as
the root dataset; the governed workflow (`Snakefile`) as its main entity;
the end-to-end demonstration and the model catalog; `pyproject.toml`,
`uv.lock` and an environment entity whose versions are read from the lock;
the stack and authority registries, the completion contract and its
derived status, the completion matrix, the forensic inventory and the
content audit, the equivalence contracts, the learned-model dataset manifest
and claims; the manifest and its detached hash; and a `CreateAction`
binding the workflow to the status it feeds. Every data entity carries its
size and SHA-256. License: no authoritative record exists — stated, none
invented.

The root description says what the crate is not -- no experiment
performed, a simulation is not experimental validation, learned outputs are
NON_AUTHORITATIVE, a checksum attests bytes and not correctness -- and the
validator requires those statements and refuses a forbidden claim anywhere
else. Determinism: sorted entities and keys, relative POSIX paths, no UUIDs,
no timestamps.
