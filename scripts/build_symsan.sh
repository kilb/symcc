#!/bin/bash
# 构建 R-Fuzz/SymSan(ko-clang 编译器 + DFSan runtime + fgtest driver),供 --engine symsan 使用。
# 本脚本编码了在本机(Ubuntu 24.04 / clang-18.1.3)实测【端到端跑通】的完整配方。
# 见 docs/engine_abstraction.md。
set -euo pipefail

SS="${SYMSAN_SRC:?设 SYMSAN_SRC 指向 symsan 源码: git clone https://github.com/R-Fuzz/symsan}"
INSTALL="${SYMSAN_INSTALL:-$SS/install}"

# 1) 依赖(SymSan 的构建级联,本机实测):
#    apt-get install -y libc++-18-dev libc++abi-18-dev libunwind-18-dev libboost-container-dev \
#                       protobuf-compiler libprotobuf-dev libgoogle-perftools-dev libbsd-dev
#    Z3 >= 4.8.15(系统 Z3 常为 4.8.12,过旧)。CI 用 Z3 4.15.4;本机用 4.13.0 prebuilt + 下面的 shim 亦可。
Z3_ARGS=()
if [ -n "${Z3_ROOT:-}" ]; then
  Z3_ARGS=(-DZ3_LIBRARY="$Z3_ROOT/bin/libz3.so" -DZ3_INCLUDE_DIR="$Z3_ROOT/include")
fi

# 2) shim:若 Z3 的 C API 缺 Z3_mk_string_from_code / Z3_mk_string_to_code(SMT 字符串理论,
#    字节级 fuzz 目标不需要),给 solvers/z3-ts.cpp 加抛异常桩使其编译。用 Z3 4.15.4 通常无需此步。
Z3TS="$SS/solvers/z3-ts.cpp"
if ! grep -q "SYMCC_Z3_STRCODE_SHIM" "$Z3TS"; then
  python3 - "$Z3TS" <<'PY'
import sys
p=sys.argv[1]; L=open(p).read().splitlines(keepends=True)
i=max(k for k,l in enumerate(L) if l.startswith("#include"))
L.insert(i+1, '\n// SYMCC_Z3_STRCODE_SHIM: 本机 Z3 C API 无 str.from_code/to_code -> 抛异常桩(字节级目标不需要)\n'
              '#include <stdexcept>\n#ifndef SYMCC_HAVE_Z3_STRCODE\n'
              'static inline Z3_ast Z3_mk_string_from_code(Z3_context, Z3_ast){throw std::runtime_error("no str.from_code");}\n'
              'static inline Z3_ast Z3_mk_string_to_code(Z3_context, Z3_ast){throw std::runtime_error("no str.to_code");}\n#endif\n')
open(p,"w").write("".join(L))
print("z3-ts shim inserted")
PY
fi

# 2.5) 移植的 SymCC 自研技术补丁(给 DFSan 运行时/launcher/fgtest):
#      ④ 选择性符号化(focus_bytes 门控 taint 源)、③ 字典引导(SYMCC_DICT)、① 多字段解组合
#      (SYMCC_MULTI_SOLVE)。见 docs/symsan_ported_techniques.md。
#      幂等:dfsan_flags.inc 已含 focus_bytes 则视为已打补丁,跳过。
PATCH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/symsan_patches/symsan_ported_techniques.patch"
if [ -f "$PATCH" ] && ! grep -q "focus_bytes" "$SS/runtime/dfsan/dfsan_flags.inc"; then
  ( cd "$SS" && git apply --whitespace=nowarn "$PATCH" ) \
    && echo "symsan ported-techniques patch applied (④选择性符号化 ③字典 ①多字段组合)" \
    || echo "WARN: 技术补丁应用失败(upstream 可能已改动),请手工核对 $PATCH"
fi

# 3) 构建 + 安装(install 生成 ko-clang 期望的 ../lib/symsan/ 布局:passes + runtime + *.a + taint.ld + abilist)
rm -rf "$SS/build"; mkdir -p "$SS/build"; cd "$SS/build"
cmake -DCMAKE_C_COMPILER=clang-18 -DCMAKE_CXX_COMPILER=clang++-18 \
      -DLLVM_DIR="$(llvm-config-18 --cmakedir)" -DCMAKE_BUILD_TYPE=Release \
      -DCMAKE_INSTALL_PREFIX="$INSTALL" "${Z3_ARGS[@]}" "$SS"
make -j"$(nproc)"
make install

FGTEST="$(find "$INSTALL" "$SS/build" -name fgtest -type f | head -1)"
echo
echo "=== SymSan 构建完成 ==="
echo "  ko-clang : $INSTALL/bin/ko-clang"
echo "  fgtest   : $FGTEST"
echo
echo "编译目标(FastGen 插桩模式,fgtest 驱动需要):"
echo "  KO_CC=clang-18 KO_USE_FASTGEN=1 KO_DONT_OPTIMIZE=1 $INSTALL/bin/ko-clang -o target_symsan target.c"
echo "跑一次 concolic(--engine symsan 内部即这样调):"
echo "  TAINT_OPTIONS=\"taint_file=<in> output_dir=<out>\" $FGTEST target_symsan <in>   # 解写进 <out>/id-*"
echo "接入并行编排:  SYMSAN_FGTEST=$FGTEST  python3 benchmark/run_benchmark.py --engine symsan ..."
