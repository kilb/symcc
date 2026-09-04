#!/usr/bin/env bash
set -euo pipefail

VERSION=0.9.1
COMMIT=8d1eb01093ae54d9b4586456b69c3bf31000a4c2
PREFIX=${BITWUZLA_PREFIX:-"${HOME}/.local/opt/bitwuzla-${VERSION}"}
SOURCE=${BITWUZLA_SOURCE:-"${HOME}/.cache/symcc/bitwuzla-${VERSION}"}

for tool in git python3 meson ninja pkg-config; do
  command -v "${tool}" >/dev/null
done
pkg-config --atleast-version=6.3.0 gmp
pkg-config --atleast-version=4.2.1 mpfr

if [[ ! -d "${SOURCE}/.git" ]]; then
  mkdir -p "$(dirname "${SOURCE}")"
  git clone --filter=blob:none \
    https://github.com/bitwuzla/bitwuzla.git "${SOURCE}"
fi

git -C "${SOURCE}" fetch --depth 1 origin "${COMMIT}"
git -C "${SOURCE}" checkout --detach "${COMMIT}"
[[ "$(git -C "${SOURCE}" rev-parse HEAD)" == "${COMMIT}" ]]

(
  cd "${SOURCE}"
  python3 configure.py release \
    --build-dir "${SOURCE}/build-symcc-${VERSION}" \
    --prefix "${PREFIX}" \
    --no-python \
    --no-testing \
    --wipe
)
ninja -C "${SOURCE}/build-symcc-${VERSION}"
ninja -C "${SOURCE}/build-symcc-${VERSION}" install

[[ "$("${PREFIX}/bin/bitwuzla" --version)" == "${VERSION}" ]]
sha256sum "${PREFIX}/bin/bitwuzla"
printf 'Add %s/bin to PATH\n' "${PREFIX}"
