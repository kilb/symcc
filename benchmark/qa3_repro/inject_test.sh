#!/usr/bin/env bash
# 决定性实验：正在运行的 afl-fuzz 会不会捡走【直接丢进它自己 queue/ 目录】的文件？
# 方法：起 AFL，记下 corpus_count；丢 200 个必然带来新覆盖的文件进 queue/；等 60s；再看 corpus_count。
set -u
W="${WORK:-/tmp/symcc_qa3}/inject"; rm -rf "$W"; mkdir -p "$W/cwd" "$W/seeds"
R="${SYMCC_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
printf '<a/>' > "$W/seeds/s1"
cd "$W/cwd" || exit 1
export AFL_NO_UI=1 AFL_SKIP_CPUFREQ=1 AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1
afl-fuzz -M fuzzer01 -i "$W/seeds" -o "$W/out" \
  -- "$R/benchmark/public/bin/google-fts-afl/xml_read_fuzzer" > "$W/afl.log" 2>&1 &
AFLPID=$!
for i in $(seq 120); do [ -f "$W/out/fuzzer01/fuzzer_stats" ] && break; sleep 0.5; done
sleep 20
Q="$W/out/fuzzer01/queue"
before_cnt=$(grep -E '^corpus_count' "$W/out/fuzzer01/fuzzer_stats" | tr -d ' ' | cut -d: -f2)
before_files=$(ls "$Q" | wc -l)
echo "注入前: corpus_count=$before_cnt  queue 文件数=$before_files"

# 造 200 个内容各异、几乎肯定带来新覆盖的 XML（用 symcc 的命名格式，与 helper 一致）
for i in $(seq 0 199); do
  printf '<r%d attr%d="v%d"><c%d/></r%d>' "$i" "$i" "$i" "$i" "$i" > "$W/tmp_$i"
  mv "$W/tmp_$i" "$Q/$(printf 'id:symcc_%06d,src:000000' "$i")"
done
echo "已直接注入 200 个文件到 $Q"
sleep 120
# 让 AFL 正常退出以刷新最终 fuzzer_stats
kill -INT $AFLPID 2>/dev/null; sleep 8
after_cnt=$(grep -E '^corpus_count' "$W/out/fuzzer01/fuzzer_stats" | tr -d ' ' | cut -d: -f2)
after_files=$(ls "$Q" | wc -l)
own_after=$(ls "$Q" | grep -c '^id:0')
echo "注入后 120s + 正常退出: corpus_count=$after_cnt  queue 文件数=$after_files  其中 AFL 自产=$own_after"
echo
if [ "$after_cnt" = "$own_after" ]; then
  echo ">>> 结论：corpus_count($after_cnt) == AFL 自产数($own_after) ⇒ 【直注文件未被 AFL 采纳】"
else
  echo ">>> 结论：corpus_count($after_cnt) != AFL 自产数($own_after) ⇒ 直注文件【可能】被采纳，需进一步核"
fi
pgrep -x afl-fuzz | xargs -r kill 2>/dev/null
