#!/usr/bin/env bash
# Canonical in-container verification (run as non-root 'qta').
set -euo pipefail
python -c "import numpy,scipy,qutip; print(numpy.__version__,scipy.__version__,qutip.__version__)"
python qta_full_sim.py
python package_consistency_check.py
python manuscript_consistency_check.py
for t in tests/test_*.py; do python "$t"; done
python stage6_preservation_check.py
echo "CONTAINER VERIFICATION COMPLETE (software checks; scientific gate PASS remains zero)"
