#!/usr/bin/env bash
set -euo pipefail

# This is the CaDiCaL fork pinned by the SAT 2026 PalRUP/Mallob artifact.
COMMIT=be7a0f84190b3216c589696b2010e8cbf8a8252e
REMOTE=https://github.com/domschrei/cadical.git
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PREFIX=${CADICAL_PALRUP_PREFIX:-"${HOME}/.local/opt/cadical-palrup-sat2026"}
SOURCE=${CADICAL_PALRUP_SOURCE:-"${HOME}/.cache/symcc/cadical-palrup-sat2026"}
PRODUCER_SOURCE=${SYMCC_QFBV_PALRUP_PRODUCER_SOURCE:-"${SCRIPT_DIR}/../util/qfbv_cadical_palrup_producer.cpp"}
POOL_SOURCE=${SYMCC_QFBV_PALRUP_POOL_SOURCE:-"${SCRIPT_DIR}/../util/qfbv_cadical_palrup_pool.cpp"}

for tool in git g++ gcc make nm sha256sum; do
  command -v "${tool}" >/dev/null
done

if [[ ! -d "${SOURCE}/.git" ]]; then
  mkdir -p "$(dirname "${SOURCE}")"
  git clone --filter=blob:none \
    "${REMOTE}" "${SOURCE}"
fi

[[ "$(git -C "${SOURCE}" remote get-url origin)" == "${REMOTE}" ]]
git -C "${SOURCE}" fetch --depth 1 origin "${COMMIT}"
git -C "${SOURCE}" checkout --detach "${COMMIT}"
[[ "$(git -C "${SOURCE}" rev-parse HEAD)" == "${COMMIT}" ]]
git -C "${SOURCE}" diff --quiet
git -C "${SOURCE}" diff --cached --quiet

(
  cd "${SOURCE}"
  ./configure -fPIC
  make -j"${CADICAL_PALRUP_BUILD_JOBS:-2}"
)

mkdir -p "${PREFIX}/bin" "${PREFIX}/lib" "${PREFIX}/include" \
  "${PREFIX}/share"
install -m 0755 "${SOURCE}/build/cadical" "${PREFIX}/bin/cadical-palrup"
install -m 0644 "${SOURCE}/build/libcadical.a" \
  "${PREFIX}/lib/libcadical.a"
# This fork's public header includes auxiliary headers from the same source
# directory, so install the complete fixed header set instead of one file.
for header in "${SOURCE}"/src/*.h "${SOURCE}"/src/*.hpp; do
  install -m 0644 "${header}" "${PREFIX}/include/$(basename "${header}")"
done

[[ -f "${PRODUCER_SOURCE}" ]]
[[ -f "${POOL_SOURCE}" ]]
PRODUCER_TEMP=$(mktemp "${PREFIX}/lib/.palrup-producer.XXXXXX.so")
POOL_TEMP=$(mktemp "${PREFIX}/lib/.palrup-pool.XXXXXX.so")
cleanup() {
  [[ ! -e "${PRODUCER_TEMP}" ]] || unlink "${PRODUCER_TEMP}"
  [[ ! -e "${POOL_TEMP}" ]] || unlink "${POOL_TEMP}"
}
trap cleanup EXIT
g++ -std=c++17 -fPIC -shared -fvisibility=hidden \
  -Wall -Wextra -Werror -pthread \
  -I"${PREFIX}/include" "${PRODUCER_SOURCE}" \
  "${PREFIX}/lib/libcadical.a" \
  -o "${PRODUCER_TEMP}"
g++ -std=c++17 -fPIC -shared -fvisibility=hidden \
  -Wall -Wextra -Werror -pthread \
  -I"${PREFIX}/include" "${POOL_SOURCE}" \
  "${PREFIX}/lib/libcadical.a" \
  -o "${POOL_TEMP}"
chmod 0755 "${PRODUCER_TEMP}" "${POOL_TEMP}"
mv -f "${PRODUCER_TEMP}" \
  "${PREFIX}/lib/libsymcc_qfbv_palrup_producer.so"
mv -f "${POOL_TEMP}" "${PREFIX}/lib/libsymcc_qfbv_palrup_pool.so"

printf '%s\n' "${COMMIT}" >"${PREFIX}/share/source-commit~"
mv -f "${PREFIX}/share/source-commit~" "${PREFIX}/share/source-commit"
SYMBOLS=${PREFIX}/share/producer-symbols
nm -D "${PREFIX}/lib/libsymcc_qfbv_palrup_producer.so" >"${SYMBOLS}"
grep -q ' symcc_qfbv_palrup_producer_protocol$' "${SYMBOLS}"
grep -q ' symcc_qfbv_palrup_producer_source_commit$' "${SYMBOLS}"
grep -q ' symcc_qfbv_palrup_produce$' "${SYMBOLS}"
POOL_SYMBOLS=${PREFIX}/share/pool-symbols
nm -D "${PREFIX}/lib/libsymcc_qfbv_palrup_pool.so" >"${POOL_SYMBOLS}"
grep -q ' symcc_qfbv_palrup_pool_protocol$' "${POOL_SYMBOLS}"
grep -q ' symcc_qfbv_palrup_pool_source_commit$' "${POOL_SYMBOLS}"
grep -q ' symcc_qfbv_palrup_pool_result_fields_per_rank$' "${POOL_SYMBOLS}"
grep -q ' symcc_qfbv_palrup_produce_pool$' "${POOL_SYMBOLS}"
sha256sum "${PREFIX}/bin/cadical-palrup" "${PREFIX}/lib/libcadical.a" \
  "${PREFIX}/lib/libsymcc_qfbv_palrup_producer.so" \
  "${PREFIX}/lib/libsymcc_qfbv_palrup_pool.so" \
  "${PRODUCER_SOURCE}" "${POOL_SOURCE}" | tee \
  "${PREFIX}/share/build-sha256sums~"
{
  printf 'source_commit=%s\n' "${COMMIT}"
  printf 'source_remote=%s\n' "${REMOTE}"
  printf 'compiler='
  g++ --version | sed -n '1p'
} >"${PREFIX}/share/build-metadata~"
mv -f "${PREFIX}/share/build-sha256sums~" \
  "${PREFIX}/share/build-sha256sums"
mv -f "${PREFIX}/share/build-metadata~" \
  "${PREFIX}/share/build-metadata"
trap - EXIT
printf 'Installed pinned SAT 2026 CaDiCaL PalRUP producer at %s\n' "${PREFIX}"
