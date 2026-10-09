"""JAX, when the ``neural`` extra is installed -- and a named refusal when not.

The import is guarded so that importing ``scientific_ai.neural.model``
without the extra fails with :class:`NeuralBackendUnavailable` at first use,
not with an ImportError from deep inside a call, and so that the
dependency-declaration check (D-2026-74) sees an optional import.
"""
from __future__ import annotations

try:
    import jax
    import jax.numpy as jnp
except ImportError:   # the neural extra is not installed
    # absent, and require() refuses rather than returning these
    jax = None  # type: ignore[assignment]
    jnp = None  # type: ignore[assignment]


class NeuralBackendUnavailable(RuntimeError):
    """The learned-model substrate needs ``uv sync --extra neural``."""


def require():
    if jax is None:
        raise NeuralBackendUnavailable(
            "scientific_ai.neural.model needs JAX: uv sync --extra neural")
    return jax, jnp


def versions() -> dict:
    j, _ = require()
    import importlib.metadata as md
    import platform
    out = {"python": platform.python_version(), "jax": j.__version__}
    for dist in ("jaxlib", "ml_dtypes", "numpy"):
        try:
            out[dist] = md.version(dist)
        except md.PackageNotFoundError:
            out[dist] = None
    out["backend"] = j.default_backend()
    out["devices"] = [str(d) for d in j.devices()]
    return out
