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
* The Rust path is **off unless explicitly requested** -- ``dispatch(name,
  backend="rust")`` or ``QTA_RUST_KERNELS=1`` -- AND the kernel's committed
  decision (``docs/rust_kernel_decisions.json``, written by
  ``tools/rust_kernel_decision.py`` from measurement) is ADOPTED AND that
  decision's certificate applies to this process: the same NumPy version and
  dispatch, the same extension bytes. A request that fails any of these
  RAISES ``BackendRefused``; it never falls back silently, and an on-host
  parity check never selects a backend. The host does not decide which
  implementation is authoritative (D-2026-58; directive s.21).

Decided: both kernels are REJECTED (no production call site; the power law
is also not bit-identical under AVX-512 and slower than NumPy), so the Rust
backend is NOT ACTIVE and NumPy is in force everywhere. That is a completed
decision, not an open one.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
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
DECISIONS = Path(__file__).resolve().parents[2] / "docs" / \
    "rust_kernel_decisions.json"


class BackendRefused(RuntimeError):
    """An explicit request for a backend that may not serve it."""


def numpy_dispatch() -> str:
    """The SIMD loops this process's NumPy will use, as a stable string.

    A bit-parity verdict is a fact about a kernel AND the reference it was
    compared against, and NumPy's reference moves with the host: measured
    here, ``conductivity_power_law`` differs from the Rust kernel by 2 ulp
    with AVX-512 available and is BIT-IDENTICAL without it. The verdict
    flips, and with it ``dispatch()``'s choice of backend. So every verdict
    carries the dispatch it was taken under, and none of them can be read as
    a property of the kernel alone. D-2026-58.
    """
    try:
        found = np.show_config(mode="dicts")["SIMD Extensions"]["found"]
    except Exception:          # a NumPy that will not describe itself
        return "UNKNOWN"
    return "+".join(sorted(found)) or "NONE"
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
                "parity": "NOT_MEASURED",
                "numpy_dispatch": numpy_dispatch(),
                "reason": "qta_kernels extension not importable",
                "build_command": BUILD_COMMAND}
    fn = _rust_fn(name)
    if fn is None:
        return {"kernel": name, "availability": "AVAILABLE",
                "parity": "NOT_MEASURED",
                "numpy_dispatch": numpy_dispatch(),
                "reason": f"extension exports no '{name}'"}
    args, kwargs = _test_vectors(name, seed, n)
    reference: Callable = spec["numpy"]
    ref = np.asarray(reference(*args, **kwargs), dtype=np.float64)
    got = np.asarray(fn(*args, **kwargs), dtype=np.float64)
    if got.shape != ref.shape:
        return {"kernel": name, "availability": "AVAILABLE",
                "parity": "NOT_BIT_IDENTICAL",
                "numpy_dispatch": numpy_dispatch(),
                "reason": f"shape {got.shape} != reference {ref.shape}"}
    identical = bool(np.array_equal(got.view(np.int64), ref.view(np.int64)))
    ulp = int(np.max(np.abs(got.view(np.int64) - ref.view(np.int64)))) \
        if ref.size else 0
    with np.errstate(divide="ignore", invalid="ignore"):
        rel = np.abs(got - ref) / np.where(ref != 0, np.abs(ref), 1.0)
    return {"kernel": name, "availability": "AVAILABLE",
            "parity": "BIT_IDENTICAL" if identical else "NOT_BIT_IDENTICAL",
            "parity_rule": PARITY_RULE,
            # The verdict is about this kernel AGAINST THIS NUMPY. Recorded
            # beside it so that a report read on another host is read as a
            # different measurement rather than a contradiction.
            "numpy_dispatch": numpy_dispatch(),
            "verdict_is_dispatch_conditional": True,
            "bit_identical": identical,
            "max_ulp_difference": ulp,
            "max_relative_difference": float(np.max(rel)) if ref.size else 0.0,
            "n_test_values": int(ref.size), "seed": int(seed),
            "note": "a measurement on this host; adoption is decided by "
                    "docs/rust_kernel_decisions.json, never by this verdict"}


def decisions() -> dict:
    """The committed per-kernel decisions. Missing or unreadable is a
    refusal of every Rust request, never an adoption."""
    try:
        doc = json.loads(DECISIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackendRefused(f"no readable decision registry "
                             f"({DECISIONS.name}): {exc}") from exc
    if doc.get("schema") != "rust-kernel-decisions/1" or \
            not isinstance(doc.get("kernels"), dict):
        raise BackendRefused("the decision registry has an unknown shape")
    return doc


def rust_enabled() -> bool:
    """Whether the Rust path was explicitly requested for this process."""
    return os.environ.get(ENABLE_ENV_VAR, "").strip() in ("1", "true", "TRUE")


def _extension_sha256() -> str | None:
    try:
        import hashlib

        import qta_kernels
        so = qta_kernels.qta_kernels.__file__
        with open(so, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except Exception:
        return None


def certificate_applies(doc: dict) -> tuple[bool, str]:
    """Whether the decision's measurement describes THIS process."""
    m = doc.get("measured_on") or {}
    if m.get("numpy_version") != np.__version__:
        return False, (f"certified against NumPy {m.get('numpy_version')}, "
                       f"this process has {np.__version__}")
    here = np.show_config(mode="dicts")["SIMD Extensions"]
    if m.get("simd") != here:
        return False, (f"certified under dispatch {m.get('simd')}, this "
                       f"process has {here}")
    if m.get("extension_sha256") != _extension_sha256():
        return False, "the loaded extension is not the certified build"
    return True, "certificate applies"


def dispatch(name: str, backend: str | None = None) -> Callable:
    """The kernel to call, by EXPLICIT selection; the host never chooses.

    ``backend`` defaults to what the process requested (``numpy`` unless
    ``QTA_RUST_KERNELS=1``). ``numpy`` returns the reference. ``rust``
    returns the extension only for a kernel whose committed decision is
    ADOPTED and whose certificate applies here; otherwise it raises
    ``BackendRefused`` -- an explicit request is never silently served by
    something else.
    """
    if name not in KERNELS:
        raise KeyError(f"unknown kernel '{name}'")
    if backend is None:
        backend = "rust" if rust_enabled() else "numpy"
    if backend == "numpy":
        numpy_reference: Callable = KERNELS[name]["numpy"]
        return numpy_reference
    if backend != "rust":
        raise ValueError(f"backend must be 'numpy' or 'rust', not "
                         f"{backend!r}")
    doc = decisions()
    dec = doc["kernels"].get(name) or {}
    if not str(dec.get("decision", "")).endswith("_ADOPTED"):
        raise BackendRefused(
            f"{name}: {dec.get('decision', 'UNDECIDED')} "
            f"({'; '.join(dec.get('failed_criteria', [])) or 'no record'})"
            " -- the NumPy reference is in force; request backend='numpy'")
    ok, why = certificate_applies(doc)
    if not ok:
        raise BackendRefused(f"{name}: ADOPTED, but {why}")
    fn = _rust_fn(name)
    if fn is None:
        raise BackendRefused(f"{name}: ADOPTED, but the extension is not "
                             "importable here")
    return fn


def backend_in_force(name: str, backend: str | None = None) -> str:
    """``numpy``, ``rust``, or ``REFUSED`` for the requested backend."""
    try:
        fn = dispatch(name, backend)
    except BackendRefused:
        return "REFUSED"
    return "rust" if fn is not KERNELS[name]["numpy"] else "numpy"


def status_report(out_dir: StrPath | None = None) -> dict:
    """Per-kernel adoption status for the Stage-10 workflow."""
    kernels: list[dict] = [kernel_parity(name) for name in sorted(KERNELS)]
    doc = decisions()
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
        "selection": "explicit; a committed decision, never an on-host "
                     "parity verdict, admits a kernel",
        "backend": doc["backend"],
        "decisions": {k: v["decision"] for k, v in
                      sorted(doc["kernels"].items())},
        "kernels": kernels,
        "adopted_kernels": sorted(k for k, v in doc["kernels"].items()
                                  if v["decision"].endswith("_ADOPTED")),
        "numpy_dispatch": numpy_dispatch(),
        "dispatch_conditionality": (
            "The parity measurements below were taken against THIS host's "
            "NumPy: conductivity_power_law differs by 2 ulp where AVX-512 "
            "is available and is bit-identical where it is not. They decide "
            "nothing -- the committed decision registry does, and it "
            "rejects that kernel. D-2026-58."),
        "authority": "the NumPy reference in this module; a Rust kernel is "
                     "an accelerator admitted only by a committed decision "
                     "whose certificate applies to the process, never a "
                     "second source of truth",
        "note": "no solver imports these kernels; both are REJECTED and "
                "the Rust backend is not active",
    }
    if out_dir is not None:
        out = guard_output_dir(out_dir)
        write_json_deterministic(out / "rust_kernel_status.json", report)
    return report
