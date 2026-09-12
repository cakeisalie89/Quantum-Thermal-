#!/usr/bin/env bash
# Run a verification command and report its REAL exit code.
#
# WHY THIS EXISTS
#
# `pytest ... | tail -5` exits 0 whatever pytest did, because a pipeline's
# status is its LAST command's. This project's defect ledger already carries
# "pipeline exit code hiding" once, from the container verification script.
# It recurred in an agent's own verification procedure and cost a red push:
# the suite failed, `tail` succeeded, the transcript said "exited with code
# 0", and the tree went to origin broken.
#
# So: capture to a file, print the tail, exit with the command's status.
# Never pipe a verifier into anything.
set -uo pipefail

log="${QTA_VERIFY_LOG:-$(mktemp)}"
lines="${QTA_VERIFY_TAIL:-25}"

"$@" >"$log" 2>&1
status=$?

tail -n "$lines" "$log"
echo "--- exit ${status} :: $* ---"
if [ "$status" -ne 0 ]; then
  echo "VERIFICATION FAILED (exit ${status}). Full log: ${log}" >&2
fi
exit "$status"
