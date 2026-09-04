#!/usr/bin/env bash
set -euo pipefail

VERSION=5.0.10
ARCHIVE="openmpi-${VERSION}.tar.gz"
URL="https://download.open-mpi.org/release/open-mpi/v5.0/${ARCHIVE}"
SHA256=5692cc80554a7117c99eaa725d35100edd8bbf73423a5e265ff867979192df7d
PREFIX=${OPENMPI_ULFM_PREFIX:-"${HOME}/.local/opt/openmpi-${VERSION}-ulfm"}
CACHE=${OPENMPI_ULFM_CACHE:-"${HOME}/.cache/symcc/openmpi-${VERSION}"}
SOURCE="${CACHE}/source"
BUILD="${CACHE}/build"
DOWNLOAD="${CACHE}/${ARCHIVE}"
BUNDLED_DEPS=${OPENMPI_ULFM_BUNDLED_DEPS:-1}

case "${BUNDLED_DEPS}" in
  1)
    LIBEVENT_MODE=internal
    HWLOC_MODE=internal
    # A portable runtime must not inherit optional accelerator or XML
    # dependencies from the build host through the bundled hwloc probe.
    HWLOC_PORTABILITY_ARGS=(--disable-io --disable-libxml2)
    ;;
  0)
    LIBEVENT_MODE=external
    HWLOC_MODE=external
    HWLOC_PORTABILITY_ARGS=()
    ;;
  *)
    printf 'OPENMPI_ULFM_BUNDLED_DEPS must be 0 or 1\n' >&2
    exit 2
    ;;
esac

for tool in curl gcc g++ grep make nm perl sha256sum tar; do
  command -v "${tool}" >/dev/null
done

mkdir -p "${CACHE}"
if [[ ! -f "${DOWNLOAD}" ]]; then
  curl --fail --location --retry 3 --output "${DOWNLOAD}.tmp" "${URL}"
  mv "${DOWNLOAD}.tmp" "${DOWNLOAD}"
fi
printf '%s  %s\n' "${SHA256}" "${DOWNLOAD}" | sha256sum --check --status

rm -rf "${SOURCE}" "${BUILD}"
mkdir -p "${SOURCE}" "${BUILD}"
tar -xzf "${DOWNLOAD}" --strip-components=1 -C "${SOURCE}"

(
  cd "${BUILD}"
  "${SOURCE}/configure" \
    --prefix="${PREFIX}" \
    --with-ft=ulfm \
    --disable-oshmem \
    --disable-mpi-fortran \
    --without-ucx \
    --without-libfabric \
    --without-psm2 \
    --without-cuda \
    --without-rocm \
    --without-knem \
    --without-xpmem \
    --with-libevent="${LIBEVENT_MODE}" \
    --with-hwloc="${HWLOC_MODE}" \
    "${HWLOC_PORTABILITY_ARGS[@]}" \
    --with-pmix=internal \
    --with-prrte=internal \
    --enable-mpi1-compatibility \
    --enable-mpi-ext=ftmpi \
    --enable-shared \
    --disable-static \
    --enable-dlopen \
    --disable-debug \
    --disable-mem-debug \
    --disable-mem-profile \
    --disable-mpi-interface-warning \
    --disable-dependency-tracking \
    --disable-sphinx \
    --enable-mca-no-build=btl-ofi,btl-uct,mtl-ofi,mtl-psm2,mtl-psm,mtl-psm3
)

make -C "${BUILD}" -j "${OPENMPI_ULFM_BUILD_JOBS:-2}"
make -C "${BUILD}" install

"${PREFIX}/bin/ompi_info" --version
"${PREFIX}/bin/ompi_info" --parsable --all >"${BUILD}/ompi-info.txt"
grep -q 'mpi_ft_enable' "${BUILD}/ompi-info.txt"
nm -D "${PREFIX}/lib/libmpi.so" >"${BUILD}/libmpi-symbols.txt"
for symbol in MPIX_Comm_revoke MPIX_Comm_shrink MPIX_Comm_agree \
  MPIX_Comm_get_failed MPIX_Comm_ack_failed; do
  grep -q " T ${symbol}$" "${BUILD}/libmpi-symbols.txt"
done
printf '%s\n' "${VERSION}" >"${PREFIX}/share/openmpi/symcc-source-version"
printf '%s\n' "${SHA256}" >"${PREFIX}/share/openmpi/symcc-source-sha256"
printf '%s\n' \
  "libevent=${LIBEVENT_MODE} hwloc=${HWLOC_MODE} hwloc_portable_io_disabled=${BUNDLED_DEPS}" \
  >"${PREFIX}/share/openmpi/symcc-dependency-mode"
printf 'Installed Open MPI %s with ULFM at %s\n' "${VERSION}" "${PREFIX}"
printf 'Run with: %s/bin/mpirun --with-ft ulfm ...\n' "${PREFIX}"
