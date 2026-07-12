# Manuscript Change Log (`qta_manuscript_v4.tex`)

Consistency, formatting, and documentation revision. **No physics or claims changed.** Canonical claims
(BLOCKED; 0 PASS; NOT AVAILABLE; no breakthrough; tau_c >= 292 us; C_contr UNKNOWN;
LCVD/NV mutually exclusive; 3D future-only; numerical != physical validation) are
unchanged.

## Changes
1. **Gate-count contradiction fixed.** The stale "Summary: 0 Pass | 39 conditional
   | 0 Fail | 2 unknown | 21 blocked | 1 derived" row (total 63) was replaced with
   the canonical full-table summary derived from `results_gate_table.csv`:
   **83 total: 0 PASS, 47 CONDITIONAL, 23 BLOCKED, 2 UNKNOWN, 11 DERIVED_CHECK, 0 FAIL**. The selected-critical-gates table rows are now explicitly labelled a
   *subset*; counts are over all 83 gates.
2. **Status taxonomy split into three axes** (Evidence Status Vocabulary section):
   parameter evidence (ASSUMED/UNKNOWN/MANUFACTURER_SPEC/LITERATURE_BOUND/
   DESIGN_SPECIFIED/MEASURED_EXTERNAL/MEASURED_IN_SYSTEM), implementation state
   (NOT_INSTALLED/INSTALLED_NOT_TESTED/TESTED/VERIFIED), and gate verdict
   (BLOCKED/CONDITIONAL/UNKNOWN/DERIVED_CHECK/FAIL/PASS). Cryotrap and NEG pump are
   now dual-labelled DESIGN_SPECIFIED + NOT_INSTALLED (no longer contradictory).
3. **Hype wording removed.** "serious 1D/2D" and "serious spatial refinement" ->
   "spatially resolved 1D/2D reduced-order" / "spatially resolved refinement".
   Breakthrough/Nobel/DARPA language retained only in the non-claims section.
4. **Numerical-methods summary added** to Section 16 (lumped comparator; 1D
   finite-volume method-of-lines with stiff BDF, rtol 1e-6 / atol 1e-9, 200 cells;
   2D axisymmetric; boundary conditions; source terms; mesh-convergence, energy/
   source conservation, Kapitza-sign, axis non-singularity, 2D->1D ~1.4% checks).
   Confirmed against code (`config.py`, `gas_transport_1d.py`, `grids.py`):
   the solver is **stiff BDF method-of-lines**, not explicit FTCS; no CFL claim.
5. **Repo-operational instructions moved out of the body.** Inline
   "Run python3 qta_full_sim.py" replaced with a pointer to Appendix (output
   schema) and the repository docs.
6. **Repo docs created:** NUMERICAL_METHODS.md, VALIDATION_PLAN.md,
   GATE_TABLE_README.md, SOURCE_AUDIT_STATUS.md (CLAIMS_BOUNDARY.md already present).
7. **Consistency checker added:** `manuscript_consistency_check.py` (fails on
   gate-count drift, unmarked 27.7 us, PASS-from-weak-evidence, 3D/COMSOL/
   validated-hardware/simultaneous-LCVD+sensing language, Mode B/D laser mixing,
   page-number leakage, and "serious 1D/2D"). The checker is verified to fail on
   representative violations and to pass on the current manuscript.
8. **PDF regenerated** from the corrected source (clean LaTeX pagination; no
   page-number leakage in extracted text).

## Reproducible PDF build
The PDF is byte-reproducible so it stays consistent with `final_manifest.json`.
Rebuild with:
```
SOURCE_DATE_EPOCH=0 FORCE_SOURCE_DATE=1 pdflatex -interaction=nonstopmode qta_manuscript_v4.tex
SOURCE_DATE_EPOCH=0 FORCE_SOURCE_DATE=1 pdflatex -interaction=nonstopmode qta_manuscript_v4.tex
```
