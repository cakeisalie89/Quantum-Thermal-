"""The architecture that meets a parameter budget -- solved, not guessed.

A design fixes the STRUCTURE (width, depth, attention, how many experts per
layer, which layers mix) for reasons it states. Two quantities are then a
matter of arithmetic, and this module does the arithmetic:

* ``expert_hidden`` sets the TOTAL. The trainable count is affine in it
  with slope ``moe_layers * (num_experts + shared share) * 3 * d`` -- every
  routed expert of every MoE layer widens together -- so the solver takes
  the aligned width whose exact total (``accounting.count``) is nearest the
  target, ties to the smaller width;
* ``top_k`` then sets the ACTIVE count, affine in ``top_k`` with slope
  ``moe_layers * 3 * d * expert_hidden``; the solver takes the ``top_k`` in
  ``[1, num_experts]`` whose exact active count is nearest the target, ties
  to the smaller ``top_k``.

The targets are TRAINABLE parameters and active parameters per token as
``accounting`` defines them. Both results must land inside their declared
ranges or the solve is refused (:class:`BudgetError`): a configuration that
misses its range is not renamed to fit it, and nothing is padded to reach a
number -- every parameter the solver adds is expert width the model uses.

Every candidate evaluated is recorded, with its exact counts, so the
choice can be re-derived from the record alone.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from . import accounting
from .config import ConfigError, ModelConfig


class BudgetError(ValueError):
    """A target that is malformed, or a structure that cannot meet it."""


@dataclass(frozen=True)
class BudgetTarget:
    total: int
    active: int
    total_range: tuple
    active_range: tuple

    def validate(self) -> "BudgetTarget":
        for name in ("total", "active"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise BudgetError(f"target {name} must be a positive int, "
                                  f"got {v!r}")
        for name in ("total_range", "active_range"):
            r = getattr(self, name)
            if not (isinstance(r, tuple) and len(r) == 2
                    and all(isinstance(x, int) and not isinstance(x, bool)
                            for x in r) and 0 < r[0] <= r[1]):
                raise BudgetError(f"{name} must be (lo, hi) ints with "
                                  f"0 < lo <= hi, got {r!r}")
        if not self.total_range[0] <= self.total <= self.total_range[1]:
            raise BudgetError(f"target total {self.total} lies outside its "
                              f"own range {self.total_range}")
        if not self.active_range[0] <= self.active <= self.active_range[1]:
            raise BudgetError(f"target active {self.active} lies outside "
                              f"its own range {self.active_range}")
        if self.active >= self.total or \
                self.active_range[1] >= self.total_range[0]:
            raise BudgetError("an active target at or above the total is "
                              "not a mixture of experts")
        return self

    def to_dict(self) -> dict:
        return {"total": self.total, "active": self.active,
                "total_range": list(self.total_range),
                "active_range": list(self.active_range),
                "total_means": "trainable_parameters",
                "active_means": "estimated_total_active_parameters_per_token"}


def _with(template: ModelConfig, f: int, k: int) -> ModelConfig:
    profiles = template.moe_block.routing_profiles
    if profiles:
        shift = k - dict(profiles)["standard"]
        profiles = tuple((n, max(1, min(template.moe_block.num_experts,
                                         pk + shift)))
                         for n, pk in profiles)
    return template.with_moe(expert_hidden=f, top_k=k,
                             routing_profiles=profiles)


def solve(template: ModelConfig, target: BudgetTarget, *,
          alignment: int = 256) -> dict:
    """Resolve ``expert_hidden`` and ``top_k`` of ``template`` against
    ``target``. Returns the solve record; ``record["config"]`` is the
    configuration as a dict (``ModelConfig.from_dict`` rebuilds it)."""
    target.validate()
    if isinstance(alignment, bool) or not isinstance(alignment, int) \
            or alignment < 1:
        raise BudgetError(f"alignment must be a positive int: {alignment!r}")
    if template.moe is None:
        raise BudgetError("the budget solver sizes experts; the template "
                          "has none")
    try:
        template.validate()
    except ConfigError as exc:
        raise BudgetError(f"template is not a valid configuration: "
                          f"{exc}") from exc
    m = template.moe
    d = template.hidden_size
    n_moe = len(template.moe_layer_indices)
    evaluated = []

    def total_at(f: int) -> int:
        pc = accounting.count(_with(template, f, 1))
        evaluated.append({"expert_hidden": f, "top_k": 1,
                          "trainable": pc.trainable_parameters})
        return pc.trainable_parameters

    base = total_at(alignment)
    slope = n_moe * m.num_experts * 3 * d
    raw = (target.total - base) / slope + alignment
    lo = max(alignment, math.floor(raw / alignment) * alignment)
    candidates = sorted({lo, lo + alignment})
    f = min(candidates, key=lambda x: (abs(total_at(x) - target.total), x))

    def active_at(k: int) -> int:
        pc = accounting.count(_with(template, f, k))
        evaluated.append({"expert_hidden": f, "top_k": k,
                          "active": pc.active_parameters_per_token})
        return pc.active_parameters_per_token

    per_k = n_moe * 3 * d * f
    a1 = active_at(1)
    k_raw = (target.active - a1) / per_k + 1
    ks = sorted({max(1, min(m.num_experts, math.floor(k_raw))),
                 max(1, min(m.num_experts, math.floor(k_raw) + 1))})
    k = min(ks, key=lambda x: (abs(active_at(x) - target.active), x))

    cfg = _with(template, f, k).validate()
    pc = accounting.count(cfg)
    total, active = pc.trainable_parameters, pc.active_parameters_per_token
    in_total = target.total_range[0] <= total <= target.total_range[1]
    in_active = target.active_range[0] <= active <= target.active_range[1]
    if not (in_total and in_active):
        raise BudgetError(
            f"no aligned solution in range: expert_hidden {f}, top_k {k} "
            f"gives trainable {total} (range {target.total_range}) and "
            f"active {active} (range {target.active_range})")
    return {
        "method": "affine solve over expert_hidden then top_k; exact "
                  "counts from scientific_ai.neural.accounting",
        "template_digest": accounting.count(_with(template, alignment, 1))
        .config_digest,
        "target": target.to_dict(), "alignment": alignment,
        "slope_per_expert_hidden": slope, "slope_per_top_k": per_k,
        "expert_hidden": f, "top_k": k,
        "trainable_parameters": total,
        "active_parameters_per_token": active,
        "total_deviation": total - target.total,
        "active_deviation": active - target.active,
        "total_relative_deviation": (total - target.total) / target.total,
        "active_relative_deviation": (active - target.active)
        / target.active,
        "evaluated": evaluated,
        "config_digest": pc.config_digest,
        "config": cfg.to_dict(),
    }


def config_from(record: dict) -> ModelConfig:
    """The configuration a solve record names, checked against its digest."""
    cfg = ModelConfig.from_dict(record["config"])
    if cfg.digest() != record["config_digest"]:
        raise BudgetError("the solve record's configuration does not match "
                          "its digest")
    return cfg


def with_experts(template: ModelConfig, num_experts: int) -> ModelConfig:
    """``template`` with a different expert count -- for comparing
    alternatives in the same solve, never for the flagship itself."""
    return replace(template, moe=replace(template.moe_block,
                                         num_experts=num_experts))
