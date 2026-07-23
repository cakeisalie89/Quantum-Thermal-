# mypy report (Stage 7)
Staged enforcement (pyproject [tool.mypy]).
- Stage-7 modules (strict-ish; gate: mypy --follow-imports=silent on the
  stage-7 files): 0 errors in 3 files after proper annotations (zero
  'type: ignore' anywhere).
- Governance modules: 0 own errors; import-following surfaces exactly 1
  legacy finding: qta_multiphysics/coupled_mode_solver.py:94 [arg-type]
  (str method as max() key confuses the overload) - catalogued as typing
  debt, deliberately untouched (numerical legacy; no behavior risk taken).
- Third-party without types: qutip, scipy, snakemake
  (ignore_missing_imports scoped to those modules only).
- Remaining staged work: annotate the numerical legacy tree in a future
  stage.
