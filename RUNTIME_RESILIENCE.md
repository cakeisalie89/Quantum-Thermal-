# Runtime Resilience (Stage 7.5)

MODEL-ONLY / FORECAST-ONLY. Additive tooling; no scientific code, setting,
or output changed (88/88 governed outputs byte-identical, proven).

## Why
Under sandbox load troughs the four-stage canonical run needs more wall
time (measured: core 93.0 s + layers 96.8 s + three_d 120.1 s = 309.9 s)
than the ~282 s external per-command ceiling. Profiling (cProfile, scratch
copies): core = 143 solve_ivp / 102,655 BDF steps (72x thermal-1D under
the Monte-Carlo driver + 6x thermal-2D) — 118/120 s; three_d = 16
canonical 3-D transients (convergence study 56 s, sensitivity 33 s) with
5,712 SuperLU factorizations (63 s) inside BDF's reuse heuristic; layers
is QuTiP-mesolve-bound. Every hotspot is the science at canonical
tolerances — no optimization is safe under byte-identity, so none was
made.

## Staged driver — `qta_sim_stages.py`
Executes the identical four calls, same arguments, same seed, one stage
per invocation: `core` (qta_full_sim.main + engineering fixes), `layers`
(run_integrated_layers, profile=standard, seed=42), `three_d`
(run_3d_all). Checkpoints: written only after success, atomically;
bind driver+orchestrator source hashes, the full qta_multiphysics tree
hash, interpreter/numpy/scipy/qutip versions, and every stage-output
hash (packaging metadata excluded); ANY difference fails closed; partial
stages never complete; prerequisite ordering enforced; incompatible runs
cannot merge. `python3 qta_sim_stages.py status` reports marker validity.

**The single-command `python3 qta_full_sim.py` remains the authoritative
release path.** The staged path exists to produce the byte-identical set
when the ceiling binds — demonstrated: full staged sequence -> 89 files,
88/88 byte-identical to the canonical roots.

## Checker `--verify-existing`
`python3 package_consistency_check.py --verify-existing --sim-log LOG`
validates an EXISTING COMPLETE set: exactly 89 files required (truncated
or foreign sets refused), a captured simulation stdout log required for
the Step-3 stale-language audit (never skipped), then every downstream
check runs unchanged. Banner states it is NOT the release gate; the
default full-regeneration branch is preserved and remains the sole
release verification.

## Operational baseline policy (approved)
In ceiling-bound sandboxes: staged complete regeneration + 88/88
comparison + `--verify-existing` = the operational baseline. The
monolithic command is mandatory release verification whenever an
adequate uninterrupted window or unrestricted environment is available.

## package_consistency_check.py's 300 s subprocess budget — UNRESOLVED ENVIRONMENTAL VERIFICATION

`package_consistency_check.py:155` runs `qta_full_sim.py` as a subprocess with
`timeout=300`. On the machine used for this remediation that subprocess does
not finish, so the checker raises `subprocess.TimeoutExpired` and exits
non-zero.

**Measured evidence.**

| Quantity | Value |
|---|---|
| `qta_full_sim.py` wall time, this machine | **323 s** |
| Checker budget | 300 s |
| Overrun | 23 s (7.7%) |
| Machine | 4 × Intel Xeon @ 2.80 GHz, container |
| Runtime recovered by removing the dead Mode-C solve (§7) | **0.037 s** |

**Classification: UNRESOLVED ENVIRONMENTAL VERIFICATION. Not a PASS, and not
a branch regression.**

- It is **not** caused by this branch. The same timeout occurs on a pristine
  `origin/main` worktree, checked directly.
- It is **not** meaningfully attributable to the dead Mode-C solve removed in
  §7. That solve cost 0.037 s — 0.01% of the run, and 0.16% of the overrun.
  §7 said the budget could be reconsidered only after obvious waste was
  removed; the waste was removed and it was negligible, so the budget question
  is untouched by it.
- The budget has **not** been raised. A 7.7% overrun on one slower machine is
  evidence that this container is below the budget's assumed speed, not
  evidence that the budget is wrong. Raising a timeout so a check goes green in
  the environment that happens to be running it is exactly the change this
  project's rules forbid without owner authority.

**What would resolve it:** either a run on hardware where the canonical
generator completes inside 300 s (which would confirm the budget is adequate
and this environment is simply slow), or an owner decision to widen the budget
with a stated target machine. Neither is available here.

Every other governed checker passes in this environment: `pytest tests/`
(449/449), `manuscript_consistency_check.py`, `stage6_preservation_check.py`,
`generate_manifest.py --check`, `validate_hdf5_equivalence.py`, and
`ro_crate_tools.py validate`.
