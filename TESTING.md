# Test execution contract

## The canonical checker and the test suite are serial operations

`package_consistency_check.py`, in its default full-regeneration mode, does
this near the top of Step 1:

```python
shutil.rmtree(gen_outputs_dir, ignore_errors=True)
```

It then re-runs `qta_full_sim.py` to rebuild `outputs/` from scratch. That
directory is gitignored and generated, and several tests read it — including
`tests/test_stage7_5_resilience.py`, which invokes the checker itself.

So for a window of a few minutes during any full checker run, `outputs/` does
not exist, and any test that expects it will fail through no fault of its own.

**Run them serially.** Two failures were observed exactly this way — the
checker was started in the background while `pytest` ran in the foreground, and
`test_snapshot_excludes_packaging_metadata` and
`test_verify_existing_requires_sim_log` both failed on a missing `outputs/`.
Neither was a real defect; both passed on a serial re-run.

Correct order for a full local verification:

```sh
python package_consistency_check.py     # regenerates outputs/ and verifies it
python -m pytest                        # only after the checker has finished
```

No locking is provided, and none should be added. Concurrent execution of the
canonical regeneration and the test suite is **not a supported use case** —
they contend for one generated workspace by design, and the workspace is
supposed to be rebuilt from scratch, not shared. A lock would make an
unsupported pattern appear supported.

Tests that need a controlled `outputs/` state build their own isolated copy in
`tmp_path` instead of touching the real one — see
`tests/test_checker_missing_outputs.py` and
`tests/test_stage7_5_resilience.py::test_verify_existing_requires_exactly_89`.

## `--verify-existing` never regenerates

`--verify-existing` is verification-only and is **not** the release gate; full
regeneration remains authoritative. It refuses rather than repairing, with a
classified message and a nonzero exit:

| classification | state |
|---|---|
| `MISSING_EXISTING_OUTPUTS` | `outputs/` does not exist |
| `EXISTING_OUTPUTS_NOT_A_DIRECTORY` | the path exists but is not a directory |
| `INCOMPLETE_EXISTING_OUTPUTS` | fewer than the canonical 89 files |
| `FOREIGN_EXISTING_OUTPUTS` | more than 89 files |
| `UNREADABLE_EXISTING_OUTPUT` | a file could not be hashed for comparison |

None of these produces a traceback, and none produces a vacuous PASS: with no
usable generated set, the byte-match comparison reports `NOT CHECKED` as a
failure rather than staying silent, because silence there would read as
agreement.

## Order independence

The suite is run both forward and in reverse file order, and both must collect
and pass the same number of tests. A difference means a test is leaking state
into another.

```sh
python -m pytest
python -m pytest $(ls tests/test_*.py | sort -r)
```

## Long-running evidence

`§6 L3` (`THERMAL_3D_L3.md`) takes ~2 624 s for the 3D solve alone and is **not**
part of the routine suite. It is re-run only when production physics or solver
code capable of changing its result is modified — not for documentation,
tooling, workflow or provenance changes.
