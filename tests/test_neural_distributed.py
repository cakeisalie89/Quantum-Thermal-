"""NF-1T s.36-38, s.41, s.79: distributed software readiness -- on
SIMULATED devices only, and said to be."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from neural_support import require_jax  # noqa: E402

jax = require_jax()

from scientific_ai.neural import family, solver  # noqa: E402
from scientific_ai.neural.model import parallel as par  # noqa: E402


@pytest.fixture(scope="module")
def report():
    env = dict(os.environ)
    env["XLA_FLAGS"] = "--xla_force_host_platform_device_count=8"
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "neural.py"),
                        "_distributed-inner"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=1200)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_every_parallel_check_passes_on_eight_simulated_devices(report):
    failed = [c for c in report["checks"] if not c["passed"]]
    assert report["result"] == "PASS", failed
    assert len(report["devices"]) == 8


def test_expert_parallel_dispatch_reproduces_the_single_device_layer(
        report):
    eq = {c["check"]: c for c in report["checks"]
          if c["check"].startswith("expert_parallel_equivalence")}
    assert set(eq) == {f"expert_parallel_equivalence_ep{n}"
                       for n in (8, 4, 2)}
    for c in eq.values():
        assert c["max_abs_difference"] <= 1e-6


def test_the_report_does_not_claim_hardware(report):
    prof = report["execution_profile"]
    assert prof["kind"] == "SIMULATED_MULTI_DEVICE"
    assert prof["hardware_executed"] is False
    assert any("DISTRIBUTED_HARDWARE_VALIDATED is not claimed" in lim
               for lim in report["limitations"])


def test_accumulation_and_sharding_are_judged_on_gradients(report):
    by = {c["check"]: c for c in report["checks"]}
    acc = by["gradient_accumulation_equivalence"]
    assert acc["max_relative_gradient_difference"] <= 1e-5
    # the load-balancing term really does change under accumulation; the
    # report says by how much instead of hiding it
    assert acc["with_load_balancing_term_relative_difference"] > \
        acc["max_relative_gradient_difference"]
    fs = by["fsdp_sharded_gradient_equivalence"]
    assert fs["sharded_leaves"] > 0
    assert "one_adam_step_max_parameter_difference" in fs


def test_a_plan_must_divide_experts_heads_layers_and_batch():
    flag = solver.config_from(family.solve_flagship())
    n64 = par.ExecutionProfile("B300_CLASS_NODE", 64, 8).validate()
    n48 = par.ExecutionProfile("MULTI_GPU_NODE", 48, 8).validate()
    ok = par.ParallelPlan((("data", 8), ("expert", 8))).validate(
        flag, n64, global_batch=1024)
    assert ok["experts_per_expert_rank"] == 8
    assert ok["expert_owner"]["63"] == 7
    cases = [  # (profile, mesh, global batch, legal)
        (n64, (("data", 2), ("expert", 32)), 1024, True),
        (n64, (("data", 8), ("tensor", 8)), 1024, True),
        (n64, (("data", 2), ("pipeline", 32)), 1024, True),
        (n64, (("data", 4), ("expert", 8)), 1024, False),   # 32 != 64
        (n64, (("expert", 64),), 100, False),               # batch
        # 960 splits over 2 x 24, so ONLY the expert count can refuse it
        (n48, (("data", 2), ("expert", 24)), 960, False),   # 64 % 24
        (n48, (("data", 2), ("expert", 24)), 1000, False),  # batch too
        (n48, (("data", 2), ("pipeline", 24)), 1024, False),  # layers
        (n48, (("data", 6), ("tensor", 8)), 1032, True),
        (n64, (("data", 1), ("tensor", 64)), 1024, True),
        (n64, (("data", 1), ("warp", 64)), 1024, False),    # unknown axis
    ]
    for prof, mesh, batch, legal in cases:
        plan = par.ParallelPlan(mesh)
        if legal:
            plan.validate(flag, prof, global_batch=batch)
        else:
            with pytest.raises(par.PlanError):
                plan.validate(flag, prof, global_batch=batch)


def test_a_simulated_profile_cannot_be_a_hardware_execution():
    with pytest.raises(par.PlanError):
        par.ExecutionProfile("SIMULATED_MULTI_DEVICE", 8, 8,
                             hardware_executed=True).validate()
    par.ExecutionProfile("GB300_CLASS_RACK", 72, 8).validate()  # 9 x 8
    with pytest.raises(par.PlanError):
        par.ExecutionProfile("GB300_CLASS_RACK", 72, 10).validate()
    with pytest.raises(par.PlanError):
        par.ExecutionProfile("A_PROVIDER_NAME", 8, 8).validate()


def test_rank_shares_are_disjoint_and_cover_whole_batches():
    order = np.random.Generator(np.random.PCG64(1)).permutation(103)
    parts = [par.rank_indices(103, r, 4, order) for r in range(4)]
    flat = np.concatenate(parts)
    assert len(flat) == len(set(flat.tolist())) == 100
    assert all(len(p) == 25 for p in parts)
    with pytest.raises(par.PlanError):
        par.rank_indices(103, 4, 4, order)


def test_every_parameter_category_has_a_sharding_rule():
    from scientific_ai.neural.accounting import CATEGORIES
    assert set(CATEGORIES) <= set(par.SHARDING_RULES)
