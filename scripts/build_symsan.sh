#!/bin/bash
# 构建 R-Fuzz/SymSan 核心(compiler wrapper ko-clang + DFSan runtime + fgtest driver),
# 供 --engine symsan 使用。实验性,见 docs/engine_abstraction.md。
#
# 依赖:LLVM 18(clang-18/libc++)、Z3 >= 4.8.15(系统 Z3 常为 4.8.12,过旧——用 Z3_ROOT 指向较新 Z3)。
# 用法:
#   SYMSAN_SRC=/path/to/symsan  [Z3_ROOT=/path/to/z3-4.13.x]  scripts/build_symsan.sh
# 产出:$SYMSAN_SRC/build/ 下的 ko-clang / libdfsan_rt / fgtest;把 fgtest 路径给 SYMSAN_FGTEST。
set -u
SS="${SYMSAN_SRC:?请设 SYMSAN_SRC 指向 symsan 源码(git clone https://github.com/R-Fuzz/symsan)}"
Z3_ARGS=()
if [ -n "${Z3_ROOT:-}" ]; then
  Z3_ARGS=(-DZ3_LIBRARY="$Z3_ROOT/bin/libz3.so" -DZ3_INCLUDE_DIR="$Z3_ROOT/include")
fi
rm -rf "$SS/build"; mkdir -p "$SS/build"; cd "$SS/build"
cmake -DCMAKE_C_COMPILER=clang-18 -DCMAKE_CXX_COMPILER=clang++-18 \
      -DLLVM_DIR="$(llvm-config-18 --cmakedir)" -DCMAKE_BUILD_TYPE=Release \
      "${Z3_ARGS[@]}" "$SS" || { echo "cmake 失败(常见:Z3<4.8.15 → 设 Z3_ROOT)"; exit 1; }
make -j"$(nproc)" || { echo "make 失败"; exit 1; }
echo "OK. fgtest = $(find "$SS/build" -name fgtest -type f | head -1)"
