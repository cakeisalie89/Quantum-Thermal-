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
