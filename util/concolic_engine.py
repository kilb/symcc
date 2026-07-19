"""Concolic 引擎抽象层:让编排层可在 SymCC / SymSan 之间切换。

选择方式:环境变量 `SYMCC_ENGINE=symcc|symsan`(默认 symcc),或 benchmark 的 `--engine` 参数
(它会把该环境变量透传给各 MPI worker)。

设计依据:编排层(`mpi_fuzzing_helper`)已把 concolic 二进制当【黑盒】——喂一个种子文件、运行、
然后从一个输出目录里扫出编号的新测试用例。两个引擎都满足"跑一次 → 把解写进一个目录"的契约,
差异只在四处,本模块把它们收敛到一个 `ConcolicEngine` 接口:
  1. 目标二进制后缀(`_symcc` vs `_symsan`);
  2. 判定"已插桩"的符号;
  3. 编译 wrapper(`symcc/sym++` vs `KO_CC/KO_CXX` 即 ko-clang);
  4. 【如何运行 + 设什么环境变量】。

运行模型的关键区别:
  - **SymCC**:二进制【自驱】。`SYMCC_OUTPUT_DIR=<out> ./target_symcc <input>`,运行时自己把 Z3 求出的
    新输入写进 `<out>`。
  - **SymSan**:二进制【不自驱】(DFSan 只做 label 传播,不含求解循环),需一个 driver。R-Fuzz/symsan 的
    `driver/fgtest.cpp` 就是这样一个独立 driver:`TAINT_OPTIONS="output_dir=<out>" fgtest ./target_symsan <input>`,
    它加载目标、跑 DFSan 取 label、进程内 Z3 求解、把新输入写进 `<out>/id-*`——正好复现 SymCC 的目录契约,
    因此编排层 ~90% 可原样复用(见 `docs/engine_abstraction.md` / `SymSan_迁移评估.md`)。
"""
from __future__ import annotations

import os


class ConcolicEngine:
    """concolic 引擎接口。子类实现 `wrap_run` 把"目标命令 + 输入 + 输出目录"翻译成实际要执行的
    (命令列表, 环境字典)。"""

    name: str = "base"
    binary_suffix: str = ""
    detect_symbols: tuple[str, ...] = ()

    def wrap_run(self, target_cmd: "list[str]", input_file: str, output_dir: str,
                 env: "dict[str, str]", use_stdin: bool, timeout_sec: int
                 ) -> "tuple[list[str], dict[str, str], bool]":
        """返回 (cmd, env, feed_stdin)：
          - cmd: 交给 subprocess.run 的命令(已含 timeout 包装);
          - env: 运行环境(在传入 env 基础上加引擎专属变量);
          - feed_stdin: 是否把 input_file 作为 stdin 喂入(SymCC stdin 模式为真;SymSan 恒为假,走 argv)。
        """
        raise NotImplementedError

    def build_argv(self, compiler: str, source: str, out: str
                   ) -> "tuple[list[str], dict[str, str]]":
        """把单文件 C 目标编译成本引擎插桩二进制,返回 (编译命令, 额外环境变量)。
        `compiler` 为本引擎的编译器路径(SymCC 的 symcc / SymSan 的 ko-clang)。"""
        raise NotImplementedError


class SymCCEngine(ConcolicEngine):
    """SymCC(QSYM/Z3 后端):二进制自驱,写 SYMCC_OUTPUT_DIR。保持与既有行为逐字节一致(默认引擎)。"""

    name = "symcc"
    binary_suffix = "_symcc"
    detect_symbols = ("__sym_ctor", "_sym_build")

    def wrap_run(self, target_cmd, input_file, output_dir, env, use_stdin, timeout_sec):
        env = dict(env)
        env["SYMCC_OUTPUT_DIR"] = output_dir
        env["SYMCC_ENABLE_LINEARIZATION"] = "1"
        # hint 文件默认开,允许上游 env 关(消融技术③)
        env["SYMCC_EMIT_HINTS"] = os.environ.get("SYMCC_EMIT_HINTS", "1")
        if use_stdin:
            cmd = ["timeout", "-k", "5", str(timeout_sec)] + list(target_cmd)
            return cmd, env, True
        env["SYMCC_INPUT_FILE"] = str(input_file)
        cmd = ["timeout", "-k", "5", str(timeout_sec)] + [
            arg.replace("@@", str(input_file)) for arg in target_cmd
        ]
        return cmd, env, False

    def build_argv(self, compiler, source, out):
        return [compiler, "-O2", str(source), "-o", str(out)], {}


class SymSanEngine(ConcolicEngine):
    """SymSan(DFSan + 进程内/外求解):经 fgtest driver 驱动,写 TAINT_OPTIONS 里的 output_dir。

    ⚠️ 实验性:需先构建 SymSan(见 `scripts/build_symsan.sh`,依赖 Z3>=4.8.15 + LLVM 18)并用 `KO_CC`
    重编目标为 `*_symsan`;`SYMSAN_FGTEST` 指向构建出的 fgtest。5 个自研技术点(多分支联合求解/hint/
    字典/选择性符号化/fast-solve)尚未在 SymSan 侧重写——见 `docs/engine_abstraction.md` 的 TODO。
    """

    name = "symsan"
    binary_suffix = "_symsan"
    detect_symbols = ("dfs$", "__dfsan_", "__dfsw_")

    def __init__(self) -> None:
        # fgtest driver 路径(build_symsan.sh 产出);默认在 PATH 里找 fgtest
        self.fgtest = os.environ.get("SYMSAN_FGTEST", "fgtest")

    def wrap_run(self, target_cmd, input_file, output_dir, env, use_stdin, timeout_sec):
        env = dict(env)
        # fgtest 从 TAINT_OPTIONS 解析 taint_file=(污点源=输入文件)与 output_dir=(解写入此目录),
        # 见 symsan driver/fgtest.cpp。两者用空格分隔(已端到端验证:seed→求解分支→输出 id-*)。
        env["TAINT_OPTIONS"] = f"taint_file={input_file} output_dir={output_dir}"
        binary = target_cmd[0]                 # _symsan 二进制;fgtest 只取 (target, input),丢弃 @@ 等额外 argv
        cmd = ["timeout", "-k", "5", str(timeout_sec), self.fgtest, binary, str(input_file)]
        return cmd, env, False                 # SymSan 走 argv 文件输入,不喂 stdin

    def build_argv(self, compiler, source, out):
        # ko-clang 需 FastGen 插桩模式(KO_USE_FASTGEN=1),fgtest 才有回调;KO_DONT_OPTIMIZE 保留分支
        env = {
            "KO_CC": os.environ.get("KO_CC", "clang-18"),
            "KO_USE_FASTGEN": "1",
            "KO_DONT_OPTIMIZE": "1",
            "KO_USE_NATIVE_LIBCXX": os.environ.get("KO_USE_NATIVE_LIBCXX", "1"),
        }
        return [compiler, "-O2", str(source), "-o", str(out)], env


_ENGINES: "dict[str, type[ConcolicEngine]]" = {
    "symcc": SymCCEngine,
    "symsan": SymSanEngine,
}


def get_engine(name: "str | None" = None) -> ConcolicEngine:
    """按名字(或 SYMCC_ENGINE 环境变量,默认 symcc)取引擎实例。"""
    key = (name or os.environ.get("SYMCC_ENGINE", "symcc")).lower()
    cls = _ENGINES.get(key)
    if cls is None:
        raise ValueError(
            f"未知 concolic 引擎: {key!r}(可选: {sorted(_ENGINES)})")
    return cls()


def available_engines() -> "list[str]":
    return sorted(_ENGINES)
