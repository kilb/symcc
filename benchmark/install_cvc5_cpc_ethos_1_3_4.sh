#!/usr/bin/env bash
set -euo pipefail

CVC5_VERSION=1.3.4
CVC5_COMMIT=f3b21c4483d3b88dc63cb7cd3e5eb092eee5e341
CVC5_ARCHIVE_SHA256=dcdbfada0ce493ee98259c0816e0daafc561c223aadb3af298c2968e73ea39c6
PREFIX=${1:-"$HOME/.local/share/symcc-cpc-${CVC5_VERSION}"}
WORK=$(mktemp -d "${TMPDIR:-/tmp}/symcc-cpc-install.XXXXXX")

cleanup() {
  find "$WORK" -depth -delete
}
trap cleanup EXIT

ARCHIVE="$WORK/cvc5.zip"
curl -fL --retry 3 \
  -o "$ARCHIVE" \
  "https://github.com/cvc5/cvc5/releases/download/cvc5-${CVC5_VERSION}/cvc5-Linux-x86_64-static.zip"
printf '%s  %s\n' "$CVC5_ARCHIVE_SHA256" "$ARCHIVE" | sha256sum -c -
unzip -q "$ARCHIVE" -d "$WORK/cvc5-dist"

git clone --depth 1 --branch "cvc5-${CVC5_VERSION}" \
  https://github.com/cvc5/cvc5.git "$WORK/cvc5-src"
test "$(git -C "$WORK/cvc5-src" rev-parse HEAD)" = "$CVC5_COMMIT"
"$WORK/cvc5-src/contrib/get-ethos-checker"

mkdir -p "$PREFIX/bin" "$PREFIX/share/cpc"
install -m 0755 \
  "$WORK/cvc5-dist/cvc5-Linux-x86_64-static/bin/cvc5" \
  "$PREFIX/bin/cvc5"
install -m 0755 "$WORK/cvc5-src/deps/bin/ethos" "$PREFIX/bin/ethos"
cp -a "$WORK/cvc5-src/proofs/eo/cpc/." "$PREFIX/share/cpc/"

test "$(sha256sum "$PREFIX/bin/cvc5" | cut -d' ' -f1)" = \
  7562a8b0b835e3eaad5f1a7b4616cd762350cf567b6be03d7e8ee24fa5ced5ee
"$PREFIX/bin/cvc5" --version
printf 'Installed pinned CPC proof toolchain at %s\n' "$PREFIX"
