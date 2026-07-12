# Manuscript Consistency Report

Generated against `results_gate_table.csv` (single source of truth, produced by
`qta_full_sim.py`).

| Item | Value |
|---|---|
| Gate counts (canonical) | 83 total: 0 PASS, 47 CONDITIONAL, 23 BLOCKED, 2 UNKNOWN, 11 DERIVED_CHECK, 0 FAIL |
| PASS count | 0 |
| Current system | BLOCKED |
| Validated system | NOT AVAILABLE |
| Breakthrough claim | NOT MADE |
| Canonical tau_c threshold | 292 us (v3.3) |
| Superseded tau_c | 27.7 us (v3.0) -- labelled SUPERSEDED |
| C_contr at 10 mK | UNKNOWN |
| Non-lumped solver method | finite-volume method-of-lines + stiff BDF (rtol 1e-6, atol 1e-9); NOT explicit FTCS; no CFL claim |
| 3D | future work only; not implemented |
| LCVD + NV sensing | mutually exclusive, hardware-interlocked |

## Automated checks (`manuscript_consistency_check.py`): RESULT PASS (8/8)
- canonical gate summary present and equals the CSV
- no stale/contradictory gate totals
- tau_c=27.7 us always labelled SUPERSEDED
- no PASS asserted from weak evidence (ASSUMED/DESIGN_SPECIFIED/MANUFACTURER_SPEC/
  LITERATURE_BOUND/UNKNOWN/NOT_INSTALLED)
- no forbidden phrases (3D implemented / COMSOL / validated hardware / simultaneous
  LCVD+sensing)
- Mode B (fs LCVD) and Mode D (NV readout) laser vectors kept separate
- no page-number leakage in extracted PDF text
- no unprofessional wording ("serious 1D/2D") or bullet/glyph artifacts

Numerical self-consistency does not imply physical validation.
