"""The repository root on `sys.path`, for every test module, whatever runs first.

WHY THIS FILE EXISTS

`tests/` has no `__init__.py`, so pytest inserts `tests/` on `sys.path` and not
the repository root. A module that writes

    from tools.cross_env_semantics import compare

therefore imports only if something else already put the root there. 103 of
the 106 test modules do it themselves, with their own copy of

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

and three did not:

    tests/test_agent_authority_boundaries.py
    tests/test_cross_env_semantics.py
    tests/test_hotspot_ranking_determinism.py

Those three passed in the full run and failed on their own, because by the
time pytest reached them some earlier module had already mutated `sys.path`.
Measured, not supposed: `pytest <file> --collect-only` over all 106, three
collection errors.

That is a result that depends on ambient state rather than on its subject --
the same shape as a byte comparison whose scope is whatever the pipeline
happened to produce (D-2026-61) and a headline drawn from an identical tree
(D-2026-62). It is also live: a `-k` selection, a shard, a parallel worker or
a reordering can schedule one of the three first, and the suite that has been
green all along fails to collect.

pytest imports `conftest.py` before collecting anything, so putting the root
here makes the guarantee structural instead of a convention 103 files happen
to follow. The per-module inserts stay: they are harmless, and removing a
hundred of them would be churn that buys nothing.

MODEL-ONLY / FORECAST-ONLY. Nothing here changes a gate, a threshold or a
canonical output. PASS remains 0.
"""
import pathlib
import sys

ROOT = str(pathlib.Path(__file__).resolve().parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
