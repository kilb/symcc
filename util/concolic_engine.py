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

    def wrap_run(self, target_cmd: "list[str]", input_file: str, output_dir: str,
                 env: "dict[str, str]", use_stdin: bool, timeout_sec: int
                 ) -> "tuple[list[str], dict[str, str], bool]":
        env = dict(env)
        if not target_cmd:
            raise ValueError("SymSan target command must not be empty")
        env["SYMCC_OUTPUT_DIR"] = output_dir
        env["SYMCC_ENABLE_LINEARIZATION"] = "1"
        # hint 文件默认开,允许【调用方传入的 env】关(消融技术②)。
        # 必须先看 env 再看 os.environ:消融实验是往 env 里塞 SYMCC_EMIT_HINTS=0,
        # 若只读 os.environ 就会被默认值 "1" 覆盖回去,开关形同虚设。
        env["SYMCC_EMIT_HINTS"] = env.get(
            "SYMCC_EMIT_HINTS", os.environ.get("SYMCC_EMIT_HINTS", "1"))
        if use_stdin:
            cmd = ["timeout", "-k", "5", str(timeout_sec)] + list(target_cmd)
            return cmd, env, True
        env["SYMCC_INPUT_FILE"] = str(input_file)
        cmd = ["timeout", "-k", "5", str(timeout_sec)] + [
            arg.replace("@@", str(input_file)) for arg in target_cmd
        ]
        return cmd, env, False

    def build_argv(self, compiler: str, source: str, out: str
                   ) -> "tuple[list[str], dict[str, str]]":
        return [compiler, "-O2", str(source), "-o", str(out)], {}


class SymSanEngine(ConcolicEngine):
    """SymSan(DFSan + 进程内/外求解):经 fgtest driver 驱动,写 TAINT_OPTIONS 里的 output_dir。

    ⚠️ 实验性:需先构建 SymSan(见 `scripts/build_symsan.sh`,依赖 Z3>=4.8.15 + LLVM 18)并用 `KO_CC`
    重编目标为 `*_symsan`;`SYMSAN_FGTEST` 指向构建出的 fgtest。5 个自研技术点已移植 4 个(④选择性符号化/
    ③字典引导/②hint/①多字段解组合,均经与 SymCC 同名的 SYMCC_FOCUS_BYTES/SYMCC_DICT/SYMCC_EMIT_HINTS/
    SYMCC_MULTI_SOLVE 通道下发,见 `docs/symsan_ported_techniques.md`);⑤fast-solve 基本作废(SymSan 单
    task 多字节解 + JIGSAW 本就 JIT 快解)。已跑通真实公开目标 LAVA-M base64(`scripts/build_public_symsan.sh`)。
    """

    name = "symsan"
    binary_suffix = "_symsan"
    detect_symbols = ("dfs$", "__dfsan_", "__dfsw_")

    def __init__(self) -> None:
        # fgtest driver 路径(build_symsan.sh 产出);默认在 PATH 里找 fgtest
        fg = os.environ.get("SYMSAN_FGTEST", "fgtest")
        # SOTA 求解栈:SYMSAN_SOLVER=rgd 时改用 RGD/JIGSAW driver(I2S input-to-state →
        # JIGSAW 梯度 → Z3 级联,源自 SymSan 的 aflpp 参考实现)。默认取 fgtest 同目录的
        # fgtest_rgd 兄弟;可用 SYMSAN_FGTEST_RGD 显式指定。JIGSAW 需再设 SYMSAN_USE_JIGSAW=1
        # (经 env 透传给 driver)。见 docs/symsan_ported_techniques.md。
        if os.environ.get("SYMSAN_SOLVER", "").lower() == "rgd":
            rgd = os.environ.get("SYMSAN_FGTEST_RGD")
            if not rgd and fg.endswith("fgtest"):
                rgd = fg + "_rgd"
            if not rgd:
                # 不静默降级:请求了 RGD 却推不出 driver 路径,若默默退回 fgtest,
                # 整批实验会以为在跑 RGD/JIGSAW,实际跑的是 Z3 基线——数据无声作废。
                raise ValueError(
                    f"SYMSAN_SOLVER=rgd 但无法定位 RGD driver:SYMSAN_FGTEST={fg!r} "
                    "不以 'fgtest' 结尾,无法推出兄弟路径。请显式设 SYMSAN_FGTEST_RGD "
                    "指向构建出的 fgtest_rgd。")
            fg = rgd
        self.fgtest = fg

    def wrap_run(self, target_cmd: "list[str]", input_file: str, output_dir: str,
                 env: "dict[str, str]", use_stdin: bool, timeout_sec: int
                 ) -> "tuple[list[str], dict[str, str], bool]":
        env = dict(env)
        # fgtest 从 TAINT_OPTIONS 解析 taint_file=(污点源=输入文件)与 output_dir=(解写入此目录),
        # 见 symsan driver/fgtest.cpp。两者用空格分隔(已端到端验证:seed→求解分支→输出 id-*)。
        # 该格式【没有引号/转义】:driver 用 strchr(s,':') / strchr(s,' ') 找值的结尾,
        # 故路径里出现空格或冒号都会被截断成一个不存在的目录——输出全部丢失且不报错。
        # 与其让它安静地跑空,不如在这里就明确失败。
        for _label, _path in (("输入文件", input_file), ("输出目录", output_dir)):
            _bad = [c for c in (" ", ":", "\t", "\n") if c in str(_path)]
            if _bad:
                raise ValueError(
                    f"SymSan {_label}路径含 TAINT_OPTIONS 分隔符 {_bad!r},会被 driver 截断: "
                    f"{_path!r}。请改用不含空格/冒号的路径。")
        taint_opts = f"taint_file={input_file} output_dir={output_dir}"
        # 技术④ 选择性符号化:编排层把选中的字节区间放在 SYMCC_FOCUS_BYTES(单区间 "s-e"),
        # 这里翻译成 fgtest→launcher→DFSan 运行时认得的 focus_bytes=。SymSan 的 DFSan 会据此
        # 仅对该区间的输入偏移打标签,其余字节保持具体值,缩小符号状态、省下无关字节的求解开销
        # (语义与 SymCC 的 SYMCC_FOCUS_BYTES 一致;见 runtime dfsan_custom.cpp get_label_for 门控)。
        focus = self._sanitize_focus(env.get("SYMCC_FOCUS_BYTES", ""))
        if focus:
            taint_opts += f" focus_bytes={focus}"
        env["TAINT_OPTIONS"] = taint_opts
        binary = target_cmd[0]
        cmd = [
            "timeout", "-k", "5", str(timeout_sec),
            self.fgtest, binary, str(input_file),
        ]
        if len(target_cmd) > 1:
            # Driver 的前两个参数仍是“污点输入契约”；`--` 后才是目标
            # argv[1:]。逐元素传递而非拼 shell 字符串，空参数、空格和标点均
            # 保持原样，且 @@ 与 SymCC 后端采用相同替换语义。
            cmd += ["--"] + [
                arg.replace("@@", str(input_file)) for arg in target_cmd[1:]
            ]
        return cmd, env, False                 # SymSan 走 argv 文件输入,不喂 stdin

    @staticmethod
    def _sanitize_focus(spec: str) -> str:
        """把 SYMCC_FOCUS_BYTES 规约为单个 "s-e" 区间。

        DFSan 的 sanitizer flag 解析把 ',' 也当分隔符(sanitizer_flag_parser.cpp is_space),
        逗号形式的多区间会破坏 flag 串;故与 SymCC 的 sscanf("%lu-%lu") 一致,仅取首个区间。
        非法输入返回空串(=不启用 focus,退回全字节符号化,保证不丢覆盖)。"""
        spec = (spec or "").strip()
        if not spec:
            return ""
        first = spec.split(",")[0].strip()   # 逗号形式取首段,匹配 SymCC 单区间语义
        parts = first.split("-")
        if len(parts) != 2:
            return ""
        try:
            lo, hi = int(parts[0]), int(parts[1])
        except ValueError:
            return ""
        if lo < 0 or hi < lo:
            return ""
        return f"{lo}-{hi}"

    def build_argv(self, compiler: str, source: str, out: str
                   ) -> "tuple[list[str], dict[str, str]]":
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
