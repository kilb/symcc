# F394 UCSan Explicit-Object Checker Evidence

This directory seals the local evidence for the allocation-level explicit-object
OOB/UAF closure. The implementation was compared with UCSan OSDI 2026 section
3.4 and the official source revision recorded in `upstream-revision.txt`.

## Verified scope

- dynamic stack-frame tokens and exact `alloca` size registration;
- C and common Itanium C++ heap allocator registration;
- lower, upper, and crossing allocation-bound checks before native access;
- shared tombstones for heap UAF, stack-use-after-return, and double free;
- prepare/native/commit handling for realloc failure, in-place resize, movement,
  and reallocarray multiplication overflow;
- pointer-slot shadow preservation in retained bytes and clearing in changed
  resize ranges;
- normal return, Itanium cleanup/resume, and direct noreturn throw exits;
- compile-time rejection of `musttail` and Windows funclet exits;
- LLVM 17 and LLVM 18 instrumented native regressions.

## Results

| Gate | Result |
| --- | --- |
| Canonical Python gate | 954 passed + 235 subtests; exact 954/954 identity |
| Full LLVM lit | 255 passed + 1 existing unsupported |
| UCSan filter, LLVM 18 | 9/9 passed |
| UCSan filter, LLVM 17 | 9/9 passed |
| Native mechanism matrix | 14 modes x 11 samples = 154/154 expected outcomes |
| Python static checks | Ruff, format check, py_compile, diff check passed |

`mechanism-benchmark.json` reports whole-process latency. Abort rows include
signal delivery and the host core handler, so the values are not per-check
instruction cost.

## Claim boundary

This evidence proves the implemented checker mechanisms and regression gates.
It does not claim byte-level UNINIT/UBI, global-object or full DFSan propagation,
custom allocator effect models, Windows funclet support, kernel ABI coverage,
coverage improvement, vulnerability yield, or cross-system speedup. It is not a
full reproduction of the UCSan paper's benchmark results.

`SHA256SUMS.txt` covers every local evidence file except itself. The top-level
Codex manifest independently covers this README, the local manifest, report,
diagram pair, and delivery verifier.
