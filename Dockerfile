# QTA reproducible container (Stage 7). MODEL-ONLY / FORECAST-ONLY project.
# Pin policy: tag pinned exactly; digest MUST be recorded at first build
# (this build sandbox has no registry access, so the digest could not be
# resolved here — see container_verification.md for the exact blocker).
FROM python:3.12.11-slim-bookworm
# digest: UNRESOLVED-IN-BUILD-SANDBOX (record sha256 digest at first pull)

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
USER qta
ENV PATH="/opt/venv/bin:$PATH"
# writable workspace for generated outputs (canonical sources untouched)
VOLUME ["/qta/workspace"]
# canonical verification command:
CMD ["bash", "container_verify.sh"]
