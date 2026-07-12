"""Deterministic simulation-dataset generation for the deep layer.

Latin-hypercube / Sobol sampling of the trainable latent subspace at a fixed
design (or a design sampler), evaluated through the simulator adapter with
deterministic per-sample seeds. Produces a reproducible train/validation/test
split with no leakage, a SHA-256 over the canonical serialization, and a compact
summary. Bulk arrays are written under ``outputs/deep_expdesign/`` and are NOT
committed by default; only summaries/hashes/schemas/CI fixtures are canonical.

All data is SIMULATION_GENERATED / FORECAST_ONLY / NOT_MEASURED_IN_THIS_SYSTEM.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from .config import DeepConfig, SCHEMA_VERSION
from .schemas import DatasetSummary, SIMULATION_LABELS
from . import parameter_space as _ps
from .design_space import ExperimentDesign
from .simulator_adapter import simulate, observable_names, _source_commit


@dataclass
class Dataset:
    latent_names: list
    observable_names: list
    theta: np.ndarray          # (n, d_latent) natural units
    theta_model: np.ndarray    # (n, d_latent) model space
    x: np.ndarray              # (n, d_obs)
    success: np.ndarray        # (n,) bool
    design: dict               # the fixed design used (dict)
    split: dict                # {'train': idx, 'val': idx, 'test': idx}
    sha256: str
    source_commit: str

    def subset(self, which: str):
        idx = self.split[which]
        return self.theta_model[idx], self.x[idx]


def _benchmark_design() -> ExperimentDesign:
    """A fixed, valid Mode-D sensing design used for dataset generation."""
    return ExperimentDesign(experiment_id="EXP-A", pulse_sequence="hahn", mode="D",
                            measurement_time=1e-3, surface_temperature=1e-2,
                            he3_coverage_target=1.0, recovery_duration=0.0)


def _canonical_bytes(theta: np.ndarray, x: np.ndarray, success: np.ndarray) -> bytes:
    """Stable byte serialization for hashing (rounded to avoid FP noise)."""
    payload = {
        "theta": np.round(theta, 10).tolist(),
        "x": np.round(x, 10).tolist(),
        "success": success.astype(int).tolist(),
    }
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def _split_indices(n: int, fractions: tuple, seed: int) -> dict:
    """Disjoint train/val/test indices (deterministic, no leakage)."""
    rng = np.random.default_rng(seed)
    perm = rng.permutation(n)
    n_tr = int(round(fractions[0] * n))
    n_va = int(round(fractions[1] * n))
    n_tr = min(n_tr, n)
    n_va = min(n_va, n - n_tr)
    train = np.sort(perm[:n_tr])
    val = np.sort(perm[n_tr:n_tr + n_va])
    test = np.sort(perm[n_tr + n_va:])
    return {"train": train, "val": val, "test": test}


def generate_dataset(cfg: DeepConfig, *, repo_root: Optional[Path] = None,
                     design: Optional[ExperimentDesign] = None) -> Dataset:
    """Generate the deterministic simulation dataset described by ``cfg``."""
    cfg.validate()
    root = Path(repo_root) if repo_root else Path(__file__).resolve().parents[2]
    latents = _ps.build_latent_parameters(root)
    tr = _ps.trainable_subspace(latents)
    design = design or _benchmark_design()

    samp = _ps.sample_prior(latents, n=cfg.n_dataset, seed=cfg.master_seed,
                            sampler=cfg.sampler)
    names = samp["names"]
    theta_nat = samp["natural"]
    theta_mod = samp["model"]
    obs_names = observable_names()

    x = np.full((cfg.n_dataset, len(obs_names)), np.nan)
    success = np.zeros(cfg.n_dataset, dtype=bool)
    for i in range(cfg.n_dataset):
        theta_i = {names[j]: float(theta_nat[i, j]) for j in range(len(names))}
        # deterministic per-sample seed
        resp = simulate(theta_i, design, seed=cfg.master_seed + 7919 * (i + 1),
                        repo_root=root)
        success[i] = resp.success
        if resp.success:
            x[i, :] = [resp.observables[k] for k in obs_names]

    sha = hashlib.sha256(_canonical_bytes(theta_nat, x, success)).hexdigest()
    split = _split_indices(cfg.n_dataset, cfg.split_fractions, cfg.master_seed + 13)
    return Dataset(latent_names=names, observable_names=obs_names, theta=theta_nat,
                   theta_model=theta_mod, x=x, success=success, design=design.to_dict(),
                   split=split, sha256=sha, source_commit=_source_commit(root))


def splits_disjoint(ds: Dataset) -> bool:
    a, b, c = (set(ds.split["train"].tolist()), set(ds.split["val"].tolist()),
               set(ds.split["test"].tolist()))
    union_ok = len(a | b | c) == (len(a) + len(b) + len(c))
    covers = len(a | b | c) == ds.theta.shape[0]
    return union_ok and covers


def summarize(ds: Dataset, cfg: DeepConfig) -> DatasetSummary:
    return DatasetSummary(
        schema_version=SCHEMA_VERSION,
        n_total=int(ds.theta.shape[0]),
        n_success=int(ds.success.sum()),
        n_failure=int((~ds.success).sum()),
        n_train=int(len(ds.split["train"])),
        n_val=int(len(ds.split["val"])),
        n_test=int(len(ds.split["test"])),
        latent_names=list(ds.latent_names),
        design_names=sorted(ds.design.keys()),
        observable_names=list(ds.observable_names),
        sampler=cfg.sampler, master_seed=cfg.master_seed,
        source_commit=ds.source_commit, dataset_sha256=ds.sha256,
        split_disjoint=splits_disjoint(ds),
    )


def persist_bulk(ds: Dataset, out_dir: Path) -> Path:
    """Write the bulk arrays (NOT committed by default) as compressed npz."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "deep_dataset.npz"
    np.savez_compressed(p, theta=ds.theta, theta_model=ds.theta_model, x=ds.x,
                        success=ds.success)
    return p
