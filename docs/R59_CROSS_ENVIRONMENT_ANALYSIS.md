# R59 — the 3D cross-environment divergence, analysed

**MODEL-ONLY / FORECAST-ONLY.** Nothing here changes a gate, a threshold or a
canonical output. PASS remains 0.

## What R59 recorded before this analysis

> **blocker.missing_input** — the numerical cause of an 8-file byte divergence
> between the local and hosted/container environments for the 3D outputs
>
> **blocker.unblocked_by** — analysing artifact from run 32626098635 to compare
> BLAS/SIMD dispatch and CPU flags against the local fingerprint, then deciding
> between numerical-equivalence and byte-identity semantics

The hypothesis under investigation was numerical: different BLAS, different
SIMD dispatch, different floating-point reduction order.

## What was actually found

**The divergence does not reproduce, and the instrument that was to explain it
was manufacturing one.**

### 1. The diagnostic collector did not reproduce the canonical pipeline

`analysis/collect_container_3d.py` regenerated the outputs with:

```python
run_3d_all(out, heavy=False, verbose=False)
run_all(out, verbose=False)          # <-- mc_samples defaults to 60
```

`qta_full_sim.py` line 2059 calls:

```python
qta_multiphysics.run_all(str(_mp_out), mc_samples=30, ...)
```

`run_all`'s default is **60**. The collector therefore regenerated
`multiphysics_summary.json` with twice the Monte Carlo samples, and every
`monte_carlo.distributions[*]` field differed — 41 of 146 leaf values, all
downstream of `n: 30` becoming `n: 60`.

A diagnostic that does not reproduce the pipeline it is diagnosing manufactures
the divergence it was built to explain. Any conclusion drawn from artifact
`container-3d-diagnostic` (run 32626098635) about that file would have been a
statement about the collector.

Fixed: the collector now passes `CANONICAL_MC_SAMPLES = 30` and
`tests/test_container_diagnostic_fidelity.py` asserts its arguments still match
`qta_full_sim.py`'s.

### 2. This container reproduces every canonical output byte-for-byte

Run in the Claude Code remote execution container, into an empty directory:

| result | count |
| --- | --- |
| files regenerated | 63 |
| byte-identical to the committed copies | **63** |
| differing | **0** |

Including all 28 files whose names carry `3d`. Before the collector fix the
count was 62/63, with `multiphysics_summary.json` the sole difference — which
is how the defect above was found.

Environment fingerprint of that reproduction:

| field | value |
| --- | --- |
| python | 3.12.3 |
| platform | Linux-6.18.44-fc-v24-x86_64-with-glibc2.39 |
| numpy / scipy / h5py / qutip | 2.4.4 / 1.17.1 / 3.16.0 / 5.2.1 |
| BLAS / LAPACK | `scipy_openblas64`, detected by pkgconfig |
| SIMD baseline | `X86_V2`; found `X86_V3`, `X86_V4`, `AVX512_ICL`, `AVX512_SPR` |
| CPU | Intel Xeon @ 2.10GHz, 4 cores |
| thread env vars | all unset |

This is a **different** container from the one that diverged, with AVX-512
available and no thread pinning — the conditions most likely to expose a
reduction-order difference. It produced identical bytes.

### 3. The most recent container run did not fail on 3D reproduction either

Hosted run **33113363458** (container-verify #7, head `096fb90`, 2026-08-27)
is red, and its failure is not numerical. Its 48 failures are

```
/usr/local/lib/python3.12/subprocess.py:1955: FileNotFoundError
```

across `test_manifest_completeness`, `test_manifest_policy`,
`test_csv_schema_governance`, `test_authority_*`, `test_release_*`,
`test_single_source_of_truth` and `test_stage10_stack::test_rag_*` — every
suite that shells out to `git`.

The cause is in the image, not in the science:

* the base is `python:3.12.11-slim-bookworm`, which ships **no git binary**;
* `.dockerignore` excludes **`.git`**, so `/qta` is not a repository even if
  git were installed.

And `container_verify.sh` runs under `set -euo pipefail` in this order:

1. `python qta_full_sim.py`
2. `python package_consistency_check.py`
3. `python manuscript_consistency_check.py`
4. `python -m pytest tests/` ← **failed here**

Reaching step 4 means steps 1–3 exited 0. Step 2 includes the check that
hashes every file listed in `final_manifest.json` — the same check that
reported `18 mismatches: attic/delivery_artifacts/...: missing file` in run
32618887858. Had the regenerated 3D outputs differed from the committed
copies, that check would have failed there.

**This is an inference from `set -e` ordering, not a reading of those steps'
output.** The GitHub job-logs API returned only the pytest tail (5006 lines of
381,590 characters) and would not serve the earlier steps.

## What is now known, and what is not

**Established**

* The collector had a real defect that produced a spurious one-file
  divergence with a completely misleading cause.
* An independent container reproduces all 63 canonical outputs byte-for-byte,
  3D included.
* The most recent container run's failure is a packaging gap — no `git`
  binary, no `.git` directory — and is unrelated to numerics.

**Not established**

* What the original 8-file count measured. The
  `container-3d-diagnostic` artifact from run 32626098635 could not be
  downloaded: its host, `productionresultssa14.blob.core.windows.net`, is
  denied by this environment's egress policy
  (`connect_rejected`, 403 on CONNECT), and the job-logs API caps at the
  pytest tail.
* Whether the divergence ever existed in the canonical path, or was an
  artefact of the same collector defect in an earlier form.

## Disposition

R59's blocker as written — *the numerical cause of an 8-file byte divergence* —
is **not supported by any evidence reachable from here**, and is contradicted
by a byte-exact reproduction in an independent container. It is therefore no
longer recorded as an epistemic blocker: there is no missing knowledge holding
it, only a container that cannot run the git-dependent half of its own suite.

That packaging gap is ordinary engineering and is recorded as such. Fixing it
requires building the image, which this environment cannot do — the sandbox
egress proxy denies Docker layer blobs — so any fix must be verified on a
hosted runner before it is claimed to work.

**No tolerance was widened. No file was exempted. No canonical output was
rewritten.** The route out was reproducing the measurement and finding the
instrument wrong, which is the only route that was ever going to be honest.
