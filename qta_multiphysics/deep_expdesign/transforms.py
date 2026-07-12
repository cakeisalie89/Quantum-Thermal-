"""Invertible transforms between natural and unconstrained model space.

Used so the density model works in an unconstrained space while priors/outputs
are reported in natural units. Each transform is exactly invertible (verified by
the test suite's round-trip checks).
"""
from __future__ import annotations

import math
from typing import Tuple

import numpy as np


def to_model(value: np.ndarray, transform: str) -> np.ndarray:
    """Map natural-unit values into model space."""
    v = np.asarray(value, dtype=float)
    if transform == "linear":
        return v
    if transform == "log10":
        if np.any(v <= 0):
            raise ValueError("log10 transform requires strictly positive values")
        return np.log10(v)
    raise ValueError(f"unknown transform {transform!r}")


def from_model(z: np.ndarray, transform: str) -> np.ndarray:
    """Inverse of :func:`to_model`."""
    z = np.asarray(z, dtype=float)
    if transform == "linear":
        return z
    if transform == "log10":
        return np.power(10.0, z)
    raise ValueError(f"unknown transform {transform!r}")


def normalize(z: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Affine map [lo, hi] -> [0, 1] in model space."""
    if hi <= lo:
        raise ValueError("hi must exceed lo")
    return (np.asarray(z, dtype=float) - lo) / (hi - lo)


def denormalize(u: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Inverse of :func:`normalize`."""
    return np.asarray(u, dtype=float) * (hi - lo) + lo


def roundtrip_max_abs_error(value: np.ndarray, transform: str) -> float:
    """Return max |x - from_model(to_model(x))| for a transform (test helper)."""
    v = np.asarray(value, dtype=float)
    back = from_model(to_model(v, transform), transform)
    return float(np.max(np.abs(v - back)))
