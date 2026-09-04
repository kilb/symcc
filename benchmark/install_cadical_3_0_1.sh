#!/usr/bin/env bash
set -euo pipefail

VERSION=3.0.1
COMMIT=c60730422e758ef1cebe7aeddf2dda31c996bf04
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PREFIX=${CADICAL_PREFIX:-"${HOME}/.local/opt/cadical-${VERSION}"}
SOURCE=${CADICAL_SOURCE:-"${HOME}/.cache/symcc/cadical-${VERSION}"}
REALTIME_SOURCE=${SYMCC_QFBV_REALTIME_SOURCE:-
  "${SCRIPT_DIR}/../util/qfbv_cadical_realtime.cpp"}
COMPRESSION_SOURCE=${SYMCC_QFBV_COMPRESSION_SOURCE:-
  "${SCRIPT_DIR}/../util/qfbv_clause_compression.cpp"}

for tool in git g++ gcc make nm sha256sum; do
  command -v "${tool}" >/dev/null
done

if [[ ! -d "${SOURCE}/.git" ]]; then
  mkdir -p "$(dirname "${SOURCE}")"
  git clone --filter=blob:none \
    https://github.com/arminbiere/cadical.git "${SOURCE}"
fi

git -C "${SOURCE}" fetch --depth 1 origin "${COMMIT}"
git -C "${SOURCE}" checkout --detach "${COMMIT}"
[[ "$(git -C "${SOURCE}" rev-parse HEAD)" == "${COMMIT}" ]]

(
  cd "${SOURCE}"
  ./configure --quiet -shared
  make -j"${CADICAL_BUILD_JOBS:-2}"
)

mkdir -p "${PREFIX}/bin" "${PREFIX}/lib" "${PREFIX}/include"
install -m 0755 "${SOURCE}/build/cadical" "${PREFIX}/bin/cadical"
install -m 0755 "${SOURCE}/build/cadical-dynamic" \
  "${PREFIX}/bin/cadical-dynamic"
install -m 0644 "${SOURCE}/build/libcadical.so" \
  "${PREFIX}/lib/libcadical.so"
install -m 0644 "${SOURCE}/src/ccadical.h" \
  "${PREFIX}/include/ccadical.h"
install -m 0644 "${SOURCE}/src/cadical.hpp" \
  "${PREFIX}/include/cadical.hpp"

[[ -f "${REALTIME_SOURCE}" ]]
[[ -f "${COMPRESSION_SOURCE}" ]]
g++ -std=c++17 -fPIC -shared -fvisibility=hidden \
  -Wall -Wextra -Werror -pthread \
  -I"${PREFIX}/include" -I"$(dirname "${COMPRESSION_SOURCE}")" \
  "${REALTIME_SOURCE}" "${COMPRESSION_SOURCE}" \
  -L"${PREFIX}/lib" -lcadical -Wl,-rpath,'$ORIGIN' \
  -o "${PREFIX}/lib/libsymcc_qfbv_cadical_realtime.so"

[[ "$("${PREFIX}/bin/cadical" --version)" == "${VERSION}" ]]
nm -D "${PREFIX}/lib/libcadical.so" | grep -q ' ccadical_solve$'
nm -D "${PREFIX}/lib/libcadical.so" | grep -q ' ccadical_failed$'
nm -D "${PREFIX}/lib/libsymcc_qfbv_cadical_realtime.so" | \
  grep -q ' symcc_qfbv_realtime_solve$'
nm -D "${PREFIX}/lib/libsymcc_qfbv_cadical_realtime.so" | \
  grep -q ' symcc_qfbv_realtime_enqueue$'
nm -D "${PREFIX}/lib/libsymcc_qfbv_cadical_realtime.so" | \
  grep -q ' symcc_qfbv_realtime_dequeue_ack$'
nm -D "${PREFIX}/lib/libsymcc_qfbv_cadical_realtime.so" | \
  grep -q ' symcc_qfbv_realtime_clear_termination$'
nm -D "${PREFIX}/lib/libsymcc_qfbv_cadical_realtime.so" | \
  grep -q ' symcc_qfbv_realtime_activity_protocol$'
nm -D "${PREFIX}/lib/libsymcc_qfbv_cadical_realtime.so" | \
  grep -q ' symcc_qfbv_realtime_enable_activity$'
nm -D "${PREFIX}/lib/libsymcc_qfbv_cadical_realtime.so" | \
  grep -q ' symcc_qfbv_realtime_dequeue_activity$'
nm -D "${PREFIX}/lib/libsymcc_qfbv_cadical_realtime.so" | \
  grep -q ' symcc_qfbv_realtime_compression_protocol$'
nm -D "${PREFIX}/lib/libsymcc_qfbv_cadical_realtime.so" | \
  grep -q ' symcc_qfbv_clause_compression_protocol$'
nm -D "${PREFIX}/lib/libsymcc_qfbv_cadical_realtime.so" | \
  grep -q ' symcc_qfbv_clause_compress$'
nm -D "${PREFIX}/lib/libsymcc_qfbv_cadical_realtime.so" | \
  grep -q ' symcc_qfbv_clause_decompress$'
sha256sum "${PREFIX}/bin/cadical" "${PREFIX}/lib/libcadical.so" \
  "${PREFIX}/lib/libsymcc_qfbv_cadical_realtime.so"
printf 'Installed pinned CaDiCaL %s at %s\n' "${VERSION}" "${PREFIX}"
