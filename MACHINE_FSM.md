# QTA Machine Finite-State Architecture

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

The hierarchical finite-state machine in `qta_multiphysics/machine_fsm.py` is
the operational controller of the THEORETICAL machine and its simulation
framework. It expands the canonical Mode A/B/C/D controller into the complete
operational lifecycle without replacing it: the four modes remain the
operational core, now embedded in the full campaign
`OFFLINE -> INIT -> PREP_VACUUM -> COOLDOWN -> MODE_A_BASELINE ->
MODE_B_PROCESS -> MODE_C_RECOVERY -> MODE_D_SENSE -> (SHUTDOWN | repeat)`,
with `SAFE_RECOVERY` reachable from any operational state on a BLOCKED-class
trip and `FAULT_LATCHED` on any IMPOSSIBLE-class violation.

The tables of record are generated deterministically by the pipeline and are
byte-gated canonical outputs:

- `machine_fsm_states.csv` — every state with all nineteen operational
  aspects (purpose, entry/exit conditions, hardware enabled/disabled over the
  thirteen hardware axes, sensors monitored, validation conditions with gate
  provenance, applicable interlocks, thermal/gas/optical/microwave/cryogenic
  configuration, outputs produced, physics modules executed, recovery and
  fault behavior).
- `machine_fsm_transitions.csv` — every ALLOWED transition with its guards
  (each guard names the model quantity, the bound, and its gate/interlock
  provenance) and physical justification, plus every FORBIDDEN pair with the
  interlock that forbids it.
- `machine_fsm_interlocks.csv` — the eighteen interlocks: the fourteen
  canonical interlocks IL-01..IL-14 mapped onto hardware/context predicates,
  plus four FSM-level interlocks (FSM-IL-15..18) exposed by the state
  expansion.
- `machine_fsm_lifecycle_trace.csv` / `machine_fsm_summary.json` — the FSM
  executed through the nominal campaign against the model's own quantities
  (3D-sequence probe temperatures, the canonical purge-forecast residual, the
  canonical vibration metrics, the post-bakeout chamber target). Procedural
  milestones with no model dynamics are declared as such; RGA clearances are
  FORECAST-basis (measured clearance NOT AVAILABLE, gate E04).
- `machine_fsm_diagram.mmd` — a generated Mermaid state diagram.

Design principles: every transition is justified by a physical operating
requirement and guarded by readiness conditions with provenance; illegal
hardware combinations are impossible by interlock predicate (IMPOSSIBLE
class) or refused until the condition clears (BLOCKED class); refusals never
mutate state; guards evaluate on model quantities that exist in this
repository, and anything without a canonical parameterization (pumpdown
curves, warmup rates, damage bounds) is honestly NOT_IMPLEMENTED.

The nominal lifecycle trace ends with the Mode-D sensing hold **refused under
IL-08**: the canonical vibration-chain amplitude (4.65e-9 m) exceeds the
Mode-D threshold (1e-10 m). This is the same model condition that keeps the
canonical vibration gate CONDITIONAL and blocks the NV eligibility forecast —
an honest forecast result demonstrating the guard chain, not an error. The
FSM makes no hardware-validation claims, can never emit PASS, and preserves
`measured_in_this_system=false` and `can_PASS_now=NO` throughout.
