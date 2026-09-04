# F395 UCSan Byte-Initialization and UBI Evidence

This directory seals local mechanism evidence for explicit-object byte-level
initialization propagation and sink-only use-before-initialization checking.
The implementation was compared with UCSan OSDI 2026 section 3.4 and the
official source revision recorded in `upstream-revision.txt`.

## Verified scope

- alloca/malloc/new UNINIT and calloc INIT byte state;
- exact store/memset and overlap-safe memcpy/memmove byte-tag transfer;
- load-to-SSA, PHI/select, scalar operation and scoped call propagation;
- nonzero pointer, branch/switch, memory-extent and external-pointer sinks;
- zero-length memory-operation semantics;
- realloc failure/prefix/growth/movement tag transactions;
- atomic RMW, cmpxchg field and pointer-slot provenance in the tested
  single-thread semantics;
- LLVM 17 and LLVM 18 instrumented native regressions.

## Results

| Gate | Result |
| --- | --- |
| Canonical Python gate | 954 passed + 235 subtests; exact 954/954 identity |
| Full LLVM lit | 256 passed + 1 existing unsupported |
| UCSan filter, LLVM 18 | 10/10 passed |
| UCSan filter, LLVM 17 | 10/10 passed |
| Native mechanism matrix | 28 modes x 11 samples = 308/308 expected outcomes |
| Python static checks | Ruff, format check, py_compile and diff check passed |

`mechanism-benchmark.json` reports whole-process latency. Abort rows include
signal delivery and the host core handler, so the values are not per-check
instruction cost.

## Claim boundary

This evidence proves the listed mechanisms and regression gates. It does not
claim global Super Object support, full DFSan propagation, general libc/custom
effects, concurrent shadow linearizability, coverage improvement,
vulnerability yield, per-instruction overhead, or cross-system speedup. A copy
from an unknown native source into an explicit object is currently treated as
initialized. This is not a full reproduction of UCSan paper experiments.

`SHA256SUMS.txt` covers every local evidence file except itself. The top-level
Codex manifest independently covers this README, the local manifest, report,
diagram pair and delivery verifier.
