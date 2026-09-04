# Current Document Review Verification

Run sequentially on 2026-07-30 UTC from `/home/ubuntu/code/symcc`.
The suites were deliberately not run concurrently so machine-level oversubscription
could not create resource-sensitive false failures.

| Gate | Result | Suite time |
|---|---:|---:|
| `env PATH=/usr/lib/llvm-18/bin:$PATH /home/ubuntu/venv/symcc/bin/lit -sv -j16 build/test` | **209/209 passed** | 134.25 s |
| `env PATH=/usr/lib/llvm-17/bin:$PATH /home/ubuntu/venv/symcc/bin/lit -sv -j16 build-llvm17/test` | **208 passed, 1 unsupported** | 133.42 s |
| `python3 -m unittest discover -s test -p 'test_*.py' -v` | **475/475 passed** | 82.962 s |

## Interpretation

- `verification.md` is the earlier campaign-time record. Its `207/207` lit count is
  historically correct for that point in the working tree, before two later tests
  were added; it is not the current gate.
- LLVM 17 reports one explicitly unsupported test rather than a failure.
- These gates establish regression compatibility for the tested contracts. They do
  not measure performance gains, prove semantic equivalence for arbitrary LLVM IR,
  or replace long-running equal-budget benchmark campaigns.
