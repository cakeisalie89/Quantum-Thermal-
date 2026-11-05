# The Scientific AI Harness, as a reproducible container.
# Pin policy: pinned by DIGEST, not by tag. The digest is the OCI image index
# for python:3.12.11-slim-bookworm, resolved from registry-1.docker.io and
# confirmed stable across two independent requests. The image is built and
# run on GitHub-hosted runners by .github/workflows/container-verify.yml
# (this sandbox cannot pull the layer blobs); container_verification.md
# records what the hosted runs showed.
#   linux/amd64 sub-manifest:
#   sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49
FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC \
    PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv
# OS deps: GIT, and nothing else.
#
# numpy, scipy and h5py ship manylinux wheels, so the base image covers the
# numerics. What it does not cover is the GOVERNANCE half of the suite: the
# tests that enumerate the tracked tree do it with `git ls-files`, and every
# one of them died with FileNotFoundError in hosted run 33113363458 because
# python:3.12-slim carries no git binary. Letting those tests skip would mean
# the container verifies less than it claims to.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -m harness
WORKDIR /harness
COPY --chown=harness:harness pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv==0.11.7 && \
    uv sync --frozen --all-groups --extra neural
COPY --chown=harness:harness . /harness
# WORKDIR creates /harness owned by root, and `COPY --chown` sets ownership
# on the entries it copies, NOT on the pre-existing destination directory.
# The governed workspace (verification/) is gitignored, so it is absent from
# the build context and is created at runtime -- which a non-root process
# cannot do inside a root-owned directory (hosted run 32618446522 died
# exactly there).
RUN chown harness:harness /harness
USER harness
ENV PATH="/opt/venv/bin:$PATH"
# writable workspace for generated outputs (tracked sources untouched)
VOLUME ["/harness/workspace"]
# canonical verification command:
CMD ["bash", "container_verify.sh"]
