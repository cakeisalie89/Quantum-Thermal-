"""NF-1T s.54: what a configuration may and may not be."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scientific_ai.neural import family  # noqa: E402
from scientific_ai.neural.config import (AttentionConfig,  # noqa: E402
                                         ConfigError, HeadSpec, ModelConfig,
                                         MoEConfig, PrecisionProfile)
from scientific_ai.neural.solver import (BudgetError,  # noqa: E402
                                         BudgetTarget, solve)


def dev() -> ModelConfig:
    return family.development()


def with_moe(**kw) -> ModelConfig:
    c = dev()
    return replace(c, moe=replace(c.moe, **kw))


def test_the_development_and_flagship_configurations_are_valid():
    dev().validate()
    family.flagship_template().validate()


def test_a_configuration_round_trips_through_its_dict_and_digest():
    c = dev()
    back = ModelConfig.from_dict(c.to_dict())
    assert back == c and back.digest() == c.digest()


def test_any_change_changes_the_digest():
    c = dev()
    assert with_moe(top_k=3).digest() != c.digest()
    assert replace(c, num_layers=3).digest() != c.digest()


def test_from_dict_refuses_an_extra_or_missing_key():
    d = dev().to_dict()
    with pytest.raises(ConfigError):
        ModelConfig.from_dict({**d, "padding_tensors": 1})
    d.pop("num_layers")
    with pytest.raises(ConfigError):
        ModelConfig.from_dict(d)


@pytest.mark.parametrize("hidden", [0, -64, 63, True, 64.0])
def test_an_invalid_hidden_dimension_is_refused(hidden):
    with pytest.raises(ConfigError):
        replace(dev(), hidden_size=hidden).validate()


def test_heads_times_head_dim_must_be_the_hidden_size():
    with pytest.raises(ConfigError, match="num_heads"):
        replace(dev(), attention=AttentionConfig(3, 16, 3)).validate()


@pytest.mark.parametrize("head_dim", [0, -16, 15])
def test_an_invalid_head_dimension_is_refused(head_dim):
    with pytest.raises(ConfigError):
        replace(dev(), attention=AttentionConfig(4, head_dim, 4)).validate()


@pytest.mark.parametrize("kv", [0, 3, 8])
def test_kv_heads_must_divide_the_heads(kv):
    with pytest.raises(ConfigError):
        replace(dev(), attention=AttentionConfig(4, 16, kv)).validate()


def test_grouped_query_attention_is_a_valid_choice():
    replace(dev(), attention=AttentionConfig(4, 16, 2)).validate()


@pytest.mark.parametrize("e", [0, 1, -8, True])
def test_an_invalid_expert_count_is_refused(e):
    with pytest.raises(ConfigError):
        with_moe(num_experts=e).validate()


@pytest.mark.parametrize("k", [0, -1, True])
def test_an_invalid_top_k_is_refused(k):
    with pytest.raises(ConfigError):
        with_moe(top_k=k).validate()


def test_top_k_above_the_expert_count_is_refused():
    with pytest.raises(ConfigError, match="top_k"):
        with_moe(top_k=9).validate()


@pytest.mark.parametrize("f", [0, -128, 1.5])
def test_an_invalid_expert_width_is_refused(f):
    with pytest.raises(ConfigError):
        with_moe(expert_hidden=f).validate()


def test_a_shared_expert_width_without_shared_experts_is_refused():
    with pytest.raises(ConfigError, match="shared"):
        with_moe(shared_hidden=64).validate()
    with pytest.raises(ConfigError, match="shared"):
        with_moe(num_shared_experts=1).validate()
    with_moe(num_shared_experts=1, shared_hidden=64).validate()


@pytest.mark.parametrize("bad", [
    PrecisionProfile(param_dtype="float16"),
    PrecisionProfile(param_dtype="float8_e4m3"),
    PrecisionProfile(compute_dtype="float8_e4m3"),
    PrecisionProfile(router_dtype="bfloat16"),
])
def test_a_malformed_precision_profile_is_refused(bad):
    with pytest.raises(ConfigError):
        replace(dev(), precision=bad).validate()


@pytest.mark.parametrize("kw,match", [
    ({"dense_ffn_hidden": 128}, "no layer is dense"),
    ({"moe": None}, "dense"),
    ({"moe_layer_start": 5}, "no layer"),
    ({"reconstruction_head": "shared"}, "reconstruction_head"),
    ({"heads": ()}, "head"),
    ({"heads": (HeadSpec("a"), HeadSpec("a"))}, "twice"),
    ({"heads": (HeadSpec("a", "bounded_gaussian", 1.0, 0.0),)}, "lower"),
    ({"heads": (HeadSpec("a", "gaussian", 0.0, 1.0),)}, "bounded"),
])
def test_an_unsupported_combination_is_refused(kw, match):
    with pytest.raises(ConfigError, match=match):
        replace(dev(), **kw).validate()


def test_routing_profiles_must_name_the_standard_top_k():
    with pytest.raises(ConfigError, match="standard"):
        with_moe(routing_profiles=(("deep", 3),)).validate()
    with pytest.raises(ConfigError, match="num_experts"):
        with_moe(routing_profiles=(("standard", 2), ("deep", 9))).validate()


@pytest.mark.parametrize("kw", [
    {"total": 0, "active": 1, "total_range": (1, 2),
     "active_range": (1, 1)},
    {"total": 10, "active": 2, "total_range": (11, 20),
     "active_range": (1, 3)},
    {"total": 100, "active": 2, "total_range": (90, 110),
     "active_range": (3, 5)},
    {"total": 100, "active": 100, "total_range": (90, 110),
     "active_range": (90, 100)},
    {"total": 100, "active": 20, "total_range": (110, 90),
     "active_range": (10, 30)},
])
def test_an_invalid_budget_target_is_refused(kw):
    with pytest.raises(BudgetError):
        BudgetTarget(**kw).validate()


def test_a_structure_that_cannot_meet_its_budget_is_refused_not_renamed():
    t = BudgetTarget(total=10_000, active=2_000, total_range=(9_000, 11_000),
                     active_range=(1_500, 2_500))
    with pytest.raises(BudgetError, match="no aligned solution"):
        solve(dev(), t, alignment=64)


@settings(max_examples=150, deadline=None, derandomize=True)
@given(heads=st.integers(1, 16), head_dim=st.integers(1, 64),
       kv=st.integers(1, 16), e=st.integers(2, 64), k=st.integers(1, 64))
def test_validation_accepts_exactly_the_legal_dimensions(heads, head_dim,
                                                         kv, e, k):
    c = replace(dev(), hidden_size=heads * head_dim,
                attention=AttentionConfig(heads, head_dim, kv),
                moe=MoEConfig(num_experts=e, top_k=k, expert_hidden=8))
    legal = heads % kv == 0 and kv <= heads and k <= e
    if legal:
        c.validate()
    else:
        with pytest.raises(ConfigError):
            c.validate()


@pytest.mark.parametrize("field", ["param_dtype", "compute_dtype"])
def test_fp8_is_refused_with_the_reason_it_is_an_estimate_only(field):
    with pytest.raises(ConfigError, match="estimation mode"):
        replace(dev(), precision=PrecisionProfile(
            **{field: "float8_e4m3"})).validate()
