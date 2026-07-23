# Ruff report (Stage 7)
Rules: E, F, W (line-length 79); excludes: attic/, EXPERIMENT_PLAYBOOKS/,
outputs/ (generated/prose per project policy); per-file E501 relaxation on
the legacy scientific tree and tests (no behavior-risk reformatting).
- New/modified Stage-7 modules: 0 findings (7 initial - 4x E702, 2x F401,
  1x E501 - all fixed properly; the registry builder's regenerated outputs
  verified byte-identical after the style edits).
- Legacy catalogue (whole tree, informational): Found 1446 errors - catalogued as
  debt, deliberately NOT auto-fixed (no formatting rewrite of scientific
  code; zero '# noqa' added anywhere).
