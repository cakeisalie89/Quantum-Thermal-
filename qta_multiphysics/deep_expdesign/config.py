"""Configuration for the deep (simulation-based) experimental-design layer.

This module centralises every tunable: execution profiles, RNG seeds, device /
dtype, and — most importantly — the *trust thresholds* that gate whether a
learned surrogate is ever allowed to influence experiment ordering. Thresholds
live here (not scattered through the code) and are deliberately conservative.

Backend decision (documented, per directive):
    The directive prefers PyTorch / a simulation-based-inference dependency.
    In this repository's sandboxed network only ``pypi.org`` /
    ``files.pythonhosted.org`` are reachable; the CPU-only PyTorch wheel index
    (``download.pytorch.org``) is blocked, and the default pypi ``torch`` wheel
    bundles CUDA (large, GPU-oriented), which contradicts the mandatory
    CPU-only / no-CUDA unit-test requirement. We therefore implement the deep
    density/EIG models as a *documented neural density estimator with
    reproducible CPU support* in pure NumPy/SciPy (the directive's explicitly
    allowed third path). This keeps the unit suite deterministic and CUDA-free.
    If a pinned CPU PyTorch/SBI dependency becomes installable, the
    ``posterior_model`` / ``eig_surrogate`` backends can be swapped behind their
    existing interfaces without changing the rest of the package.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

SCHEMA_VERSION = "deep_expdesign/1.0.0"

# The pure-NumPy CPU backend actually used (see module docstring for rationale).
BACKEND = "numpy_cpu"
BACKEND_REASON = (
    "CPU-only torch wheel index (download.pytorch.org) is blocked in this "
    "sandbox and the pypi torch wheel bundles CUDA; using the directive-allowed "
    "reproducible-CPU neural density estimator implemented in NumPy/SciPy."
)


@dataclass(frozen=True)
class TrustThresholds:
    """Numerical gates a learned surrogate must pass to control ordering.

    Conservative by design. Every one must pass before a surrogate may be marked
    ``VALIDATED_SURROGATE`` / ``controls_experiment_ordering=true``.
    """
    max_mean_abs_eig_error_nats: float = 0.05
    max_max_abs_eig_error_nats: float = 0.15
    max_normalized_eig_error: float = 1.0        # error / direct-MC SE, averaged
    min_spearman_rank_corr: float = 0.95
    min_top1_agreement: float = 1.0              # must match the direct-MC top pick
    min_top3_agreement: float = 0.66
    max_calibration_error: float = 0.10          # max |empirical - nominal| coverage
    min_posterior_coverage_90: float = 0.80      # 90% CR must cover >= 80% empirically
    max_repeat_seed_eig_std_nats: float = 0.02   # cross-seed stability of predictions

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DeepConfig:
    profile: str = "ci"
    backend: str = BACKEND
    device: str = "cpu"
    dtype: str = "float64"

    # dataset
    n_dataset: int = 64            # total simulation samples (ci)
    sampler: str = "lhs"           # 'lhs' or 'sobol'
    split_fractions: tuple = (0.6, 0.2, 0.2)   # train / val / test (sum to 1)

    # model (pure-numpy conditional mixture-density posterior + EIG head)
    hidden_units: int = 32
    n_mixture: int = 3
    train_steps: int = 300
    learning_rate: float = 5e-3
    weight_decay: float = 1e-4

    # validation benchmark
    n_benchmark_designs: int = 6   # deterministic design set for direct-MC compare
    eig_ref_outer: int = 600
    eig_ref_inner: int = 600
    eig_ref_batches: int = 8
    n_repeat_seeds: int = 3

    # OOD
    ood_quantile: float = 0.99     # training-support quantile for OOD boundary
    ood_mahalanobis_factor: float = 1.5

    # seeds / determinism
    master_seed: int = 20240601
    deterministic: bool = True

    thresholds: TrustThresholds = field(default_factory=TrustThresholds)

    def validate(self) -> "DeepConfig":
        if self.profile not in {"ci", "standard", "heavy"}:
            raise ValueError("profile must be 'ci', 'standard', or 'heavy'")
        if self.device not in {"cpu", "cuda"}:
            raise ValueError("device must be 'cpu' or 'cuda'")
        if abs(sum(self.split_fractions) - 1.0) > 1e-9 or len(self.split_fractions) != 3:
            raise ValueError("split_fractions must be three values summing to 1")
        if any(f <= 0 for f in self.split_fractions):
            raise ValueError("split_fractions must be positive")
        if self.n_dataset < 8:
            raise ValueError("n_dataset must be >= 8")
        if self.sampler not in {"lhs", "sobol"}:
            raise ValueError("sampler must be 'lhs' or 'sobol'")
        if self.n_mixture < 1:
            raise ValueError("n_mixture must be >= 1")
        return self

    def as_dict(self) -> dict:
        d = asdict(self)
        d["schema_version"] = SCHEMA_VERSION
        d["backend_reason"] = BACKEND_REASON
        return d


def ci_config(**overrides) -> DeepConfig:
    """Tiny, deterministic, CPU-only configuration for the unit suite / CI."""
    base = dict(profile="ci", n_dataset=256, hidden_units=32, n_mixture=3,
                train_steps=600, n_benchmark_designs=6, eig_ref_outer=300,
                eig_ref_inner=300, eig_ref_batches=3, n_repeat_seeds=2)
    base.update(overrides)
    return DeepConfig(**base).validate()


def standard_config(**overrides) -> DeepConfig:
    """Larger CPU (optionally GPU if a backend supports it) configuration."""
    base = dict(profile="standard", n_dataset=512, hidden_units=64, n_mixture=5,
                train_steps=1500, n_benchmark_designs=6, eig_ref_outer=600,
                eig_ref_inner=600, eig_ref_batches=8, n_repeat_seeds=3)
    base.update(overrides)
    return DeepConfig(**base).validate()


def heavy_config(**overrides) -> DeepConfig:
    """Heavy profile (more samples / steps). GPU optional; CPU fallback mandatory."""
    base = dict(profile="heavy", n_dataset=4096, hidden_units=128, n_mixture=8,
                train_steps=6000, n_benchmark_designs=6, eig_ref_outer=1200,
                eig_ref_inner=1200, eig_ref_batches=12, n_repeat_seeds=5)
    base.update(overrides)
    return DeepConfig(**base).validate()


def config_for(profile: str, **overrides) -> DeepConfig:
    return {"ci": ci_config, "standard": standard_config,
            "heavy": heavy_config}[profile](**overrides)
