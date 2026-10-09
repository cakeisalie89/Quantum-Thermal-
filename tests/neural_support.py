"""Shared by the NF-1T test modules: JAX, or a decision about its absence.

The learned-model tests need the ``neural`` extra. Without it a developer's
run SKIPS them with the reason; where ``QTA_NEURAL_REQUIRED=1`` (every CI
job that runs them) its absence is a FAILURE, so a CI run that installed
the wrong environment cannot go green by skipping the substrate's tests.
"""
from __future__ import annotations

import os

import pytest


def require_jax():
    try:
        import jax
    except ImportError:
        if os.environ.get("QTA_NEURAL_REQUIRED") == "1":
            pytest.fail("QTA_NEURAL_REQUIRED=1 and JAX is not installed: "
                        "uv sync --extra neural", pytrace=False)
        pytest.skip("the neural extra is not installed (uv sync --extra "
                    "neural)", allow_module_level=True)
    return jax


def batch(cfg, b: int = 3, f: int | None = None, seed: int = 1):
    """A synthetic token batch of the right shapes for ``cfg``."""
    import jax.numpy as jnp
    import numpy as np
    f = cfg.feature_vocab_size if f is None else f
    rng = np.random.Generator(np.random.PCG64(seed))
    return {
        "feature_ids": jnp.asarray(np.tile(np.arange(f, dtype=np.int32)
                                           % cfg.feature_vocab_size,
                                           (b, 1))),
        "context_ids": jnp.zeros((b, f), jnp.int32),
        "validity": jnp.zeros((b, f), jnp.int32),
        "dim_exponents": jnp.asarray(rng.integers(-2, 3, size=(b, f, 7))
                                     .astype(np.float32)),
        "dim_class": jnp.zeros((b, f), jnp.int32),
        "values": jnp.asarray(rng.normal(size=(b, f, 4))
                              .astype(np.float32)),
    }
