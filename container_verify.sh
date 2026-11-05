#!/usr/bin/env bash
# Canonical in-container verification of the Scientific AI Harness (run as
# the non-root user 'harness').
#
# The test step runs pytest, not a loop over the test files. Executing
# "python tests/test_X.py" runs whatever that module does under __main__ and
# exits 0 when it does nothing: a loop once silently skipped 91 of 325 test
# functions and still reported success. Collection is asserted below so a
# zero-collection regression fails the container instead of passing quietly.
set -euo pipefail

# EVERY STEP ANNOUNCES ITSELF, because "it passed" was an inference: a hosted
# run once had its early steps recorded as successful from `set -e` ordering
# alone, because the job-logs API served only the pytest tail. A marker per
# step turns the inference into a record, greppable one step at a time.
HARNESS_STEPS_OK=""
HARNESS_STEP_CURRENT=""
step() { HARNESS_STEP_CURRENT="$1"; echo "::HARNESS-STEP-BEGIN:: $1"; }
done_() {
  echo "::HARNESS-STEP-OK:: $1"
  HARNESS_STEPS_OK="${HARNESS_STEPS_OK}${HARNESS_STEPS_OK:+,}$1"
  HARNESS_STEP_CURRENT=""
}

# A SUMMARY AT THE END, because the beginning of the log is not reachable:
# the job-logs API serves the TAIL, and the early markers are far above it.
# An EXIT trap, so it prints on failure too -- which is the case where
# knowing how far it got actually matters.
_harness_summary() {
  local rc=$?
  echo "::HARNESS-STEPS-COMPLETED:: ${HARNESS_STEPS_OK:-none}"
  if [ -n "$HARNESS_STEP_CURRENT" ]; then
    echo "::HARNESS-STEP-FAILED:: ${HARNESS_STEP_CURRENT}"
  fi
  echo "::HARNESS-EXIT:: ${rc}"
  return $rc
}
trap _harness_summary EXIT

step "environment"
python -c "import json,sys,platform,numpy,scipy,h5py
try:
    cfg = numpy.show_config(mode='dicts'); dep = cfg.get('Build Dependencies', {})
    blas = dep.get('blas', {}).get('name'); lapack = dep.get('lapack', {}).get('name')
    simd = cfg.get('SIMD Extensions', {})
except Exception:
    blas = lapack = None; simd = {}
print('::HARNESS-ENV:: ' + json.dumps({
    'python': sys.version.split()[0], 'platform': platform.platform(),
    'machine': platform.machine(), 'numpy': numpy.__version__,
    'scipy': scipy.__version__, 'h5py': h5py.__version__,
    'blas': blas, 'lapack': lapack, 'simd': simd,
    'threads': {k: __import__('os').environ.get(k)
                for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS',
                          'MKL_NUM_THREADS','PYTHONHASHSEED')},
}, sort_keys=True))"
done_ "environment"

# git is REQUIRED, not optional: the governance tests enumerate the tracked
# tree with it, and without it they fail three frames deep in subprocess
# with a FileNotFoundError that says nothing about the property under test.
step "git-available"
git --version
git -C /harness rev-parse --is-inside-work-tree
echo "::HARNESS-TRACKED-FILES:: $(git -C /harness ls-files | wc -l)"
done_ "git-available"

# The harness's own evidence and audits: the learned-model evidence
# re-derived (abstractly: the ~1T flagship is never allocated -- verify
# refuses a real allocation), the repository content audit and the applied
# forensic inventory, the claims boundary, the completion matrix, and a
# generic model checked against an independent reference -- with a
# deliberately wrong producer that must be rejected.
step "harness-generic"
HARNESS_NEURAL_REQUIRED=1 python tools/neural.py verify
python tools/repo_content_audit.py --check
python tools/repo_inventory.py check --applied
python tools/claims_enforcement.py
python tools/completion_matrix.py
python - <<'PY'
import scientific.models.slab_transient as ST
from scientific.checks import slab_series as SS
p = {"L_m": 0.05, "k_W_m_K": 15.0, "rho_c_J_m3_K": 3.6e6, "q_W_m3": 2.0e5,
     "h_W_m2_K": 50.0, "T_inf_K": 300.0, "T0_K": 300.0, "t_end_s": 600.0}
ok = SS.run_check(ST.SlabTransientModel().run(p), verifier_id="container")
orig = ST._solve
ST._solve = lambda q, n, m: orig({**q, "k_W_m_K": q["k_W_m_K"] * 1.1}, n, m)
bad = SS.run_check(ST.SlabTransientModel().run(p), verifier_id="container")
ST._solve = orig
print("::HARNESS-GENERIC:: slab vs series", ok.status.value,
      "| wrong producer", bad.status.value)
if not (ok.status.value == "PASS" and bad.status.value == "FAIL"):
    raise SystemExit("the independent reference did not separate the "
                     "correct producer from the wrong one")
PY
done_ "harness-generic"

# The generic end-to-end demonstration, in the image: proposal, governed
# retrieval, the ingress, a governed model run, HDF5, the independent
# checks, the reviewer's decision, a restart from a checkpoint,
# reconstruction and an RO-Crate. Without the FEniCSx and FMI runtimes (not
# in this image) its generic leg runs. The report goes to
# HARNESS_EVIDENCE_DIR when the host mounts one, so the workflow can compare
# it with a native run.
step "harness-demo"
EVID="${HARNESS_EVIDENCE_DIR:-/tmp}"
python tools/harness_demo.py run --out "$EVID/harness_demo_container.json"
echo "::HARNESS-DEMO:: report sha256 $(sha256sum "$EVID/harness_demo_container.json" | cut -c1-64)"
done_ "harness-demo"

# Fail closed if collection collapses: a suite that collects nothing must not
# be reported as a passing suite.
step "pytest"
collected=$(python -m pytest tests/ -q --collect-only 2>/dev/null \
  | awk -F': ' '/: [0-9]+$/{s+=$2} END{print s+0}')
if [ "${collected:-0}" -lt 300 ]; then
  echo "CONTAINER VERIFICATION FAILED: pytest collected ${collected:-0} tests (expected >= 300)" >&2
  exit 1
fi
echo "pytest collected ${collected} tests"
HARNESS_NEURAL_REQUIRED=1 python -m pytest tests/ -q
done_ "pytest"

step "manifest_freshness"
python generate_manifest.py --check
done_ "manifest_freshness"

step "ro_crate"
python ro_crate_tools.py validate
done_ "ro_crate"

echo "CONTAINER VERIFICATION COMPLETE (software checks only; nothing here is a scientific or experimental result)"
