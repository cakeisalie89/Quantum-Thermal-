#!/usr/bin/env bash
# Build the CANONICAL REFERENCE BACKEND described by reference_backend/spec.json.
#
# Every input is pinned and checked before it is used: the three source
# archives and the root filesystem by sha256, the interpreter by the sha256 of
# its binary, the toolchain by exact package version. Anything that does not
# match stops the build; nothing is "close enough".
#
# What makes the arithmetic fixed, not merely pinned:
#   * OpenBLAS with DYNAMIC_ARCH=0 and one TARGET: one set of kernels, chosen
#     here, not by the CPU the library later loads on; USE_THREAD=0.
#   * NumPy with cpu-dispatch=none over an explicit baseline: no function has
#     a second implementation for the runtime dispatcher to prefer.
#   * -ffp-contract=off and no fast-math anywhere; no -march=native.
#   * the wheels are repaired, so the OpenBLAS and Fortran runtime they run on
#     travel inside them, by their bytes.
# What is NOT fixed by any build: glibc's libm selects variants by CPUID at
# run time. run.sh supplies the CPU (qemu, Nehalem-v1) and the libm (the
# pinned root filesystem), which is why the reference is always RUN there.
#
# Usage: reference_backend/build.sh            (prefix from spec.json)
#        QTA_REF_JOBS=4 reference_backend/build.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
eval "$(python3 "$REPO/tools/reference_backend.py" env)"
J="${QTA_REF_JOBS:-2}"
export SOURCE_DATE_EPOCH=1700000000 LC_ALL=C TZ=UTC

log() { printf '== %s %s\n' "$(date -u +%T)" "$*"; }
die() { printf 'REFERENCE BUILD REFUSED: %s\n' "$*" >&2; exit 1; }

# ---- the toolchain is the declared one ------------------------------------------
for pkg in gcc-13 gfortran-13 binutils qemu-user; do
  var="QTA_REF_TOOL_$(echo "$pkg" | tr 'a-z-' 'A-Z_')"
  want="${!var}"
  have="$(dpkg-query -W -f='${Version}' "$pkg" 2>/dev/null || true)"
  [ "$have" = "$want" ] || die "$pkg is '$have', the recipe declares '$want'"
done
export CC=gcc-13 CXX=g++-13 FC=gfortran-13

mkdir -p "$QTA_REF_PREFIX/src" "$QTA_REF_PREFIX/build" "$QTA_REF_PREFIX/wheels"
cd "$QTA_REF_PREFIX/src"

fetch() {  # fetch <url> <sha256> <file>
  [ -f "$3" ] || curl -fsSL -o "$3.part" "$1" && { [ -f "$3" ] || mv "$3.part" "$3"; }
  echo "$2  $3" | sha256sum -c --quiet - || die "$3 does not have the pinned sha256"
}
fetch "$QTA_REF_OPENBLAS_URL" "$QTA_REF_OPENBLAS_SHA256" OpenBLAS.tar.gz
fetch "$QTA_REF_NUMPY_URL" "$QTA_REF_NUMPY_SHA256" "numpy-$QTA_REF_NUMPY_VERSION.tar.gz"
fetch "$QTA_REF_SCIPY_URL" "$QTA_REF_SCIPY_SHA256" "scipy-$QTA_REF_SCIPY_VERSION.tar.gz"
fetch "$QTA_REF_ROOTFS_URL" "$QTA_REF_ROOTFS_SHA256" rootfs.tar.gz

# ---- the software CPU's userspace -------------------------------------------------
if [ ! -f "$QTA_REF_PREFIX/rootfs/.extracted" ]; then
  log "root filesystem"
  rm -rf "$QTA_REF_PREFIX/rootfs" && mkdir -p "$QTA_REF_PREFIX/rootfs"
  tar -xzf rootfs.tar.gz -C "$QTA_REF_PREFIX/rootfs" --no-same-owner \
      --exclude='./dev/*' 2>/dev/null || true
  touch "$QTA_REF_PREFIX/rootfs/.extracted"
fi

# ---- the interpreter -------------------------------------------------------------
log "interpreter $QTA_REF_PYTHON_REQUEST"
uv python install "$QTA_REF_PYTHON_REQUEST" --install-dir "$QTA_REF_PREFIX/python" >/dev/null
PYBIN="$QTA_REF_PREFIX/python/$QTA_REF_PYTHON_REQUEST/bin/python3.12"
echo "$QTA_REF_PYTHON_SHA256  $(readlink -f "$PYBIN")" | sha256sum -c --quiet - \
  || die "the interpreter binary does not have the pinned sha256"

# ---- OpenBLAS, one target, one thread --------------------------------------------
if [ ! -f "$QTA_REF_PREFIX/openblas/lib/libopenblas.so" ]; then
  log "OpenBLAS $QTA_REF_OPENBLAS_VERSION TARGET=$QTA_REF_OB_TARGET DYNAMIC_ARCH=$QTA_REF_OB_DYNAMIC_ARCH"
  rm -rf "$QTA_REF_PREFIX/build/OpenBLAS" && mkdir -p "$QTA_REF_PREFIX/build/OpenBLAS"
  tar -xzf OpenBLAS.tar.gz -C "$QTA_REF_PREFIX/build/OpenBLAS" --strip-components=1
  for i in $(seq 0 $((QTA_REF_PATCH_COUNT - 1))); do   # the pinned patches, each verified first
    pv="QTA_REF_PATCH_${i}_PATH"; sv="QTA_REF_PATCH_${i}_SHA256"; pf="$REPO/${!pv}"; ps="${!sv}"
    echo "$ps  $pf" | sha256sum -c --quiet - || die "$pf does not have the pinned sha256"
    patch -s -p1 -d "$QTA_REF_PREFIX/build/OpenBLAS" < "$pf" || die "$pf does not apply"
  done
  OB=(TARGET="$QTA_REF_OB_TARGET" DYNAMIC_ARCH="$QTA_REF_OB_DYNAMIC_ARCH"
      USE_THREAD="$QTA_REF_OB_USE_THREAD" USE_OPENMP="$QTA_REF_OB_USE_OPENMP"
      NUM_THREADS="$QTA_REF_OB_NUM_THREADS" NO_AFFINITY="$QTA_REF_OB_NO_AFFINITY"
      INTERFACE64="$QTA_REF_OB_INTERFACE64" BUILD_BFLOAT16="$QTA_REF_OB_BUILD_BFLOAT16"
      COMMON_OPT="$QTA_REF_OB_COMMON_OPT -ffile-prefix-map=$QTA_REF_PREFIX/build=."
      FCOMMON_OPT="$QTA_REF_OB_FCOMMON_OPT -ffile-prefix-map=$QTA_REF_PREFIX/build=."
      CC="$CC" FC="$FC" NO_STATIC=1)
  make -C "$QTA_REF_PREFIX/build/OpenBLAS" -j"$J" "${OB[@]}" libs netlib shared >"$QTA_REF_PREFIX/build/openblas.log" 2>&1 \
    || die "OpenBLAS build failed; see $QTA_REF_PREFIX/build/openblas.log"
  make -C "$QTA_REF_PREFIX/build/OpenBLAS" "${OB[@]}" PREFIX="$QTA_REF_PREFIX/openblas" install >>"$QTA_REF_PREFIX/build/openblas.log" 2>&1
fi

# ---- the build environment, pinned -------------------------------------------------
log "build environment"
[ -x "$QTA_REF_PREFIX/buildenv/bin/python" ] || "$PYBIN" -m venv "$QTA_REF_PREFIX/buildenv"
BPY="$QTA_REF_PREFIX/buildenv/bin/python"
"$BPY" -m pip install -q --disable-pip-version-check $QTA_REF_BUILD_DEPS
export PATH="$QTA_REF_PREFIX/buildenv/bin:$PATH"   # cython, meson, ninja: the pinned ones

export PKG_CONFIG_PATH="$QTA_REF_PREFIX/openblas/lib/pkgconfig"
export LD_LIBRARY_PATH="$QTA_REF_PREFIX/openblas/lib"
FLAGS="$QTA_REF_CFLAGS -ffile-prefix-map=$QTA_REF_PREFIX/build=."
export CFLAGS="$FLAGS" CXXFLAGS="$FLAGS" FFLAGS="$FLAGS"

# ---- NumPy: no runtime dispatch ------------------------------------------------------
if ! ls "$QTA_REF_PREFIX"/wheels/numpy-*manylinux*.whl >/dev/null 2>&1; then
  log "NumPy $QTA_REF_NUMPY_VERSION cpu-baseline=$QTA_REF_NP_CPU_BASELINE cpu-dispatch=$QTA_REF_NP_CPU_DISPATCH"
  rm -rf "$QTA_REF_PREFIX/build/numpy" && mkdir -p "$QTA_REF_PREFIX/build/numpy"
  tar -xzf "numpy-$QTA_REF_NUMPY_VERSION.tar.gz" -C "$QTA_REF_PREFIX/build/numpy" --strip-components=1
  (cd "$QTA_REF_PREFIX/build/numpy" && "$BPY" -m pip wheel -q --no-build-isolation --no-deps . -w "$QTA_REF_PREFIX/build/raw" \
     -Csetup-args=-Dblas="$QTA_REF_NP_BLAS" -Csetup-args=-Dlapack="$QTA_REF_NP_LAPACK" \
     -Csetup-args=-Duse-ilp64="$QTA_REF_NP_USE_ILP64" \
     -Csetup-args=-Dcpu-baseline="$QTA_REF_NP_CPU_BASELINE" \
     -Csetup-args=-Dcpu-dispatch="$QTA_REF_NP_CPU_DISPATCH" >"$QTA_REF_PREFIX/build/numpy.log" 2>&1) \
    || die "NumPy build failed; see $QTA_REF_PREFIX/build/numpy.log"
  "$BPY" -m auditwheel repair --plat "$QTA_REF_WHEEL_PLAT" -w "$QTA_REF_PREFIX/wheels" "$QTA_REF_PREFIX"/build/raw/numpy-*.whl
  "$BPY" -m pip install -q --no-deps --force-reinstall "$QTA_REF_PREFIX"/wheels/numpy-*.whl
fi

# ---- SciPy, on the same OpenBLAS ----------------------------------------------------
if ! ls "$QTA_REF_PREFIX"/wheels/scipy-*manylinux*.whl >/dev/null 2>&1; then
  log "SciPy $QTA_REF_SCIPY_VERSION"
  rm -rf "$QTA_REF_PREFIX/build/scipy" && mkdir -p "$QTA_REF_PREFIX/build/scipy"
  tar -xzf "scipy-$QTA_REF_SCIPY_VERSION.tar.gz" -C "$QTA_REF_PREFIX/build/scipy" --strip-components=1
  (cd "$QTA_REF_PREFIX/build/scipy" && "$BPY" -m pip wheel -q --no-build-isolation --no-deps . -w "$QTA_REF_PREFIX/build/raw" \
     -Csetup-args=-Dblas="$QTA_REF_SP_BLAS" -Csetup-args=-Dlapack="$QTA_REF_SP_LAPACK" >"$QTA_REF_PREFIX/build/scipy.log" 2>&1) \
    || die "SciPy build failed; see $QTA_REF_PREFIX/build/scipy.log"
  "$BPY" -m auditwheel repair --plat "$QTA_REF_WHEEL_PLAT" -w "$QTA_REF_PREFIX/wheels" "$QTA_REF_PREFIX"/build/raw/scipy-*.whl
fi

# ---- the reference runtime: the locked set, with these two builds -------------------
log "reference runtime"
rm -rf "$QTA_REF_PREFIX/venv"
"$PYBIN" -m venv --without-pip "$QTA_REF_PREFIX/venv"
(cd "$REPO" && uv export --frozen --all-groups --no-emit-project --no-header \
   --no-annotate --no-emit-package numpy --no-emit-package scipy) \
   > "$QTA_REF_PREFIX/requirements.txt"
uv pip install -q --python "$QTA_REF_PREFIX/venv/bin/python" --no-deps \
   --require-hashes -r "$QTA_REF_PREFIX/requirements.txt"
uv pip install -q --python "$QTA_REF_PREFIX/venv/bin/python" --no-deps \
   "$QTA_REF_PREFIX"/wheels/numpy-*.whl "$QTA_REF_PREFIX"/wheels/scipy-*.whl

python3 "$REPO/tools/reference_backend.py" build-record > "$QTA_REF_PREFIX/BUILD_RECORD.json"
log "built; record: $QTA_REF_PREFIX/BUILD_RECORD.json"
