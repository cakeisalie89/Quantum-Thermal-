# Corpus recovery triage — PR #16 `qta-complete-corpus-recovery`

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

This records what the recovery corpus was searched for, what it contained, what
was recovered from it, and what it did **not** contain — so a later reader does
not have to re-derive any of that from the evidence blobs.

## 1. What arrived

PR #16 (branch `qta-complete-corpus-recovery`, head `d584662`, opened
2026-09-03) carries six recovery documents and four evidence blobs:

| Blob | Contents |
|---|---|
| `STEP_1_LOCAL_DIVERGENT_06A171_GIT_BUNDLE.gz.b64` | git bundle, head `06a171a`, 175 files differing from `main` |
| `STEP_1_CP_M11_446_STAGED_FULL_INDEX.patch.gz.b64` | staged-candidate patch index |
| `STEP_1_LATE_REPLAY_TRACKED_FULL_INDEX.patch.gz.b64` | tracked late-replay patch index |
| `STEP_1_LATE_REPLAY_UNTRACKED.tar.gz.b64` | untracked late-replay working files |

The bundle needed `096fb90` as a prerequisite, so it unbundles only inside this
repository — it is a delta against `main`, not a standalone history.

## 2. What it is, and what it is not

The corpus is a **scientific checkpoint-recovery ledger** (CP-M0 … CP-M23+):
geometry and materials registries, FSM graphs, surface-coverage physics,
chemistry scenarios, mechanical interface graphs, Stage-8 provenance. It maps to
the scientific side of the programme.

It contains **no prior agent-runtime implementation.** Searched for, and absent:
agent scheduler, tool authority, execution governance, sandbox design, agent
memory, capability model, policy engine, agent identity, multi-agent
coordination, context management, durable task lifecycle, lease/ownership,
egress control, secret handling.

The apparent hits are false friends: `m1_capability_*` is *hardware* capability
grading, and the `*_audit.json` files are scientific path audits. So the
`qta_agent/` layer had no stronger historical version to restore, and nothing in
it was reimplemented over recovered work.

## 3. What WAS recovered, and what it proved

One file: `tests/test_agent_authority_boundaries.py`, absent from `main`,
adversarial tests for Stage-10 write authority and retrieval trust. Every API it
calls still exists, so it ran against current `main` unchanged.

**It failed, and the failures were real.** Not message drift — the guarantees
were genuinely gone:

- `write_text_deterministic` **overwrote `README.md`**;
- `write_json_deterministic` **wrote `{"status": "PASS"}` into
  `results_gate_table.csv`**, the canonical gate table.

Both during an ordinary test run, with no privilege and no bypass. The cause was
structural rather than a slip: `guard_output_dir` guarded *directories*, while
the writers took any path at all and called `write_bytes`. The guard was
therefore advisory — correct only for callers who remembered to call it — and
the one thing a governed repository must never permit was one forgotten call
away. A denylist of protected directories compounded it: `outputs/`, `docs/` and
every loose canonical file at the repository root were writable because nobody
had listed them.

Retrieval had the mirrored defect. `load_index` validated `schema_version` and
then took `chunks` and `file_digests` verbatim from the file, so a rewritten
index could put words into a governed document's mouth — text, line ranges and
source paths all attacker-chosen, provenance fields still looking impeccable.

## 4. What was done about it

Both are now allowlists enforced at the point of use rather than by convention:

- **Writes.** `assert_in_workspace` resolves the target and requires it inside
  `verification/stage10/`. It runs *inside* the writers, so forgetting
  `guard_output_dir` no longer grants authority. Resolution follows symlinks
  deliberately: a link created in the workspace pointing at a canonical file
  resolves to that file and is refused, which a string comparison would miss.
- **`relpath_in_repo`** raises on an external path instead of falling back to
  its basename. The fallback laundered `/etc/secret.md` into `secret.md`, so a
  provenance record would name a repository file that is not the file the bytes
  came from — worse than a missing entry, because it reads as evidence.
- **Retrieval.** A serialized index is treated as a *cache*: `load_index`
  rebuilds from the source files it names and requires the stored document to
  match field for field. Not a checksum over the document — whoever rewrote it
  could recompute that — but a rebuild from the corpus. Caller-supplied paths
  are validated against traversal, absolute paths, excluded trees, non-document
  suffixes, symlinks, duplicates and root escape. `k` is bounded and typed.

A mismatch is reported as **"stale or forged"** because from the index alone
those are indistinguishable, and naming only one would be a guess presented as a
diagnosis.

## 5. Verification

`tools/mutations/stage10_authority.json` — 15 mutations, each reopening one of
these holes — **15/15 killed**.

Four survived the first run. Three were masked identically: the hostile paths in
the recovered tests do not exist, so *"is not a regular file"* refused them
before the guard under test ran. Those tests were proving that something
rejected a missing file. The added fixtures create the hostile files for real,
including a **symlinked parent directory** — the file itself perfectly ordinary,
so the symlink rule passes cleanly and only the root-escape rule catches it.

## 6. Recovery status

| Item | Status |
|---|---|
| Agent-runtime historical implementation | `NO_CURRENT_EVIDENCE_FOUND` — absent from the corpus; nothing to restore |
| `tests/test_agent_authority_boundaries.py` | **RECOVERED AND INTEGRATED**, plus four isolating additions |
| Stage-10 write authority | **REGRESSION FOUND AND FIXED** |
| Retrieval index trust | **REGRESSION FOUND AND FIXED** |
| Scientific CP-M* checkpoint reconciliation | Out of scope for this branch; PR #16 remains open and is the authority for it |

No scientific claim, gate, threshold or canonical output was changed. PASS
remains 0.
