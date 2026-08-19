"""Selective Rust kernels with a bit-parity admission rule.

MODEL-ONLY / FORECAST-ONLY / PRE-EXPERIMENTAL. Zero PASS. No measured data.

"Selective" is the operative word. Rewriting numerical code in a second
language is a reproducibility risk before it is a speed win: two
implementations of the same formula can disagree in the last ulp, and in a
project whose outputs are byte-gated that is a broken build, not a rounding
detail. So this module makes adoption conditional and mechanical:

* A kernel is **admitted only if it is bit-for-bit identical** to the NumPy
  reference on a fixed test vector. Not "close", not "within tolerance" —
  identical, because the canonical outputs are compared by SHA-256.
* The **NumPy reference is the authority.** It always exists, it is what runs
  by default, and it is what the project ships. The Rust path is an
  accelerator that must earn its place on every process start.
* Adoption is **per kernel**, not per crate: one kernel failing parity does
  not disqualify the others, and one passing does not vouch for the rest.
* The Rust path is **off unless explicitly requested** (``QTA_RUST_KERNELS=1``)
  *and* parity passes. CI, the container, and the release workflow all run the
  NumPy path.

Nothing here is imported by the solvers today: this is the mechanism, proven
on two representative kernels, that any future acceleration must pass through.
"""
from __future__ import annotations

import os
from typing import Any, Callable

import numpy as np
import numpy.typing as npt

from . import AUTOMATIC_GATE_EFFECT, LABEL
from .workspace import StrPath, guard_output_dir, write_json_deterministic

ENABLE_ENV_VAR = "QTA_RUST_KERNELS"
CRATE_PATH = "rust/qta_kernels"
BUILD_COMMAND = ("maturin build --release --manifest-path "
                 f"{CRATE_PATH}/Cargo.toml -i python3.12")
PARITY_RULE = "bit_for_bit_identical_to_numpy_reference"
PARITY_SEED = 20260819
PARITY_N = 4096


# ---------------------------------------------------------------- references
def numpy_face_conductance(area: npt.ArrayLike, d_left: npt.ArrayLike,
                           k_left: npt.ArrayLike, d_right: npt.ArrayLike,
                           k_right: npt.ArrayLike) -> np.ndarray:
    """Series-resistance face conductance ``A / (dL/kL + dR/kR)``.

    The parenthesisation is part of the contract, not a style choice: the
    Rust kernel reproduces this exact association order, which is what makes
    bit parity attainable at all.
    """
    area, d_left, k_left, d_right, k_right = (
        np.asarray(x, dtype=np.float64)
        for x in (area, d_left, k_left, d_right, k_right))
    return area / (d_left / k_left + d_right / k_right)


def numpy_conductivity_power_law(temperature: npt.ArrayLike, k0: float,
                                 t_ref: float,
                                 exponent: float) -> np.ndarray:
    """Power-law conductivity ``k0 * (T / T_ref) ** exponent``."""
    t = np.asarray(temperature, dtype=np.float64)
    return k0 * (t / t_ref) ** exponent


KERNELS: dict[str, dict[str, Any]] = {
    "face_conductance": {
        "numpy": numpy_face_conductance,
        "arity": 5,
        "meaning": "finite-volume face conductance (series resistance)",
    },
    "conductivity_power_law": {
        "numpy": numpy_conductivity_power_law,
        "arity": 4,
        "meaning": "temperature-dependent thermal conductivity",
    },
}


# ------------------------------------------------------------------ backend
def rust_available() -> bool:
    try:
        import qta_kernels  # noqa: F401
        return True
    except Exception:
        return False


def _rust_fn(name: str) -> Callable | None:
    try:
        import qta_kernels
        return getattr(qta_kernels, name)
    except Exception:
        return None


def _test_vectors(name: str, seed: int = PARITY_SEED, n: int = PARITY_N
                  ) -> tuple[tuple, dict]:
    """Deterministic, physically plausible inputs for the parity check."""
    rng = np.random.default_rng(seed)
    if name == "face_conductance":
        return ((rng.uniform(1e-14, 1e-8, n),      # area  [m^2]
                 rng.uniform(1e-9, 1e-5, n),       # d_left  [m]
                 rng.uniform(1e-3, 1e4, n),        # k_left  [W/m/K]
                 rng.uniform(1e-9, 1e-5, n),       # d_right [m]
                 rng.uniform(1e-3, 1e4, n)), {})
    if name == "conductivity_power_law":
        return ((rng.uniform(0.005, 700.0, n),),   # temperature [K]
                {"k0": 2300.0, "t_ref": 300.0, "exponent": 2.7})
    raise KeyError(name)


def kernel_parity(name: str, seed: int = PARITY_SEED, n: int = PARITY_N
                  ) -> dict:
    """Compare one Rust kernel with its NumPy reference, bit for bit."""
    spec = KERNELS[name]
    if not rust_available():
        return {"kernel": name, "availability": "UNAVAILABLE",
                "adopted": False, "backend_in_force": "numpy",
                "reason": "qta_kernels extension not importable",
                "build_command": BUILD_COMMAND}
    fn = _rust_fn(name)
    if fn is None:
        return {"kernel": name, "availability": "AVAILABLE",
                "adopted": False, "backend_in_force": "numpy",
                "reason": f"extension exports no '{name}'"}
    args, kwargs = _test_vectors(name, seed, n)
    reference: Callable = spec["numpy"]
    ref = np.asarray(reference(*args, **kwargs), dtype=np.float64)
    got = np.asarray(fn(*args, **kwargs), dtype=np.float64)
    if got.shape != ref.shape:
        return {"kernel": name, "availability": "AVAILABLE", "adopted": False,
                "backend_in_force": "numpy",
                "reason": f"shape {got.shape} != reference {ref.shape}"}
    identical = bool(np.array_equal(got.view(np.int64), ref.view(np.int64)))
    ulp = int(np.max(np.abs(got.view(np.int64) - ref.view(np.int64)))) \
        if ref.size else 0
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.abs(got - ref) / np.where(ref != 0, np.abs(ref), 1.0)
    return {"kernel": name, "availability": "AVAILABLE",
            "adopted": identical, "parity_rule": PARITY_RULE,
            "bit_identical": identical,
            "max_ulp_difference": ulp,
            "max_relative_difference": float(np.max(rel)) if ref.size else 0.0,
            "n_test_values": int(ref.size), "seed": int(seed),
            "backend_in_force": "rust" if identical else "numpy",
            "verdict": "ADOPTED" if identical else "REJECTED_NOT_BIT_IDENTICAL"}


_PARITY_CACHE: dict = {}


def parity_ok(name: str) -> bool:
    """Cached per-process parity verdict for one kernel."""
    if name not in _PARITY_CACHE:
        _PARITY_CACHE[name] = bool(kernel_parity(name).get("adopted", False))
    return _PARITY_CACHE[name]


def rust_enabled() -> bool:
    """Whether the Rust path was explicitly requested for this process."""
    return os.environ.get(ENABLE_ENV_VAR, "").strip() in ("1", "true", "TRUE")


def dispatch(name: str) -> Callable:
    """Return the kernel to call: Rust only if requested *and* bit-identical.

    Default in every environment the project actually ships (CI, container,
    release workflow) is the NumPy reference.
    """
    if name not in KERNELS:
        raise KeyError(f"unknown kernel '{name}'")
    if rust_enabled() and rust_available() and parity_ok(name):
        fn = _rust_fn(name)
        if fn is not None:
            return fn
    numpy_reference: Callable = KERNELS[name]["numpy"]
    return numpy_reference


def backend_in_force(name: str) -> str:
    return "rust" if dispatch(name) is not KERNELS[name]["numpy"] else "numpy"


def status_report(out_dir: StrPath | None = None) -> dict:
    """Per-kernel adoption status for the Stage-10 workflow."""
    kernels: list[dict] = [kernel_parity(name) for name in sorted(KERNELS)]
    report = {
        "label": LABEL,
        "automatic_gate_effect": AUTOMATIC_GATE_EFFECT,
        "producer": "qta_multiphysics.stack.rust_kernel",
        "component": "selective Rust kernels (PyO3)",
        "crate_path": CRATE_PATH,
        "build_command": BUILD_COMMAND,
        "admission_rule": PARITY_RULE,
        "enable_env_var": ENABLE_ENV_VAR,
        "extension_importable": rust_available(),
        "enabled_this_process": rust_enabled(),
        "default_backend": "numpy",
        "kernels": kernels,
        "adopted_kernels": sorted(k["kernel"] for k in kernels
                                  if k.get("adopted")),
        "authority": "the NumPy reference in this module; a Rust kernel is "
                     "an accelerator that must re-prove bit parity on every "
                     "process start, never a second source of truth",
        "note": "no solver imports these kernels yet; this is the admission "
                "mechanism any future acceleration must pass through",
    }
    if out_dir is not None:
        out = guard_output_dir(out_dir)
        write_json_deterministic(out / "rust_kernel_status.json", report)
    return report
