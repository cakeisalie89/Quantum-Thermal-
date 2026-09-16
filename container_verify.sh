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

# EVERY STEP ANNOUNCES ITSELF, because "it passed" was an inference.
#
# R59 recorded that steps 1-3 succeeded in hosted run 33113363458, derived
# from `set -e` ordering and the fact that step 4 was reached. That is sound
# reasoning and it is not a reading of their output: the job-logs API served
# only the pytest tail, so the earlier steps' results were never actually
# seen. A marker per step turns the inference into a record, and the markers
# are greppable so a later reader can confirm each one individually.
QTA_STEPS_OK=""
QTA_STEP_CURRENT=""
step() { QTA_STEP_CURRENT="$1"; echo "::QTA-STEP-BEGIN:: $1"; }
done_() {
  echo "::QTA-STEP-OK:: $1"
  QTA_STEPS_OK="${QTA_STEPS_OK}${QTA_STEPS_OK:+,}$1"
  QTA_STEP_CURRENT=""
}

# A SUMMARY AT THE END, because the beginning of the log is not reachable.
#
# The per-step markers turned the inference into a record, and the record was
# still unreadable: the job-logs API serves the TAIL, and by the time this
# script has run qta_full_sim.py and a pytest suite the early markers are
# fifteen hundred lines above it. R59 recorded "steps 1-3 passed" as an
# inference from `set -e` ordering; a marker nobody can scroll to is the same
# inference wearing a record's clothes.
#
# An EXIT trap, so it prints on failure too -- which is the case where
# knowing how far it got actually matters.
_qta_summary() {
  local rc=$?
  echo "::QTA-STEPS-COMPLETED:: ${QTA_STEPS_OK:-none}"
  if [ -n "$QTA_STEP_CURRENT" ]; then
    echo "::QTA-STEP-FAILED:: ${QTA_STEP_CURRENT}"
  fi
  echo "::QTA-EXIT:: ${rc}"
  return $rc
}
trap _qta_summary EXIT

step "environment"
python -c "import json,sys,platform,numpy,scipy
try:
    import qutip; q = qutip.__version__
except Exception as e:
    q = 'UNAVAILABLE: %s' % type(e).__name__
try:
    cfg = numpy.show_config(mode='dicts'); dep = cfg.get('Build Dependencies', {})
    blas = dep.get('blas', {}).get('name'); lapack = dep.get('lapack', {}).get('name')
    simd = cfg.get('SIMD Extensions', {})
except Exception:
    blas = lapack = None; simd = {}
print('::QTA-ENV:: ' + json.dumps({
    'python': sys.version.split()[0], 'platform': platform.platform(),
    'machine': platform.machine(), 'numpy': numpy.__version__,
    'scipy': scipy.__version__, 'qutip': q,
    'blas': blas, 'lapack': lapack, 'simd': simd,
    'threads': {k: __import__('os').environ.get(k)
                for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS',
                          'MKL_NUM_THREADS','PYTHONHASHSEED')},
}, sort_keys=True))"
done_ "environment"

# git is REQUIRED, not optional. 48 governance tests enumerate the corpus
# with it, and without it they fail three frames deep in subprocess with a
# FileNotFoundError that says nothing about the property under test.
step "git-available"
git --version
git -C /qta rev-parse --is-inside-work-tree
echo "::QTA-TRACKED-FILES:: $(git -C /qta ls-files | wc -l)"
done_ "git-available"

step "qta_full_sim"
python qta_full_sim.py
done_ "qta_full_sim"

# The 3D cross-environment comparison, IN THE JOB LOG rather than only in an
# artifact, and BEFORE the checkers that can fail. The artifact route is
# authenticated and works; its signed storage host is denied by some egress
# policies, so the evidence that matters is emitted where the logs API can
# serve it.
#
# Ordered ahead of package_consistency_check.py because of what happened
# when it was not: the package check failed on a byte divergence, `set -e`
# ended the run, and the diagnostic that exists to EXPLAIN a byte divergence
# never executed. The one case where the measurement matters most was the
# one case it was skipped in.
#
# It is a diagnostic: it reports a divergence and never fails on one. Putting
# it first therefore cannot mask a failure, only inform one.
step "cross-environment-3d"
python analysis/collect_container_3d.py /tmp/qta-3d-diag --emit-summary
done_ "cross-environment-3d"

step "package_consistency"
python package_consistency_check.py
done_ "package_consistency"

step "manuscript_consistency"
python manuscript_consistency_check.py
done_ "manuscript_consistency"

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

step "stage6_preservation"
python stage6_preservation_check.py
done_ "stage6_preservation"

step "manifest_freshness"
python generate_manifest.py --check
done_ "manifest_freshness"

step "hdf5_equivalence"
python validate_hdf5_equivalence.py
done_ "hdf5_equivalence"

step "ro_crate"
python ro_crate_tools.py validate
done_ "ro_crate"

echo "CONTAINER VERIFICATION COMPLETE (software checks; scientific gate PASS remains zero)"
