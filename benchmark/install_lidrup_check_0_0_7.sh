#!/usr/bin/env bash
set -euo pipefail

VERSION=0.0.7
COMMIT=3ae8c23cd978c313ee14472327bf0f9560601015
PREFIX=${LIDRUP_PREFIX:-"${HOME}/.local/opt/lidrup-check-${VERSION}"}
SOURCE=${LIDRUP_SOURCE:-"${HOME}/.cache/symcc/lidrup-check-${VERSION}"}

for tool in git gcc make sha256sum; do
  command -v "${tool}" >/dev/null
done

if [[ ! -d "${SOURCE}/.git" ]]; then
  mkdir -p "$(dirname "${SOURCE}")"
  git clone --filter=blob:none \
    https://github.com/arminbiere/lidrup-check.git "${SOURCE}"
fi

git -C "${SOURCE}" fetch --depth 1 origin "${COMMIT}"
git -C "${SOURCE}" checkout --detach "${COMMIT}"
[[ "$(git -C "${SOURCE}" rev-parse HEAD)" == "${COMMIT}" ]]

(
  cd "${SOURCE}"
  ./configure
  make -j"${LIDRUP_BUILD_JOBS:-2}"
)

mkdir -p "${PREFIX}/bin" "${PREFIX}/share"
install -m 0755 "${SOURCE}/lidrup-check" "${PREFIX}/bin/lidrup-check"
install -m 0644 "${SOURCE}/LICENSE" "${PREFIX}/share/LICENSE"
printf '%s\n' "${COMMIT}" >"${PREFIX}/share/source-commit"

[[ "$("${PREFIX}/bin/lidrup-check" --version)" == "${VERSION}" ]]
sha256sum "${PREFIX}/bin/lidrup-check"
printf 'Installed pinned lidrup-check %s at %s\n' "${VERSION}" "${PREFIX}"
