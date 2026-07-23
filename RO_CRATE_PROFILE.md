# RO-Crate Profile (Stage 8)

Specification: **RO-Crate 1.1** (`https://w3id.org/ro/crate/1.1`),
hand-built as deterministic JSON-LD by `ro_crate_tools.py` — no crate
library dependency (recorded limitation: structural in-repo validator
only; no external conformance tool executable in this sandbox).

Crate root `ro-crate/ro-crate-metadata.json` (30 entities, 24 referenced
files with SHA-256 + sizes): the QTA project root dataset; the
authoritative Stage-7.5 input ZIP (identifier + sha256 + size); the
canonical runner, staged driver, 3-D runner, workflow, checkers, and
preservation checker; pyproject/uv.lock and the environment entity
(exact locked versions); the HDF5 mapping/schema/artifact; gate table,
validation matrix, all four Stage-6 registries; equivalence report;
manifest + detached hash; documentation; a `CreateAction` binding the
monolithic release verification (exit 0, 88/88) with instrument/object/
result relations. License: no authoritative record exists — stated,
none invented.

Claim boundaries embedded verbatim in the root description (PASS=0,
can_PASS_now=NO, measured_in_this_system=false, Campaign-1
PROPOSED_NOT_PERFORMED, HDF5 = representation not evidence) and
enforced by the validator (which excludes only that sanctioned
disclaimer from its forbidden-claim scan). Determinism: sorted
entities/keys, relative POSIX paths, no UUIDs, no timestamps —
twice-built byte-identical (sha 51247255…).
