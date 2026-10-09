# Installing the framework

```
uv sync --frozen --all-groups
uv run python -m pytest tests/ -q
```

`uv.lock` pins every dependency; `pyproject.toml` declares them, grouped
by what needs them. The optional stack elements (SALib, OpenMDAO, usd-core,
the Rust kernels, FEniCSx) are described below and in `STACK.md`; an absent
one reports itself UNAVAILABLE and never substitutes a result.

## The legacy QTA verifier

The rest of this section concerns `package_consistency_check.py`, the
LEGACY_QTA_VERIFIER that re-runs the QTA pipeline and byte-compares its
outputs.

## External system dependencies

The legacy verifier's core has minimal dependencies. The optional full PDF
text validation requires one external binary.

### Required

- **Python 3** (tested with 3.10+). No third-party Python packages are required
  by `qta_full_sim.py` or `package_consistency_check.py`; both use only the
  Python standard library.

### Optional

- **pdftotext** (from Poppler or Xpdf). Required only for Step 7 of
  `package_consistency_check.py`, which validates the extracted text content of
  `qta_manuscript_v4.pdf`. If `pdftotext` is not on `PATH`, the consistency
  checker explicitly reports

      [PASS] pdftotext not installed; PDF text validation skipped
             (install Poppler/Xpdf pdftotext for this optional check)

  and continues with all other checks. Core consistency verification (gate
  table, manifest hashes, taxonomy, bibliography hygiene, packaging hygiene,
  Step 8B/8C/8D) runs without `pdftotext`.

### Installing pdftotext

- **Windows**: download Poppler for Windows (e.g. from the `poppler-windows`
  GitHub release), extract, and add the `bin/` directory to your `PATH`.
  Alternatively use Xpdf command-line tools.
- **macOS**: `brew install poppler`
- **Ubuntu / Debian**: `sudo apt install poppler-utils`
- **Fedora / RHEL**: `sudo dnf install poppler-utils`

## Running

From the package root (where `qta_full_sim.py` lives):

    python qta_full_sim.py
    python package_consistency_check.py

Both should exit 0. The first regenerates all simulation outputs in
`outputs/`. The second runs all consistency checks and reports `[PASS]` /
`[FAIL]` per check.

## Reproducibility notes

- All Python source files declare UTF-8 explicitly via `reconfigure()` and a
  local `open()` wrapper that defaults text-mode opens to `encoding="utf-8"`.
  This stabilises behaviour on Windows where the default text-mode encoding
  is locale-dependent (e.g. `cp1252`).
- `subprocess.run(..., text=True)` calls also pass `encoding="utf-8",
  errors="replace"` to avoid mojibake when stdout contains non-ASCII.

## Deep experimental-design layer

The deep simulation-based-inference layer (`qta_multiphysics/deep_expdesign/`) requires no extra
dependencies: it is implemented in pure NumPy/SciPy and runs deterministically on CPU (no CUDA /
GPU is required for the unit suite). The directive's preferred PyTorch/SBI path is unavailable in
the sandboxed environment (the CPU-only PyTorch wheel index is blocked and the default wheel
bundles CUDA), so a documented reproducible-CPU neural density estimator is used instead; the
backends can be swapped for a pinned CPU PyTorch build behind their existing interfaces.

Run: `python tests/test_deep_expdesign.py && python tests/test_deep_expdesign_stage2.py`, then
`python qta_full_sim.py --ci --deep`.

> The framework includes direct Bayesian experimental design. A deep simulation-based inference and EIG layer may be trained and numerically validated against the direct reference estimator. This does not constitute experimental validation of the physical architecture.

## Canonical regeneration and profiles (legacy QTA pipeline)

The canonical regeneration command — the one `package_consistency_check.py`
runs and the one all committed root outputs must byte-match — is:

    python3 qta_full_sim.py

with no flags (the default profile). Two optional flags exist: `--ci` runs the
integrated forecast layers (NV spin dynamics / design registry / Bayesian
experimental design) at a reduced, faster sampling profile intended for tests,
and `--deep` additionally runs the optional fail-closed deep SBI layer. Outputs
produced under `--ci` are numerically legitimate but differ from the canonical
default-profile outputs in the sampled layers; do not copy `--ci` outputs over
the committed root files. The checker's stale-snapshot guard (Step 2b) enforces
byte-agreement between the committed root outputs and a fresh default-profile
regeneration, with `deep_surrogate_readiness.json` exempt by documented design.

## Rebuilding the manuscript PDF reproducibly

`qta_manuscript_v4.pdf` is byte-reproducible so it stays consistent with
`final_manifest.json`. Rebuild with:

    SOURCE_DATE_EPOCH=0 FORCE_SOURCE_DATE=1 pdflatex -interaction=nonstopmode qta_manuscript_v4.tex
    SOURCE_DATE_EPOCH=0 FORCE_SOURCE_DATE=1 pdflatex -interaction=nonstopmode qta_manuscript_v4.tex

(two passes; the environment variables pin the embedded timestamps).

## Stage-10 scientific-stack extras (all optional)

The Stage-10 adapters in `qta_multiphysics/stack/` are additive and fail
closed: without their optional packages each one reports
`availability = UNAVAILABLE`, names the in-repo authority that stays in force,
and produces no substitute result. Nothing in the canonical pipeline, the
container, or the release workflow depends on any of them, and
`uv sync --all-groups` deliberately does *not* install them (they are project
*extras*, not dependency groups).

    uv sync --frozen --all-groups                       # core: everything works, adapters report UNAVAILABLE
    uv sync --frozen --all-groups --extra viz --extra uq # + usd-core, SALib, OpenMDAO

- `viz` — `usd-core`, used only to *validate* an exported `.usda`; the file
  itself is written directly, so export works without it.
- `uq` — `SALib` (global-sensitivity cross-check) and `openmdao`
  (design-space exploration).

**FEniCSx** is intentionally not an extra: `dolfinx` is not installable as a
plain wheel. The harness's independent verifier runs it from its own
environment, created from a hash-pinned conda-forge lock (every package's
sha256 is checked before use):

    uv run python tools/fenicsx_env.py create --prefix <dir>/fenicsx --root <dir>/mamba
    export QTA_FENICSX_PYTHON=<dir>/fenicsx/bin/python

**The FMI runtime** (fmpy) likewise lives in its own environment, from its
hash-pinned lock, and the FMU is built from vendored FMI 3.0 headers:

    uv run python tools/isolated_runtime.py create fmi --prefix <dir>/fmi-runtime
    export QTA_FMI_PYTHON=<dir>/fmi-runtime/bin/python
    uv run python tools/fmi_build.py --out <dir>/fmu_build

With both set, the generic end-to-end demonstration runs through its
Snakemake rule, which passes the two runtimes to the tool
(`uv run snakemake harness_demo --cores 1`); run directly, the tool takes
them as arguments and can be told they are REQUIRED:

    uv run python tools/harness_demo.py run --out report.json \
        --fenicsx-python "$QTA_FENICSX_PYTHON" --fmi-python "$QTA_FMI_PYTHON" \
        --require fenicsx,fmi

The acceptance campaigns run through `tools/fenicsx_acceptance.py` and
`tools/fmi_acceptance.py`. Without the runtimes the demonstration runs a
smaller scope and says so in its report: the slab is decided without the
FEniCSx check (REJECTED, since the series check alone does not admit it) and
the FMU legs do not run. That is a demonstration of the refusal path, not of
admission; `--require` turns a missing runtime into a failure. The
Stage-10 adapter for the legacy finite-volume backends,
`qta_multiphysics/stack/fem_fenicsx.py`, detects `dolfinx` at runtime and
stays STAGED for that scope.

**The Rust kernels are built on demand** and are not part of any environment:

    maturin build --release --manifest-path rust/qta_kernels/Cargo.toml -i python3.12
    uv pip install rust/qta_kernels/target/wheels/qta_kernels-*.whl
    QTA_RUST_KERNELS=1 python -c "from qta_multiphysics.stack import rust_kernel; \
        print(rust_kernel.status_report()['adopted_kernels'])"

Even when installed, the Rust path stays off unless `QTA_RUST_KERNELS=1` is
set *and* the kernel re-proves bit-for-bit parity with its NumPy reference at
process start. See `STACK.md` for the adoption ladder and the current
per-kernel verdicts.

Run the whole stage with `snakemake --cores 1 s10_full`; the two heavier
studies are opt-in (`s10_uq_sobol`, `s10_mdao_doe`) because each model
evaluation is a full 3D transient solve.
