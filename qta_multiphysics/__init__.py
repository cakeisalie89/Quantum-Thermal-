"""QTA non-lumped 1D/2D reduced-order multiphysics forecast layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.
3D is FUTURE_WORK / NOT_IMPLEMENTED (see future_3d.py).
"""
__all__ = ["run_all"]

from .runner import run_all  # noqa: E402
