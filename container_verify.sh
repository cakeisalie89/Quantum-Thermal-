#!/usr/bin/env bash
# Canonical in-container verification (run as non-root 'qta').
#
# The test step runs pytest, not a loop over the test files. Executing
# "python tests/test_X.py" runs whatever that module does under __main__ and
# exits 0 when it does nothing: four modules (test_authority_invariants,
# test_manifest_policy, test_record_parsers, test_stage10_stack) are
# pytest-only, so the previous loop silently skipped 91 of 325 test functions
# and still reported success. Collection is asserted below so a zero-collection
# regression fails the container instead of passing quietly.
set -euo pipefail

python -c "import numpy,scipy,qutip; print(numpy.__version__,scipy.__version__,qutip.__version__)"

python qta_full_sim.py
python package_consistency_check.py
python manuscript_consistency_check.py

# Fail closed if collection collapses: a suite that collects nothing must not
# be reported as a passing suite.
collected=$(python -m pytest tests/ -q --collect-only 2>/dev/null \
  | awk -F': ' '/: [0-9]+$/{s+=$2} END{print s+0}')
if [ "${collected:-0}" -lt 300 ]; then
  echo "CONTAINER VERIFICATION FAILED: pytest collected ${collected:-0} tests (expected >= 300)" >&2
  exit 1
fi
echo "pytest collected ${collected} tests"
python -m pytest tests/ -q

python stage6_preservation_check.py
python generate_manifest.py --check
python validate_hdf5_equivalence.py
python ro_crate_tools.py validate

echo "CONTAINER VERIFICATION COMPLETE (software checks; scientific gate PASS remains zero)"
