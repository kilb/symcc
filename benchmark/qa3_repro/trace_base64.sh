#!/usr/bin/env bash
# Reproduce the real-target DSE trace used by Architecture_QA3.md section 4.9.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TARGET="${TARGET:-$ROOT/benchmark/public/bin/lava-m/base64}"
CORPUS_ROOT="${CORPUS_ROOT:-$ROOT/benchmark/public/lava_corpus/LAVA-M/base64}"
WORK_BASE="${WORK:-/tmp/symcc_qa3}"

mkdir -p "$WORK_BASE"
RUN_DIR="$(mktemp -d "$WORK_BASE/base64-trace.XXXXXX")"

if [[ ! -x "$TARGET" ]]; then
  echo "missing SymCC target: $TARGET" >&2
  exit 2
fi

run_seed() {
  local label="$1"
  local seed="$2"
  local out="$RUN_DIR/$label/out"
  local telemetry="$RUN_DIR/$label/telemetry.json"
  local bitmap="$RUN_DIR/$label/qsym_bitmap"

  if [[ ! -f "$seed" ]]; then
    echo "missing seed: $seed" >&2
    return 2
  fi

  mkdir -p "$out"
  set +e
  SYMCC_OUTPUT_DIR="$out" \
  SYMCC_INPUT_FILE="$seed" \
  SYMCC_AFL_COVERAGE_MAP="$bitmap" \
  SYMCC_TELEMETRY_OUT="$telemetry" \
  SYMCC_DATA_COVERAGE=1 \
    "$TARGET" -d "$seed" >"$RUN_DIR/$label/stdout" 2>"$RUN_DIR/$label/stderr"
  local rc=$?
  set -e

  python3 - "$label" "$seed" "$out" "$telemetry" "$bitmap" "$rc" <<'PY'
import hashlib
import json
import os
import sys

label, seed, out_dir, telemetry_path, bitmap_path, rc = sys.argv[1:]
files = sorted(
    os.path.join(out_dir, name)
    for name in os.listdir(out_dir)
    if os.path.isfile(os.path.join(out_dir, name))
)
hashes = []
optimistic = 0
for path in files:
    with open(path, "rb") as stream:
        hashes.append(hashlib.sha256(stream.read()).hexdigest())
    if "optimistic" in os.path.basename(path):
        optimistic += 1

print(f"[{label}] seed={seed}")
print(
    f"  rc={rc} outputs={len(files)} exact={len(files) - optimistic} "
    f"optimistic={optimistic} unique_content={len(set(hashes))} "
    f"bitmap_bytes={os.path.getsize(bitmap_path) if os.path.exists(bitmap_path) else 0}"
)
if not os.path.exists(telemetry_path):
    print("  telemetry=missing (the target may have timed out or terminated abnormally)")
    raise SystemExit(0)

with open(telemetry_path, "r", encoding="utf-8") as stream:
    data = json.load(stream)

keys = (
    "input_bytes",
    "symbolic_branches",
    "interesting_branches",
    "solver_queries",
    "solver_sat",
    "solver_unsat",
    "solver_unknown",
    "z3_solves",
    "generated",
    "solver_time_us",
    "relevant_input_bytes",
    "max_dependency_bytes",
)
print("  " + " ".join(f"{key}={data.get(key, 0)}" for key in keys))
PY
}

set -e
run_seed "rand" "$CORPUS_ROOT/fuzzer_input/rand.b64"
run_seed "utmp100" "$CORPUS_ROOT/inputs/utmp-fuzzed-100.b64"

echo "artifacts=$RUN_DIR"
