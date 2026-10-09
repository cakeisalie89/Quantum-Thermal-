"""The models and independent checks this repository admits. Default-deny.

One place, so the governed tools resolve a model or a check by name against
a closed set rather than importing whatever a caller names. Registration
computes each model's implementation digest, so a model whose source cannot
be read is not admitted at all.
"""
from __future__ import annotations

from .registry import ModelRegistry


def models() -> ModelRegistry:
    from .models.slab_transient import SlabTransientModel
    from .models.surface_adsorption import SurfaceAdsorptionModel
    from .models.thermal_1d import Thermal1DModel
    from .models.thermal_2d import Thermal2DModel
    from .models.thermal_rc2 import ThermalRC2Model
    reg = ModelRegistry()
    reg.register(Thermal1DModel())
    reg.register(Thermal2DModel())
    reg.register(SurfaceAdsorptionModel())
    reg.register(SlabTransientModel())
    reg.register(ThermalRC2Model())
    return reg


#: External models: executed outside this package (an FMU in its own
#: runtime) and admitted by THIS table, not by the model registry -- their
#: implementation is a binary archive, identified by its digest, never code
#: here. Each names the check that verifies it and where its identity is.
EXTERNAL_MODELS = {
    ("fmi.thermal_rc2", "1.0.0"): {
        "check": "thermal.rc2_fmu",
        "identity": "provenance.fmi.archive_sha256 == implementation_digest",
        "authority": "NON_AUTHORITATIVE",
    },
}


def external_model_problems(bundle) -> list:
    """Why a bundle claiming an external model is not one, or []."""
    key = (bundle.model_id, bundle.model_version)
    spec = EXTERNAL_MODELS.get(key)
    if spec is None:
        return [f"{key[0]}@{key[1]} is not an admitted external model"]
    fmi = (bundle.provenance or {}).get("fmi") or {}
    out = []
    if fmi.get("archive_sha256") != bundle.implementation_digest:
        out.append("the bundle's implementation is not the FMU archive its "
                   "provenance names")
    if (fmi.get("claim_boundary") or {}).get("authority") != \
            spec["authority"]:
        out.append("the FMU's claim boundary is not NON_AUTHORITATIVE")
    return out


def check(check_id: str):
    """``(run_check, check_digest)`` for an admitted independent check."""
    if check_id == "thermal_1d.reduction_2d_radial_disabled":
        from .checks import reduction_2d
        return reduction_2d.run_check, reduction_2d.check_digest
    if check_id == "thermal_2d.reduction_3d_adiabatic_lateral":
        from .checks import reduction_3d
        return reduction_3d.run_check, reduction_3d.check_digest
    if check_id == "surface_adsorption.rk4_pressure_form":
        from .checks import langmuir_rk4
        return langmuir_rk4.run_check, langmuir_rk4.check_digest
    if check_id == "thermal.slab_series":
        from .checks import slab_series
        return slab_series.run_check, slab_series.check_digest
    if check_id == "thermal.slab_fenicsx":
        from .checks import fenicsx_slab
        return fenicsx_slab.run_check, fenicsx_slab.check_digest
    if check_id == "thermal.rc2_fmu":
        from .checks import fmu_rc2
        return fmu_rc2.run_check, fmu_rc2.check_digest
    raise KeyError(f"no independent check {check_id!r} is admitted")
