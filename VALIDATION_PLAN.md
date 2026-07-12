# QTA Validation Plan

The current system is **BLOCKED**; **0 gates PASS**; the validated system is **NOT
AVAILABLE**; **no breakthrough is claimed**. This plan lists the measurements that
would move gates from BLOCKED/CONDITIONAL toward (eventually) PASS. None has been
performed in-system.

## Canonical bottlenecks
- `tau_c` (3He surface correlation time) — canonical threshold **>= 292 us** at
  10 mK [UNKNOWN]. The older 27.7 us threshold (v3.0) is **SUPERSEDED**.
- `C_contr` (NV ODMR contrast at 10 mK) — **UNKNOWN**; co-equal bottleneck.

## First decisive experiments (must precede any PASS)
1. Execute bakeout (E01) and RGA residual-gas verification (E04).
2. Fabricate/measure the Ag-sinter/diamond interface `G_eff` (D5).
3. Measure `tau_c` for 3He on the actual F-terminated diamond surface at 10 mK.
4. Measure NV ODMR contrast `C_contr` at 10 mK under pulsed 532 nm excitation.
5. Install and verify the cryotrap and NEG pump (currently DESIGN_SPECIFIED,
   NOT_INSTALLED).

## Rules
- LCVD growth (Mode B) and NV sensing (Mode D) are **mutually exclusive,
  hardware-interlocked** modes; they never run simultaneously.
- A gate reaches PASS only with `MEASURED_IN_SYSTEM` evidence and a `VERIFIED`
  implementation state. Numerical convergence is not physical validation.
See also `FIRST_VALIDATION_EXPERIMENTS.md`.
