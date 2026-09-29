#!/usr/bin/env bash
# Run a Python command on the CANONICAL REFERENCE BACKEND.
#
#   reference_backend/run.sh tools/reference_backend.py verify
#   reference_backend/run.sh qta_full_sim.py
#
# The command runs under the pinned software CPU (qemu-user, the declared
# model -- never the host's), against the pinned root filesystem's C and math
# libraries and loader, with the reference interpreter and the reference
# builds of NumPy and SciPy, one thread, and an environment emptied of
# everything but what is set here. The physical host executes the emulator;
# it does not choose the arithmetic.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
eval "$(python3 "$REPO/tools/reference_backend.py" env)"
[ -x "$QTA_REF_PREFIX/venv/bin/python" ] || {
  echo "REFERENCE RUNTIME ABSENT: run reference_backend/build.sh first" >&2; exit 2; }
exec env -i \
  HOME=/tmp PATH=/usr/bin:/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC \
  PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 \
  OMP_NUM_THREADS="$QTA_REF_THREADS_OMP_NUM_THREADS" \
  OPENBLAS_NUM_THREADS="$QTA_REF_THREADS_OPENBLAS_NUM_THREADS" \
  GOTO_NUM_THREADS="$QTA_REF_THREADS_GOTO_NUM_THREADS" \
  MKL_NUM_THREADS="$QTA_REF_THREADS_MKL_NUM_THREADS" \
  BLIS_NUM_THREADS="$QTA_REF_THREADS_BLIS_NUM_THREADS" \
  NUMEXPR_NUM_THREADS="$QTA_REF_THREADS_NUMEXPR_NUM_THREADS" \
  VECLIB_MAXIMUM_THREADS="$QTA_REF_THREADS_VECLIB_MAXIMUM_THREADS" \
  "$QTA_REF_EMULATOR" -cpu "$QTA_REF_CPU" -L "$QTA_REF_PREFIX/rootfs" \
  "$QTA_REF_PREFIX/venv/bin/python" "$@"
