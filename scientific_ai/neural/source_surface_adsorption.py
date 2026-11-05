"""The development dataset's source: the admitted Langmuir-capture model.

``surface.langmuir_capture@1.0.0`` (``scientific/models/surface_adsorption``)
is a governed ScientificModel: closed form, five declared invariants, every
output a Quantity with its unit and resolution. Each sample here is ONE run
of it, through ``scientific.model.run_model`` -- validated parameters,
implementation digest checked, invariants computed -- and the sample keeps
the bundle's digest, its parameter digest and the environment it ran in.
These are simulation results of a model, labelled as such; nothing is
measured and nothing is invented.

WHY THIS SOURCE

Seven continuous inputs in five dimensions (Pa, K, u, m^-2, s, plus two
ratios) and an integer count, spanning up to nine decades; four outputs, of
which three span tens of decades and one is a bounded fraction; and the
model's own invariants as external constraints a prediction can be checked
against (``constraints``). That exercises every part of the token layout
the flagship would use, on a law whose ground truth can be recomputed.

REGIONS (the design, declared before any sample exists)

``in_distribution`` -- the train/validation/test pool -- draws temperature
log-uniformly in [10, 300] K; ``ood`` draws it in (300, 1000] K and is the
extrapolation split. Every other input has one range for both. Values are
Latin-hypercube stratified per dimension from a seeded PCG64 stream and
written to 12 significant digits, so a design point does not depend on
which math library rounded 10**x. Zero pressure, zero sticking and an
empty window are EXCLUDED by the ranges: they give zero inventory gain and
zero fluence, which a log10 target cannot represent -- an exclusion rule,
recorded in the manifest, not a silent filter.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from scientific.identity import digest

from . import manifests
from .datasets import Sample, assign_split
from .features import FeatureSchema, FeatureSpec, TargetSpec

MODEL_ID = "surface.langmuir_capture"
MODEL_VERSION = "1.0.0"

SCHEMA = FeatureSchema(
    name="surface_adsorption_v1",
    features=(
        FeatureSpec("T_gas_K", "K", "log10_positive", lower=1.0, upper=1e4,
                    description="temperature of the impinging gas"),
        FeatureSpec("capacity_per_m2", "m^-2", "log10_positive",
                    lower=1e12, upper=1e21,
                    description="saturation inventory of the surface"),
        FeatureSpec("initial_coverage", "1", lower=0.0, upper=1.0,
                    description="coverage before the first window"),
        FeatureSpec("mass_amu", "u", "log10_positive", lower=1.0,
                    upper=1e3, description="molecular mass"),
        FeatureSpec("n_windows", "COUNT", role="CONDITION", lower=1,
                    upper=10000, integer=True,
                    description="consecutive exposure windows"),
        FeatureSpec("pressure_Pa", "Pa", "log10_positive", lower=1e-12,
                    upper=1e5, description="gas pressure at the surface"),
        FeatureSpec("sticking", "1", lower=1e-6, upper=1.0,
                    description="sticking coefficient on the bare surface"),
        FeatureSpec("window_s", "s", "log10_positive", lower=1e-9,
                    upper=1e9, description="duration of one window"),
    ),
    targets=(
        TargetSpec("admitted_fluence", "m^-2", "log10_positive",
                   description="impinging particles per area over all "
                               "windows"),
        TargetSpec("final_coverage", "1", lower=0.0, upper=1.0,
                   description="N / N_cap after the last window"),
        TargetSpec("final_inventory", "m^-2", "log10_positive",
                   description="captured particles per area"),
        TargetSpec("impingement_flux", "m^-2 s^-1", "log10_positive",
                   description="kinetic-theory flux"),
    ),
)

#: name -> (kind, lo, hi): ``log`` log-uniform, ``lin`` uniform,
#: ``choice`` uniform over the listed integers.
COMMON: dict[str, tuple[str, Any, Any]] = {
    "capacity_per_m2": ("log", 1e17, 1e20),
    "initial_coverage": ("lin", 0.0, 0.5),
    "mass_amu": ("log", 2.0, 200.0),
    "n_windows": ("choice", (1, 2, 4, 8), None),
    "pressure_Pa": ("log", 1e-7, 1e1),
    "sticking": ("lin", 0.05, 1.0),
    "window_s": ("log", 1e-3, 1e3),
}
REGIONS: dict[str, dict[str, tuple[str, Any, Any]]] = {
    "in_distribution": {**COMMON, "T_gas_K": ("log", 10.0, 300.0)},
    "ood": {**COMMON, "T_gas_K": ("log", 300.0, 1000.0)},
}
OOD_TEMPERATURE_K = 300.0
EXCLUSION_RULES = (
    "pressure, sticking and window are drawn from strictly positive "
    "ranges: a zero gives zero gain and zero fluence, which a log10 target "
    "cannot represent",
    "a run whose outputs are not all OK, or whose invariants do not all "
    "hold, is rejected and counted, never kept",
    "a run whose output unit differs from the target schema's is "
    "rejected",
)


def in_ood_region(inputs: dict) -> bool:
    return inputs["T_gas_K"] > OOD_TEMPERATURE_K


def _q12(x: float) -> float:
    return float(f"{x:.12g}")


def design(region: str, n: int, seed: int) -> list:
    """``n`` design points of ``region``: per-dimension Latin hypercube."""
    if region not in REGIONS:
        raise ValueError(f"unknown region {region!r}")
    rng = np.random.Generator(np.random.PCG64(seed))
    spec = REGIONS[region]
    cols: dict[str, list[Any]] = {}
    for name in sorted(spec):
        kind, lo, hi = spec[name]
        if kind == "choice":
            cols[name] = [int(lo[i]) for i in rng.integers(0, len(lo), n)]
            continue
        strata = (rng.permutation(n) + rng.random(n)) / n
        if kind == "log":
            a, b = math.log10(lo), math.log10(hi)
            cols[name] = [_q12(10.0 ** (a + u * (b - a))) for u in strata]
        else:
            cols[name] = [_q12(lo + u * (hi - lo)) for u in strata]
    if region == "ood":
        # the OOD region is OPEN at 300 K; a point exactly on the boundary
        # belongs to neither and is nudged up by one quantum of the design
        cols["T_gas_K"] = [t if t > OOD_TEMPERATURE_K else
                           _q12(OOD_TEMPERATURE_K * (1 + 1e-9))
                           for t in cols["T_gas_K"]]
    return [{k: cols[k][i] for k in sorted(cols)} for i in range(n)]


_WORKER: dict = {}


def _worker_state():
    if not _WORKER:
        from scientific import catalog, reproduction
        from scientific.backend_probe import run_environment
        reg = catalog.models()
        _WORKER["model"] = reg.lookup(MODEL_ID, MODEL_VERSION)
        ident = reproduction.backend_identity(run_environment())
        _WORKER["backend"] = (reproduction.identity_digest(ident)
                              if ident else None)
    return _WORKER


def run_point(item: tuple) -> dict:
    """One governed run. ``item``: (sample_id, region, inputs)."""
    from scientific.model import run_model
    sample_id, region, inputs = item
    st = _worker_state()
    bundle = run_model(st["model"], dict(inputs))
    targets, reasons = {}, []
    for t in SCHEMA.targets:
        out = bundle.output(t.name)
        if out.status.value != "OK" or out.quantity is None:
            reasons.append(f"{t.name}: {out.status.value} {out.reason}")
            continue
        if out.quantity.unit != t.unit:
            reasons.append(f"{t.name}: unit {out.quantity.unit!r} is not "
                           f"the schema's {t.unit!r}")
            continue
        targets[t.name] = out.quantity.value
    if not bundle.all_invariants_hold:
        failed = [i.invariant_id for i in bundle.invariants if not i.holds]
        reasons.append(f"invariants failed: {failed}")
    for t in SCHEMA.targets:
        v = targets.get(t.name)
        if v is not None and t.transform == "log10_positive" and not v > 0:
            reasons.append(f"{t.name}: {v!r} is not positive")
    return {"sample_id": sample_id, "region": region, "inputs": inputs,
            "targets": targets, "rejected": reasons,
            "bundle_digest": bundle.digest(),
            "parameter_digest": bundle.parameter_digest,
            "implementation_digest": bundle.implementation_digest,
            "environment_digest": bundle.environment_digest,
            "backend_identity": st["backend"]}


def generate(*, dataset_id: str, n_in: int, n_ood: int, design_seed: int,
             split_seed: int, source_commit, generated_at: str,
             workers: int = 1):
    """The samples and the rejections. ``source_commit``: a commit sha of a
    CLEAN tree, or ``manifests.unavailable(...)``."""
    items = []
    for region, n, seed in (("in_distribution", n_in, design_seed),
                            ("ood", n_ood, design_seed + 1)):
        for i, x in enumerate(design(region, n, seed)):
            items.append((f"{region}/{i:05d}", region, x))
    if workers > 1:
        import multiprocessing as mp
        with mp.get_context("spawn").Pool(workers) as pool:
            rows = pool.map(run_point, items, chunksize=16)
    else:
        rows = [run_point(it) for it in items]
    samples, rejected = [], []
    for r in rows:
        if r["rejected"]:
            rejected.append({"sample_id": r["sample_id"],
                             "reasons": r["rejected"]})
            continue
        prov = {
            "input_digest": digest(r["inputs"]),
            "target_digest": digest(r["targets"]),
            "source_dataset": dataset_id,
            "source_artifact": {
                "kind": "scientific.ResultBundle",
                "digest": r["bundle_digest"],
                "stored": False,
                "rederivable_by": "run_model of the generator at "
                                  "scientific_configuration_digest on a "
                                  "backend with environment_identity"},
            "source_commit": source_commit,
            "generator_version": {"model_id": MODEL_ID,
                                  "model_version": MODEL_VERSION,
                                  "implementation_digest":
                                      r["implementation_digest"]},
            "scientific_configuration_digest": r["parameter_digest"],
            "backend_identity": r["backend_identity"]
            or manifests.unavailable("the backend was UNRESOLVED"),
            "environment_identity": r["environment_digest"],
            "schema_version": "scientific-sample/1",
            "generation_timestamp": generated_at,
            "validation_status": "INVARIANTS_HOLD",
            "region": r["region"],
        }
        split = "ood" if r["region"] == "ood" else assign_split(
            r["parameter_digest"], split_seed)
        samples.append(Sample(r["sample_id"], split, r["inputs"],
                              r["targets"], prov))
    return samples, rejected
