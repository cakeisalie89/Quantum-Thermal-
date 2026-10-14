# Installing the Scientific AI Harness

```
uv sync --frozen --all-groups
uv run python -m pytest tests/ -q
```

`uv.lock` pins every dependency; `pyproject.toml` declares them, grouped
by what needs them. Python 3.12 is required (`requires-python = "==3.12.*"`,
the version the locked numeric stack resolves for). `git` must be on `PATH`:
the governance tests enumerate the tracked tree with it.

## Optional integrations

Each optional integration fails closed: without its package it reports
`availability = UNAVAILABLE` and nothing is recorded as a pass. None of them
is installed by `uv sync --all-groups`; they are project *extras*:

    uv sync --frozen --all-groups                        # core: integrations report UNAVAILABLE
    uv sync --frozen --all-groups --extra viz --extra uq # + usd-core, SALib, OpenMDAO
    uv sync --frozen --all-groups --extra neural         # + JAX, for the learned-model substrate

- `viz` -- `usd-core`, used only to *validate* an exported `.usda`; the file
  itself is written directly, so export works without it.
- `uq` -- `SALib` (global-sensitivity cross-check) and `openmdao`
  (design-space exploration).
- `neural` -- `jax`, `jaxlib` and `ml-dtypes`, pinned exactly: a training
  run's numerics belong to its framework version. Without it the
  learned-model tests skip, unless `HARNESS_NEURAL_REQUIRED=1` (CI sets it),
  which turns a skip into a failure.

## FEniCSx and FMI

**FEniCSx** is intentionally not an extra: `dolfinx` is not installable as a
plain wheel. The independent verifier runs it from its own environment,
created from a hash-pinned conda-forge lock (every package's sha256 is
checked before use):

    uv run python tools/fenicsx_env.py create --prefix <dir>/fenicsx --root <dir>/mamba
    export HARNESS_FENICSX_PYTHON=<dir>/fenicsx/bin/python

**The FMI runtime** (fmpy) likewise lives in its own environment, from its
hash-pinned lock, and the FMU is built from vendored FMI 3.0 headers:

    uv run python tools/isolated_runtime.py create fmi --prefix <dir>/fmi-runtime
    export HARNESS_FMI_PYTHON=<dir>/fmi-runtime/bin/python
    uv run python tools/fmi_build.py --out <dir>/fmu_build

With both set, the generic end-to-end demonstration runs through its
Snakemake rule, which passes the two runtimes to the tool
(`uv run snakemake harness_demo --cores 1`); run directly, the tool takes
them as arguments and can be told they are REQUIRED:

    uv run python tools/harness_demo.py run --out report.json \
        --fenicsx-python "$HARNESS_FENICSX_PYTHON" --fmi-python "$HARNESS_FMI_PYTHON" \
        --require fenicsx,fmi

The acceptance campaigns run through `tools/fenicsx_acceptance.py` and
`tools/fmi_acceptance.py`. Without the runtimes the demonstration runs a
smaller scope and says so in its report: the slab is decided without the
FEniCSx check (REJECTED, since the exact-series reference alone does not
admit it) and the FMU legs do not run. That is a demonstration of the
refusal path, not of admission; `--require` turns a missing runtime into a
failure.

## The workflow

    uv run snakemake --cores 1                 # the default target: the whole governed workflow
    uv run snakemake --cores 1 uq_sobol        # opt-in: OAT and Sobol cross-check
    uv run snakemake --cores 1 mdao_doe        # opt-in: OpenMDAO exploration

Every rule writes under `verification/` and nothing else; the last rule of
the default target re-verifies `final_manifest.json` against the tracked
tree.

## The container

`Dockerfile` builds the declared image (base pinned by digest, the uv
environment frozen, non-root user `harness`), and `container_verify.sh` is
its entrypoint. The image is built and run on a hosted runner by
`.github/workflows/container-verify.yml`; `container_verification.md`
records what those runs showed.
