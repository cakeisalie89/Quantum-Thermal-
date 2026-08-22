"""The package checker's guarded subprocess must stay guarded, and stay fast.

Two failure modes this pins.

RUNTIME. The canonical regeneration ran 296 s against its 300 s budget on an
idle runner -- four seconds of headroom. Profiling put 27% of the whole run in
407 calls to numpy.trapezoid, almost all of them inside the NV filter function,
which materialised a 4000x4000 complex128 array (256 MB) and then a second one
of the same size for the product, 69 times per run. Evaluating omega in blocks
removed that; the run is now ~198 s. The rows of that array are independent, so
blocking changes nothing any row sees -- test_filter_function_blocking_is_bit_exact
asserts that against the whole-array form rather than trusting the argument.

TIMEOUT. subprocess.run(..., timeout=N) is only a hang detector while the
timeout is actually passed. An unbounded call added later would turn the guard
into a no-op silently, so every spawn in the checker is required by AST to
carry one.

MODEL-ONLY / FORECAST-ONLY. Nothing here asserts a scientific value.
"""
import ast
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CHECKER = os.path.join(ROOT, "package_consistency_check.py")


def _checker_tree():
    with open(CHECKER, encoding="utf-8") as f:
        return ast.parse(f.read())


# ------------------------------------------------- every spawn is bounded ----

def _spawns(tree):
    """(lineno, callee) for every subprocess spawn in the checker."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = None
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id == "subprocess":
            name = f.attr
        if name in ("run", "call", "check_call", "check_output", "Popen"):
            out.append((node.lineno, name, node))
    return out


def test_every_subprocess_spawn_declares_a_timeout():
    """An unbounded spawn makes the budget a no-op without changing any test."""
    tree = _checker_tree()
    spawns = _spawns(tree)
    assert spawns, "no subprocess spawns found; this test is watching nothing"
    unbounded = []
    for lineno, name, node in spawns:
        if name == "Popen":
            # Popen has no timeout= at all; it must not be used here.
            unbounded.append(f"line {lineno}: subprocess.Popen (no timeout possible)")
            continue
        if not any(k.arg == "timeout" for k in node.keywords):
            unbounded.append(f"line {lineno}: subprocess.{name}() without timeout=")
    assert not unbounded, "unbounded subprocess spawns: " + "; ".join(unbounded)


def test_the_sim_budget_is_a_named_constant_not_a_magic_number():
    tree = _checker_tree()
    names = {t.id for n in tree.body if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Name)}
    assert "SIM_TIMEOUT_S" in names, (
        "the regeneration budget must be a named module constant so it can be "
        "found, documented and changed deliberately")
    for lineno, name, node in _spawns(tree):
        for k in node.keywords:
            if k.arg == "timeout":
                assert isinstance(k.value, ast.Name), (
                    f"line {lineno}: timeout= is a literal; use the named budget")


def test_the_guarded_spawn_handles_timeout_explicitly():
    """A bare TimeoutExpired escapes as a traceback with no RESULT line.

    That is fail-closed at the pipeline level -- the process exits non-zero --
    but it produces no diagnosis and no classified failure, so a timeout is
    indistinguishable from a crash in the report.
    """
    tree = _checker_tree()
    handlers = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for h in node.handlers:
                handlers.append(ast.unparse(h.type) if h.type else "bare")
    joined = " ".join(handlers)
    assert "TimeoutExpired" in joined, (
        "the guarded spawn does not classify subprocess.TimeoutExpired")
    assert "FileNotFoundError" in joined, (
        "a missing interpreter is not distinguished from a failed run")


def test_no_bare_except_swallows_a_failed_check():
    """A verifier must not turn an exception into a silent success."""
    tree = _checker_tree()
    bare = [h.lineno for n in ast.walk(tree) if isinstance(n, ast.Try)
            for h in n.handlers
            if h.type is None and all(isinstance(b, ast.Pass) for b in h.body)]
    assert not bare, f"bare `except: pass` in a verifier at lines {bare}"


# ------------------------------------------- blocking must be bit-exact ------

def _whole_array_filter_function(sequence, omega, total_time_s):
    """The pre-optimisation form, kept here as the reference implementation."""
    from qta_multiphysics.nv_spin.noise import sequence_modulation
    T = total_time_s
    n = 4000
    t = np.linspace(0.0, T, n)
    s = sequence_modulation(sequence, t, T)
    phase = np.exp(1j * np.outer(omega, t))
    y = np.trapezoid(phase * s[None, :], t, axis=1)
    return np.abs(y) ** 2


def test_filter_function_blocking_is_bit_exact():
    """Every returned bit must match the whole-array evaluation."""
    from qta_multiphysics.nv_spin import noise
    for sequence, T, n_omega in (("ramsey", 1e-3, 512),
                                 ("hahn", 4e-3, 777),
                                 ("xy8", 2e-3, 1024)):
        omega = np.linspace(1e-6 / 1e-3, 50.0 / 1e-3, n_omega)
        got = noise._filter_function(sequence, omega, T)
        ref = _whole_array_filter_function(sequence, omega, T)
        assert np.array_equal(got, ref), (
            f"{sequence}: blocked evaluation differs from the whole-array form; "
            f"max|d| = {float(np.max(np.abs(got - ref)))}")


def test_block_size_does_not_change_the_result():
    """Any block size must give identical bits; only runtime may differ."""
    from qta_multiphysics.nv_spin import noise
    omega = np.linspace(1e-3, 5e4, 600)
    original = noise._FF_BLOCK_BYTES
    results = []
    try:
        for nbytes in (16 << 10, 1 << 20, 64 << 20):
            noise._FF_BLOCK_BYTES = nbytes
            results.append(noise._filter_function("hahn", omega, 4e-3))
    finally:
        noise._FF_BLOCK_BYTES = original
    for r in results[1:]:
        assert np.array_equal(results[0], r), "block size changed the result"


def test_a_degenerate_block_size_still_produces_every_row():
    from qta_multiphysics.nv_spin import noise
    omega = np.linspace(1e-3, 5e4, 37)
    original = noise._FF_BLOCK_BYTES
    try:
        noise._FF_BLOCK_BYTES = 1          # forces block = 1
        got = noise._filter_function("hahn", omega, 4e-3)
    finally:
        noise._FF_BLOCK_BYTES = original
    assert got.shape == omega.shape
    assert np.all(np.isfinite(got))


def test_coherence_is_unchanged_by_the_optimisation():
    """The consumer, not just the helper: chi and W must be identical."""
    from qta_multiphysics.nv_spin import noise
    for tau_c, sigma, T in ((1e-3, 1e4, 4e-3), (1e-5, 5e3, 1e-3)):
        got = noise.filter_function_coherence(
            sequence="hahn", tau_c_s=tau_c, sigma_rad_s=sigma, total_time_s=T)
        omega_max = 50.0 / tau_c
        omega = np.linspace(1e-6 / tau_c, omega_max, 4000)
        S = 2.0 * sigma ** 2 * tau_c / (1.0 + (omega * tau_c) ** 2)
        yf2 = _whole_array_filter_function("hahn", omega, T)
        import math
        chi = (1.0 / math.pi) * np.trapezoid(S * yf2, omega)
        assert got == float(math.exp(-max(0.0, chi))), (
            f"coherence changed for tau_c={tau_c}, sigma={sigma}, T={T}")


if __name__ == "__main__":
    ns = dict(globals())
    for _n, _f in ns.items():
        if _n.startswith("test_") and callable(_f):
            _f()
    print("RESULT: checker runtime/timeout contract holds")
