#!/usr/bin/env bash
set -euo pipefail

COMMIT=d9382fb4b0acf094034ee91e2ed0a22b1b479c1d
PREFIX=${PALRUP_PREFIX:-"${HOME}/.local/opt/palrup-check-sat2026"}
SOURCE=${PALRUP_SOURCE:-"${HOME}/.cache/symcc/palrup-check-sat2026"}
BUILD=${PALRUP_BUILD:-"${SOURCE}/build-symcc"}

for tool in git cmake gcc make sha256sum; do
  command -v "${tool}" >/dev/null
done

if [[ ! -d "${SOURCE}/.git" ]]; then
  mkdir -p "$(dirname "${SOURCE}")"
  git clone --filter=blob:none \
    https://github.com/rubenGoetz/PalRUP-Check.git "${SOURCE}"
fi

git -C "${SOURCE}" fetch --depth 1 origin "${COMMIT}"
git -C "${SOURCE}" checkout --detach "${COMMIT}"
[[ "$(git -C "${SOURCE}" rev-parse HEAD)" == "${COMMIT}" ]]

cmake -S "${SOURCE}" -B "${BUILD}" -DCMAKE_BUILD_TYPE=Release
cmake --build "${BUILD}" --parallel "${PALRUP_BUILD_JOBS:-2}" --target \
  proof_fragment_to_txt palrup_local_check palrup_redistribute palrup_confirm

mkdir -p "${PREFIX}/bin" "${PREFIX}/share"
for tool in proof_fragment_to_txt palrup_local_check palrup_redistribute \
  palrup_confirm; do
  install -m 0755 "${BUILD}/${tool}" "${PREFIX}/bin/${tool}"
done
install -m 0644 "${SOURCE}/LICENSE" "${PREFIX}/share/LICENSE"
printf '%s\n' "${COMMIT}" >"${PREFIX}/share/source-commit"

sha256sum "${PREFIX}/bin/"*
printf 'Installed pinned SAT 2026 PalRUP checker at %s\n' "${PREFIX}"
