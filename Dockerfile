# QTA reproducible container (Stage 7). MODEL-ONLY / FORECAST-ONLY project.
# Pin policy: pinned by DIGEST, not by tag. The digest is the OCI image index
# for python:3.12.11-slim-bookworm, resolved from registry-1.docker.io and
# confirmed stable across two independent requests. It was previously recorded
# as UNRESOLVED-IN-BUILD-SANDBOX because an earlier environment had no registry
# route; this one does. See container_verification.md for what is still not
# verified (the image is still never built or run here).
#   linux/amd64 sub-manifest:
#   sha256:c00fc7b44d844b6da22861ec24af43968a5200eac4ec607b4725d585165d6b49
FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 TZ=UTC \
    PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv
# OS deps: none beyond the base image — numpy/scipy/qutip ship manylinux
# wheels; the project writes CSV/JSON only (no HDF5 system libs in use).
RUN useradd -m qta
WORKDIR /qta
COPY --chown=qta:qta pyproject.toml uv.lock requirements.txt ./
RUN pip install --no-cache-dir uv==0.11.7 && \
    uv sync --frozen --all-groups
COPY --chown=qta:qta . /qta
# WORKDIR creates /qta owned by root, and `COPY --chown` sets ownership on the
# entries it copies, NOT on the pre-existing destination directory. `outputs/`
# is gitignored, so it is absent from the build context and must be created at
# runtime -- which a non-root process cannot do inside a root-owned directory.
# The first hosted run (32618446522) died exactly there: qta_full_sim.py printed
# its full report and then failed at OUTPUT_DIR.mkdir(). The checker also
# removes and recreates outputs/, so the directory itself must be writable.
RUN chown qta:qta /qta
USER qta
ENV PATH="/opt/venv/bin:$PATH"
# writable workspace for generated outputs (canonical sources untouched)
VOLUME ["/qta/workspace"]
# canonical verification command:
CMD ["bash", "container_verify.sh"]
