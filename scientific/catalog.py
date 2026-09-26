"""The models and independent checks this repository admits. Default-deny.

One place, so the governed tools resolve a model or a check by name against
a closed set rather than importing whatever a caller names. Registration
computes each model's implementation digest, so a model whose source cannot
be read is not admitted at all.
"""
from __future__ import annotations

from .registry import ModelRegistry


def models() -> ModelRegistry:
    from .models.thermal_1d import Thermal1DModel
    from .models.thermal_2d import Thermal2DModel
    reg = ModelRegistry()
    reg.register(Thermal1DModel())
    reg.register(Thermal2DModel())
    return reg


def check(check_id: str):
    """``(run_check, check_digest)`` for an admitted independent check."""
    if check_id == "thermal_1d.reduction_2d_radial_disabled":
        from .checks import reduction_2d
        return reduction_2d.run_check, reduction_2d.check_digest
    if check_id == "thermal_2d.reduction_3d_adiabatic_lateral":
        from .checks import reduction_3d
        return reduction_3d.run_check, reduction_3d.check_digest
    raise KeyError(f"no independent check {check_id!r} is admitted")
