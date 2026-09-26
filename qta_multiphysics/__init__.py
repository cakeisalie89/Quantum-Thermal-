"""QTA non-lumped 1D/2D reduced-order multiphysics forecast layer.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.
A 3D transient layer exists as forecast-only / benchmark-numerical
validation, reduction-checked against 1D/2D; it is not hardware-validated
and adds no PASS gate (see future_3d.py, STATUS).
"""
__all__ = ["run_all"]

from .runner import run_all  # noqa: E402
