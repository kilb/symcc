# F438 Generation-Fenced Malleable Worker Evidence

This directory seals the F438 controller, production QueryStore adapter,
physical MPI membership oracle, complete regression gates, review record,
research scope, source identity, and canonical figure.

## Inventory

| Artifact | Purpose |
| --- | --- |
| `seed-62520-mpi.json`, `seed-128057-mpi.json` | Sealed five-rank logical trace and physical operation-ownership attestations |
| `full_python_gate.json` | Capability-closed Python gate and exact node-ID identity |
| `focused_tests.txt` | F438 controller/store/service/evaluation regressions |
| `llvm17_lit.txt`, `llvm18_lit.txt` | Serial complete cross-LLVM gate summaries |
| `full_suite_summary.json` | Machine-readable aggregate and claim boundary |
| `review_findings.txt` | Five review rounds and repaired findings |
| `research_sources.md` | Primary sources and implementation boundaries |
| `static_checks.txt` | Syntax, lint, generated-index, figure and diff checks |
| `source_manifest.txt` | SHA-256 contract for authoritative F438 paths |
| `delivery_verifier.txt` | Repository document/evidence integrity gate |
| `f438_malleable_worker_pool.{svg,png}` | Canonical reviewed mechanism diagram |

`SHA256SUMS.txt` covers every regular file in this directory except itself.

## Sealed result

- Focused F438: 17 passed.
- Complete Python: 1,307 passed plus 291 passed subtests; exact 1307/1307
  node-ID equality; no failure, skip, xfail, xpass, deselection, collection
  error, missing node, or unexpected node.
- LLVM 17: 328 discovered, 326 passed, two expected unsupported; LLVM 18:
  328 discovered, 327 passed, one expected unsupported. Both serial gates have
  zero failed or unresolved tests.
- Two physical five-rank runs: 16 epochs, 38 attach, 38 retire, 38 durable
  proofs, 31 expected stale-fence rejections, and 145 worker-operation
  ownership attestations.

## Reproduction

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  -p no:cacheprovider -W error test/test_qfbv_malleable_workers.py

mpiexec -n 5 python3 benchmark/run_qfbv_malleable_multirank.py \
  --epochs 8 --jobs 3 --backlog-per-slot 2 --seed 62520 \
  --output /tmp/f438-mpi-62520.json

ninja -C build-llvm17 check
ninja -C build check
```

The LLVM commands are intentionally serial. A simultaneous run exposed shared
runtime-resource interference; every affected test passed in isolated `-j1`
reproduction, and both serial complete gates passed.

## Claim boundary

The evidence proves deterministic allocation, two-phase logical reassignment,
exact lease/proof conservation, assignment fencing, durable recovery, and
same-run physical MPI rank operation ownership over prelaunched slots. It does
not establish dynamic MPI spawn, failed-communicator repair, solver speedup,
fuzzing coverage, defect yield, network scalability, or multi-node behavior.
