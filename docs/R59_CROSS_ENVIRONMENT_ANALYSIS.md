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

> **The two claims marked SUPERSEDED below were wrong, and the next section
> says why.** They are left in place rather than edited out: a record that
> quietly repairs itself is not a record.

**Established**

* The collector had a real defect that produced a spurious one-file
  divergence with a completely misleading cause.
* ~~An independent container reproduces all 63 canonical outputs
  byte-for-byte, 3D included.~~ **SUPERSEDED.** That measurement compared a
  regeneration against a regeneration; it could not have disagreed. See
  *The divergence is real, and it is the BLAS kernel*.
* The most recent container run's failure is a packaging gap — no `git`
  binary, no `.git` directory — and is unrelated to numerics.

**Not established**

* ~~What the original 8-file count measured.~~ **SUPERSEDED and answered.**
  The 8 was the slice width in `package_consistency_check.py`, which printed
  `_drift[:8]` with no count beside it. The artifact from run 32626098635 is
  still unreachable — its host, `productionresultssa14.blob.core.windows.net`,
  is denied by this environment's egress policy (`connect_rejected`, 403 on
  CONNECT) — and it no longer needs to be read, because the number it would
  have explained was never a measurement.
* ~~Whether the divergence ever existed in the canonical path.~~
  **SUPERSEDED.** It does, it reproduces on demand, and its cause is named.

## The divergence is real, and it is the BLAS kernel

*Added after the section above, which was wrong about the central fact. The
earlier conclusion — "the divergence does not reproduce" — rested on a
comparison that could not have disagreed.*

### The instrument compared a regeneration against a regeneration

`compare_with_committed` looked for the committed canonical copies under
`outputs/`. Two things are wrong with that, and the second is worse.

`outputs/` is gitignored. On a fresh checkout it does not exist, so every
regenerated file landed in `not_committed`: a hosted run reported **0 of 63
compared** while printing a clean-looking summary. The anti-vacuity step in
`agent-substrate.yml` is what refused it.

And on any machine where `outputs/` *does* exist — every machine that has run
the pipeline, or `package_consistency_check.py`, which removes and recreates
it — `outputs/` is **itself a regeneration**. So the comparison asked one
reading of a computation whether it matched another reading of the same
computation. It could not disagree. "63 of 63 byte-identical", the sentence
this document previously rested on, was measured against the wrong side.

The committed canonical copies are at the **repository root**. The comparison
now reads them there, and `test_the_comparison_finds_the_canonical_copies_
where_they_live` builds its inventory from `final_manifest.json` rather than
from the directory the comparison looks in — the earlier version of that test
read names *and* bytes out of `outputs/`, which is why it was green
throughout.

### The 8-file divergence was a slice width

`package_consistency_check.py` reported `stale root copies: {_drift[:8]}` —
the first eight names, with no count beside them. A hosted run whose
regeneration diverged in twenty files printed eight names, and "an 8-file
divergence" is what got written into R59's blocker and chased for weeks.

The number 8 is `[:8]`. It was never a measurement. The checker now prints the
count first and says when the list is truncated.

### What the divergence actually is

OpenBLAS ships DYNAMIC_ARCH: one library containing several hand-written
kernels, one selected at load time from the host CPU's feature flags. The
kernels differ in blocking, vector width and accumulation order, so they
differ in the last bits of a floating-point reduction — and a CSV of
sixteen-significant-digit numbers records the last bits.

`OPENBLAS_CORETYPE` forces the selection, which turns "another environment"
into a variable on one machine, with one interpreter, one dependency set and
one set of inputs. `tools/blas_kernel_sensitivity.py` runs that sweep. On the
sandbox that wrote this section (Intel Xeon @ 2.10 GHz, AVX-512, 4 cores,
numpy 2.4.4 / scipy 1.17.1):

| `OPENBLAS_CORETYPE` | kernel selected | byte-identical | differing |
|---|---|---:|---:|
| `(unset)` | `SkylakeX` | 63 / 63 | 0 |
| `SkylakeX` | `SkylakeX` | 63 / 63 | 0 |
| `Haswell` | `Haswell` | 43 / 63 | 20 |
| `Nehalem` | `Nehalem` | 41 / 63 | 22 |

Nothing else changed between those rows. The committed canonical outputs are
**SkylakeX-kernel outputs**.

The fingerprint could not have said so before: it recorded
`numpy.show_config`, which reports what the wheel was **built** with — the
string on this machine says `Haswell` — while the kernel actually **selected**
is SkylakeX. The single field the whole comparison turns on named the wrong
thing. `openblas_runtime_core()` now reads the selected kernel from the
bundled library and the summary emits it.

### The sweep, as recorded

`tools/blas_kernel_sensitivity.py` runs it; `docs/blas_kernel_sensitivity.json`
is the result. On the sandbox that wrote this section (Intel Xeon @ 2.10 GHz,
AVX-512, 4 cores, numpy 2.4.4 / scipy 1.17.1):

| pinned | kernel selected | byte-identical | differing |
|---|---|---:|---:|
| `(unset)` | `SkylakeX` | 63 / 63 | 0 |
| `SkylakeX` | `SkylakeX` | 63 / 63 | 0 |
| `Haswell` | `Haswell` | 43 / 63 | 20 |
| `Haswell, 1 thread` | `Haswell` | 43 / 63 | 20 |
| `Haswell, numpy AVX2 only` | `Haswell` | 40 / 63 | 23 |
| `Nehalem` | `Nehalem` | 41 / 63 | 22 |

### The hosted runner, and the row that reproduces it

At commit `72b1f8c`, the `cross-environment-3d` job on `ubuntu-latest`
reported, in its own job log:

```
COMPARED 63 files: 40 identical, 23 differing, 0 uncompared
(unset)      selected=Haswell    40/63 identical, 23 differing
SkylakeX     FAILED
Haswell      selected=Haswell    40/63 identical, 23 differing
Nehalem      selected=Nehalem    41/63 identical, 22 differing
```

Three things follow, and the third is the one that finishes the
investigation.

**The runner's default kernel is Haswell.** It is not SkylakeX, and asking
for SkylakeX fails: the runner's CPU has no AVX-512, so the kernel the
committed outputs were produced with cannot be selected there at all.

**The count is 23, not 8.** `package_consistency_check.py` in the container
at the same commit said `24 stale root copies (first 8 shown)` — 24 because
it compares a slightly larger set than the 63 3D files. Either way the
"8-file divergence" that R59 recorded as its blocker was the slice width.

**Pinning the kernel alone did not explain it.** This sandbox at
`OPENBLAS_CORETYPE=Haswell` reproduced 43 of 63; the runner at Haswell
reproduced 40. Same kernel name, same interpreter, same dependency set,
three files apart. Pinning threads to one changed nothing here, ruling that
variable out.

What was left is numpy's **own** SIMD dispatch, which `OPENBLAS_CORETYPE`
does not touch: numpy compiles several versions of its element-wise loops and
selects one from the host's CPU features, independently of BLAS. Dropping
numpy to AVX2 as well —

```
OPENBLAS_CORETYPE=Haswell NPY_DISABLE_CPU_FEATURES="X86_V4 AVX512_ICL AVX512_SPR"
```

— gives **40 of 63 on this machine, and the same twenty-three files by
name**. Not "close to the runner": the same set.

The divergence is therefore fully attributed, to two host-CPU-dependent
dispatch decisions taken by two different libraries. Both are reproducible
here on demand, from the same source tree, by pinning environment variables.

### The pool is mixed, and one run showed both answers

Run `34296217403` at `7ccfd02` is the clearest evidence in this document,
and it is not a comparison between two environments — it is a comparison
inside one workflow run.

Two of its jobs ran on different machines from GitHub's `ubuntu-latest`
pool:

| job | machine | result |
|---|---|---|
| `102293316307` (`full-suite`) | has AVX-512 | `package_consistency_check.py` → **PASS**, every committed byte reproduced |
| `102293316177` (`cross-environment-3d`) | no AVX-512 | `SkylakeX UNSUPPORTED ON THIS HOST: exit -4`, Haswell **40 / 63** |

Same commit, same workflow, same dependency lockfile, ten minutes apart.

Three things follow.

**A machine without AVX-512 does not fall back when asked for SkylakeX.** It
dies: exit `-4` is SIGILL, an illegal instruction. Pinning the kernel that
reproduces the committed bytes is therefore not a portability trade — on
those hosts it is a crash.

**The byte gate's hosted result depends on which machine the job draws.**
That is not flakiness in the check; the check is deterministic given a host.
It is the byte gate correctly reporting that this host's arithmetic differs,
on a pool that does not promise a uniform host. Earlier runs `34216535309`
and `34234781324` drew non-AVX-512 machines and were red on exactly this.

**The local attribution is confirmed from the other side.** This runner's
plain `Haswell` row and its `Haswell + numpy AVX2 only` row are identical —
40 / 63 both times — because the host has no AVX-512 for numpy to use
either way. That is exactly what the local sweep predicted: the extra pin
changes nothing on a machine that was never taking the AVX-512 path.

### What this does and does not establish

It establishes that byte-identity of the 3D outputs is a property of a
**declared environment down to the CPU's vector features**, not of the
mathematics — and that a difference between two hosts can be reproduced on
one of them, which is what makes it an explanation rather than an
observation.

It does not establish that any dispatch is more correct than another. They
all compute the same problem to the same order of accuracy, and a byte
comparison cannot rank them. The differences are last-bit; nothing here
suggests a numerical defect, and nothing here would detect one.

It was not fixed by pinning these variables repository-wide. The
configuration that reproduces the committed bytes needs AVX-512, and a
machine without it does not decline the kernel — it executes an illegal
instruction and dies. So pinning it would make the pipeline crash on such
hardware rather than merely differ, and pinning a configuration every host
can run would invalidate every committed canonical output. Choosing the
host's CPU is not something a repository does, and on a shared runner pool
it is not something the job gets to do either.

**No tolerance was widened. No file was exempted. No canonical output was
rewritten.** The byte gate stays closed, and it is now closed around a
statement that says what it depends on.

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
