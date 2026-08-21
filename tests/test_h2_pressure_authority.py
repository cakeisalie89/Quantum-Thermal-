"""§14 regression: named H2 pressures, and an unresolved conflict left visible.

Five different residual-H2 partial pressures were spread across qta_full_sim as
bare literals, and "post-bakeout H2 pressure" alone had three values (1e-12,
2e-12, 5e-12) depending on which line you read. They are genuinely distinct
concepts -- a modelled chamber state, a literature assumption, a design target,
a measurement threshold -- so they are named rather than collapsed.

One conflict is NOT resolved by this: gate B4's coverage forecast uses the
literature ASSUMED 5e-12 Pa while the chamber-state path uses the modelled
1e-12 Pa for the same quantity, and the Monte-Carlo range excludes the 5e-12
nominal. Which is authoritative is an owner decision. These tests keep that
visible rather than letting it be silently normalized away.

MODEL-ONLY / FORECAST-ONLY. Software verification; not a hardware statement.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qta_full_sim as Q                                        # noqa: E402


def test_each_named_quantity_keeps_its_distinct_value():
    """Distinct concepts must not have been collapsed onto one number."""
    assert Q.P_H2_PRE_BAKEOUT_PA == 1e-10
    assert Q.P_H2_POST_BAKEOUT_NEG_PA == 1e-12
    assert Q.P_H2_POST_BAKEOUT_ONLY_PA == 2e-12
    assert Q.P_H2_POST_BAKEOUT_ASSUMED_PA == 5e-12
    assert Q.P_H2_ACCEPTANCE_TARGET_PA == 2e-12
    assert Q.P_H2_RGA_VALIDATION_THRESHOLD_PA == 2e-14
    assert Q.P_H2_MC_RANGE_PA == (5e-13, 2e-12)


def test_pre_bakeout_is_worse_than_post_bakeout():
    """Sanity on the ordering the names assert."""
    assert Q.P_H2_PRE_BAKEOUT_PA > Q.P_H2_POST_BAKEOUT_ONLY_PA
    assert Q.P_H2_POST_BAKEOUT_ONLY_PA > Q.P_H2_POST_BAKEOUT_NEG_PA
    assert Q.P_H2_RGA_VALIDATION_THRESHOLD_PA < Q.P_H2_POST_BAKEOUT_NEG_PA


def test_chamber_state_reports_the_named_values():
    from copy import deepcopy
    pre = deepcopy(Q.CURRENT_CHAMBER)
    assert pre.P_H2_Pa() == Q.P_H2_PRE_BAKEOUT_PA
    baked = deepcopy(Q.CURRENT_CHAMBER)
    baked.bakeout_done = True
    assert baked.P_H2_Pa() == Q.P_H2_POST_BAKEOUT_ONLY_PA
    baked.NEG_installed = True
    assert baked.P_H2_Pa() == Q.P_H2_POST_BAKEOUT_NEG_PA


def test_no_bare_h2_pressure_literals_remain_in_consumers():
    """Every consumer must reference a name, not re-type the number."""
    src = (os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    text = open(os.path.join(src, "qta_full_sim.py"), encoding="utf-8").read()
    body = text.split("H2_PRESSURE_AUTHORITY_UNRESOLVED", 1)[1]
    offenders = []
    for m in re.finditer(r"P_H2\s*=\s*([0-9.]+e-1[0-9])", body):
        offenders.append(m.group(0))
    for m in re.finditer(r'"P_H2_Pa":\s*([0-9.]+e-1[0-9])', body):
        offenders.append(m.group(0))
    assert not offenders, f"bare H2 pressure literals still in consumers: {offenders}"


def test_the_unresolved_conflict_is_recorded_not_normalized():
    rec = Q.H2_PRESSURE_AUTHORITY_UNRESOLVED
    assert rec["status"] == "UNRESOLVED_REQUIRES_OWNER_AUTHORITY"
    vals = set(rec["competing_values_Pa"].values())
    assert len(vals) == 3, "the competing values must remain distinct"
    assert vals == {1e-12, 2e-12, 5e-12}
    assert "picking the most frequent literal" in rec["not_resolved_by"]
    assert "owner decision" in rec["what_would_resolve_it"]


def test_the_monte_carlo_range_still_excludes_the_nominal():
    """This is the conflict, stated as an executable fact.

    If someone later 'tidies' this by widening the range or moving the
    nominal, that is a scientific decision and must be a deliberate one.
    """
    lo, hi = Q.P_H2_MC_RANGE_PA
    assert not (lo <= Q.P_H2_POST_BAKEOUT_ASSUMED_PA <= hi), (
        "the MC range now contains the ASSUMED nominal; if that was "
        "intentional, H2_PRESSURE_AUTHORITY_UNRESOLVED must be updated to "
        "say so")


def test_coverage_forecast_and_chamber_model_still_disagree():
    """The conflict is real and must not be papered over silently."""
    assert Q.P_H2_POST_BAKEOUT_ASSUMED_PA != Q.P_H2_POST_BAKEOUT_NEG_PA
    rec = Q.H2_PRESSURE_AUTHORITY_UNRESOLVED
    assert "gate B4" in rec["conflict"]


if __name__ == "__main__":
    ns = dict(globals())
    fails = 0
    for name, fn in sorted(ns.items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:                                # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if fails else 0)
