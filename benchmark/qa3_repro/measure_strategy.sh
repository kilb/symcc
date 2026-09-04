#!/usr/bin/env bash
# 测量各求解策略在真实目标上的实际占比：输出文件名后缀即策略标签。
set -u
R=/home/ubuntu/code/symcc
W="$1"; NAME="$2"; BIN="$3"; SEEDDIR="$4"; shift 4
EXTRA_ENV="$*"
OUT="$W/$NAME/out"; rm -rf "$W/$NAME"; mkdir -p "$OUT"
SEED=$(ls "$SEEDDIR"/* 2>/dev/null | head -1)
[ -z "$SEED" ] && { echo "$NAME: no seed"; exit 1; }
TEL="$W/$NAME/telemetry.json"
start=$(date +%s.%N)
env $EXTRA_ENV \
    SYMCC_OUTPUT_DIR="$OUT" \
    SYMCC_INPUT_FILE="$SEED" \
    SYMCC_TELEMETRY_OUT="$TEL" \
    timeout 300 "$BIN" "$SEED" > "$W/$NAME/stdout.log" 2> "$W/$NAME/stderr.log"
rc=$?
end=$(date +%s.%N)
echo "$NAME rc=$rc elapsed=$(python3 -c "print(f'{$end-$start:.2f}')")s seed=$(basename $SEED) size=$(stat -c%s $SEED)"
echo "  outputs=$(ls "$OUT" 2>/dev/null | grep -v '\.hints$' | wc -l)"
ls "$OUT" 2>/dev/null | grep -v '\.hints$' | sed 's/^[0-9]*//; s/^-//; s/^$/NOMINAL(strict)/' | sort | uniq -c | sed 's/^/  /'

python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print('  telem: '+' '.join(f'{k}={d[k]}' for k in ('symbolic_branches','interesting_branches','skipped_branches','solver_queries','solver_sat','solver_unsat','solver_unknown','z3_solves','z3_timeouts','fast_solves','solver_time_us') if k in d))" "$TEL" 2>/dev/null || echo "  (no telemetry)"
