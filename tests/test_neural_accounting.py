"""NF-1T s.55 and s.39/40: exact parameter accounting, without JAX.

The counts are re-derived here by hand for the development model, compared
category by category with the network's own tensors in
``test_neural_model.py``, and the flagship's are pinned: a change to the
architecture changes these numbers on purpose, in review, or not at all.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scientific_ai.neural import (accounting, estimates,  # noqa: E402
                                  family, solver, tokens)
from scientific_ai.neural.units import DIMENSION_CLASSES  # noqa: E402

FLAGSHIP_TRAINABLE = 996_509_217_800
FLAGSHIP_ACTIVE = 200_832_889_864


def dev():
    return family.development()


def flagship():
    return solver.config_from(family.solve_flagship())


def hand_count(c) -> dict:
    """The development model's tensors, written out longhand."""
    d, L, vf = c.hidden_size, c.num_layers, c.feature_vocab_size
    e, k, f = c.moe.num_experts, c.moe.top_k, c.moe.expert_hidden
    hv, nh = c.value_encoder_hidden, len(c.heads)
    return {
        "feature_embedding": vf * d,
        "readout_embedding": c.num_readout_tokens * d,
        "context_embedding": (c.context_vocab_size + 2) * d,
        "dimensional_encoder": (7 + 4) * d,
        "numerical_encoder": 4 * hv + hv + hv * d + d,
        "attention": L * 4 * d * d,
        "normalization": 2 * L * d + d,
        "dense_ffn": 0,
        "router": L * d * e,
        "expert": L * e * 3 * d * f,
        "shared_expert": 0,
        "output_head": nh * (d + 1),
        "uncertainty_head": nh * (d + 1),
        "reconstruction_head": 0,
        "_active_expert": L * k * 3 * d * f,
    }


def test_the_development_count_matches_a_longhand_derivation():
    c = dev()
    pc = accounting.count(c)
    want = hand_count(c)
    active_expert = want.pop("_active_expert")
    assert pc.by_category == want
    assert pc.trainable_parameters == sum(want.values()) == 431_784
    assert pc.expert_active_parameters == active_expert


def test_trainable_shared_expert_router_and_head_counts_are_exact():
    c = dev()
    pc = accounting.count(c).to_dict()
    d, L = c.hidden_size, c.num_layers
    e, f = c.moe.num_experts, c.moe.expert_hidden
    assert pc["expert_parameters"] == L * e * 3 * d * f
    assert pc["router_parameters"] == L * d * e
    assert pc["shared_parameters"] == pc["trainable_parameters"] \
        - pc["expert_parameters"]
    assert pc["output_head_parameters"] == len(c.heads) * (d + 1)
    assert pc["uncertainty_head_parameters"] == len(c.heads) * (d + 1)
    assert pc["attention_parameters"] == L * 4 * d * d
    assert pc["expert_parameters_per_expert"] == 3 * d * f


def test_a_tied_parameter_is_counted_once():
    c = dev()
    none = accounting.count(replace(c, reconstruction_head="none"))
    tied = accounting.count(replace(c, reconstruction_head="tied"))
    untied = accounting.count(replace(c, reconstruction_head="untied"))
    vfd = c.feature_vocab_size * c.hidden_size
    # tied: one bias more, the decoder IS the identity table
    assert tied.trainable_parameters == none.trainable_parameters + 1
    assert tied.tied_parameters == vfd
    assert untied.trainable_parameters == none.trainable_parameters + vfd + 1
    assert untied.tied_parameters == 0
    assert tied.by_category["feature_embedding"] == vfd


def test_buffers_are_not_trainable_parameters():
    c = dev()
    pc = accounting.count(c)
    assert pc.non_trainable_parameters == 2 * c.feature_vocab_size \
        + 2 * len(c.heads)
    assert pc.total_parameters == pc.trainable_parameters \
        + pc.non_trainable_parameters
    assert pc.trainable_parameters == sum(pc.by_category.values())


def test_active_shared_count_by_its_definition():
    c = dev()
    pc = accounting.count(c)
    d = c.hidden_size
    want = (d + 2 * d + (tokens.N_BASE_DIMS + 1) * d
            + pc.by_category["numerical_encoder"]
            + pc.by_category["attention"] + pc.by_category["normalization"]
            + pc.by_category["router"] + pc.by_category["output_head"]
            + pc.by_category["uncertainty_head"])
    assert pc.shared_active_parameters == want
    assert pc.active_by_category["readout_embedding"] == 0
    assert pc.active_by_category["reconstruction_head"] == 0
    assert pc.active_parameters_per_token == \
        pc.shared_active_parameters + pc.expert_active_parameters


def test_active_count_moves_by_exactly_one_expert_per_moe_layer_per_k():
    c = dev()
    per = 3 * c.hidden_size * c.moe.expert_hidden * c.num_layers
    prev = None
    for k in range(1, c.moe.num_experts + 1):
        pc = accounting.count(replace(c, moe=replace(c.moe, top_k=k)))
        assert pc.expert_active_parameters == k * per
        if prev is not None:
            assert pc.active_parameters_per_token - prev == per
        prev = pc.active_parameters_per_token
        # the total does not depend on k: all experts exist either way
        assert pc.trainable_parameters == accounting.count(c) \
            .trainable_parameters


def test_counting_all_experts_as_active_is_not_the_active_count():
    pc = accounting.count(flagship())
    assert pc.active_parameters_per_token < pc.trainable_parameters
    assert pc.expert_active_parameters * pc.num_experts_per_moe_layer \
        == pc.experts_selected_per_token * pc.expert_parameters


def test_the_flagship_counts_are_pinned_and_in_range():
    pc = accounting.count(flagship())
    assert pc.trainable_parameters == FLAGSHIP_TRAINABLE
    assert pc.active_parameters_per_token == FLAGSHIP_ACTIVE
    lo, hi = family.FLAGSHIP_TARGET.total_range
    assert lo <= pc.trainable_parameters <= hi
    lo, hi = family.FLAGSHIP_TARGET.active_range
    assert lo <= pc.active_parameters_per_token <= hi
    assert pc.moe_layer_count == 64 and pc.num_experts_per_moe_layer == 64
    assert pc.experts_selected_per_token == 12
    assert flagship().moe.expert_hidden == 9728


def test_the_flagship_solve_is_the_nearest_aligned_solution():
    rec = family.solve_flagship()
    tpl = family.flagship_template()
    f, k = rec["expert_hidden"], rec["top_k"]
    tgt = family.FLAGSHIP_TARGET

    def at(ff, kk):
        # the solver's own edit, which keeps the routing profiles valid
        return accounting.count(solver._with(tpl, ff, kk))

    best = abs(at(f, k).trainable_parameters - tgt.total)
    for ff in (f - 256, f + 256):
        assert abs(at(ff, k).trainable_parameters - tgt.total) >= best
    besta = abs(at(f, k).active_parameters_per_token - tgt.active)
    for kk in (k - 1, k + 1):
        assert abs(at(f, kk).active_parameters_per_token - tgt.active) \
            >= besta


def test_every_routing_profile_reports_its_exact_active_count():
    c = flagship()
    pc = accounting.count(c)
    per = pc.moe_layer_count * pc.expert_parameters_per_expert
    for name, k in c.moe.routing_profiles:
        prof = pc.routing_profiles[name]
        assert prof["top_k"] == k
        assert prof["active_parameters_per_token"] == \
            pc.shared_active_parameters + k * per
    assert pc.routing_profiles["standard"]["active_parameters_per_token"] \
        == FLAGSHIP_ACTIVE


def test_a_batch_touches_at_most_every_expert_and_at_least_the_active_set():
    c = dev()
    pc = accounting.count(c)
    one = accounting.batch_touched_upper_bound(c, 1)
    many = accounting.batch_touched_upper_bound(c, 10_000)
    assert one >= pc.active_parameters_per_token - c.feature_vocab_size \
        * c.hidden_size
    assert many == pc.trainable_parameters
    with pytest.raises(ValueError):
        accounting.batch_touched_upper_bound(c, 0)


def test_every_ladder_rung_lands_in_its_ranges():
    for name, rec in family.solve_ladder():
        t = rec["target"]
        assert t["total_range"][0] <= rec["trainable_parameters"] \
            <= t["total_range"][1], name
        assert t["active_range"][0] <= rec["active_parameters_per_token"] \
            <= t["active_range"][1], name
        assert solver.config_from(rec).digest() == rec["config_digest"]


def test_a_solve_record_that_does_not_match_its_digest_is_refused():
    rec = family.solve_flagship()
    rec["config"]["moe"]["router_temperature"] = 2.0   # valid, different
    with pytest.raises(solver.BudgetError, match="digest"):
        solver.config_from(rec)


def test_estimates_label_what_they_measure_and_add_up():
    c = flagship()
    e = estimates.estimate(c)
    pc = accounting.count(c)
    ps = e["parameter_storage"]
    assert "parameter-only" in ps["label"]
    assert ps["bfloat16_bytes"] == 2 * pc.trainable_parameters
    assert ps["float32_bytes"] == 4 * pc.trainable_parameters
    assert ps["float8_e4m3_bytes"] > pc.trainable_parameters
    ts = e["training_state"]
    assert ts["total_bytes"] == ts["compute_copy_bytes"] \
        + ts["master_weights_bytes"] + ts["gradient_bytes"] \
        + ts["optimizer_moments_bytes"]
    assert ts["bytes_per_parameter"] == 18.0
    fl = e["flops"]
    assert fl["forward_flops_per_token"] >= 2 * FLAGSHIP_ACTIVE
    assert fl["training_flops_per_token"] == 3 * fl["forward_flops_per_token"]
    assert "not a benchmark" in e["status"]
    assert e["communication"]["experts_selected_per_token"] == 12
    assert e["inference_state"]["parameter_bytes"] == ps["bfloat16_bytes"]


def test_the_dimension_class_table_is_the_one_the_count_uses():
    assert accounting.N_DIM_CLASSES == len(DIMENSION_CLASSES) == 4
    assert accounting.N_VALUE == len(tokens.VALUE_CHANNELS) == 4


def test_grouped_query_attention_counts_its_smaller_key_value_heads():
    from scientific_ai.neural.config import AttentionConfig
    c = replace(dev(), attention=AttentionConfig(4, 16, 2))
    d, L, kv = c.hidden_size, c.num_layers, 2 * 16
    assert accounting.count(c).by_category["attention"] == \
        L * (2 * d * d + 2 * d * kv)
