# Parallel Hybrid Execution: 2026 Research and Refactoring Plan

## Scope

The project is a general parallel symbolic-execution framework. Its primary objective is to
increase fuzzing coverage per CPU-hour; vulnerability discovery is not part of the architecture
or evaluation contract. Relevant work is often published in security venues because fuzzing and
symbolic execution are common experimental workloads there.

Strict status note: names ending in `-style` below describe an adapted design
idea, not a paper-faithful reproduction. The code-level status, remaining
mechanisms, and dependency-ordered implementation plan are maintained in
[`SOTA_Gap_Audit_2026-07-24.md`](SOTA_Gap_Audit_2026-07-24.md). In particular,
the current distributed state task can reference a content-addressed solver/
store/page-COW-memory bundle, and a conservative compiler pass can lower a
bounded integer-SSA/static-global/input-buffer/fixed-stack/bounded-heap/
finite-alias LLVM/C subset into executable
continuation IR, but the compiler/runtime cannot yet pause and resume an
arbitrary native continuation;
static Data Coverage has compiler/runtime
and coordinator support but not repeated public-benchmark evidence; the reusable
solution generator implements byte/range/field sampling, a Query-IR invertible
subset, verifier-gated optimistic slicing, and native Z3 `simplify/solve-eqs`
model conversion, but does not persist context-local converter closures; string support
now maintains an exact bounded SMT String/BV dual view, guarded runtime operations,
validated Z3/cvc5 adapters, and a contextual backend selector, but not a complete
smt-switch incremental operation model; and current general SMTgazer-style scheduling selects Z3
runtime profiles while the asynchronous query service now records multi-helper
portfolio attempts/disagreements and can independently lower Query IR QF_BV to
capability-gated cvc5-compatible SMT-LIB with twice-validated models and
prefix-keyed persistent push/pop contexts; current PSCache-style reuse is a
QueryStore-level partial byte-assignment cache plus proof-carrying
assumption-conflict extraction and Query IR validation. It reconstructs input
bytes and minimizes verified Z3 assumption cores, but does not hook every
internal bit-blast CDCL conflict.

## Evidence reviewed

| Work | Relevant result | Architectural implication |
| --- | --- | --- |
| [SimiFuzz (ISSTA 2026)](https://conf.researchr.org/details/issta-2026/issta-2026-research-papers/16/SimiFuzz-Seed-Worker-Scheduling-for-Parallel-Fuzzing-via-Contextual-Bandits) | Joint seed-worker contexts and time-sliced LinUCB feedback account for divergent worker exploration state and group redundancy. | Learn assignments rather than globally ranking seeds and handing the same order to every worker. |
| [S2F (2026 preprint)](https://arxiv.org/abs/2601.10068) | A lightweight execution tree prevents tailored concolic executors from sleeping after local opposite-branch pruning; sampling is useful on hard, high-reward branches rather than everywhere. Reported mean gains are 6.14% edge coverage and 32.6% crashes over the studied SOTA baseline. | Preserve path context above individual executions, maintain high/low priority work, and reserve expensive multi-solution generation for difficult productive branches. |
| [GenSlv, ICSE 2026](https://conf.researchr.org/details/icse-2026/icse-2026-research-track/44/Generator-Solving-for-Symbolic-Execution) | Generator solving reuses invertible Z3 model converters, builds hierarchical range samplers, and uses optimistic simplification so one symbolic query can yield a reusable input generator rather than a single model. | Persist a versioned solution-generator IR; compute target-slice ranges in a weakened solver; run Z3's native `simplify/solve-eqs` model converter over enumerated subgoal models; verifier-gate every recovered assignment. |
| [Hybrid Approach to Directed Fuzzing (2025)](https://arxiv.org/abs/2507.04855) | Target-related interestingness and coverage drive dynamic seed exchange between fuzzing and symbolic execution. | Treat scheduling as a first-class feedback loop, not FIFO directory synchronization. |
| [MultiGo, ACM TOSEM 2025](https://dl.acm.org/doi/10.1145/3735555) | Multi-path directed hybrid fuzzing needs path difficulty, target-path diversity, and contextual scheduling across fuzzing/symbolic agents and exploration/exploitation goals. | Track target-path frequency and Poisson difficulty in PrefixDAG, then score replay/actionseed work by target distance, reward, under-exploration, and difficulty. |
| [TACO-Fuzz, OOPSLA 2026](https://2026.splashcon.org/details/oopsla-2026/35/Efficient-Directed-Hybrid-Fuzzing-via-Target-Centric-Seed-Selection-and-Generation) | Target-centric seed selection prioritizes under-explored paths, while extended path conditions improve concolic seed generation quality. | Batch same-seed high-queue branches into S2F actionseeds and boost under-explored near-target prefixes instead of solving only one branch per selected seed. |
| [Data Coverage for Guided Fuzzing, USENIX Security 2024](https://www.usenix.org/conference/usenixsecurity24/presentation/wang-mingzhe) | Constant-data access coverage complements code coverage and improved normalized FuzzBench coverage from 87.65 to 98.31 in the paper's setup. Strategic seed replacement avoids corpus explosion. | Add compiler/runtime data-access features, but couple them to dominance-based corpus replacement instead of blindly saving every feature. |
| [Evaluating and Improving Hybrid Fuzzing / CoFuzz, ICSE 2023](https://doi.org/10.1109/ICSE48619.2023.00045) | Edge-oriented online regression plus sampling-augmented synchronization improved edge coverage by 16.31% over the best evaluated hybrid baseline. | Learn utility online from each program and coordinate scheduling and synchronization together. |
| [Probabilistic Path Prioritization / DigFuzz](https://doi.org/10.1109/TDSC.2020.3042259) | Monte Carlo path probability identifies paths that mutation is unlikely to cover. | Include branch/path difficulty in priority rather than equating rarity with utility. |
| [MEUZZ, RAID 2020](https://www.usenix.org/conference/raid2020/presentation/chen) | Learned seed scheduling reported 27.1% more coverage than QSYM. | Use cheap contextual features and labels obtained from actual downstream yield. |
| [Pangolin, IEEE S&P 2020](https://doi.org/10.1109/SP40000.2020.00063) | Reusing polyhedral path abstractions supports incremental solving and multi-solution sampling. | Persist reusable constraint summaries instead of restarting all reasoning for every cross-seed. |
| [Backsolver, ACM TOSEM 2025](https://doi.org/10.1145/3712194) | Preceding concrete path choices can make targets involving implicit flows spuriously unsatisfiable; adapting those choices exposes additional paths. | Preserve control-dependent values as ITE expressions and retry ITE targets outside an incompatible concrete prefix. |
| [Cottontail, IEEE S&P 2026](https://mboehme.github.io/paper/SP26-cottontail.pdf) | Partially context/path-sensitive expressive coverage and duplicate-constraint normalization expose structured input progress to an LLM-guided concolic loop. | ECT/context telemetry and F399 Query-IR alpha-normalized constraint selection are implemented; the remaining gap is the paper-compatible LLM seed-acquisition/iterative solve-complete loop and public equal-budget evaluation. |
| [Lari--Young Inside-Outside SCFG estimation](https://www.cs.jhu.edu/~jason/600.665/lari-young.pdf) | Production probabilities and inside/outside dynamic programming assign mass to alternative derivations, but full re-estimation over hidden trees is cubic in the classical formulation. | Learn only from independently accepted concrete trees, then use bounded packed-DAG inside/outside mass as scheduling evidence rather than implementing unverified EM. |
| [ExplainFuzz (2026 preprint)](https://arxiv.org/abs/2604.06559) | Grammar-aware probabilistic circuits capture conditioned, context-sensitive dependencies that ordinary PCFG families miss. | Treat the PCFG, pairwise relations, parent/grandparent/sibling/history hierarchy, exact order-1/order-2 factor messages, prequential fallback, fixed-recency gate, adaptive window, anytime audit, and sealed five-level cost ablation as an interpretable bounded circuit baseline; require residual evidence plus arbitrary-distance and cross-parent factors before considering a full probabilistic-circuit proposal plane. |
| [Online control of the FWER](https://arxiv.org/abs/1910.04900) | Online alpha-spending controls an a priori unbounded hypothesis stream under arbitrary dependence; adaptive discarding can improve power but needs stronger null/dependence assumptions. | Allocate a non-recycled outer alpha sequence across grammar contexts, retain the inner anytime construction, and reserve ADDIS-style recycling for campaigns that establish its assumptions. |
| [Gordian (2026 preprint)](https://arxiv.org/abs/2603.19239) | Selective inverse, surrogate, and heap-partition ghost code can propose candidates for solver-hostile semantics when a verifier loop rejects incorrect completions. | Keep semantic generators outside the trusted path; accept bounded data proposals only after exact target replay and global coverage triage. |
| [SMTgazer, ASE 2025](https://conf.researchr.org/details/ase-2025/ase-2025-papers/47/SMTgazer-Learning-to-Schedule-SMT-Algorithms-via-Bayesian-Optimization) | Bayesian optimization can learn per-instance schedules over complementary SMT algorithms. | Extend the executor portfolio from fixed runtime profiles to tactic/algorithm-level censored-cost observations and uncertainty-aware selection. |
| [PSCache, FSE 2024](https://doi.org/10.1145/3660817) | Partial solutions derived from solver conflicts can be reused across related path constraints when assignments are reconstructed and checked against prefix/off-path formulas. | Retain SAT models and Z3-assumption-UNSAT-core-verified input assignments, normalize signed prefix/off-path literals, and require Query IR validation before candidate emission; evaluate direct bit-blast trail sampling separately. |
| [GenSym, ICSE 2023](https://conf.researchr.org/details/icse-2023/icse-2023-artifact-evaluation/20/Compiling-Parallel-Symbolic-Execution-with-Continuations) | Compiled continuations expose symbolic branching as parallel work while retaining state locality. | A paper-faithful implementation requires live continuation frames, symbolic stores and persistent memory; seed/prefix/target ownership only supplies reusable control-plane mechanics. |
| [Source-DPOR / Optimal-DPOR, POPL 2014](https://user.it.uu.se/~parosha/publications/papers/popl2014.pdf) | Source sets, sleep sets and wakeup trees explore Mazurkiewicz classes without redundant executions; the wakeup-tree invariant is essential to optimality. | The implemented weak-initial tree and cooperative-ready evidence check bounded invariants; reserve full Optimal-DPOR claims for complete operational enabledness, maximal executions, and an unbounded proof. |
| [ConDPOR, CONCUR 2025](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.CONCUR.2025.26) | Concolic input and schedule nondeterminism are combined through execution graphs with read-from, coherence and program-order relations under parameterized memory models. | The bounded po/rf/co graph performs checked backward revisits and deterministic observed-suffix extensions; full claims still require path-dependent event generation, an interpreter-level consistency oracle, and the paper's proofs. |
| [Stateless Model Checking under TSO/PSO, TACAS 2015](https://doi.org/10.1007/978-3-662-46681-0_28) | Chronological traces provide an optimal equivalence for bounded weak-memory exploration under operational TSO/PSO semantics. | Treat the current TSO store-forwarding/FIFO formula as a litmus-tested bounded subset; a paper-level weak-memory explorer needs an operational propagation/equivalence model. |
| [NeuroSCA (2026 preprint)](https://arxiv.org/abs/2603.01272) | Learned arithmetic constraint solving can complement exact SMT on selected nonlinear constraint classes. | A neural solver must remain a proposal backend with exact expression/concrete validation, calibrated fallback, and a separate evaluation budget. |
| [UCSan, OSDI 2026](https://www.usenix.org/conference/osdi26/presentation/yin) | Compilation-based under-constrained execution can synthesize harnesses from LLVM IR and structured object seeds while preserving pointer validity with pseudo/shadow pointer metadata. | Add an opt-in function-entry harness, pseudo-pointer shadow propagation, JIT object provisioning, and structured seed import/export instead of requiring users to hand-write every unit harness. |
| [SymSan, USENIX Security 2022](https://www.usenix.org/conference/usenixsecurity22/presentation/chen-ju) | Decoupled taint propagation and expression construction substantially reduce concolic time and memory overhead. | Keep the engine interface independent and compare SymCC expression construction with the existing SymSan path using the same coordinator. |
| [AFL++ current feature documentation](https://github.com/AFLplusplus/AFLplusplus/blob/stable/docs/features.md) | CmpLog, context coverage, non-colliding coverage, persistent mode, shared-memory inputs, selective instrumentation, and adaptive mutation are production-grade. | Reuse AFL++ mechanisms and bitmap semantics rather than duplicating mutation and coverage machinery in Python. |

## Refactoring principles

1. Optimize coverage features per CPU-second, not raw generated test count.
2. Keep engine telemetry structured and versioned; the coordinator must not parse stderr.
3. Learn policies online per target. Fixed weights remain cold-start priors only.
4. Separate seed context, engine strategy, coverage triage, and distributed transport.
5. Preserve exact fallback behavior behind environment switches for controlled ablation.
6. Do not claim a SOTA gain without repeated, equal-CPU benchmark trials and confidence intervals.

## Current architecture and data flow

1. The root compiler layer (`compiler/`, `build/symcc`) inserts symbolic hooks into LLVM IR. It is
   backend-agnostic and owns compilation, not search policy.
2. The `runtime` submodule implements the symbolic ABI and selects a backend. The nested QSYM
   backend owns path constraints, coverage-guided branch pruning, dependency tracking, solver
   invocation, and generated inputs.
3. `util/mpi_fuzzing_helper.py` is the hybrid control plane. Rank 0 scans and ranks cross-seeds,
   allocates strategy/target work, merges AFL bitmap features, and performs corpus triage. Worker
   ranks execute isolated runtime instances and return generated inputs plus structured telemetry.
4. AFL++ remains the high-throughput mutation and coverage engine. Inputs flow in both directions;
   accepted SymCC outputs are synchronized back to the AFL queue, while AFL queue entries become
   symbolic work.
5. `benchmark/` owns equal-budget experiments and coverage measurement. It must remain separate
   from policy training so benchmark observations cannot leak into the online scheduler.

## Implemented architecture

The QSYM runtime emits `SYMCC_TELEMETRY_OUT` as atomic JSON with:

- ordered path fingerprint and unique branch sites;
- prefix-sensitive branch trace for observed and opposite branches;
- symbolic, interesting, and globally skipped branch counts;
- SAT, UNSAT, unknown, timeout, fast-solve, and generated counts;
- solver/wall time and input dependency width;
- Pangolin-style poly cache hits, entries, and generated samples;
- asynchronous Query IR result generators, including range/field metadata,
  verified model counts, content hashes, and candidate manifests;
- solver-portfolio attempt telemetry, winners, and SAT/UNSAT disagreement
  markers for asynchronous Query IR solving;
- optional string-constraint artifacts for memcmp/strcmp/strncmp, including
  concrete tokens, NUL flags, direct input-offset patches, and AFL mutator
  consumption;
- optional immediate and static-data coverage features, including stable
  object/offset/width keys and access-kind telemetry;
- an engine-neutral expressive coverage tree that joins runtime branch
  prefixes with compiler source/function/switch metadata, comparison
  dependencies, solver outcomes, and concrete coverage quality.

The MPI hybrid coordinator now provides:

- exact incremental AFL bitmap-feature reward, including hit-count buckets;
- a dependency-free LinUCB contextual seed model;
- a SimiFuzz-style seed-worker LinUCB layered after structural ownership. Its
  context combines seed features, worker-local exploration/yield/cost state,
  structural affinity, site/path novelty, cross-learning opportunity, and
  in-flight assignment diversity. Updates are aggregated in fixed time slices
  from exact global bitmap gains and worker-local acquisition of globally known
  branch sites;
- a cost-aware UCB portfolio over baseline, fast solve, multi-solve levels, and a combined profile;
- a SYMCTS-inspired optimistic-first solver profile, where the current flipped constraint is checked
  before the expensive strict path-constrained query;
- a bounded prefix execution DAG that merges shared actual prefixes across seeds, propagates subtree
  reward, tracks open opposite branches, and schedules target replay with a UCT-style frontier score
  over exploration, ColorGo static/dynamic feasibility, data progress, mutation rarity, and solver
  cost;
- terminal SAT/UNSAT/stale summaries and timeout backoff for targeted prefixes;
- selective multi-solution strategy selection for difficult, high-reward branches with low estimated
  mutation probability;
- S2F-style exact/tailored executor scheduling: explicit open-prefix targets get an exact first
  attempt, then fast/multi-solve tailored profiles and a bounded polyhedral sampling profile compete
  on hard follow-up work;
- SYMCTS-style edge-dependence coverage: the concolic side keeps its own bounded branch-pair
  min/max matrix and replays inputs from under-explored rows instead of relying only on AFL's
  coarse edge bitmap;
- optional directed hybrid mode through `SYMCC_DIRECTED_SITES` and `SYMCC_DIRECTED_DISTANCE`, with
  compile-time ColorGo-style coloration (`SYMCC_COLOR_TARGETS`/`SYMCC_COLORATION_OUT`) to emit
  matching mergeable site-distance maps from LLVM CFG, direct-call structure, and bounded
  indirect-call summaries;
- dominance tracking for data-coverage features, so stronger matched-prefix seeds replace weaker
  winners per data site;
- extended libc constant-data telemetry for memcmp/bcmp and bounded strcmp/strncmp comparisons where
  one side is symbolic and the other side is concrete data;
- native AFL data-coverage injection through `AFL_PRELOAD`, making matched-prefix progress visible
  to AFL's own shared bitmap and queue retention;
- concrete string-token export from symbolic-vs-concrete libc comparisons for AFL extras/mutators;
- an AFL++ Python custom mutator that feeds SymCC hint tokens and poly-cache model/box entries back
  into AFL's mutation loop;
- xFUZZ/KRAKEN-style AFL++ profile orchestration across available LAF/CompCov, CTX, Ngram, CmpLog,
  MOpt, and power-schedule variants;
- experimental agentic-concolic JSONL hooks plus a built-in deterministic stateful planner that
  exports/consumes focus, target, and strategy hints without making model calls inside the fuzzer;
- DynamiQ-style structural tasks: the compiler emits a mergeable interprocedural task graph,
  recursive call-graph SCCs become deterministic regions, and the adaptive coordinator assigns
  region ownership using online coverage, reward, cost, and stagnation feedback, with bounded work
  stealing under queue pressure;
- Empc-style multiple minimum path covers: compiler summaries preserve true/false CFG successors,
  loop SCCs are collapsed before maximum matching, bounded alternative maximum matchings provide
  diverse covers, actual traces narrow the covers active for a seed, and infeasible targets trigger
  predecessor/dependence fallback rather than repeatedly selecting the same edge;
- content-addressed input transport, sharded bitmap delta journals, and persistent analyzed-input
  digest shards for MPI workers;
- bounded-DPOR pthread schedule exploration: MPI workers preload a native synchronization tracer,
  return logical-thread-id and memory traces, and the master persists conflict-derived replay
  prefixes plus bounded SC schedule-SMT v7 artifacts. Replay-visible events retain exact bounded
  permutation positions, while controlled lifecycle ranks use one scaled anchor each and leave
  sufficient integer space for non-controlled events. A structured order IR, constructive
  linear-extension checker, query binding, violation-driven section refinement, direct libz3 model
  extraction, and CLIs project verified models to runtime logical-thread prefixes; legacy eager
  and pairwise permutation modes remain available for differential validation and ablation. The
  operational subset recovers complete
  mutex/rwlock sections, splits condition waits at mutex release/reacquire, offers optional
  one-to-one signal or reusable broadcast wake witnesses without excluding spurious wakeups,
  and propagates stable logical-thread create/start/exit/join ordering, detach/cancel outcomes,
  cancelled joins, and explicit identity retirement;
- multi-master fenced leases: shared work-id records use short update locks plus fencing tokens,
  heartbeat active dispatches, and allow other coordinators to steal expired continuation work
  without accepting stale completions;
- authoritative multi-master coverage shards: bitmap hit-count bits are
  atomically OR-claimed under short per-shard locks, with epoch gossip and
  heartbeat-based preferred-owner failover;
- a verified semantic proposal channel: inverse/surrogate/heap-partition
  planners may submit bounded data transformations, but the real target must
  confirm the requested prefix branch before ordinary global AFL coverage
  triage can retain the candidate;
- semantic constraint fallback routing: telemetry is abstracted into byte-local, token-progress,
  wide/nonlinear, timeout-prone, and opaque branch classes, then converted into exact,
  sampling, or skip actionseed hints for the executor portfolio;
- separate QSYM internal pruning bitmaps and AFL showmap bitmaps, avoiding hash-space corruption;
- a versioned, atomically persisted scheduler state file containing policy, DAG, summary-cache, and
  data-coverage state;
- compatibility switch `SYMCC_ADAPTIVE_SCHEDULER=0` and replay control
  `SYMCC_REPLAY_COOLDOWN=<seconds>`.
- a versioned ParaSuit-style parameter registry with conditional activation,
  phase/target/task/input-size contextual posteriors, bounded pair interactions,
  assignment-exact reward attribution, and read-only cross-campaign priors;
- an executable `symcc-parameter-provider-v1` path for the coordinator and
  `symcc-query-solver`, with bounded capture, provider-atomic conflict rejection,
  canonical provenance, and lifecycle routing across 28 per-task, 23
  query-service, and 2 coordinator-campaign parameters; only per-task values are
  admitted to seed-level learning, so process-lifetime settings cannot receive
  no-op rewards;
- a program-bound ParaSuit Algorithm 2 adaptation: exact argv and executable
  content bind bounded per-parameter value histories; deterministic MeanShift
  and a silhouette gate choose between contextual exploration and cost-aware
  cluster exploitation, while assignment tokens, finite-value totalization,
  and a hash-bound state pair prevent replay or cross-program contamination;
- a ParaSuit parameter-selection stage over assignment-local stable branch
  outcomes: deterministic MinHash bounds each coverage set, standalone
  declared-value runs establish inverse-frequency baselines, below-baseline
  combinations receive zero member credit, and pure rarity, hierarchical, or
  hybrid selection can be ablated; a hash-bound third sidecar preserves the
  exact base/value/selection state triple without replacing AFL corpus novelty;
- an online token-grammar plane that expands comparison-taint cores to bounded
  lexical spans, learns stable literal/choice/sequence/optional/repetition
  productions, emits variable-length completions, and ranks rules only from
  concrete target validation plus authoritative AFL retention feedback;
- a fail-soft selective-query solver that closes target byte dependencies over
  the full prefix, freezes only disconnected components to the concrete witness,
  accepts SAT models, and treats local UNSAT/unknown strictly as a signal to
  rerun the unrestricted solver;
- opt-in UCSan-style compilation-based under-constrained execution: the SymCC
  pass can synthesize a main harness for a configured function entry, propagate
  pointer shadow provenance through scoped LLVM IR, translate pseudo pointers
  into JIT-provisioned runtime objects at dereference sites, and read/write
  structured root/object seeds with optional seed-byte symbolization. F393 adds
  strict canonical durable JITI context replay; F394 adds explicit stack/heap
  allocation-level OOB and alias UAF; F395 adds byte-level initialization,
  SSA/call propagation, pointer/branch/extent sinks, and realloc/atomic tag
  transfer. Global Super Objects, full DFSan, general libc/custom effects and
  concurrent metadata linearizability remain open.

## Implementation notes

### Seed-worker contextual scheduling

Global seed ranking remains useful for cold start, but it cannot distinguish
workers that have reached different program regions. The pair scheduler keeps
bounded site, path, cost, yield, and structural-region state per MPI worker.
For each idle worker it evaluates a bounded window of structurally eligible
work and reserves the selected pair before the next worker is scored, making
simultaneous duplicate assignments immediately visible.

On completion, globally new AFL bitmap features remain the primary reward.
Branch sites that are already globally known but new to the receiving worker
form a separate cross-learning signal. Pair observations are committed to
LinUCB together at `SYMCC_SIMIFUZZ_SLICE` boundaries to reduce variance from
individual solver outcomes. Worker, seed-profile, and pair-model state is
persisted in scheduler schema 8.

### Bounded execution DAG and selective sampling

The coordinator builds a bounded prefix DAG from runtime branch traces. Each node is keyed by the
prefix-sensitive branch identifier emitted by QSYM, so the same static branch reached through
different path prefixes remains distinguishable. Observed prefixes accumulate discounted reward from
downstream coverage and data-coverage gains. Open opposite branches become explicit replay jobs with
`SYMCC_TARGET_BRANCH`, and the runtime reports whether the exact target was reached.

The selector estimates whether mutation is likely to cover a branch using sibling visit frequency,
combines that signal with subtree reward, difficulty, and UCB exploration, then reserves a bounded
worker lane for the best open-prefix targets. Multi-solution solver profiles are enabled only for
branches that are difficult, historically useful, and unlikely to be reached by mutation.

### Data coverage

The LLVM pass now instruments immediate comparisons, bounded switch neighbors, simple loads from
static storage, load-derived predicates, and region comparisons. Constant globals have deterministic
object ids; the QSYM runtime adds a fallback registry for readable, non-writable ELF segments in
already loaded modules. Runtime addresses are normalized to
`(object_id, byte_offset, effective_width_bits)`, so ASLR does not perturb the novelty key.

Equality progress counts equal bits, ordered predicates count equal leading bits, and byte-region
progress combines the equal prefix with the equal bits in the first differing byte. The coordinator
persists winners in an independent structured novelty set. A seed wins only after a strict matched-bit
improvement; replacement across different path fingerprints is rejected conservatively. This avoids
collisions with the AFL edge map and prevents physical deletion of seeds that still represent other
code/data features.

The AFL preload remains a compatibility channel for comparison-prefix progress and AFL's native
queue retention. It is deliberately not treated as the authoritative static-data novelty set. The
implementation has microbenchmarks for lookup/static graph access and an uninstrumented DSO, but it
still needs equal-CPU repeated evaluation on real lexer/parser targets before claiming a SOTA gain.

`SYMCC_CMP_TAINT` adds a Cottontail-style structure signal to the same telemetry path. For bounded
branch comparisons, QSYM records the dependent input-byte count and min/max dependency offsets beside
the prefix-sensitive branch id. The scheduler converts this into a comparison-locality feature: dense,
localized dependencies are ranked as better candidates for local mutation, smart solving, and agentic
seed completion than wide, diffuse comparisons.

The coordinator now turns those structure signals into Gordian-style compact input linearization.
Rather than forwarding a single broad `min-max` focus range, it writes a bounded `.compact_focus_set`
from recent AFL hint offsets and comparison-taint dependencies, then dispatches ordinary work with
`SYMCC_FOCUS_SET`. QSYM symbolises only that sparse set plus a small margin, while targeted prefix
replay and explicit worker partitions keep their previous full/range symbolization to preserve path
reachability.

Hybrid adaptive mode also builds `util/afl_data_coverage_rt.c` and injects it with `AFL_PRELOAD`.
That runtime intercepts `memcmp`, `strcmp`, `strncmp`, `strcasecmp`, and `strncasecmp`, computes the
matched byte prefix, and increments prefix buckets in AFL's native shared bitmap. It uses exported
`__afl_area_ptr` when available and falls back to attaching the `__AFL_SHM_ID` SysV shared-memory
map, which is the path expected for ordinary AFL++ targets. This is separate from the QSYM telemetry
path: AFL can now retain data-progress inputs through its ordinary map and queue mechanics, while the
symbolic scheduler still gets structured data features for seed ranking.

Adaptive hybrid mode also wires AFL++'s Python custom-mutator API to
`util.afl_symcc_hint_mutator` when the local `afl-fuzz` build advertises Python mutator support. The
mutator watches the SymCC hint directory and `.poly_cache`, then applies cached Z3 model bytes,
full-matrix polyhedral samples, hint-token insertion/overwrite, and AFL splice fallbacks as an
additional mutation stage. This keeps symbolic discoveries inside AFL's native queue/mutation
pipeline instead of depending only on out-of-band queue synchronization.

The benchmark runner can also build and discover AFL++ profile variants. Built-in micro targets can
be compiled as default, LAF/CompCov, CTX, Ngram4, LAF+CTX, and CmpLog binaries; public benchmark
directories using `<suite>-afl-laf`, `<suite>-afl-ctx`, `<suite>-afl-ngram4`,
`<suite>-afl-laf-ctx`, and `<suite>-cmplog` are discovered automatically. Hybrid instances then use
structured profiles rather than only changing AFL power schedules.

### Bounded implicit-flow adaptation

The compiler now imports the runtime's existing `_sym_build_ite` ABI and uses
it to preserve all three symbolic operands of LLVM `select`. It also performs a
conservative control-dependence reconstruction for two-input PHIs in a
bounded acyclic conditional region. Incoming values can be constants,
previously available symbolic values, or bounded side-effect-free SSA
computations cloned from the two branch arms. This is the practical
Veritesting-style easy-region layer: arithmetic, comparisons, casts, and
selects can be merged into an ITE. Nested two-input PHIs are recursively folded
into nested ITEs, preserving inner control choices when the outer value is
merged, while memory operations, calls,
division/remainder, loops, and non-canonical control flow retain the existing
fallback behavior.

QSYM recognizes target expressions containing those ITEs. It first attempts
the ordinary exact query under the recorded prefix. If that query is
UNSAT/unknown, or an equivalent strict UNSAT is found in the core/poly cache,
the Backsolver path enumerates near-current ITE-controller outcomes, directly
inverts the corresponding controller predicates where possible, and validates
each candidate by replaying the target plus retained independent prefix
constraints through the lightweight expression evaluator. This gives the fast
path the important Backsolver property: the earlier input-dependent choice can
change, but accepted outputs are still concrete-model checked before they are
written.

If direct inversion is exhausted, QSYM falls back to an adapted Z3 query that
drops only prefix constraints overlapping ITE-controller dependencies and keeps
independent prefix constraints. Outputs are labeled `-backsolve`; MPI workers
execute them concretely and retain only candidates that contribute AFL bitmap
coverage.

Telemetry exposes `backsolver_targets`, `backsolver_attempts`,
`backsolver_sat`, `backsolver_direct_attempts`, `backsolver_direct_sat`,
`backsolver_validations`, `backsolver_validation_failures`, and
`backsolver_z3_fallbacks`, plus retained/dropped prefix-constraint counts. The
adaptive scheduler folds the observed direct/aggregate yield into its
prefix-node reward, persists it with the DAG, and restores it after restart.
`SYMCC_BACKSOLVER=0` provides an ablation switch.

This is still bounded by compiler-visible ITE creation: LLVM `select`,
two-predecessor PHIs, at most 12 recursive expression levels, and 32 acyclic
basic blocks per arm. Read-only load snapshots are accepted only when
MemorySSA/AliasAnalysis prove that the region has no interfering write or
escaping alias. It does not claim arbitrary IFSS region discovery, general
multi-state merging, ambiguous memory, or expression substitution across loops
and non-canonical control flow. Those cases require a dedicated
control-dependence/state-merging IR.

### Under-constrained execution

`SYMCC_UCSAN_CONFIG` and `SYMCC_UCSAN_ENTRY` enable the compilation-based
under-constrained path. The compiler renames an existing `main`, creates a new
entry harness, materializes entry arguments from structured roots, and passes
pointer shadow tokens through TLS argument/return channels for scoped calls.
Pointer-typed loads, stores, GEPs, casts, PHIs, selects, memory intrinsics, and
atomics are instrumented so pseudo pointers are translated only at actual
memory accesses.

The runtime stores pointer provenance as an object id, logical pseudo base, and
point-by path. `_sym_ucsan_check` provisions backing objects lazily, including
container-of style negative lower-bound growth and upper-bound extension.
Pointer fields carry memory shadow metadata; `memcpy`/`memmove` preserve those
shadows, while ordinary non-pointer stores and arbitrary external stubs clear
or invalidate affected object state. Structured seeds use root entries for
entry arguments and object entries for pointee paths; seed bytes can be marked
symbolic at their original file offsets so QSYM mutates the structured seed
directly.

Configured externals are handled as `passthrough`, `pure`, `arbitrary`, or a
same-type wrapper. This is intentionally an opt-in under-constrained executor
for unit-level symbolic exploration, not a replacement for full-program hybrid
mode.

### Constraint-summary reuse

The coordinator persists bounded outcome summaries keyed by prefix-sensitive target id. SAT, UNSAT,
and stale targets are retired; timeout targets use exponential retry backoff. This implements the
safe coordinator-level reuse needed to avoid repeating completed target work across executions.

The QSYM runtime now also provides an in-process and cross-execution Pangolin-style cache behind
`SYMCC_POLY_CACHE`. The cache key combines the prefix-sensitive open-branch id, the expression hash,
the input size, and the dependent byte offsets. SAT entries store the changed bytes from the Z3
model; UNSAT and timeout entries avoid repeated expensive attempts on the same prefix. For SAT paths,
the runtime computes a bounded byte-interval polyhedral box by querying min/max feasible values for
dependent bytes (`SYMCC_POLY_RANGE_BYTES`). It also extracts normalized linear constraints from the
actual prefix plus the flipped target branch when the QSYM expression tree is linearizable
(`SYMCC_POLY_LINEAR_BYTES`), then tightens selected byte, pairwise sum, pairwise difference, and
path-linear templates with exact min/max checks under the strict Z3 context
(`SYMCC_POLY_TEMPLATE_BYTES`, `SYMCC_POLY_TEMPLATE_PAIRS`). Fresh and cached SAT entries then emit
deterministic samples (`SYMCC_POLY_SAMPLES`) from the complete projected `A x <= b` system. The
default John walk factors the full barrier Hessian, applies bounded Lewis/leverage-score fixed-point
weighting to every facet, and draws correlated directions through the inverse Cholesky factor.
Dense Dikin mode uses the same matrix without John weighting. Rounded candidates are checked against
the stored integer inequalities, with integer hit-and-run and coordinate fallbacks for singular or
boundary polyhedra.

Prefix translation is also cached in-process (`SYMCC_PREFIX_CONTEXT_CACHE`) as reusable Z3 assertion
vectors for repeated branch, address, and solve-all queries over the same dependency forest. In
parallel, `SYMCC_UNSAT_CORE_CACHE` now uses Z3 tracked assertions to cache minimized UNSAT cores and
matches later cores with a structural read-index bijection, so same-shaped contradictory prefixes can
be skipped even when input-byte ids differ.

The dense representation is bounded by `SYMCC_POLY_DENSE_DIM`; excluded variables are concretized
into row bounds instead of silently dropping their coefficients. This supplies full-matrix
polyhedral context reuse over QSYM's extractable linear IR while ordinary Z3 remains the exact
fallback for non-linear constraints.

F176 extends that cache beyond exact prefix keys for SAT artifacts only. Cached and current linear
templates are normalized and ranked as equivalent, cached-subset, current-subset, or compatible;
the relation is a retrieval heuristic rather than a proof. Every foreign model and every derived
John/Dikin/integer-walk sample is evaluated against the complete current prefix and desired target
before emission. Cross-prefix UNSAT reuse is prohibited, probing is bounded, and separate telemetry
records relation recall, exact-validation hits, and failures. Variable projection/renaming and
nonlinear abstraction remained outside the F176 implementation.

F177 adds the missing verifier-gated optimistic layer to reusable solution generators. The
persistent query helper records the assertion boundary before loading a target, builds a proposal
solver from the target assertions alone, and computes byte ranges in that weakened domain. A
versioned certificate binds the original query hash to a complete kept/dropped assertion partition
and the implication direction `original-implies-weakened`. Every generated byte assignment is then
checked in the untouched full-prefix solver; validation counts must reconcile exactly with the
stored verified models. Thus the implementation can aggressively enlarge a generator domain without
treating weakened satisfiability as path feasibility.

F178 closes the in-process converter gap using Z3's supported goal model-conversion API. The helper
applies `simplify & solve-eqs` to a model-enabled proposal goal, enumerates surviving 8-bit input
variables in its single subgoal, and converts every subgoal model back to the original goal before
full-prefix validation. A versioned provenance object records the pipeline, subgoal/model funnel,
query identity, and validation accounting. The internal converter closure remains context-local and
is not serialized; multi-subgoal composition and non-byte enumeration remain explicit limits.

F179 extends the retrieval abstraction across input lengths and missing variables. Cache rows are
normalized against the artifact's own input size, then projected to shared current offsets by
eliminating each missing byte over `[0,255]`. For a row `L <= kept + eliminated <= U`, the retained
row is conservatively relaxed to `L - max(eliminated) <= kept <= U - min(eliminated)`, with
saturating wide arithmetic. This per-row projection is an over-approximation when eliminated
variables couple multiple rows, so it remains retrieval-only: cross-prefix UNSAT is forbidden and
every model/sample is checked against the complete current expression forest. Field renaming and
nonlinear abstraction remain outside the implementation.

F180 adds bounded structure-preserving field renaming for equal-size inputs whose equivalent
linear fields moved to different byte offsets. Coefficient/row-occurrence signatures provide a
stable search order over variable bijections; the transformed full matrix must still satisfy the
existing normalized relation classifier. The same bijection rewrites the cached model and byte
box. Search is capped by variable and complete-bijection budgets, applies only to SAT retrieval,
and every emitted candidate remains gated by the complete current expression forest. This is a
byte-variable permutation, not semantic schema inference: width/endian conversion, packing, and
nonlinear field transforms remain outside the implementation.

F181 makes the persistent Query-IR converter representation executable across processes. Converter
recipes now have a strict versioned schema, a bounded invertible-operation whitelist, byte
assignments, and an explicit full-validation obligation. The offline sampler emits individual and
conflict-free cumulative recipe proposals. QueryStore reloads the content-addressed expression DAG
and accepts a proposal only when its bounded evaluator proves every prefix and target root true.
Manifests distinguish these Query-IR-verified proposals from solver-verified models, and concrete
replay remains mandatory. This serializes an auditable inverse recipe, not Z3's context-local model
converter closure.

F182 separates an exact integer projection relation from its sampling representation. The interval
elimination matrix remains a conservative proposal envelope, while bounded Presburger formulas
existentially quantify non-shared byte variables on both sides. Z3 checks both implications and
their intersection to classify exact equivalence, subset, compatibility, or disjointness. Variable,
row, timeout, and per-branch probe budgets bound the cost; `unknown` falls back to the interval
relation. A parity regression (`exists y. x = 2y` versus `x = 1`) demonstrates why this separation
is necessary. Exact relation ranking still does not replace full current-expression validation.

F183 extends structure-preserving field renaming across total input sizes. QSYM's linear IR already
represents multi-byte concatenation through byte coefficients, so a same-width endian change is a
checkable coefficient-preserving byte permutation rather than an ABI guess. The accepted bijection
rewrites the matrix, model, and box, then undergoes complete current-expression validation. A
two-byte big-endian field is replayed into a relocated four-byte little-endian field. Width
extension, bitfield packing, scaling, and semantic schema inference remain excluded.

F184 composes field alignment with exact integer projection for widening representations. When the
cached system has fewer byte variables, it searches an injective mapping into current variables and
requires the unmatched current bytes to pass F182's existential relation proof. An interval-only
match is rejected. The model and box follow the same mapping, while unmatched current bytes retain
their concrete base and undergo full validation. The regression reuses a two-byte big-endian value
in a four-byte little-endian unsigned comparison. Sign extension and bitfield packing remain
conservative fallbacks.

F185 closes the exact-proof narrowing direction. When the cached system has more byte variables,
the search injects a structurally supported cached subfield into the current variables and assigns
unselected cached bytes fresh existential offsets. Each mapping edge must share a normalized
coefficient or a complete occurrence signature; the transformed systems must then pass F182's
exact integer relation check. This prevents unconstrained variable permutations from being treated
as field identity. A relocated low 16-bit subfield of a four-byte artifact is replayed into a
two-byte query, while the parity-disjoint projection regression remains rejected. Signed
extension, packed bitfields, scaling, and nonlinear encodings remain excluded.

The S2F coordinator now stores an action subtree on every prefix node. Exact, tailored, and sampling
executors have independent attempt, success, reward, cost, and failure state. The first target
attempt is exact, then a local action score filters the global cost-aware bandit. Productive,
directed, data-progress, and path-cover seeds enter a high queue, while an explicitly reserved low
queue continues exploring difficult or unproven prefixes. Selected seeds now carry an explicit
actionseed into the QSYM runtime: high-queue branches attached to the same seed become `solve` or
`sample` actions, same-seed low-queue branches can be marked `skip`, and QSYM honors those actions at
the reached open branch rather than relying only on a single `SYMCC_TARGET_BRANCH`.

### Concolic-local coverage

`SYMCC_EDGE_DEPENDENCE` adds a SYMCTS-inspired metric that is intentionally separate from AFL's
queue-retention bitmap. For every concolic trace, the coordinator converts telemetry branch sites and
outcomes into branch-edge ids, updates a bounded branch-pair matrix with min/max visit counts, and
keeps a small corpus per source row when a cell changes. Replay then prioritizes rows with low
`traced + 100 * scheduled`, matching the paper's under-explored branch intuition while keeping the
implementation bounded by `SYMCC_EDGE_DEP_BRANCHES`, `SYMCC_EDGE_DEP_CELLS`, and
`SYMCC_EDGE_DEP_TRACE`.

Directed mode now supports a continuous distance signal through `SYMCC_DIRECTED_DISTANCE` in addition
to the binary `SYMCC_DIRECTED_SITES` set. The file may be JSON or text lines of `site distance`; the
master gives a larger contextual bonus to seeds whose telemetry reaches smaller-distance sites, while
the prefix DAG applies the same distance-derived weight when replaying target-adjacent nodes.

`SYMCC_DYNAMIC_COLORATION` adds the runtime half of ColorGo's incremental coloration. For every
trace, the prefix DAG updates a dynamic feasibility estimate from target hits, target misses,
directed-pruned branches, solver status, and observed path reward. It then performs a bounded
Bellman-style backup over the prefix DAG, combining feasibility, subtree value, transition frequency,
and solver/timeout cost into an MDP value. The coordinator uses this value as a soft scheduling
signal and persists it in the adaptive state; hard branch skipping remains opt-in through
`SYMCC_DIRECTED_PRUNE`.

For concurrency-heavy programs, `SYMCC_CONCURRENCY_OUT` emits a Schfuzz-style guidance map.
The compiler marks pthread/C11 thread lifecycle calls, lock/condition/barrier/semaphore calls, LLVM
atomic operations, and fences as concurrency targets, then computes branch-site distance to those
targets over the same interprocedural CFG. `SYMCC_CONCURRENCY_GUIDANCE` lets the coordinator consume
that map as a soft seed/prefix priority signal and as a ConcoLLMic-style hierarchy: records are
bucketed by distance-to-concurrency frontier, near-frontier seeds are replayed first, and the
prefix-sensitive opposite branches observed at those sites become explicit target replay jobs before
ordinary edge-dependence replay. This improves coverage guidance around interleaving-sensitive code
without claiming deterministic schedule exploration.

The compiler pass now emits those distance maps directly when `SYMCC_COLOR_TARGETS` and
`SYMCC_COLORATION_OUT` are set. Target specs can name functions, source `file:line` locations, or
numeric runtime site ids. The pass uses the same instruction-pointer-derived site id convention as
runtime telemetry, builds a module-level CFG plus direct-call and bounded compatible indirect-call
over-approximation, reverse-BFSes from target basic blocks, and writes rows consumable by
`SYMCC_DIRECTED_DISTANCE`. The sidecar also carries block, call, entry/exit, target, and site
summaries in comment-prefixed rows; `util/merge_directed_distance.py` merges multi-module fragments
into a whole-program map. The benchmark wrapper automates this for built-in targets through
`--directed-targets`.

QSYM can additionally consume the same text distance map at runtime with `SYMCC_DIRECTED_PRUNE=1`.
This leaves coverage bookkeeping intact but suppresses Z3 attempts for branch sites whose static
distance exceeds `SYMCC_DIRECTED_MAX_DISTANCE`. `SYMCC_DIRECTED_PRUNE_UNREACHABLE=1` is an aggressive
whole-program mode that also prunes sites absent from a non-empty map.

`SYMCC_TACO` and `SYMCC_MULTIGO` add a target-path layer above static distance. PrefixDAG records
per-site execution frequency, computes a Poisson-style path difficulty score for observed traces,
tracks target-path visits/reward/distance on both actual and open sibling nodes, and uses an
explore/exploit schedule to alternate between easier target-reaching paths and harder
under-explored directed paths. With `SYMCC_TACO_EXTENDED_CONDITIONS=1`, selected seeds carry a
multi-branch actionseed into QSYM, so high-queue branches for the same seed become an extended path
condition and low-queue branches can be explicitly skipped for that execution.

### Agentic hooks

`SYMCC_AGENTIC_OUT` exports JSONL task summaries containing input path, strategy, executor class,
target branch, open branches, difficulty, and bounded data features. `SYMCC_AGENTIC_HINTS` accepts
external JSON hints keyed by input sha256 or path and may override `focus_bytes`, `focus_set`,
`target_branch`, `strategy`, `s2f_actions`, and a route label. `SYMCC_AGENTIC_CMD` can also query a
bounded external command online by passing the same task JSON on stdin and consuming one hint JSON
object on stdout. `SYMCC_AGENTIC_BUILTIN=1` enables the repository's deterministic planner, which
persists a bandit-style state over strategy reward/cost, target-branch outcomes, and per-input route
memories in `SYMCC_AGENTIC_STATE`.

`SYMCC_AGENTIC_ROUTE` controls the built-in route policy. `cottontail` learns comparison-taint
locality and replays structure-aware focus slices; `concollmic` maps learned target branches to
actionseed solve routes; `gordian` sends solver-hostile no-output/unknown/timeout tasks to sampling
routes that can be paired with external ghost-code hints. `hybrid` enables all three and remains
deterministic unless the user supplies an external command.

The ECT layer (`SYMCC_ECT`) makes this route structurally explicit rather than
feeding it only flat branch rows. It retains separate observed/open outcomes,
bounded function context, source location and opcode metadata, dependency
shape, data quality, solver cost, and candidate reward. Absolute input offsets
are excluded from the ECT summary shape. Repeated parser/loop shapes are counted
without duplicating unbounded path nodes. `SYMCC_ECT_OUT` exports the same
versioned JSON used by scheduler state. This remains a structural control-flow
channel: consistent with Cottontail's official artifact, solver expressions are
not embedded in ECT nodes.

F399 adds the separate solver-expression channel that earlier documentation
conflated with ECT. QueryStore validates the complete Query IR DAG and computes
alpha-normalized constraint shapes: absolute `read.index` values are renamed by
first structural occurrence, while operations, widths, constants, attributes,
ordered children/roots, and read aliases remain semantic. A joint projection
over the latest eight prefix roots plus the target preserves cross-root aliasing.
The default asynchronous traversal `structural` ranks novel contextual classes
before duplicates; rank-r starts at score `1/r`, and a 300-second linear age
term removes only this duplicate penalty. Shape never proves SAT/UNSAT, deletes
no query, and cannot bypass Query IR model validation or concrete replay.

`SYMCC_VERIFIED_PROPOSALS` is a separate acceptance channel for semantic
candidate generation. External inverse, surrogate, heap-partition, or
solve-completion logic can submit bounded JSONL data transformations. The
framework materializes them content-addressably, replays the actual target,
requires exact target-branch telemetry when requested, and only then applies
global AFL coverage triage. This realizes a verifier loop without placing the
proposal generator in the trusted execution or coverage-retention boundary.

### Distributed control plane

MPI work messages now carry content-addressed input objects. The master sends object bytes only the
first time a worker needs a digest; subsequent jobs reuse the local worker cache. The AFL coverage
state is sent as sparse byte-bit deltas from a bounded version journal, with full snapshots used only
when a worker falls behind. QSYM's internal pruning bitmap remains worker-local and is not overwritten
with AFL showmap bytes.

Each master performs local concrete triage, while `SYMCC_COVERAGE_GOSSIP`
provides the final cross-master novelty decision. The coordinator stores local
AFL bitmap delta history in deterministic index shards (`SYMCC_BITMAP_SHARDS`), persists analyzed input digests in
shard logs (`SYMCC_STATE_SHARDS`), and records dispatched-but-unfinished work in an append-only
lease journal (`SYMCC_WORK_LEASES`). The state coordinator treats each
seed/focus/target/actionseed tuple as a schedulable task, hashes it into `SYMCC_STATE_TASK_SHARDS`, prefers
owner-local worker dispatch, allows bounded work stealing, and persists reward/lease summaries in
`.state_tasks.json`. A task may reference `symcc-live-continuation-v1`; F59's
`LiveStateStore` persists content-addressed expression objects, parent-linked
solver frames, symbolic stores and concrete/symbolic page-COW memory roots.
F68--F172's `live-continuation-export` pass now lowers a conservative
integer-SSA and object-memory subset, including two-phase PHI edge copies,
selects, switches, direct internal calls, DataLayout-aware static initializers,
constant GEP, bounded multi-byte integer loads/stores, and an exact
`(pointer,size)` symbolic input-buffer entry ABI. Fixed frame-local allocas are
also lowered when constant offsets and a dominating full-width initializer are
proven. Checkpoint creation maps seed expressions into page-COW input memory,
records the actual length as a CAS root, and persists per-depth stack-byte
initialization markers. Return reclaims markers and SSA locals for the popped
frame. Direct constant-size `malloc` call sites are lowered to fixed-capacity
synthetic slot pools with explicit alloc/free operations. Allocation uses
canonical first-free selection; each slot keeps an independent stable identity,
heap-live marker, and per-byte initialization markers. Free clears both marker
classes so deterministic slot reuse cannot read stale backing bytes. Pool
exhaustion remains an explicit model bound rather than a synthesized null
return. One-term symbolic GEPs can enumerate bounded
in-object aliases; loads build finite ITE values and stores perform guarded
per-byte read-over-write updates while persisting the valid-address
disjunction together with a signed no-wrap index interval. This prevents a
wrapped bit-vector index from re-entering the object as an `inbounds` poison
path. F74 additionally preserves guarded object alternatives for acyclic
pointer selects and PHIs. Cross-object `alias_cases` combine the select/edge
guard, an optional per-arm no-wrap index interval, and numeric address equality
before finite ITE load or guarded byte store. F75 uses the same cases for every
possible malloc slot and recursively applies GEP provenance to each guarded base
alternative, so same-site multi-live instances and GEP-after-union remain
object-sensitive. F76 adds finite summaries for pointer arguments and pointer
returns across an acyclic direct-call graph. Formal and result guards retain
object identity independently of the numeric SSA call/return relation; explicit
pointer signatures are revalidated at artifact load and execution. A callee may
access a caller-owned stack object through the unique active owner frame, while
callee-stack escape and ambiguous recursive ownership are rejected.
F77 keys worker-local QF_BV contexts by the immutable CAS path-condition root.
Candidate assertions probe an exact context with push/pop; a missing child
context can be derived from cached parent assertions plus one frame delta, and
a cold worker reconstructs the full prefix. The MPI worker retains its
executor/helper across leases. Bounded SMT fragment/frame caches and a
circuit-breaker one-shot fallback keep this optimization outside checkpoint
identity and correctness.
F78 adds nullable runtime-sized malloc/calloc pools and bounded in-place
realloc with symbolic live/size/init state. F79 transports caller-domain BV
certificates with pointer arguments and returns. F80 assigns stable IDs to
finite internal function targets, dispatches guarded select/acyclic-PHI
function pointers, and rechecks typed parameter/return widths at artifact load
and runtime. F81 lowers constant-length memcmp/bcmp to ordinary object-aware
byte loads and a first-difference BV/select DAG, preserving symbolic memory
without a host-libc concretization path. F82 lowers constant-length
memcpy/memmove/memset declarations and LLVM intrinsics to the same core byte
loads/stores. Memmove snapshots all source bytes before writing, memcpy
requires finite-candidate disjointness, and memset preserves a symbolic low
byte while ordinary stores update initialization state.
F83 extends the same finite dispatch to pointer-returning targets: target-wise
object summaries and indirect pointer actuals retain the selected callee's
return-domain certificate through the ordinary frame return path.
F84 adds conditional load definedness (`!guard || access_defined`) and lowers
bounded strlen/strcmp/strncmp to guarded core IR. NUL or first-difference
short-circuiting therefore preserves finite object and logical-length bounds
without reading inactive suffix bytes.
F85 lowers bounded memchr/strchr to first-match core IR and reconstructs a
finite pointer-result provenance union for null and every feasible source
offset. F86 adds conditional store definedness and initialization markers.
It snapshots source bytes before writes, lowers strcpy through the first NUL,
and implements constant-length strncpy padding while preserving destination
pointer provenance and rejecting any finite object pair whose distinctness
cannot be proved.
F87 lowers integer division/remainder, symbolic shift ranges, and
nuw/nsw/exact defined-value conditions to ordinary BV assumptions. These
constraints become part of the content-addressed solver prefix, while direct
freeze of undef/poison remains rejected until the IR has a stable
nondeterministic-choice primitive.
F88 stores pointer-width values in ordinary page-COW memory and reconstructs
finite data-object provenance for scalar global cells and exact-slot,
unique-dominating stack/heap pointer stores. F89 preserves stable internal
function IDs across scalar function-pointer cells and reuses typed bounded
indirect dispatch. F90 extends static initialization and provenance recovery to
fixed one-dimensional global data/function pointer tables, including bounded
symbolic element selection. F91 accepts a direct-predecessor-complete memory
merge when every predecessor has exactly one full-width pointer writer and the
cell has no other writers. Loop/multi-level memory merges, non-pointer or
partial overwrites, and escaped cells remain fail-closed.
F92 additionally lowers the normal edge of an LLVM `invoke` only when the call
site, direct internal callee, or every member of a complete finite indirect
target set is `nounwind`. The return PC is explicit and retains PHI edge-copy
semantics; proven-dead unwind landingpads are omitted. Executable exception
objects and unwinding remain unsupported.
F93 gives direct integer `freeze undef/poison` a stable dynamic
nondeterministic BV choice. A persisted instance counter separates loop
executions and survives checkpoint migration, while one SSA result retains one
CAS expression. Poison propagation from flagged operations remains a
defined-value under-approximation rather than a complete poison lattice.
F94 replaces the one-level pointer-cell merge with a bounded acyclic backward
last-writer closure. Dominating defaults, nested conditional overwrites, and
writer-free forwarding blocks are supported when every entry path is
initialized; cyclic memory phis and non-pointer/partial writes remain outside
the subset.
F95 adds host-independent scalar summaries for strict-ABI absolute value,
network byte order, and LLVM byte swap. Signed-minimum undefinedness and target
endianness are represented in the same core BV/solver semantics; arbitrary
external state is still not virtualized.
F96 expands scalar LLVM population/leading/trailing-zero counts into bounded
core-BV DAGs. Zero behavior follows the `is_zero_poison` contract through an
explicit solver assumption, and the same path is exercised by C frontend
popcount builtins. Vector and target-specific overloads remain unsupported.
F97 defers `nuw`/`nsw` poison for the bounded single-use
`freeze(add/sub/mul)` pattern. The freeze selects the wrapped result only on
the proven-defined side and a stable dynamic nondeterministic value otherwise;
F110--F111 extend this to a single-consumer cast/BV/icmp/ssa.copy chain and
division/shift/exact definedness. Deferred division is totalized with a safe
selected divisor for eager concrete evaluation. F112 adds path-sensitive
select-arm definedness, F113 merges value/defined bits through two-phase PHI
edge copies, F114 carries one exact straight-line store/load definedness
sidecar, F115 recognizes equivalent constant-address GEPs through an exact
DataLayout base-plus-offset proof, and F116 carries the sidecar through a
bounded unique-predecessor/successor CFG corridor. F117 adds a validator-checked
defined-bit return ABI for one unique direct callsite, transporting poison to a
caller-side freeze. F118 adds matching typed defined-argument parameters for
the opposite caller-to-callee direction. F119 generalizes both directions to
at most 64 exact direct callsites, requiring every poison return consumer to
reach freeze. Symbolic aliasing, cyclic/multi-access memory, indirect/mixed
transfer, multi-consumer, vector, and FP poison propagation still use the
defined-only approximation.
F120 closes a pointer-memory provenance hole by merging static global
initializers with runtime reaching stores for both data and function pointers.
Finite symbolic GEP pointer values are concretized into address alternatives
under loaded-value guards, with complete dynamic identity retained in
deduplication keys.
F121 composes the argument and return defined-bit channels when a deferred
poison formal parameter reaches a return through a bounded transparent integer
slice. Both directions remain validator-checked at every direct callsite, and
the artifact advertises a distinct transitive-call capability rather than
claiming general interprocedural poison propagation.
F122 replaces the single-use restriction with a bounded all-use proof. A
fan-out value is deferred only when every reachable use terminates at a
supported freeze sink within 256 value visits; mixed consumers, cycles, and
budget exhaustion retain the defined-only under-approximation. Independent
freeze instructions keep independent stable choices.
F123 models a finite linear scalar memory version containing up to 64 exact
loads. A full same-address store clobber or function exit closes the version,
and every load is reverse-checked against its nearest store before the F122
all-use proof is applied. This also removes the earlier unsound proof shortcut
that stopped scanning after the first load.
F124 admits acyclic branch/join regions when a bounded forward traversal and
an all-predecessor reverse proof resolve every load to the identical store.
Different reaching clobbers invalidate the whole proof, so the feature does
not claim path-dependent MemorySSA definedness merging.
F125 adds that path-dependent merge for a stricter direct-predecessor shape:
each incoming block ends its memory effects with an exact full store, and its
defined bit is assigned in a dedicated CFG edge block. The function artifact
enumerates all endpoints; the production validator checks one i1 assignment
per edge and the edge-to-merge jump before execution.
F126 composes those edge assignments with the validator-checked direct-call
argument and return defined-bit ABI. Only call endpoints already accepted by
the bounded all-callsite/backward-slice proofs are treated as interprocedural
memory-PHI sources.
F127 permits each incoming edge to resolve through a bounded memory-free
predecessor subgraph. F128 uses the same validator-checked edge assignment as
a loop-carried definedness state when every entry and backedge supplies an
exact full store. A missing-store iteration invalidates the cyclic proof.
F129 admits a direct scalar global constant initializer as a defined entry
state. A no-store edge writes true into the same validated memory-PHI
destination while numeric memory retains the actual initial value.
F130 extends that entry-state proof to constant-offset aggregate subobjects.
It combines target-DataLayout base/offset normalization with LLVM constant
load folding, accepts only typed integer constants, and requires the artifact
invariant `initial_subobject => initial`. Independently recomputed equivalent
GEPs are still checked by the canonical-address proof; symbolic indices and
non-foldable initializers remain outside the claim.
F131 distinguishes store, initial, and carry incoming memory states. When a
bounded backedge subgraph reaches the same loop-header load without a writer,
its edge assignment self-carries the previous one-bit definedness state.
Artifacts mark the exact endpoint and the validator requires both an i1
self-reference and `carry => cyclic`. A store/carry mixture joined before one
final edge remains unsupported because it requires another path-sensitive
sidecar merge.
F132 admits one canonical conditional update before a backedge. The reverse
proof requires direct exact-store and carry arms of the same conditional
branch, then emits an i1 select between the store condition and prior
definedness. The validator requires exactly one self-referencing select arm;
F133 extends forwarded arms, while nested conditional transfers remain
outside the proof.
F133 permits bounded unique-predecessor forwarding inside both arms. It
enumerates conditional ancestors under the shared reverse-analysis budget,
requires the store chain to contain the exact writer, and combines arms only
at distinct successors of the same branch. F134 extends equivalent-source
multi-way joins; nested transfers remain unsupported.
F134 groups all final predecessors by complete reaching-source identity and
accepts a multi-arm join only when every endpoint in the Store and Carry
groups independently maps to the same outer branch arm. This covers
control-only splitting after one writer without treating distinct writers as
equivalent at that stage.
F135 further coalesces distinct writers only in the definedness domain when
bounded source analysis proves every stored value cannot carry deferred
poison. Each endpoint is checked against its own writer and numeric memory
retains the actual value, so the equivalence cannot merge program data.
F136 coalesces distinct writers of the exact same poison-capable LLVM SSA
value. The reaching source retains the complete writer membership set so
every store independently proves its sink and propagates the shared
condition. Different producer identities are not treated as equivalent.
F137 keeps two distinct writer conditions in a strict depth-two
Store/Store/Carry tree. It proves an inner writer split and outer carry split,
requires the inner condition and both values to dominate the eager final edge,
then emits inner and outer one-bit selects.
F138 replaces further fixed writer fields with a flat proof tree for four to
eight singleton Store/Carry leaves. A recursive common-branch partition
reconstructs depths three through six under the shared CFG budget; every
predicate and stored value must dominate the eager final edge. Lowering emits
postorder one-bit selects, and the production validator independently checks
the root-reachable acyclic tree, exact depth/leaf metadata, full-binary node
count, and unique carry leaf. Unbounded trees, non-singleton source classes,
path-local lazy staging, symbolic aliases, and partial overlap remain
unsupported.
F139 permits one recursive leaf to represent multiple source-equivalent CFG
endpoints. Every endpoint in the group must independently expose each selected
ancestor branch and map it to the same successor; checking only a representative
is insufficient. Post-store control fanout therefore remains distinct during
execution while sharing one definedness leaf. The artifact carries a separate
grouped-recursive capability and the validator enforces its parent recursive
contract. Reusing the same source at different tree positions still needs a
shared-leaf DAG or condition reduction and remains unsupported.
F140 handles the bounded Store-only repeated-position case without claiming a
canonical DAG. If one proven source group maps the same ancestor branch to
different successors, its endpoints are expanded into separate structural
leaves that reference the same writer condition. The expanded tree must still
fit the existing leaf/depth/CFG budgets and retain one Carry position. A
separate artifact capability distinguishes this from same-position grouping;
repeated Carry, Boolean minimization, and unbounded shared-node graphs remain
unsupported.
F141 applies the same positional expansion to a no-write Carry source. The
recursive artifact now records its actual carry-leaf count, and validation
reconstructs the select tree to check every prior-state self-reference rather
than assuming one direct root arm. Multi-carry remains bounded by the same
leaf/depth/CFG limits; Boolean minimization and unbounded shared-node graphs
remain unsupported.
F142 composes independent definedness PHIs for multiple scalar cells in one
loop header. Header and reverse scans may cross another access only after a
target-DataLayout constant-region proof establishes non-overlap within one
object or distinct static globals. Each cell keeps its own edge sidecar, and
artifact validation requires a same-block group of at least two marked
contracts. Symbolic aliasing, partial overlap, region writes, and general
points-to MemorySSA remain unsupported.
F143 extends the different-base proof to lowerable identified static storage:
distinct globals and entry-block static allocas. The additional artifact
hierarchy marks `identified_objects`, requires the ordinary multi-cell
contract, and requires at least two marked sidecars in the same merge block.
This is deliberately narrower than general BasicAA/MemorySSA: dynamic allocas,
heap objects, noalias arguments or returns, escaping cross-function objects,
and symbolic bases still fail closed.
F144 covers distinct fixed `malloc` sites under the lowerer's existing bounded
heap-pool semantics. Calls must be direct external allocations with one
nonzero constant size inside the configured object bound; valid simultaneous
accesses therefore refer to different live objects, and separate sites map to
separate synthetic pools. The artifact uses a distinct `fixed_heap_objects`
hierarchy. Nullable/dynamic allocation, calloc/realloc, repeated instances of
one site, custom allocators, and general lifetime-sensitive points-to analysis
remain unsupported.
F145 adds a finite product proof for pointer selects and acyclic pointer PHIs.
It recursively collects constant-offset regions rooted in the already
supported static/fixed-heap objects, caps each domain at 16 alternatives, and
requires every cross-domain pair to be disjoint. This admits bounded symbolic
pointer identity without relying on one concrete witness. Guard-correlated
overlap, cyclic PHIs, symbolic offsets, partial-byte composition, and general
alias-aware MemorySSA remain unsupported.
F146 adds a relational refinement for select guards. Region alternatives retain
literal condition identity and polarity; a spatial overlap is ignored only
when the two guard conjunctions require opposite values of the same LLVM i1
SSA condition. A compatible overlap still rejects the entire proof. PHI-edge
correlation, equivalent-but-distinct conditions, general path constraints, and
SMT-backed guard implication remain unsupported.
F147 adds direct PHI-edge correlation: alternatives from different incoming
predecessors of the same merge block are mutually exclusive, while same-edge
alternatives remain compatible. This relates different pointer PHIs without
claiming cross-block edge equivalence or a cyclic PHI fixed point.
F148 lifts one-term symbolic GEPs to conservative offset intervals using LLVM
ConstantRange, then requires interval separation for every region pair.
F149 adds bounded byte-lane definedness composition for 2--8-byte integer
loads. A reverse acyclic single-predecessor walk assigns every address-order
lane to its nearest scalar writer, optionally completing untouched lanes from
a typed constant global initializer. Unique poison-capable writer sidecars are
ANDed after the load, so a later overlapping narrow store replaces only the
lanes it writes. The validator reconstructs exact lane coverage, store width,
sidecar definitions, composition dataflow, and capability consistency.
At the F149 layer, per-lane control-flow PHIs, symbolic overlap, and
region-copy definedness remain unsupported.
F150 covers direct-predecessor per-lane PHIs. Every incoming path receives an
independently reconstructed complete lane vector, and its edge block reduces
only local writer sidecars into the shared defined destination. Typed initial
lanes are retained per endpoint. The artifact validator checks lane coverage,
store membership, edge-local Boolean dataflow, and the jump to the declared
merge. Uncovered lanes that require crossing another join, cyclic lane carry,
and recursive conditional lane trees still fail closed.
F151 adds a bounded cyclic per-lane fixed point for a single loop header.
Entry and backedge blocks transfer every lane independently from a writer
sidecar, constant true, or a same-lane carry; the header aggregate is accepted
only when dependency reconstruction proves that its AND consumes the complete
lane vector. A function-wide alias-closure proof excludes every potentially
aliasing access or unknown memory effect outside the contracted writers and
load before cyclic sink propagation is enabled. This supports partial
loop-carried overwrites without claiming general alias-aware MemorySSA.
Conditional partial stores, irreducible or multi-header cycles, symbolic
overlap, region-copy sources, and interprocedural memory effects remain outside
the proof.
F152 admits one strict conditional partial-write diamond. Store and carry arms
update the lane vector on separate synthetic edges before their join, so the
skip path never evaluates an undefined writer sidecar. The validator
reconstructs branch polarity, arm/edge/join topology, the all-carry path back to
the header, and a complete seed endpoint. Forwarded arms, switches, multiple
writers or updates, nested condition trees, and general per-lane MemorySSA
remain unsupported.
F153 allows up to 16 unconditional unique-predecessor forwarding blocks per
arm. Both corridors must originate at different successors of the same nearest
conditional branch, remain disjoint, and terminate at one join. The validator
rebuilds each jump plus exact header/corridor/join predecessor sets from the
complete continuation graph.
F154 admits a strict two-level, three-leaf conditional lane update. Two leaves
each own one distinct partial writer and the third is an all-carry leaf; every
leaf updates the full lane vector on its own synthetic edge. The production
validator reconstructs both branches, exact arm/edge/join predecessor sets,
writer sidecars, and the two-writer/one-carry partition. Forwarded multiarm
arms are covered by F155 when each is a disjoint, unique-predecessor,
single-successor corridor of at most 16 blocks; writer discovery and validation
span the complete corridor while state updates remain endpoint-local.
Forwarded root-to-inner decision paths, switches, deeper or wider trees,
multiple sequential updates, and recursive lane-source trees remain outside
the F155 proof. F156 separately accepts one strict direct binary tree with
4--8 leaves, depth at most 6, exact unique-parent partitioning, at least two
distinct partial-writer leaves, and at least one carry leaf. The artifact and
validator preserve explicit branch hierarchy rather than trusting a flat
source list. F157 admits bounded linear corridors on every recursive
branch-to-child edge, including decision edges, and reconstructs their exact
jumps and predecessor sets before accepting the same tree invariants.
Grouped/repeated writer equivalence and general cyclic MemorySSA remain outside
the F157 proof. F158 admits one internal partial writer whose strict subtree
provides the exact set of leaves that reference its sidecar; at least one
independent leaf writer and one carry leaf remain required. Multiple groups,
overrides, repeated positions, and general cyclic MemorySSA remain outside the
F158 proof. F159 allows same-range leaf-local overrides inside the group while
at least two other descendant positions retain the internal source; exact lane
sets are compared by the validator. Different-range composition, multiple
groups, and general cyclic MemorySSA remain outside the proof. F160 admits one
partially overlapping leaf-local override and composes the nearer local
source, remaining internal-source lanes, and carry independently. Compiler
lowering and validation both operate on per-lane store IDs; validation derives
the complete group set from at least two pure leaves and requires each composed
leaf's internal set to equal that group set minus its local lanes. Complete
shadowing, mixed exact/partial overrides, multiple internal groups, and general
cyclic MemorySSA remain outside the proof. F161 adds 2--8 distinct all-carry
leaves to the F160 tree, records their exact count, and requires validation to
recompute it from complete carry-only lane vectors plus unique-parent CFG
edges. Shared carry positions, multiple internal groups, and general cyclic
MemorySSA remain outside the proof. F162 admits exactly two direct, disjoint
internal writer groups. Validation separately derives each writer branch's
complete descendant-leaf set, requires equality with its repeated source
references, and rejects nested membership, local overrides, or a third source.
Mixed multi-group overrides and general cyclic MemorySSA remain outside the
proof. F163 permits exactly one of the two groups to contain one partially
overlapping leaf-local override. The validator derives both complete group
lane sets and proves the unique composed leaf retains the internal-set
difference. F164 composes F157 corridor reconstruction with the pure
single-group F158 proof: every declared branch-to-child edge is recovered
through bounded unique-predecessor jumps before internal-writer dominance and
the exact descendant/reference-set equality are checked. F165 applies the
same corridor proof to F159 exact same-range overrides and then independently
reconstructs the pure group lane set and override partition. F166 extends that
composition to F160 per-lane partial overrides, preserving a single carry leaf
and independently proving the nonempty internal-source difference. Forwarded
multi-carry/multigroup trees and general cyclic MemorySSA remain outside the
proof. F167 applies the corridor reconstruction to F162 and independently
proves two disjoint complete group partitions after recovering the actual
tree. Forwarded multi-carry/mixed-multigroup trees and general cyclic
MemorySSA remain outside the proof. F168 admits one composed leaf in exactly
one of those forwarded groups and rechecks the leaf-local source plus nonempty
internal-set difference. Forwarded multi-carry, two-composed-group, and
three-or-more-group trees remain outside the proof. F169 combines the
forwarded composed proof with 2--8 distinct all-carry leaves and requires
validation to recompute the exact count from complete lane vectors.
Two-composed-group and three-or-more-group trees remain outside the proof.
F170 admits exactly three direct pure groups within the eight-leaf budget and
requires all three reconstructed partitions to be pairwise disjoint.
Forwarded three-group and two-composed-group trees remain outside the proof.
F171 admits one composed leaf in each of two direct groups, with independent
per-group counts and source-difference proofs. Forwarded double-composed and
three-group-composed trees remain outside the proof. F172 admits the same
double-composed partition after bounded corridor reconstruction. Three-group
composition and forwarding remain outside the proof. F173 admits three pure
groups after bounded corridor reconstruction and rechecks all three complete
partitions plus every pairwise-disjoint relation. Three-group composition
remains outside the proof. F174 admits one partial-override leaf in exactly
one of three direct groups, generalizes the mixed proof to all three complete
partitions, and proves the unique local source's nonempty internal-lane
difference. F175 admits that composed three-group proof after bounded
corridor reconstruction and rechecks the actual tree before all partitions
and source differences. More than eight leaves, multiple composed leaves in
three groups, and general multi-cell cyclic MemorySSA remain outside the
proof.
F98 replaces cycle rejection in pointer-cell reaching definitions with a
bounded IN/OUT fixed point over last-writer sets and an uninitialized-path bit.
Loop-invariant and loop-carried full pointer stores are supported; exact-slot
identity, finite budgets, and loaded-value provenance guards remain mandatory.
F99 canonicalizes constant pointer-cell GEP chains by target base and modular
offset, allowing independently recomputed nonzero field addresses to share the
same memory version without merging distinct offsets. Symbolic aliases remain
outside this identity proof.
F100 adds target-independent scalar bit reversal and funnel shifts. Funnel
amounts follow LLVM's modulo-width concatenation semantics, including zero and
oversized shifts; vector and target-specific variants remain rejected.
F101--F102 cover LLVM scalar signed/unsigned saturating add, subtract, and
left shift through explicit overflow predicates and min/max clamps.
Out-of-range saturating shifts enter the defined-value poison guard; fixed
point and vector saturation remain outside the subset.
F103 implements scalar LLVM abs and signed/unsigned min/max, including the
INT_MIN poison flag. F104 treats expect/probability as validated optimizer
hints whose runtime result remains the first operand; hints never become path
assumptions.
F105 answers scalar LLVM object-size queries for fixed global, stack, heap,
null, and finite guarded pointer alternatives using exact remaining object
extent. F106 extends dynamic queries to pointer-size input buffers and
malloc/calloc: runtime logical size is reduced by the target-width pointer
offset under an in-object guard, while static queries over runtime-sized
objects keep LLVM's unknown sentinel instead of reporting pool capacity.
F109 retains bounded in-place realloc provenance per finite pointer alternative
through GEP/cast/select/PHI, using each alternative's new request size on
success and null semantics on failure. Moving realloc and
unknown custom/external allocation provenance remain unsupported.
F107 lowers all six scalar add/sub/mul overflow aggregate intrinsics into a
wrapped BV and explicit overflow flag consumed by bounded extractvalue. F108
preserves integer, finite data-pointer, and typed function-pointer provenance
through `llvm.ssa.copy`.
Unsupported dynamic/general heap, dynamic stack memory, multi-term symbolic
pointers, general points-to from memory/parameters, callee-stack escape, other
pointer ABIs, exceptions, recursion, external effects, and poison-sensitive
operations produce a rejection report instead
of a partial executable artifact. This
remains a portable bounded frontend rather than an arbitrary native GenSym
engine: it cannot capture TLS, system calls, or external resources. With
`SYMCC_RESUME=1`, a restarted coordinator restores scheduler state,
state-task shards, and stale work leases before scanning fresh AFL seeds.
Authoritative coverage-owner records are separately sharded by bitmap index.
Candidate hit-count bits are atomically OR-claimed against the persistent shard,
and monotonically increasing epochs gossip committed deltas into every
coordinator's local bitmap and worker journal. Heartbeats choose a preferred
owner per shard, but any live coordinator may proxy the commutative commit.
The remaining deployment gap is a non-shared-filesystem consensus/CAS
transport; the implemented protocol currently relies on shared atomic
mkdir/rename semantics.

## Status after the July 2026 implementation pass

The implementation now covers the main engineering path from the July audit:

1. **P1, memory-aware IFSS/Veritesting.** The compiler pass accepts bounded
   read-only memory snapshots when MemorySSA/AA proves the region has no
   interfering writes or escaping aliases. The verifier still rejects ambiguous
   memory, multi-exit regions, loops, and formula growth beyond the configured
   budget.
2. **P1, UCSan object-graph reuse and explicit-object checkers.** Structured
   under-constrained seeds now have stable serialization, canonical object
   paths, learned alias classes, strict budgeted admission and durable
   fresh-process replay. Explicit stack/heap objects additionally receive exact
   bounds/lifetime metadata and byte-level INIT/UNINIT propagation with
   sink-only UBI checks. The remaining research gap is global Super Object/full
   DFSan propagation, parameterized libc/custom effects, concurrent metadata
   transactions and grammar inference over large object families.
3. **P1, backend-neutral telemetry.** SymCC and SymSan worker observations are
   normalized into a single SolverTelemetry schema with engine, algorithm,
   capabilities, partial-data flags, signed return status, timeout/killed state,
   comparison taints, and generated-case counts. Public-suite conformance
   measurements are still required before claiming backend equivalence.
4. **P2, SMTgazer-style scheduling.** `util/smt_algorithm_scheduler.py` learns
   clustered Bayesian schedules over bounded solver-algorithm sequences
   (`exact`, `fast-exact`, `optimistic-layered`, `polyhedral-exact`, or custom
   JSON spaces), charges timeout-censored PAR-2 cost, persists posterior state,
   and records action propensities for offline evaluation. F237/F238 add a
   proof-carrying `smtlib-qfbv` boundary, real cvc5 model conformance, and
   prefix-keyed persistent contexts. F239 adds deterministic X-means with
   spherical-Gaussian BIC, timeout-censored PAR-2 IPS/ESS estimates,
   empirical-Bayes LCB selection, and a self-checking context-specific online
   prior. F240 adds inverse-propensity-weighted shallow CART bagging and
   residual boosting for completion, censored PAR-2 cost, and adjusted reward,
   then searches executable integer-budget action sequences globally and per
   cluster. F241 adds a strict-budget schedule-level expected-improvement loop
   with replayable acquisition evidence and isolated post-hoc regret.
5. **P2, semantic proposal generation.** `util/semantic_proposals.py` now
   implements built-in inverse, surrogate/exemplar, and UCSan heap-partition
   proposal generation. These candidates remain outside the trusted path and are
   admitted only through target replay plus global AFL bitmap triage. A trained
   NeuroSCA-style neural arithmetic backend remains a separate proposal source,
   not a default dependency.
6. **P2, causal/offline scheduling.** `util/offline_policy.py` records
   interference-aware trajectories, evaluates constant solver-sequence policies
   with conservative SNIPS and doubly robust estimates, gates policy promotion
   by effective sample size and lower-bound improvement, exposes a CLI for
   reproducible benchmark analysis, and stores policy artifacts in
   `benchmark_data.*`.
7. **P1, bounded Source/Optimal-DPOR evidence.** The schedule analyzer builds stable
   dependency/program-order graphs, canonical Mazurkiewicz classes, causal
   source sets, persistent sleep sets, ordered weak-initial wakeup trees, and
   cooperative runtime-ready snapshots. Versioned certificates are independently
   recomputed; they do not claim whole-program wakeup-tree optimality.
8. **P1, joint path/schedule/read-from solving.** Query IR QF_BV, schedule
   QF_LIA and tagged byte/read-from bridges share one libz3 context. QueryStore
   persists and independently rechecks the model, schedule certificate and
   bounded Query IR evaluation.
9. **P1, parameterized memory subsets.** Schedule artifacts contain
   authoritative SC last-write, TSO store-forwarding/FIFO, or RA
   rf/mo/hb/release-acquire constraints. Store-buffering, forwarding and
   message-passing litmus tests define the tested boundary; this is not full
   C11/C++ or a chronological-trace TSO explorer.
10. **P1, portable live-state data plane.** Canonical SHA objects store solver
    stacks, symbolic stores and page-COW memory, with verified checkpoint
    restore and diff. CPS/native resume and actual live-state scheduling remain
    separate work.
11. **Research evidence gates.** A versioned executable bundle records formula
    hashes, bounds, solver capabilities, semantic obligations and excluded
    claims for the bounded mechanisms above. Missing independent solver families
    are reported rather than treated as consensus.
12. **P1, bounded ConDPOR graph.** Stable action/read/write/constraint nodes,
    initial writes, po/rf/co/add-order relations, causal-successor deletion,
    backward revisits, deterministic maximal observed-suffix extensions, and
    cross-trace revisit identities are embedded in schedule artifacts. This
    does not model changed-control-flow event existence.
13. **Path-dependent regeneration.** Compiler-emitted constraint/action events
    are linked to overlapping read intervals; revisits withhold a dependent
    control-flow suffix for re-execution instead of restoring impossible
    observed events.
14. **Operational offers.** Cooperative ready evidence carries concrete
    pthread operation/object offers, and a checked certificate reconstructs
    enabled, blocked, unknown and bounded terminal status.
15. **Extended bounded RA.** Atomic instrumentation runs before LLVM atomic
    lowering and preserves load/store/RMW/cmpxchg/fence memory orders. The
    bounded model includes release sequences, fence synchronization, RMW
    atomicity, seq_cst ranks, race rejection and mixed-size interval checks.
16. **Executable portable continuations and conservative LLVM lowering.** A
    content-addressed CPS-like IR can pause, resume, call, fork symbolic
    branches and move MPI frontier checkpoints. A legacy/new-PM pass lowers
    bounded integer SSA from LLVM IR, bitcode, C, or C++; complete CAS path
    frames are checked as QF_BV before committing branch children. Referenced
    initialized globals, constant GEP, and endian-aware integer memory accesses
    use object-bounded page-COW state. Exact pointer-size input, fixed
    frame-local stack, and bounded call-site multi-instance malloc/free
    lifetimes preserve logical length, initialization, and liveness across
    checkpoints. Bounded one-term symbolic GEPs use finite ITE loads and guarded
    byte stores; guarded provenance survives pointer PHI/select, heap-pool
    alternatives, and subsequent GEP. Acyclic direct calls additionally carry
    finite pointer argument/return object summaries, with exact artifact
    signatures and caller-stack owner-frame markers. CAS-rooted feasibility
    contexts reuse exact prefixes with push/pop, derive child contexts from one
    parent-linked frame, and rebuild portably after eviction or migration.
    Nullable runtime-sized allocation, caller-domain pointer certificates,
    pointer-returning bounded local indirect dispatch, read-only memory-compare summaries, and
    bounded region-write and NUL-aware string summaries are now included.
    General heap graphs, points-to recovered from memory/parameters, broader
    guarded libraries, exception,
    external-resource, and arbitrary native lowering remain separate work.
17. **Sealed empirical protocol.** Paired randomized blocks, equal CPU-second
    affinity, source/input provenance, raw failure retention, coverage AUC,
    paired randomization/effect sizes and Holm correction are executable
    artifacts rather than reporting conventions.
18. **Fail-soft selective concolic policy.** Persistent QF_BV helpers construct
    an assertion-byte dependency hypergraph and keep the target-connected
    component symbolic. A contextual online cost model learns hit probability
    and full/selective EWMA from assertion, closure, AST, and expensive-op
    buckets, then predicts whether a short probe has positive net value and
    adapts its timeout. The concrete side explores a bounded witness/zero/0xff/
    deterministic-random completion portfolio. Every probe still asserts the
    full prefix and target: SAT is exact, while local UNSAT/unknown/exception
    always falls back to the full solver and cannot populate proof caches.
    Versioned policy state is isolated per query-service worker/backend and all
    decisions, predictions, costs, and completion outcomes are recorded for
    ablation.
19. **Query-IR-verified grammar holes.** Solver-materialized candidates carry
    the exact Query IR byte-read set, actual model-assignment set, and a real
    evaluator verdict. Online grammar rules may splice a lexical hole only
    after the manifest digest/read set is rechecked against QueryStore and the
    source candidate satisfies the stored query. Every completion is evaluated
    again; accepted proposals bind query, source, manifest, hole, and rule
    provenance in a versioned certificate. This is only the first gate:
    concrete target replay and authoritative AFL novelty remain mandatory.
    Variable-length completion is therefore usable without treating grammar
    inference or a solver label as a proof.
20. **Plateau-gated retained-history acquisition.** Only candidates retained by
    authoritative global AFL novelty enter a bounded content-addressed history
    bank. During consecutive zero-gain observations, high-yield and
    underexplored retained seeds supply lexical/closed-delimiter tokens for
    current comparison spans; any positive gain resets the gate. Seed attempts,
    target validity, repeated retention, coverage yield, and provenance survive
    restart. The history route is still an untrusted proposal mechanism and
    cannot replace target replay, parser validity, or coverage triage.
21. **Independent parser validity and contextual grammar splitting.** An
    optional campaign parser runs only after concrete target/branch replay and
    before AFL novelty triage. Exit zero is the sole parser acceptance signal;
    nonzero status, launch failure, and timeout reject the proposal without
    weakening either surrounding gate. Grammar candidates bind a stable context
    fingerprint derived from branch, input-size bucket, and bounded left/right
    lexical neighborhoods. Repeated rejection suppresses a rule only in that
    context, while later acceptance reopens it. Parser counters and bounded
    conflict contexts persist in the versioned grammar state. This is an
    auditable approximation, not a claim that lexical fingerprints recover CFG
    nonterminals or parser states.
22. **Exact String/BV dual-view queries.** Versioned string queries now declare
    both an SMT String and one 8-bit bit-vector per covered input offset.
    Character, bounded length, and first-NUL constraints link the views, while
    byte predicates can coexist with contains, indexof, substring, and other
    string predicates. A concrete evaluator checks both views, and explicit BV
    model values override potentially lossy C-string serialization. Legacy
    queries upgrade conservatively. This establishes a single-Z3 exact
    dual-view baseline; it does not yet provide smt-switch multi-backend
    lowering or operation-complete runtime string semantics.
23. **Guarded runtime string-operation artifacts.** Bounded wrappers for
    strlen, strchr, and strstr export contiguous direct-input spans through the
    first NUL, operand roles, constants, and observed length/search indices.
    They become alternative length or indexof predicates in the exact dual-view
    query rather than unverified byte patches. The runtime retains bounded BV
    path semantics: first-zero ITEs for strlen, explicit match/terminator
    conditions for strchr, and a product-limited OR-of-matches for strstr.
    Incomplete provenance, multiple symbolic roles, unsupported NUL search, and
    over-budget summaries fail closed.
24. **Validation-first parallel string portfolios.** Up to eight JSON-protocol
    or SMT-LIB CLI backends can solve a dual-view query concurrently. The
    portable CLI exchange requests only linked 8-bit values, avoiding
    backend-specific String model rendering. Every SAT model is concretely
    revalidated; duplicate valid models, invalid SAT, unknown/error, and
    SAT/UNSAT disagreement have separate telemetry. UNSAT is never authoritative
    in this candidate plane. The adapter and dual-Z3 concurrency are tested;
    the F198 gate later validates cvc5 1.1.2, while Princess and Z3str3 still
    require pinned installations.
25. **Exact-subdomain decimal conversion.** A guarded atoi model connects
    String, Integer, and 32-bit BV views only for one to ten unsigned ASCII
    digits ending at the first NUL and fitting `int`. The string query combines
    a non-empty decimal regex with `str.to_int`; the runtime uses the same
    multiply-by-ten digit fold and range constraints. Whitespace, signs,
    prefixes, suffixes, locale, and overflow fail back to concrete libc
    behavior rather than being approximated.
26. **Width-bound signed strtol semantics.** A second conversion contract adds
    optional minus and target-long bounds only when endptr is null, base is
    concrete decimal, the first NUL closes the input, and an overflow-safe fold
    agrees with libc without ERANGE. Signed regex and prefix/substr integer
    lowering match a target-width BV fold. Non-null endptr and non-decimal
    bases remain concrete because their observable consumption effects are not
    approximated.
27. **Executable cross-solver String conformance.** A versioned five-case gate
    exercises binary bytes, combined String/BV constraints, negative indexof,
    atoi, and signed strtol10, and requires every SAT assignment to pass the
    independent concrete evaluator. Real Z3 and cvc5 1.1.2 runs pass all cases.
    The comparison also removed solver-dialect dependencies by standardizing on
    `bv2nat`, `str.to_int`, `str.to_re`, and SMT-LIB negative numerals. Princess
    and Z3str3 still require pinned-version conformance and equal-CPU evaluation.
28. **Validation-aware contextual String backend selection.** An opt-in bounded
    policy clusters queries by String operation family, capacity, variable
    count, and BV linkage. It ranks solver arms using only independently
    verified SAT yield, normalized elapsed cost, cold-start coverage, and
    periodic exploration. Atomic locked state supports restart and shared
    local-MPI learning. UNSAT remains non-authoritative, and the executable
    conformance gate bypasses selection to test every configured backend.
29. **Unsigned decimal conversion with path-level overflow proof.** A guarded
    strtoul10 contract spans the target 32/64-bit unsigned-long range while
    rejecting signs, non-decimal bases, endptr effects, and ERANGE paths.
    A per-digit quotient/remainder guard now also constrains atoi and strtol
    folds, preventing symbolic alternatives from leaving the concrete
    non-overflow domain and being misrepresented as BV wraparound. The Z3/cvc5
    conformance matrix now verifies six dual-view cases.
30. **Versioned parser-state structural feedback.** An optional parser trace
    records bounded nonterminal/state byte spans and parent links after target
    replay. Exit/verdict agreement, tree containment, and replacement-span
    coverage are checked fail-closed. Grammar conflicts are then keyed by a
    rule-scoped hash of parser implementation and ancestor structural path,
    persisted across restarts, while parser validity remains separate from
    target reachability and coverage retention.
31. **Structural rule-coverage scheduling.** Coverage features are credited
    only after a verified grammar proposal survives global novelty triage, and
    are indexed by rule plus parser structural context. Per-span ordering adds
    a diminishing exploration bonus to unseen combinations. Bounded persistent
    maps and a verified-before-retained invariant prevent ungrounded feedback.
32. **Recursive parser-production induction and independent grammar coverage.**
    A validated structural trace is normalized into a stable production ID
    from parser identity, LHS/state, ordered RHS children, recursion slot, and
    terminal-gap digests. A unique direct self-recursive child may induce one
    bounded `prefix + Nonterminal + suffix` expansion only after parser
    acceptance. A separate logical 64K saturating count bitmap indexes
    rule/structural-path/production exercise, persists sparsely with collision
    telemetry, and contributes a diminishing novelty term independently of AFL
    edge/data retention. This closes bounded direct recursion, not mutual
    recursion or general probabilistic CFG inference.
33. **Bounded multi-slot and mutual CFG derivation.** A canonical parser CFG
    fragment deduplicates ancestry productions while preserving ordered RHS
    symbols, parser states, terminal-gap digests, and every direct recursive
    slot. Repeated nonterminals along the structural path form explicit mutual
    cycles such as `A -> B -> A`. The semantic layer independently recomputes
    all production/cycle IDs before learning, retains one-to-many
    rule-to-cycle provenance, and emits depth 1..N compositions under strict
    depth and input-size budgets. This is an observed-fragment derivation
    engine; it does not claim complete CFG recovery, ambiguity handling, or
    unbounded recursion.
34. **Incremental verified subtree correspondence.** Parser CFG fragments now
    separate a production shape (parser, nonterminal/state, ordered RHS) from
    concrete subtree instances (production, yield, terminal gaps, and ancestry
    path). Only independently parser-accepted instances enter bounded
    shape-equivalence classes. Concrete alternatives are proposed only in
    lexical contexts previously certified with the same shape, carry
    shape/instance provenance, and remain subject to target replay, parser
    validation, and AFL novelty. All identifiers and gap/yield hashes are
    recomputed on feedback and restart; reference-safe eviction and
    deterministic 64 KiB evidence degradation preserve graph invariants. This
    provides observed incremental ECT-style reuse. Item 37 adds bounded packed
    alternatives and verified epsilon instances, but not a complete SPPF,
    cross-parser symbol equivalence, or full expression-tree correspondence.
35. **Nine-objective contextual Pareto scheduling.** Grammar and ECT arms are
    no longer collapsed into a fixed weighted sum by default. A bounded
    extreme-preserving archive feeds exact non-dominated sorting and normalized
    crowding distance over target validity, AFL edge yield, data progress,
    independently verified String-model yield, grammar novelty, ECT novelty,
    accepted-production posterior likelihood, packed-forest
    information/reachability, and scheduling age. Neutral priors distinguish
    missing data/String/PCFG observations from failures. Persisted age supplies
    starvation resistance,
    and a scalar-policy switch provides a direct ablation. The worker exports
    only concretely revalidated String success into this feedback plane. This
    is rule-level contextual selection; item 36 adds metadata-level corpus
    replacement, while distributed physical replacement and public equal-CPU
    hypervolume/coverage evaluation remain open.
36. **Epsilon-Pareto seed metadata replacement.** The adaptive scheduler keeps
    a bounded archive over owned edge gain, data progress, independently
    verified String yield, structural-site diversity, path ownership, and
    reward/cost efficiency. Strict dominance handles direct admission and
    replacement. At capacity, an epsilon grid protects every objective's
    extreme and evicts low-utility metadata from the densest remaining cell.
    Archive membership contributes only a bounded replay priority; AFL queue
    files and authoritative bitmap ownership are never deleted. Persistent
    admission/rejection and dominance/density telemetry support ablation. This
    is metadata-level corpus control, not yet fault-tolerant distributed
    physical corpus minimization.
37. **Bounded packed parser families and verified epsilon derivation.** Parser
    trace v2 represents explicit zero-width reductions and up to eight direct
    child alternatives per node. Alternative zero is the concrete selected
    tree used for context localization; packed branches contribute canonical
    production families without taking over the selected ancestry. Primary
    productions are retained first under a total 32-production, 32-instance,
    64 KiB evidence budget. Production/shape identities bind epsilon semantics,
    while packed ordinals remain non-semantic metadata. The semantic layer
    independently recomputes every production, shape, instance, gap, and yield
    hash. Only an empty-RHS, empty-gap, empty-yield instance accepted by both
    target replay and the independent parser can induce a deletion rule, and
    that rule is generated only in a lexical context certified with the same
    ECT shape. Items 38, 39, and 42 add a bounded shared-node DAG, nullable
    fixed point, and accepted-tree PCFG scheduling respectively; an in-process
    generalized parser and context-sensitive probability model remain open.
38. **Shared packed DAG and deep alternative correspondence.** Parser trace v3
    replaces single-parent nodes with explicit roots and forward packed edges.
    Alternative zero must still induce a single-parent concrete selected tree,
    while non-primary alternatives may share DAG nodes. Extraction reserves the
    selected ancestry, then expands a deterministic 32-node/128-edge closure
    under the existing 32-production and 64 KiB budgets. Canonical node IDs bind
    parser/order/symbol/state/span/epsilon/yield; edge IDs bind parent, child,
    alternative, and slot. Deep instance IDs additionally bind their complete
    root-to-node edge path. The semantic layer recomputes the graph and every
    dependent identity before admitting deep yields into ECT shape classes.
    Packed evidence cannot authorize a mutation by itself: a selected instance
    must separately map the source lexical context to the same shape. Bounded
    graph persistence and cascading node/edge eviction prevent dangling
    provenance. This is an independently supplied SPPF projection, not an
    internal GLL/GLR parser or probabilistic forest.
39. **Nullable SCC least fixed point and proof-carrying deletion.** Parser
    trace v4 augments the forward packed DAG with at most 32 grammar-level
    `(symbol,state)` dependency rules. Canonical SCC identities record cyclic
    structure, but do not imply nullability. Empty RHS rules seed depth-zero
    proofs; a parent is admitted only after every RHS symbol has a proof, with
    deterministic minimum `(depth,rule-ID)` selection. Thus `A -> B, B -> A`
    remains non-nullable, while adding `B -> epsilon` proves `B` at depth zero
    and `A` at depth one. CFG fragment v5 carries the exact rules, proofs, and
    cyclic SCCs. The semantic layer independently recomputes the certificate,
    incrementally unions rules by parser, and rebuilds all derived state after
    restart or eviction. A nullable production shape authorizes deletion only
    in a lexical context separately certified by the selected parse tree.
    This closes the bounded nullable-cycle gap without allowing cycles in the
    packed node graph; synchronized multi-nonterminal substitution,
    incremental complete-forest parsing, and probabilistic inference remain
    open.
40. **Atomic multi-nonterminal ECT transactions.** The semantic layer derives
    synchronized mutations from already revalidated parent productions,
    alternative-zero packed edges, concrete terminal gaps, and primary child
    instances. It first reconstructs the accepted source parent byte-for-byte.
    A bounded product then changes two to four of at most eight slots, using at
    most two accepted yields per child shape and retaining no more than four
    novel transactions per parent. Canonical transaction identities bind the
    parent production/instance, every slot edge, source/target instances,
    unchanged gaps, changed-slot bitmap, and result hash. Shape context alone
    is insufficient: the current token must exactly equal the transaction's
    source parent yield, so applying a multi-slot result to another same-shape
    parent cannot silently degenerate into a single-slot mutation. The result
    is emitted by one parent-span splice and remains subject to concrete target,
    parser, and AFL validation. Transactions are recomputed from graph evidence
    after restart or eviction; persisted objects are audit data, not authority.
41. **Content-addressed incremental parser cache contract.** Parser commands
    can consume a versioned cache manifest that names an independently accepted
    base input/trace, an exact longest-common-prefix/suffix edit, and a
    conservative invalidation frontier with a 16-byte guard. Only unchanged
    prefix/suffix nodes are offered, with mapped spans and yield hashes. The
    parser must still return a complete structural trace. An optional reuse
    receipt is checked against the manifest digest and candidate
    symbol/state/span/epsilon/yield; no receipt is a valid full-parse fallback,
    while a false receipt rejects the trace. Entries are bounded,
    content-addressed, command-specific, atomically stored, and rehashed before
    use; corruption falls back to cold parsing. This supplies a verified
    parser-neutral interface and telemetry, not an in-process incremental GLR
    implementation or a measured speedup.
42. **Accepted-forest PCFG and packed-DAG inside/outside scheduling.** Only
    concrete alternative-zero derivations from independently parser-accepted
    fragments update canonical `(parser,LHS,state)` production-shape counts.
    Fragment digests provide bounded saturating deduplication. A symmetric
    Dirichlet prior with alpha 0.5 preserves nonzero mass for known unobserved
    shapes. Because parser alternative ordinals are forest-local, concrete
    observations bind the ordinal to their ECT instance, while global
    hyperedges are canonicalized by node, production, and ordered child IDs.
    Explicit root membership and forward-DAG dynamic programming recompute
    inside and outside mass after restart or graph eviction. Posterior
    likelihood and count-uncertainty/outside influence are the two additional
    Pareto objectives described in item 35. They schedule budget only: no
    probability can authorize a mutation, prune a low-frequency production, or
    bypass concrete target/parser/AFL validation. This is supervised
    concrete-tree counting over a bounded accepted forest, not ambiguous-tree
    EM, a context-sensitive PCFG, or a probabilistic circuit.
43. **Relation-aware synchronized slots with explicit counterexample
    exploration.** Reconstructed alternative-zero parents provide accepted
    observations for pairwise byte equality, canonical decimal equality,
    value/peer-length, and byte-length predicates. A parent shape needs at
    least two observations and zero current counterexamples; only the strongest
    predicate per slot pair is retained. Relations are fully derived after
    every graph change, so new accepted counterexamples weaken or remove them.
    Each parent explores at most 256 products and retains at most four
    transactions: three relation-ranked candidates plus one deliberately
    nonconforming reserve when available. Transaction v2 binds relation IDs
    and satisfied bits, while semantic state v16 binds every ECT instance to
    its exact ordered local child-edge sequence. Missing, partial, reordered,
    or cross-parent edge provenance is rejected; v15 migration succeeds only
    for a unique complete legacy edge set. This is a bounded scheduling
    heuristic, not an attribute grammar: every result still passes concrete
    target replay, independent parser acceptance, and AFL novelty.
44. **Hierarchical parent-conditioned PCFG and context-state forest mass.**
    Every production selected by a complete accepted alternative-zero tree is
    observed both globally and under a canonical
    `(child family,parent production shape,RHS slot)` context. Non-root
    posteriors use a beta-2 empirical-Bayes prior centered on the global
    Dirichlet posterior; roots and unseen contexts exactly retain the global
    fallback. Shared packed nodes are evaluated as `(node,context)` states, so
    different parents do not overwrite one posterior. Bounded contextual
    inside/outside artifacts feed the existing likelihood and
    information/reachability objectives only. State v17 persists identities,
    counts, and fragment witnesses, then performs atomic fixed-point evidence
    reconciliation on restore; no derived mass is trusted. This closes the
    first-order parent-context gap but is not ExplainFuzz's probabilistic
    circuit: grandparent, sibling-joint, symbol-table, checksum, and arbitrary
    query conditions remain outside the model.
45. **Update-before prequential calibration and drift-safe fallback.** Before
    an accepted fragment changes any count, every selected node records the
    global/context sufficient statistics needed to reconstruct its prediction.
    Per-context log loss is compared with the unconditional PCFG in
    deterministic receipt-ID order. A bounded gain-and-sample weight mixes raw
    context probability with the global posterior; absent evidence, negative
    regret, or later adverse events set the weight to zero. State v18 validates
    canonical, unique receipts against final fragment/context evidence and
    rebuilds all calibration tables. V17 migration keeps learned raw context
    counts but cannot enable them without update-before evidence. This prevents
    training-fit NLL from masquerading as predictive quality, while remaining
    a cumulative detector rather than a recency-weighted change-point model.
46. **Ordered fixed-recency calibration, stale detection, and recovery.** New
    prequential v2 receipts bind the accepted-fragment index. A globally
    advancing 16-fragment window computes recent context-versus-global regret;
    recent negative gain, fewer than two events, or no context event disables
    the conditional mixture, while later positive evidence can re-enable it.
    V19 checks the fragment-index bijection and v2 identity. V18 unordered
    receipts remain cumulative migration evidence rather than receiving a
    fabricated order. This is a deterministic fixed-window baseline for
    adaptive-window ablations, not ADWIN or a detector with false-alarm/delay
    guarantees.
47. **Bounded adaptive log-loss window with verifiable cuts.** Ordered
    receipts from the same accepted fragment are aggregated into one
    context-level sample, and their context-versus-global log-loss gain is
    clipped to a four-bit bound. An online replay over at most 64 fragments
    examines cuts with at least eight samples per side and applies a
    Hoeffding threshold with a per-scan union allocation over all candidate
    cuts. A significant cut discards the old prefix; the effective posterior
    uses the retained suffix's un-clipped, per-fragment mean NLL. State v20
    emits canonical certificates binding the evidence digest, cut, means,
    threshold, and confidence parameters, then ignores stored certificates
    and recomputes them on restore. The fixed-16 stale gate remains a
    conservative OOD guard. This is an ADWIN-inspired bounded detector with a
    per-scan bound, not a paper-faithful ADWIN or an anytime-valid guarantee.
48. **Verifier-gated grandparent multi-context probabilistic circuit.**
    Complete accepted primary trees induce a second-order context over the
    grandparent shape/slot, parent shape/slot, and child family. Its posterior
    shrinks to the parent posterior, while update-before receipts compare it
    with both global and parent predictions; the minimum of those two
    prequential gains gates the finer expert. Packed context-state v2 keeps
    distinct grandparent paths through shared nodes and evaluates a normalized
    convex mixture inside the existing sum/product forest. State v21
    reconciles context counts and fragment witnesses, validates three-layer
    sufficient-stat receipts, and recomputes adaptive circuit-cut
    certificates. This is a bounded depth-2 circuit, not an implementation of
    symbol-table, checksum, query-conditioned, or arbitrary long-range
    factors.
49. **Bounded ordered-sibling autoregressive factor.** Complete accepted
    primary trees induce a third expert keyed by the parent production, child
    slot, and immediately preceding sibling production. Its posterior shrinks
    to the finest parent/grandparent prior and is enabled only when its
    update-before loss beats global, parent, and circuit baselines. Packed
    context-state v3 uses shape-indexed forward and suffix messages, so
    ordered child mass and artifact-level outside coefficients are exact for
    the bounded first-order chain rather than an independent-child product.
    State v22 validates the left/current RHS path, counts, witnesses, receipts,
    and shared fragment-index bijections. Non-adjacent, bidirectional, symbol
    table, checksum, and cross-parent dependencies remain outside the model.
50. **Context-wise anytime-valid drift audit.** In addition to the responsive
    per-scan cut, every context-local fragment launches a forward Hoeffding
    confidence sequence. Launches and looks use nested
    `6/(pi^2 n^2)` alpha spending, and the sequence is the running
    intersection of all intervals. Under a bounded constant-conditional-mean
    null, the double budget controls the probability of any certified false
    alarm over an unbounded number of scans at 0.05 for one context. Parent,
    circuit, and sibling schemas bind both budgets, intervals, separation,
    assumptions, and canonical receipt evidence. State v23 recomputes these
    audit-only artifacts and accepts v22 evidence without trusting missing or
    persisted certificates. This is not a cross-context family-wise guarantee.
51. **Bounded second-order sibling-history factor.** For RHS slot two and
    later, complete accepted primary trees induce an expert keyed by the
    selected production shapes at the preceding two slots. Its posterior
    shrinks to the immediate-sibling raw posterior and receives weight only
    when update-before log loss beats global, parent, circuit, and sibling
    baselines. Context-state v4 carries the last two shapes and computes exact
    tuple-indexed forward and suffix messages for the bounded order-two chain.
    Responsive and anytime history certificates are independently reconstructed
    from five-layer receipts. State v24 validates both preceding RHS paths,
    reconciles counts and witnesses, checks shared fragment-index bijections,
    and refuses to infer history evidence from v23. This captures one
    non-adjacent sibling dependency but is not an arbitrary-distance,
    bidirectional, cross-parent, symbol-table, checksum, or query-conditioned
    probabilistic circuit.
52. **Cross-context online-FWER audit.** Parent, circuit, sibling, and history
    contexts enter an append-only allocation ledger. Context ordinal `c`
    receives `0.05 * 6/(pi^2 c^2)` and the existing repeated-forward
    construction spends that budget across launches and looks. The resulting
    three-level union bound controls the probability of any false global
    certificate over unbounded registered contexts and optional stopping,
    without assuming independence between context streams, provided every
    true-null stream satisfies its bounded constant-conditional-mean null.
    The registration observation is excluded so the outer alpha is fixed
    before every result used by the global test.
    State v25 validates contiguous non-recycled allocations and exact first
    evidence; corruption disables the global tier, while v24 reconstructs an
    initial ledger from validated ordered receipts. This is an audit-only
    alpha-spending baseline, not ADDIS/Fallback alpha recycling or evidence
    that their stronger assumptions hold.
53. **Cost-faithful five-level PCFG ablation and sealed evidence.** A runtime
    order selects global, parent, grandparent circuit, immediate sibling, or
    second-order history as the finest model. Disabled levels stop collecting
    contexts, update-before receipts, calibration/certificates, and factor
    states; global-only skips contextual inside/outside, while sibling and
    history retain one- and two-shape frontiers respectively. The sealed
    protocol expands one base command into five paired equal-CPU
    configurations. MPI emits a canonical-hash artifact binding run identity,
    seed, CPU budget, order, prequential NLL/gain, certificate delay/alpha,
    and state/transition costs. Missing or mismatched artifacts invalidate an
    otherwise successful cell, and the paired analyzer joins verified metrics
    by run ID. This implements the confirmatory measurement mechanism; no
    public 20-repeat result or superiority claim is implied.
54. **Persistent native Tree-sitter incremental parsing with node-identity
    proof.** A bounded Unix service retains content-addressed native `TSTree`
    objects across proposal validations. It reconstructs the exact manifest
    edit, applies `Tree.edit`, and calls `Parser.parse` with the edited old
    tree. A reuse receipt names only manifest-offered base nodes whose native
    Tree-sitter ID is present in the candidate tree; the proposal manager then
    independently verifies the mapped symbol, grammar/parse state, span,
    epsilon flag, and yield digest. Missing daemon state falls back to a cold
    parse and malformed evidence fails closed. Real Tree-sitter 0.25.2,
    tree-sitter-json 0.24.8, Unix RPC, and manager end-to-end tests exercise
    this path. It is genuine incremental concrete-tree reuse, not a claim that
    Tree-sitter exports a complete ambiguous GLR/GLL forest.
55. **Proof-carrying parser telemetry and sealed cold/incremental ablation.**
    Proposal state v11 accumulates manager wall time, native reported parse
    time, trace bytes, cache requests/offers/receipts/zero-reuse, node-ID
    proofs, and reused/invalidated nodes. Restore checks the complete counter
    lattice and bounded totals. A cache-off mode retains the same parser and
    structural validation while suppressing manifests and cache storage. The
    sealed protocol expands one base command into equal-CPU cold/incremental
    cells; MPI emits a canonical artifact bound to run identity and cache
    mode, and analysis joins only verified parser scalars. This makes speedup
    and end-to-end utility falsifiable; it does not claim the public
    confirmatory experiment has already been run.
56. **Persistent generalized Earley complete-SPPF provider.** A byte-mode
    Lark 1.3 Earley service requests `ambiguity="forest"` and exports the
    native shared packed parse forest without enumerating parse trees.
    Structural trace v3 retains symbol, LR(0)-item, packed-production, and
    token nodes; production wrappers prevent distinct derivations with equal
    child lists from collapsing. Non-primary sharing remains shared, while
    only duplicate ownership in the selected alternative-zero projection is
    cloned to satisfy its tree invariant. Zero-width subforests are represented
    as trace v4 leaves plus independently verified nullable dependency rules.
    Cyclic infinite ambiguity and every node/edge/alternative/rule/byte bound
    fail closed, so an accepted artifact is never a knowingly truncated
    forest. This is a real generalized Earley/SPPF integration in Python, not
    native-C GLR/GLL and not incremental forest reuse.
57. **Proof-carrying complete-forest telemetry and sealed calibration.**
    Proposal state v12 accepts Lark forest telemetry only when its provider
    proof, grammar digest, version, accepted/complete bit, raw/clone/encoded
    relation, alternatives, edges, nullable rules, and elapsed bounds agree
    with the independently normalized v3/v4 trace. It accumulates the forest
    funnel, size, and cost in the existing sealed parser artifact. A generated
    equal-CPU, cache-off matrix compares parser-off, selected CST, and complete
    SPPF cells; metadata binds each mode and non-forest cells cannot claim
    forest observations. This measures a cross-parser semantic/cost tradeoff,
    not a parser-controlled causal effect, because the grammars may differ.
58. **Candidate-paired dual-parser acceptance calibration.** Separate fuzzing
    cells cannot produce an acceptance confusion matrix because they observe
    different candidate streams. A bounded no-shell adapter now runs the
    complete-SPPF primary and selected-CST secondary concurrently on the same
    candidate, embeds both traces, and binds their canonical digests, parser
    argv digests, return codes, decisions, candidate digest, and elapsed time.
    The manager applies its ordinary v1--v4 validator independently to the
    nested secondary trace before counting both-accept, primary-only,
    secondary-only, and both-reject. Proposal state v13 and the sealed artifact
    enforce the quadrant and agreement identities and retain the ordered
    command pair. An equal-CPU selected/complete/paired protocol turns parser
    semantic disagreement into a falsifiable measurement; the primary remains
    the sole authorization oracle.
59. **Parser-neutral structural correspondence evidence.** For every
    both-accepted pair, the manager derives unique non-epsilon byte-yield spans
    from the primary alternative-zero projection, the full primary forest,
    and the secondary selected tree. It separately compares internal byte
    boundaries. Representation-only duplicates such as Symbol, LR-item, and
    Packed wrappers with the same yield are collapsed. Proposal state v14 and
    the artifact enforce subset, intersection, and exact union identities and
    expose selected-span, forest-span, and boundary micro Jaccard together
    with precision/recall. This is grammar-name-independent structural
    calibration, not yet a semantic symbol or production mapping.
60. **Independent generalized-LR complete-SPPF oracle.** A persistent
    parglare 0.21 service now exports the direct Parent/possibility SPPF from
    Farshi/Rekers-style GLR without consulting solution counts or enumerating
    trees. Production and terminal wrappers preserve distinct alternatives
    and object-identity interning preserves sharing. It reuses the established
    cycle, nullable-certificate, selected-projection, topological, and resource
    checks, while carrying a provider-specific proof and grammar/version
    identity. The paired oracle can therefore compare generalized Earley and
    GLR forests on the same candidate. Parglare remains pure Python and
    non-incremental, so this adds algorithmic differential evidence rather
    than closing the native/incremental performance gap.
61. **Proof-carrying cross-parser grammar correspondence.** Once both
    independently validated parsers accept the same candidate, provider-only
    packed/item/token wrappers are made transparent and every grammar node is
    assigned a parser-neutral recursive shape over its byte extent and ordered
    child extents. A symbol observation is retained only when its
    span-and-shape bucket is unique on both sides; unresolved multiplicity is
    counted rather than guessed. A production observation additionally
    requires an equal non-empty ordered child partition. Proposal state v15
    seals the canonical per-record evidence and the research artifact exports
    parser-identified symbol/production maps with support counts. Two
    independent verifiers enforce sorting, uniqueness, sums, and
    production-to-symbol-to-node bounds. This is conservative,
    sample-conditioned grammar relationship evidence, not a proof of CFG
    language equivalence or permission to trust the secondary parser.
62. **Bounded exhaustive parser-language differential certificate.** A
    shortlex enumerator now covers every string up to a declared length over
    a canonical 1--16 byte alphabet, with an exact domain cap of 65,536.
    Every case passes through the concurrent paired adapter and the manager's
    independent v1--v4 trace/proof validation. The sealed artifact binds the
    domain cardinality, four acceptance quadrants, both command identities, a
    full transcript digest, and shortlex-minimal counterexamples; incomplete
    execution cannot be labeled complete. This yields an exact finite-domain
    result and a useful CI gate. It deliberately does not decide unrestricted
    CFG equivalence, which is undecidable in general.
63. **Proof-carrying assumption-conflict PSCache.** The persistent QF_BV
    helper now reconstructs numeric input-byte equalities from the concrete
    witness and failed cache probes, checks them as Z3 assumptions against the
    full current query, and retains only non-empty UNSAT assumption cores.
    Bounded deletion checks minimize the core when every removal is decided,
    and a final UNSAT check verifies the retained certificate. QueryStore
    independently establishes that the full assignment conflicts with the
    source Query IR, stores proof/provenance, and indexes the positive or
    negated literals it actually satisfies. Reuse remains proposal-only until
    the destination Query IR and concrete replay succeed. This covers the
    backend-stable conflict/reconstruction/prefix-index path, but samples
    explicit assumption conflicts rather than every internal CDCL/bit-blast
    trail conflict described by PSCache.
64. **Proof-carrying heterogeneous QF_BV solver boundary.** The asynchronous
    service now lowers its content-addressed Query IR directly to standard
    QF_BV under a canonical operator/width/node/input capability contract,
    rather than assuming that a Z3-printed script is a backend-neutral IR.
    Sort, width, arity, extraction/extension bounds, byte reads, and Boolean
    roots are checked before process launch. A sealed lowering certificate
    binds exact operator counts, input-set, capability, and SMT-LIB digests.
    Standard `get-value` models are checked once by the adapter and again by
    QueryStore's independently loaded CAS evaluator; unsupported queries,
    false/missing models, certificate tampering, and untrusted UNSAT results
    cannot enter result caches. cvc5 1.1.2 executes a single matrix containing
    all 38 current Query IR operators. F244 later adds fixed Bitwuzla 0.9.1
    execution and replayable cross-backend evidence; native/cross-worker solver
    contexts remain open. F239--F241 add X-means, a bounded ensemble sequence
    optimizer, and expected-improvement acquisition.
65. **Prefix-keyed persistent heterogeneous solver contexts.** An opt-in
    incremental capability now gives each exact QueryStore prefix key an
    isolated interactive SMT-LIB process. Prefix assertions are loaded once,
    while targets use bounded push/check/get-value/pop frames; newly referenced
    input bytes are declared outside the frame. A 1--64 entry LRU bounds
    processes, canonical prefix-term digests detect identity violations, and
    echo markers frame multi-line responses. Timeout, EOF, malformed output, or
    protocol loss destroys only that context and forces a cold rebuild. Real
    cvc5 tests establish a same-prefix hit and LRU eviction/rebuild; a synthetic
    hung solver establishes timeout recovery. This is exact-prefix parsing and
    assertion reuse, not learned-clause transport, native multi-context sharing,
    or cross-worker solver-state migration.
66. **Proof-carrying X-means/BIC censored sequence prior.** The trajectory
    schema now records the online scheduler's complete nine-dimensional context
    and an explicit per-execution timeout bit. A deterministic offline trainer
    performs bounded two-means refinement and accepts local splits only when a
    spherical-Gaussian BIC improves. Within each cluster it charges censored
    runs PAR-2, uses bounded inverse-propensity weights, shrinks local action
    utility toward a global estimate, and requires raw samples, ESS, and a
    confidence lower bound before recommending a sequence. The versioned
    artifact seals every split and sufficient statistic; its verifier
    independently reconstructs BIC, ESS, cost, uncertainty, support, and
    recommendation decisions. The online scheduler treats a verified result as
    a 90/10 preference only, so unsupported clusters fail open to exploration.
    This implements the X-means and censored objective layer motivated by
    SMTgazer, not its dataset-level Bayesian optimizer or boosting/bagging
    surrogate, and it carries no performance claim without a holdout
    equal-CPU campaign.
67. **Bagged/boosted budgeted SMT sequence optimizer.** Three deterministic
    shallow-tree ensembles model action completion, stage-budget-aware censored
    PAR-2 ratio, and adjusted reward. Stable bootstrap bagging supplies
    diversity, residual boosting supplies bias correction, and bag variance plus
    residual RMSE supplies conservative uncertainty. A bounded beam search uses
    completion/reward lower bounds and cost upper bounds to select integer-second
    schedules under one timeout; a multi-action result must improve on the best
    single action. The sealed artifact embeds the verified F239 policy and the
    verifier replays global and per-cluster optimization from every tree. The
    scheduler executes stage budgets through the worker timeout and advances
    actions across replays while preserving exploration. Input identity, solved
    status, and assigned budget are logged for future paired estimators. This is
    an observational ensemble plus deterministic sequence search, not the
    paper's Bayesian acquisition loop or evidence of unbiased counterfactual
    performance.
68. **Bounded expected-improvement schedule SMBO.** A schedule-level optimizer
    encodes ordered actions and integer-second stage budgets in a fixed bounded
    vector. Its deterministic initial design contains every single-action
    baseline; later observations train bounded bagged and boosted shallow-tree
    surrogates and use analytic expected improvement to select one unseen
    schedule at a time. Objective values are evaluated lazily under a strict
    selection budget. Only after acquisition terminates does an isolated
    exhaustive diagnostic compute oracle objective and simple regret. The
    versioned artifact embeds the verified F240 ensemble, candidate-pool
    digest, initial design, and every mean/uncertainty/EI/incumbent transition;
    verification replays global and localized searches. The scheduler preserves
    the 90/10 exploration contract and reports a distinct prior kind. This is
    Bayesian sequential optimization of the sealed observational surrogate,
    not a reproduction of private SMTgazer code or evidence of real solver
    improvement; a cost-matched holdout campaign remains mandatory.
69. **SAT-gated bounded-consensus portfolio cancellation.** The parallel
    QueryStore portfolio can optionally start a bounded disagreement grace
    period after the first SAT result, then cancel queued futures and interrupt
    running helper process groups. UNSAT never starts early termination and
    therefore retains full collection. A SAT winner still needs exact Query IR
    model validation before persistence. One-shot and persistent JSON/QF_BV
    helpers implement query-scoped cancellation; persistent servers respawn
    cold and killed prefix contexts are removed from the LRU. Telemetry
    distinguishes cancellation request, actual cancelled attempts, and
    incomplete consensus. The default remains exhaustive collection. This is a
    measurable CPU/completeness tradeoff, not native solver interrupt, learned
    clause sharing, or complete cross-solver consensus.
70. **Query-IR structural context schema with legacy-proof migration.** The
    QSYM serializer now accumulates structural statistics while traversing each
    successfully committed Query IR DAG: nodes, distinct input reads, maximum
    bit width, and four analysis groups for comparison, nonlinear arithmetic,
    bitwise/shift, and structural/control operators. The runtime sends these
    counts through engine-neutral telemetry and offline trajectories. Context
    feature schema v2 appends bounded per-query log-scales and operator/node
    ratios to the original nine features. F239 X-means geometry/BIC, F240 tree
    verification, and F241 embedded-policy replay derive their dimension from
    an exact sealed v1 or v2 contract. Existing nine-dimensional artifacts
    retain their original digest and are projected from new contexts; old
    online moment state is zero-extended. Unknown dimensions or reordered
    feature lists fail closed. This closes the structural-observability
    implementation gap, but does not show that the extra features improve
    calibration, PAR-2, or coverage without a paired holdout campaign.
71. **Fixed-version replayable heterogeneous QF_BV conformance.** The canonical
    conformance formula exercises all 38 supported Query IR operators and has a
    fixed two-byte model. Every backend must pass adapter-side and QueryStore
    model validation, reject an untrusted contradictory result as `unknown`,
    and accept it as UNSAT only under an explicit capability. A second verifier
    reconstructs content-addressed query identities and exact lowering
    certificates. The versioned artifact binds commands, version output,
    executable hashes, capabilities, lowering proofs, and results; a separate
    semantic digest permits replay while excluding elapsed time and host paths.
    Z3 4.8.12, cvc5 1.1.2, and an official-tag Bitwuzla 0.9.1 build all pass
    38/38 operators and the trust matrix. This establishes adapter conformance, not
    solver performance, complete consensus, proof-producing UNSAT, native
    context sharing, or coverage improvement.
72. **Sealed paired QF_BV holdout campaign.** Complete Query IR envelopes are
    content-sealed, independently ingested into QueryStore, pre-lowered, and
    assigned to train or holdout by a stable hash of their query identity.
    Campaign execution is holdout-only and requires every current solver binary
    to match a verified conformance artifact by version and file digest. Each
    query/backend/repetition receives the same wall timeout in a stable
    randomized order; child-process CPU and censored PAR-2 are measured.
    Backend and pair tables report solved quadrants, SAT/UNSAT disagreement,
    complementarity, and paired cost deltas. The offline verifier reconstructs
    the corpus, split, Cartesian task set, lowering certificates, every SAT
    witness, PAR-2, and all aggregates. Confirmatory mode rejects synthetic
    corpora, non-disjoint splits, and fewer than 20 repetitions. A checked
    single-repeat synthetic smoke artifact proves the mechanism only; a public
    multi-target confirmatory corpus remains outstanding.
73. **Leakage-checked solver-strategy holdout campaign.** F240/F241 policies
    are sealed together with normalized trajectory events whose content IDs
    must belong to the corpus train split. Verification deterministically
    retrains the complete F240 ensemble and F241 SMBO artifact, preventing a
    rehashed policy or holdout-derived event from entering evaluation. For each
    holdout Query IR it independently reconstructs the F243 structural vector,
    cluster, and budgeted schedule. One randomized Cartesian task manifest runs
    every certified individual backend, F240 beam, F241 SMBO, and F242
    SAT-gated parallel cancellation. Full internal attempts are retained so
    every completed SAT model and authorized UNSAT can be checked, including
    winner/outer consistency and cancellation/disagreement invariants.
    Strategy-level child CPU, wall time, PAR-2, cancel rates, incomplete
    consensus, and all paired solved quadrants are recomputed offline. The
    contract gives equal logical tasks and a shared declared wall budget while
    explicitly recording that parallel CPU is not pre-equalized. The checked
    synthetic six-arm artifact is mechanism evidence only; public-target
    training provenance, 20 repetitions, CPU-normalized coverage, and grace
    sensitivity remain required for a performance claim.
74. **Sealed AFL edge/data coverage join and structural-feature calibration.**
    Every verified F246 SAT assignment is rematerialized as an input and paired
    with its original witness on one identity-sealed AFL target. Persistent
    `afl-showmap -S -e` oracles replay every unique input repeatedly and reject
    unstable status or sparse bitmaps. Both oracles load the same preload to
    preserve PCGUARD edge IDs; a constructor reserves a low 64 KiB native data
    namespace before forkserver map negotiation, while one oracle disables
    data writes. This fixes the prior confusion between AFL++'s internal shared
    allocation limit and its negotiated effective map. Comparison sites use
    module-relative identities rather than ASLR-sensitive addresses.
    Candidate-versus-witness edge/data novelty, strategy union gain,
    complementarity, and coverage per measured solver child CPU second are
    reconstructed offline. Train-only trajectories fit deterministic v1/v2
    feature calibrators, evaluated only on individual-backend holdout rows with
    Brier, log loss, and ECE. A five-input synthetic artifact and semantic
    replay prove the mechanism, not strategy performance or public-target
    coverage improvement.
75. **Profile-guided failure-preserving Hydra transformation with authoritative
    replay.** A pre-symbolization LLVM module pass selects exactly one stable
    profiled branch and accepts only a strict single-entry/two-arm/single-exit
    diamond. Compatible-operation LCS aligns the arms; matched ALU operations
    merge selected operands, while unmatched operations receive safe extra
    operands. Aggressive mode linearizes simple loads and implements unmatched
    stores as immediate old-value load plus condition-selected writeback.
    Generated selects carry dedicated metadata so SymCC builds ITE expressions
    without recreating path-constraint forks. A sealed driver binds both
    binaries, the one-site compiler manifest, and content-addressed inputs,
    replays every transformed result on the original, retains only
    original-confirmed failures, emits transformed-only sites as a denylist,
    and reports an original-only failure as a preservation violation. A native
    one-real/one-spurious smoke proves classification and semantic replay, not
    performance. Multi-block regions, multi-exit/loop IFSS state, MemorySSA
    alias proofs, exception edges, full poison reasoning, and public-target
    coverage/CPU evidence remain open.
76. **Bounded multi-arm IFSS region-state worklist.** For a 3--8-input PHI, the
    compiler finds the nearest common controlling dominator, requires the
    merge to postdominate it, and explicitly enumerates at most 64 paths
    through a branch-only, acyclic, single-exit region of at most 32 blocks.
    Root-to-predecessor branch literals become conjunctive path predicates;
    alternative paths to the same predecessor are disjoined, and the endpoint
    set must exactly equal the PHI incoming set. Supported incoming SSA values
    are synthesized at the merge and folded into a reverse ITE state chain,
    which substitutes the original symbolic PHI. The last arm is a
    completeness-proven default, avoiding unused symbolic calls, and a late
    unsupported value atomically rolls all speculative merge-point IR back.
    A native three-arm test produces a cross-controller Backsolver SAT witness;
    an opaque-call rejection test verifies clean rollback. This is a bounded
    mechanism result, not general IFSS or evidence of performance improvement.
    Multi-exit selectors, loop state, switch edges, alias-sensitive memory
    state, exceptions, and public-target equal-CPU evaluation remain open.
77. **MemorySSA/AA-proven IFSS write-state recovery.** A simple merge-local
    load whose direct defining access is a two-input MemoryPhi can recover the
    non-taken stored value when each incoming access is an equal-width simple
    store in its merge predecessor and AA proves both stores MustAlias the
    load. The compiler verifies a symbolic controlling branch, postdominating
    acyclic region, and rejects any other region write whose ModRef result may
    modify the target location. It then synthesizes both stored SSA values,
    substitutes an ITE memory state for all downstream uses of the ordinary
    shadow-memory read expression, and attaches stable load/controller/store
    identities in `must-alias-memoryssa-v1` metadata. Late value failure rolls
    speculative IR back; undef/poison are rejected recursively. A native test
    reaches the unexecuted store arm, while NoAlias, opaque-value, and poison
    cases prove fail-closed behavior. The metadata is a compiler-analysis
    certificate, not an independently checkable alias proof. Def-chain
    traversal, multi-arm/multi-location memory, partial overlaps, multi-exit
    state, loops, concurrency, and public-target evaluation remain open.
78. **Bounded NoMod MemorySSA def-chain recovery.** Each incoming MemoryPhi
    state may walk backward across at most eight MemoryDefs before finding its
    equal-width simple MustAlias store. Every skipped definition must be proven
    NoMod for the load location; MayAlias or PartialAlias modification, a
    nested MemoryPhi, live-on-entry, an incompatible target store, or a ninth
    definition rejects the candidate. Volatile stores, atomic RMW/CmpXchg, and
    fences are rejected even when unrelated to the target location. Chain
    metadata encodes arm-local counts and nearest-first stable definition
    sites after the load/controller/store identities. A native one-hop-per-arm
    test reaches the alternative state, while MayAlias, bound, and volatile
    tests remain verifier-clean without speculative calls. General MemorySSA
    graphs, independently replayable ModRef proofs, shared multi-arm memory
    partitions, and empirical budget selection remain open.
79. **Shared multi-arm data/memory IFSS partition.** Data PHIs and MemoryPhis
    now consume one control-region proof: two to eight unique endpoints, a
    nearest common conditional dominator, a postdominating merge, at most 32
    unique blocks across both root successors, at most 64 acyclic branch-only
    paths, no unsplit conditional merge edge, and exact endpoint-key equality.
    Memory state scales to three through eight arms by combining each
    endpoint's path literals, consuming the bounded MustAlias/NoMod def chain,
    and folding stored values into an N-1 ITE chain with a
    completeness-proven default. Multi-arm metadata records each incoming
    block/store, path count, skip count, and skipped sites while preserving the
    two-arm schemas. A native three-arm, one-skip-per-arm test reaches the
    middle store; any MayAlias arm rejects the whole merge. Shared condition
    DAG ownership, general memory graphs, exit selectors, and performance
    evidence remain open.
80. **Partition-scoped IFSS condition-DAG ownership.** Before constructing
    path predicates, each accepted multi-arm data or memory partition scans
    non-default paths in deterministic order and deduplicates original LLVM
    condition values. Every condition requiring symbolic synthesis is emitted
    as its own short-circuit `SymbolicComputation`, producing an independently
    dominating exit PHI; cached RegionValues retain only the resulting
    concrete/expression pair and never own those instructions. Computations
    are registered in definition order only after the entire partition
    succeeds, so a late value failure can still roll back all speculative IR.
    Generated expressions carry `partition-condition-cache-v1` metadata tied
    to the original condition site. This ownership rule fixes two invalid
    alternatives: merging all condition instructions into the final state
    computation lets entry code consume later definitions, while one combined
    prefix computation gives an exit PHI only to its last result. A native
    four-arm data plus four-arm MemorySSA test observes exactly two internal
    condition computations per partition, passes the LLVM verifier, and
    generates a validated cross-controller memory witness. Empty-input
    synthesis fails closed. The cache is partition-local and pointer-keyed;
    cross-partition structural hash-consing, independently replayable alias
    proofs, multi-exit/loop state, and performance evidence remain open.
81. **Bounded normal-return exit-state lowering.** An opt-in pre-symbolization
    module pass identifies two to eight scalar return exits whose nearest
    common dominator is a conditional controller. Explicit path enumeration
    proves a single-entry, branch-only, acyclic region of at most 32
    non-controller blocks and 64 paths, with exact equality between reached
    endpoints and all function returns. Accepted returns branch to a synthetic
    dispatch block whose state PHI is consumed by one return. Existing
    multi-arm IFSS synthesis can therefore build a return ITE that crosses the
    call boundary through SymCC's return-expression ABI; arm-local side effects
    remain on their original paths. Stable metadata records controller,
    ordinal, original return sites, arms, blocks, and paths. The pass rejects
    void/aggregate/vector, identical state, undef/poison, musttail, switch,
    cycle, EH/indirect, address-taken, unclosed, and ninth-exit cases. It first
    collects every candidate, then freezes original identities only when a
    rewrite will occur, preserving the no-change analysis contract. A native
    three-return callee generates a validated caller-side witness for a
    non-executed return; a six-class rejection matrix remains verifier-clean.
    This is genuine normal-return multi-exit state, but not a general
    continuation selector with live-out/memory tuples, exception exits, loop
    summaries, or public-target performance evidence.
82. **Bounded switch-to-IFSS chain lowering.** A second opt-in module pass
    accepts two to eight case/default edges spanning at least two successors
    whose arm blocks have no external predecessor and either all branch directly to one
    exact-predecessor merge or all return normally. It lowers cases in stable
    source order to equality branches and a final default edge, updates
    destination PHIs, and leaves arm-local operations and effects in place.
    Generated comparisons and branches bind the original switch site, case
    ordinal/value, and arm count; the final branch separately identifies the
    default ordinal. The ordinary shared partition then reconstructs data and
    MustAlias/NoMod memory states, while the condition cache independently owns
    non-dominating internal equality expressions. Running switch lowering
    before return-exit lowering also supports direct-return switches without a
    second state implementation. A native three-arm memory case generates a
    validated alternative-case witness, and data/memory partitions each emit
    one internal-condition computation. All edges to one destination, external
    predecessor, conditional arm, mixed exit, and ninth edge reject without
    residue. Loops, general continuation tuples, and public-target evidence
    remain open.
83. **Unsigned range-balanced switch tree.** The strict switch pass now has a
    deterministic balanced mode in addition to its source-order linear
    fallback. Cases are sorted by unsigned APInt value; internal nodes compare
    the selector to the lower half's maximum with `ule`, and leaves perform
    exact equality. Every leaf false edge reaches default, so destination PHIs
    replace their original controller input with one identical input per gap
    predecessor. The shared partition consequently proves default relevance
    as an OR of explicit paths. With seven cases the longest decision path is
    three range tests plus one equality instead of seven equalities, at the
    cost of increasing total comparisons from seven to thirteen. The shape
    stays within the existing 32-block/64-path bound and carries a distinct
    `bounded-switch-tree-v1` node-kind certificate. An eight-arm native test
    verifies 1-to-7 default PHI expansion, twelve independently owned internal
    conditions, seven state ITEs, and a validated case-6 witness. The split is
    count-balanced rather than probability-optimal; empirical policy selection
    remains open.
84. **Profile-weighted optimal alphabetic switch tree.** Profile mode reads a
    complete positive case/default frequency set keyed by stable switch site.
    An interval dynamic program minimizes weighted range-test depth while
    preserving unsigned case order and exact-equality leaves; deterministic
    tie-breaking and saturating arithmetic make the shape replayable. Root
    proof metadata binds a full 64-bit content fingerprint, ordered weights,
    default count, and recomputable objective. Missing, unreadable, malformed,
    duplicate, stale, or incomplete profiles fall back to the balanced tree
    with a classified certificate. A seven-case skew moves the root upper
    bound from two to zero and retains the native case-6 witness. Aggregate
    default weight is recorded but excluded from split cost because it contains
    no value-gap distribution. LLVM branch-weight import, an independent tree
    manifest, online confidence/decay, and public equal-CPU evidence remain
    open.
85. **Shared-destination edge and PHI multiplicity proof.** Switch collection
    distinguishes logical edges from unique region endpoints and checks that
    repeated controller edges have verifier-compatible identical PHI values.
    Lowering removes every old controller incoming and reconstructs one incoming
    per new edge. Linear lowering preserves `3→3` in the regression; range
    trees produce `3→5` because every equality leaf contributes a default miss.
    Root and PHI certificates record original/lowered total and per-destination
    multiplicity. A shared two-endpoint data PHI bypasses the unsound direct
    two-arm shortcut and uses complete path conjunction/disjunction; MemorySSA
    is likewise labeled as a multi-path proof, and shared returns compose with
    exit-state lowering. The native regression generates the unique case-B
    witness through validated Z3 fallback. External-predecessor critical-edge
    regions, indirect control, loops, and public performance evidence remain
    open.
86. **LLVM-profile import and independently verifiable switch manifest.**
    Profile mode treats an explicit stable-site file as authoritative, but can
    import LLVM's validated default-plus-case `branch_weights` vector when no
    external entry exists. Case weights are remapped from source order to
    unsigned order; zero, malformed, or wrong-cardinality vectors fall back to
    balanced. An explicitly bad external entry never silently falls through to
    metadata. Every accepted switch can append a JSONL artifact containing
    provenance, requested/effective mode, objective, complete preorder tree,
    case-to-destination mapping, shared multiplicity, and profile/tree
    fingerprints. A standalone verifier recomputes saturating interval DP,
    splits, multiplicities, and both fingerprints, and rejects a root-value
    tamper. Aggregate default observations are not redistributed across gaps or
    written back as invented standard branch probabilities. Compiler-binary
    binding, concurrent artifact sealing, online confidence, and public
    equal-CPU validation remain open.
87. **Bounded multi-continuation exit-ID and scalar live-out tuple.** An
    opt-in pre-symbolization pass enumerates a dominated, branch-only, acyclic
    controller region with two to eight exit edges, at least two continuation
    destinations, no more than 32 blocks and 64 paths, and one to eight scalar
    destination-PHI slots. Every exit is split through a capture block into a
    common state whose `i8 exit_id` is consumed by a real conditional dispatch;
    per-exit resume blocks preserve logical edge identity. Each live-out PHI
    carries its destination's original value and neutral values on exits where
    that slot cannot be consumed. Destination PHIs are rebuilt per logical
    edge only after all slot types, values, multiplicities, and budgets pass.
    Stable metadata binds controller, original terminator/successor, exit and
    destination ordinals, slot/original-PHI identity, and bounded counts.
    Repeated same-source edges retain exact multiplicity, while cycles,
    single-destination regions, direct poison/undef, vectors/aggregates, and
    incomplete PHIs fail closed. The existing complete IFSS partition builds
    exit and tuple ITEs; a three-exit/two-destination native regression starts
    on exit 2 and generates the cross-destination case-B witness. This is a
    scalar SSA tuple result, not a proof for MemorySSA locations, loops,
    exception/coroutine/indirect exits, or performance improvement.
88. **Proof-carrying bounded affine natural-loop acceleration.** A canonical
    two-block natural loop can be removed when LoopInfo proves one preheader,
    latch, backedge, exiting header, and exit; its condition is exactly
    `iv < trip`, `iv` starts at zero and advances by plain add one, and a
    bounded value interpreter proves `trip <= 8`. One to eight observed
    integer state PHIs must update independently as plain
    `state + constant`; any memory/call/atomic, extra instruction,
    nonlinear/cross-state recurrence, signed comparison, overflow flag,
    poison/undef source, or uncertain bound rejects the loop. The preheader
    receives `initial + zext_or_trunc(trip) * step` in the state's modular
    bit-vector ring, exit PHIs gain a preheader edge, and the dead
    header/latch are deleted. Metadata binds loop/state identities, maximum
    trip, and recurrence sites. A one-state solver regression produces input
    `05` in one SAT query, while a two-state cross-width case exercises PHI
    repair; original and summarized IR agree on 32 differential executions.
    This does not cover multi-block, multi-exit, variable/matrix, memory, or
    unbounded loops, and removing instrumented loop edges requires
    original-binary replay for coverage comparisons.
89. **MemorySSA/AA-proven continuation memory tuples.** After bounded
    continuation lowering, a destination scalar load can become a dispatch
    memory slot only when its MemoryUse is defined by that dispatch's
    MemoryPhi and all capture/resume metadata exactly matches the same
    controller and destination. For every relevant exit, a bounded backward
    walk crosses at most eight MemoryDefs proven NoMod by AA and must end at an
    equal-width simple MustAlias store whose value dominates the capture.
    MayAlias/Mod effects, nested MemoryPhi, atomics, missing stores, external
    predecessors, or destination-prefix writes reject the location. Up to four
    slots are committed per controller; a fifth rejects that controller's
    memory tuple. Relevant exits carry store values and other destinations
    carry typed neutral values that cannot be consumed. The original load
    remains executable while its data users consume the proven dispatch PHI.
    Metadata records load/store/exit/slot identities and each skipped NoMod
    site. A two-location, three-exit regression agrees on all 256 byte inputs
    and produces a cross-destination case-B witness. This remains a bounded
    full-width scalar proof, not partial-overlap, initial-state, symbolic-alias,
    cyclic MemorySSA, atomic/concurrent, aggregate, or cross-function memory.
90. **Replay-verifiable continuation CFG/tuple manifests.** Before appending a
    continuation JSONL record, the analysis pass reconstructs canonical
    capture/resume edges, destination partitions, scalar slots, and bounded
    counts. Memory records uniquely locate the retained source load and rerun
    the MemoryUse-to-dispatch-MemoryPhi and MustAlias/NoMod proof; recomputed
    store and skipped-definition sites must match IR metadata. The artifact
    binds module/function, all edges and slots, and per-exit memory chains in a
    canonical 64-bit content fingerprint. A standalone Python verifier checks
    ordinals, edge uniqueness, destination partitions, bounds, analysis
    classification, and the fingerprint, rejecting destination, chain, and
    hash tampering. It deliberately does not reimplement LLVM AliasAnalysis:
    independent bitcode-level ModRef replay, compiler/IR cryptographic identity,
    and concurrent artifact sealing remain required for stronger evidence.
91. **Atomic cryptographic continuation artifact envelopes.** A post-build
    sealer first verifies every continuation record, then streams SHA-256 and
    byte counts for the manifest, input IR, final lowered IR, SymCC pass
    binary, and actual LLVM tool. It also captures bounded `opt --version`
    output and the ordered proof fingerprints. Canonical sorted-key JSON is
    protected by an outer SHA-256. A POSIX advisory lock, fresh-output gate,
    same-directory temporary file, file and directory fsync, and atomic rename
    prevent partial or overwritten local evidence. Verification rechecks the
    manifest, all artifact hashes, LLVM identity, and the envelope hash.
    Duplicate sealing, changed lowered IR, and envelope tampering fail closed.
    This is reproducible local integrity, not a signing key, transparency log,
    TPM attestation, or proof that a supplied input/output pair came from one
    command invocation.
92. **Independent artifact-bound MemorySSA/AA replay.** A replay verifier first
    revalidates the complete cryptographic envelope, then launches a fresh
    sealed `opt` executable with the sealed SymCC plugin and lowered IR.
    Continuation transforms are forced off; only the continuation-memory
    analysis pass runs and may emit a new manifest after rebuilding current
    MemorySSA/AA, capture/resume partitions, MustAlias writers, bounded NoMod
    chains, and dominance checks. The new records must independently verify and
    exactly equal the sealed records. A regression changes a NoMod noise store
    into a target-location write: the IR remains verifier-clean and can receive
    a new valid seal, but semantic replay rejects it. This gives
    process-independent replay with an artifact-bound implementation, not a
    second AA algorithm, formal proof checker, hermetic runtime, or signature.
93. **Proof-carrying upper-triangular affine loop acceleration.** The canonical
    F261 loop may contain two to four same-width state PHIs whose bounded
    add/sub/constant-multiply DAG forms `x'=Ax+b`, provided `A` is
    unit-diagonal upper triangular and has a real cross-state term. With
    `N=A-I`, nilpotence yields exact modular bit-vector closed forms
    `A^T=sum C(T,k)N^k` and
    `sum(q<T)A^q b=sum C(T,k+1)N^k b`. Since `T<=8`, i16 binomial recurrence
    is exact before truncation to state width. This replaced an initially
    correct but Symbolizer-breaking trip-enumeration prototype with a compact
    shared DAG. A JSONL artifact records `A,b`, state identities, and every
    bounded power/offset; an independent verifier recomputes triangularity,
    modular matrix recurrence, and the fingerprint. A three-state chain
    agrees on 32 executions and solves the trip-5 target in one SAT query.
    This is not multi-block/multi-exit, general matrix exponentiation,
    symbolic-coefficient, memory, or unbounded loop summarization.
94. **Bounded post-update break/continue exit tuples.** A two-block summarized
    loop may have one latch exit selected by `iv == break_at` after recurrence
    updates, with the other edge continuing to the header. The invariant
    `break_at` and proven `trip<=8` give
    `break_taken=break_at<trip` and exact execution count
    `break_taken ? break_at+1 : trip`. Normal exit PHIs are constrained to
    header `iv/state`, while break exit PHIs are constrained to latch
    `iv_next/state_next`; the preheader supplies phase-correct summaries to
    both logical exits. An independent artifact verifies all CFG/state
    identities and the complete bounded trip-by-break truth table. Sixty-four
    differential executions and a cross-exit SAT witness pass; wrong predicate,
    induction phase, successor polarity, or direct update use fails closed.
    This is one equality break with implicit continue, not arbitrary
    multi-break predicates, multi-block loops, or side-effecting state.
95. **Proof-carrying multi-block linear-arm Hydra melding.** At the F268
    stage, the profiled single-site Hydra pass accepted equal-length arms
    containing one to four
    linear basic blocks. Every internal edge is unconditional with a unique
    arm predecessor; cross-block SSA stays within its arm or reaches the common
    merge PHI. Instructions are flattened in CFG order so compatible-operation
    LCS and value remapping cover cross-block def-use chains before all arm
    blocks are deleted atomically. A new manifest binds ordered arm block
    sites, every instruction-site/opcode alignment slot, output PHI identities,
    the original-coverage-replay obligation, and a structure fingerprint.
    An independent verifier is enforced by the replay campaign while preserving
    compatibility with the checked F248 artifact. A two-block-per-arm example
    agrees on all 256 byte inputs and passes symbolized LLVM verification;
    block, alignment, and hash tampering is rejected. Unequal arms failed
    closed at this stage and are superseded by the bounded F272 extension in
    item 99. Internal branch trees, loops, calls, and exception edges remain
    excluded.
96. **Partial store/live-on-entry continuation memory tuples.** For one
    continuation destination, relevant dispatch states may now mix bounded
    NoMod chains ending in an equal-width MustAlias store with chains ending
    exactly at Function MemorySSA LiveOnEntry. A live-on-entry path is accepted
    only when the source pointer dominates its capture and another relevant
    path has a real store. The pass inserts a snapshot load in that path-local
    capture, avoiding both an unsound zero and a speculative load on unrelated
    destinations. A v2 proof and backward-compatible outer manifest bind each
    state kind, source identity, chain, and content fingerprint. Independent
    replay now also checks actual dispatch-PHI values, closing a gap where
    correct metadata could previously accompany a modified tuple operand.
    A three-exit regression agrees on 256 inputs and produces a cross-initial
    Backsolver witness; kind tampering and a verifier-clean, freshly sealed
    constant-incoming tamper are rejected. Nested/cyclic MemoryPhi,
    byte-lane overlap, symbolic aliases, aggregates, atomics, and cross-function
    memory remain outside this bounded proof.
97. **Bounded proof-carrying nested-MemoryPhi continuation state.** A relevant
    dispatch incoming may now recursively traverse a non-dispatch MemoryPhi,
    provided every phi dominates its downstream use point, has two to four
    unique incoming blocks, and the unfolded provenance remains acyclic with
    at most four phis and sixteen nodes per exit. Every edge retains the
    eight-definition NoMod bound and terminates at an equal-width MustAlias
    store or path-local LiveOnEntry snapshot. The lowering constructs scalar
    PHIs bottom-up in the corresponding merge blocks. A v3 manifest records a
    canonical preorder tree with source, NoMod, incoming-block, and child
    identities; the independent verifier recomputes its structural fingerprint,
    while sealed LLVM replay recursively checks the actual nested and dispatch
    PHI incoming values. A two-level example agrees on 1024 executions and
    yields a constrained Backsolver witness; edge and freshly sealed value
    tampering are rejected, as are MayAlias and five-arm cases. The regression
    also repaired recursive region synthesis so concrete operand closures are
    hoisted in dependency order before symbolic short-circuit computations.
    This bounded tree is an unfolding of an acyclic MemorySSA DAG, not shared
    DAG compression, loop-carried/cyclic memory, byte-lane composition,
    symbolic-alias partitioning, or general heap summarization.
98. **Bounded multi-break exit-priority loop circuits.** The F261/F266
    recurrence may now be followed by two or three ordered post-update checks,
    each restricted to `iv == break_at[i]`, a unique break exit, and a false
    edge to the next check or header. Scanning sites in CFG order with strict
    `break_at[i] < winner_at`, initialized to `trip`, implements the exact
    priority rule: earliest iteration first, then lowest site ordinal on a
    tie. The selected `winner_at+1` drives the shared independent or
    upper-triangular closed form; a generated dispatch chain supplies
    phase-correct post-update state to only the winning break exit. A
    backward-compatible v2 exit artifact enumerates the complete bounded
    trip-by-break-vector table and binds ordered condition, exit, invariant
    value, state, and recurrence identities. Its independent verifier
    recomputes every lexicographic winner, execution count, phase mapping, and
    fingerprint. A two-break case agrees on 512 executions and yields a
    break1-specific solver witness; a three-break triangular case agrees on
    256 executions. Winner/site/live-out/hash tampering and four-break,
    non-equality, or reversed-edge cases are rejected. This is not arbitrary
    predicate-DAG, pre-update, multi-phase, side-effecting/memory, nested, or
    unbounded loop summarization.
99. **Proof-carrying unequal linear-arm Hydra alignment.** Each side of a
    selected Hydra diamond may now independently contain one to four linear
    blocks and at most 64 flattened instructions. The deterministic
    compatible-operation LCS therefore handles both unequal block counts and
    one-sided instruction edits while preserving cross-block SSA remapping.
    The backward-compatible v2 manifest records separate arm block/instruction
    counts, the alignment algorithm identity, every edit slot's stable
    instruction site, opcode and block ordinal, the one-sided edit distance,
    output PHIs, and a complete structure fingerprint. The independent
    verifier checks monotone per-arm order, complete unique-site consumption,
    matched-opcode agreement, edit distance, and fingerprint before a replay
    campaign starts. A 2x1 edit case and a maximal 1x4 cross-block def-use case
    each agree with original IR on all 256 byte values and pass symbolized LLVM
    verification; ordinal/edit tampering is rejected by both the direct
    verifier and campaign gate, while a fifth block and an internal branch tree
    fail closed. This is bounded linear-CFG alignment, not an independently
    reconstructed LLVM compatibility/LCS-optimality proof or arbitrary SESE
    melding; original-authoritative coverage replay remains mandatory.
100. **Path-predicate-owned internal-tree Hydra melding.** A selected arm may
    now be a bounded acyclic branch tree with at most seven blocks, three
    internal conditions, four common-merge leaf edges and 64 instructions per
    side. Unique-parent DFS topology excludes internal reconvergence. Root,
    child and leaf predicates are synthesized as explicit conjunctions of the
    outer choice and internal edge outcomes. Compatible-LCS operations consume
    guard-selected real or opcode-safe operands, while every merge PHI is
    rebuilt from the complete mutually exclusive leaf-guard set. Aggressive
    matched or extra stores preserve inactive-path memory through explicit
    readback and retain the mandatory original failure authority. A v3 proof
    binds branch terminator identities, parent/incoming edges, block/merge
    successors, leaves, alignment ordinals and a complete fingerprint; its
    independent verifier reconstructs tree indegrees, preorder and merge-edge
    coverage before campaign execution. Minimal 3x1, maximal 7x7/eight-leaf,
    and aggressive matched-store examples agree with original IR on all 256
    byte inputs and pass symbolized LLVM verification. Parent, leaf and
    successor tampering is rejected; a fourth internal condition, empty
    alignment and tree-local reconvergence fail closed. General acyclic SESE
    DAGs, internal PHIs, multiple merges, loops, calls and exception edges
    remain outside this proof, and independent sealed-bitcode reconstruction is
    still required.
101. **Unified sealed independent transformation replay.** Continuation, loop,
    and Hydra proofs now share a canonical
    `symcc-transformation-seal-v1` envelope. Each named manifest is first
    checked by its independent structural/algebraic verifier; ordered proof
    identities and file hashes are then bound to the original input IR, exact
    lowered textual IR, compiler plugin, LLVM tool, bounded LLVM version
    identity, and the replay configuration. Hydra site/mode, the available
    loop recurrence/exit evidence, and continuation memory-on/off are recovered
    from proof records rather than ambient process state. Seal publication is
    write-once under a POSIX lock with file and directory fsync plus atomic
    rename. Replay sanitizes all transformation environment variables and uses
    the sealed tool/plugin to run the full pass pipeline again from input IR.
    Regenerated manifests must independently verify and equal the original JSON
    records exactly; regenerated IR must be byte-identical by SHA-256 and pass
    LLVM verification. The seal and external artifacts are rechecked after
    replay. Positive memory-on/off continuation, recurrence+multi-break loop,
    and maximal internal-tree Hydra artifacts reproduce exactly. For each
    pipeline, verifier-clean lowered-IR semantic tampering remains detectable
    even after an attacker-like test generates a new internally valid seal,
    because the modified IR cannot be reconstructed from the bound input and
    pass configuration. This closes the local deterministic rewrite-evidence
    gap, not provenance authenticity: signatures, remote transparency logs,
    cross-version semantic equivalence, and public equal-CPU confirmation
    remain future research.
102. **Proof-carrying continuation byte-lane partial overlap.** When the
    equal-width continuation proof cannot explain a 2--8-byte integer load, a
    bounded reverse MemorySSA scan now assigns every address-order byte to its
    nearest overlapping simple store, with remaining bytes obtained from one
    path-local LiveOnEntry snapshot. Store/load addresses must share a pointer
    base with inbounds constant offsets; the scan is linear, bounded to sixteen
    definitions, and retains the eight-entry NoMod budget. DataLayout
    endianness determines both source extraction and destination positioning.
    The v4 artifact binds byte width, endianness, skipped sites and every
    lane's source kind/site/byte/width. Its structural verifier checks canonical
    coverage and fingerprint, while independent LLVM replay redoes
    MemorySSA/AA and parses the actual extract, shift and OR instruction chain.
    Little-endian overwrite plus initial-memory composition agrees on all 256
    selectors and produces a cross-continuation model; a big-endian module
    checks the inverse bit order. Lane tampering and verifier-clean source-store
    tampering after fresh sealing are rejected. Dynamic offsets, uncertain
    aliases, MemoryPhi/cycles, volatile/atomic accesses, region intrinsics,
    aggregates and cross-function memory remain outside this bounded result.
103. **Guarded two-arm symbolic alias partitions.** One byte-lane path may now
    contain one simple store addressed by an LLVM pointer select. Its
    instruction-valued condition identifies two mutually exclusive and
    complete arms; each arm must independently reduce to an inbounds
    same-base constant overlap or be AA-proven NoAlias. A byte affected by
    exactly one arm becomes a guarded overlay on the older store or
    LiveOnEntry source discovered by the F275 scan. Lowering extracts both i8
    values and uses the original guard and polarity before endian-aware
    placement. The v5 artifact binds guard, polarity, store and source-byte
    identities, and independent replay checks both arm classification and the
    actual select operands. A 1024-input differential example, sealed replay
    and cross-continuation model pass; record and verifier-clean selected-value
    tampering fail. Unknown alias arms, multiple guarded writes, argument-only
    guards without stable site identity, pointer PHIs, symbolic offsets and
    cyclic memory remain fail closed. This is a finite two-case partition, not
    general symbolic points-to analysis.
104. **Proof-carrying cyclic byte-lane continuation values.** A canonical
    two-input loop MemoryPhi can now become a real integer fixed point instead
    of forcing continuation memory recovery to fail. The unique entry edge is
    outside the header dominance region and uses the F275 byte proof; the
    unique latch is dominated by the header and scans back to the same
    MemoryPhi. Exact partial stores replace covered lanes and all remaining
    lanes carry the corresponding byte of the cycle PHI. The transfer must
    contain both kinds. Lowering creates the recursive PHI in the original
    header and emits an endian-aware latch extract/shift/OR circuit. V6 binds
    header/entry/latch sites, both NoMod chains, entry sources and backedge
    store/carry lanes; independent replay checks actual PHI incoming blocks,
    self-reference and expression sources. Little-endian 1024-input
    differential execution, big-endian mapping, two sealed replay paths and
    Backsolver model pass; carry/value tampering fails. Conditional and
    multiple latches, nested cycles, guarded or symbolic-offset backedge
    stores, full-width selection, aggregates and general heap loops remain
    outside this canonical subset.
105. **Bounded multi-level finite pointer unions.** One store pointer may now
    be a complete depth-two select tree with at most three unique internal
    decisions and four leaves. Each leaf independently proves an exact
    same-base interval or AA NoAlias effect for every load lane; each lane
    retains a fallback leaf so older provenance remains observable. V7 binds
    preorder node/child identities and all leaf source vectors. Lowering and
    independent replay recursively build and parse the actual i8 select tree.
    A maximal-tree 1024-input differential test, sealed replay and model pass;
    depth three, unknown leaves, shared select DAG and a second partition store
    fail closed.
106. **MemorySSA-ordered guarded-write priority.** Two single-level guarded
    stores can now affect one path. Reverse MemorySSA assigns newest-first
    priorities, while lowering applies oldest-first so the newest select is the
    outer expression and wins when both guards hold. V8 records sparse per-lane
    priority chains and replay peels actual selects from newest to oldest
    before checking the base source. Four guard combinations, sealed replay,
    solver model and priority/value tampering are covered. Three writers,
    pointer-tree writers, cross-PHI priority and loop-conditional writes remain
    outside the bound.
107. **Proof-carrying conditional cyclic byte-lane transfer.** A canonical
    F277 cycle may now contain one strict write/carry diamond before its unique
    latch. The latch MemoryPhi must have exactly one incoming MemoryDef chain
    that reaches the header through exact partial stores and one incoming that
    directly references the same header MemoryPhi. Both arms have a common
    conditional predecessor, one predecessor each and unique unconditional
    edges to the latch; the i1 condition is an instruction with stable identity
    that dominates the merge. Lowering emits an endian-aware recursive integer
    PHI whose affected bytes are selected between the stored and carried byte
    with the original polarity. V9 binds branch, arm, guard and polarity sites
    in metadata, manifest and fingerprint. Independent compiler replay
    reconstructs MemorySSA/AA and parses the actual guarded byte expression;
    the structural verifier checks seven-site uniqueness and canonical carry.
    A 1024-input little-endian differential run, a big-endian false-polarity
    case, both sealed replay paths and a solver model pass. Polarity and
    freshly sealed selected-value tampering fail, as do argument guards, two
    writer arms, unknown ModRef, non-strict joins and multiple backedges.
    Multi-latch/nested cycles, pointer unions, symbolic offsets, aggregates,
    atomics and general loop-memory fixed points remain outside this subset.
108. **Proof-carrying two-latch cyclic byte-lane state.** A header MemoryPhi
    may now have exactly one external entry and two internal unconditional
    latches. Each latch independently follows a bounded MemoryDef chain to the
    same header phi and must prove both exact partial-store lanes and canonical
    self-carry lanes. Lowering preserves LLVM predecessor semantics with an
    actual three-input integer PHI and emits a separate endian-aware transfer
    in each latch, rather than imposing an artificial writer order. V10 binds
    header/entry identity and two ordered backedge site/NoMod/lane records.
    Independent replay checks the actual three-input recursive PHI and both
    expression trees. Alternating low/high writes agree over 1024 executions;
    sealed replay and a solver model pass, while transfer-record and freshly
    sealed value tampering fail. Three latches, a full-width transfer on either
    latch and a nested conditional latch remain fail closed. This result does
    not cover arbitrary latch counts, irreducible loops, nested cycles,
    symbolic addressing, aggregates, atomics or general heap fixed points.
109. **Pointer-union and guarded-writer priority composition.** One newest
    depth-two finite pointer-union store may now follow one older single-level
    guarded store on the same bounded MemorySSA path. The base byte is rebuilt
    first, the older guard is applied next, and that result becomes every
    fallback leaf of the newer pointer tree; an overlap leaf therefore has
    true last-writer priority. V11 binds both the older guard/polarity/store
    source and the newer canonical node/child/leaf tree. Independent compiler
    replay redoes MemorySSA/AA, checks the actual guarded fallback select and
    recursively parses the outer partition expression; the structural verifier
    recomputes ownership, lane coverage and fingerprint. A maximal-tree
    1024-input differential test, symbolized verification, both sealed replay
    paths and a cross-continuation model pass. Guard-polarity and freshly
    sealed selected-value tampering fail, while reversed writer order and two
    older guarded writers retain the original load. This is a fixed
    one-union/one-guard bound, not general points-to, multi-writer or heap-graph
    reasoning.
110. **Conditional two-latch cyclic byte-lane fixed points.** The actual
    three-input header MemoryPhi used by F281 may now combine ordinary and
    strict conditional backedge transfers. Each of the two latches
    independently proves a partial-store/self-carry relation; a conditional
    transfer additionally requires a two-arm write/carry MemoryPhi selected by
    one instruction-valued i1 guard, and at least one transfer must be
    conditional. Lowering retains the predecessor-selected recursive integer
    PHI and emits the guarded byte update only in the owning latch, avoiding
    any artificial cross-latch priority. V12 records a transfer-kind tag and,
    for each conditional latch, its branch, write arm, carry arm, guard and
    polarity. Independent compiler replay redoes MemorySSA/AA and parses the
    actual three-input PHI plus both ordinary or guarded byte expressions.
    The structural verifier requires disjoint CFG topology but permits one
    dominating comparison to guard both latches, matching LLVM data-flow
    semantics. A mixed conditional/unconditional little-endian example agrees
    over 1024 executions and passes symbolized verification, both sealed replay
    paths and a cross-continuation model. A big-endian example uses two
    conditional latches, opposite polarities and a shared guard. Polarity and
    freshly sealed value tampering fail; argument-only guards, two writing
    arms and unknown ModRef effects retain the original load. This remains an
    exact two-latch, strict-diamond, constant-offset integer-byte subset, not
    arbitrary-predicate, arbitrary-latch, nested/irreducible or general-heap
    loop summarization.
111. **Bounded 2--4-latch predecessor-selected memory fixed points.** A loop
    header may now carry one external entry and two through four internal
    backedges. Each latch independently proves an ordinary partial-store/
    self-carry transfer or the strict conditional relation from item 110.
    Lowering emits an actual three- through five-input recursive integer PHI
    and materializes every transfer in its own predecessor; it never linearizes
    mutually exclusive latches into a writer-priority select chain. V13 is
    selected only when at least one relevant state has three or four latches,
    while another state in that slot may contain two through four. Every
    transfer has an explicit conditional-presence tag, including
    all-unconditional states. Independent replay checks the N-input PHI,
    incoming block identities and every ordinary/guarded byte expression; the
    structural verifier enforces bounds, canonical ordinals, CFG ownership,
    lane completeness, the expanded-state requirement and fingerprint. A
    four-latch mixed example agrees over 2560 executions and passes symbolized
    verification, both sealed replay paths and a Backsolver model. A
    three-latch all-unconditional big-endian proof also replays, while a fifth
    latch retains the original load. Ordinal and freshly sealed expression
    tampering fail. This is a proof budget, not an arbitrary-latch,
    nested-predicate, symbolic-address or general-heap fixed point.
112. **Bounded nested-predicate cyclic transfer trees.** A backedge transfer
    may now be selected by a unique-parent full binary tree with up to three
    internal `br i1` nodes and four leaves. Every leaf independently proves
    either pure self-carry or a same-base, constant-offset partial-store/carry
    relation back to the header MemoryPhi, and at least one leaf writes.
    Lowering materializes a complete integer in each original leaf and creates
    an actual predecessor-selected leaf PHI in the latch. It therefore avoids
    eager guard conjunction and preserves path-local LLVM evaluation while
    allowing different leaves to update different byte lanes. V14 tags each
    header transfer as unconditional, conditional or predicate-tree and binds
    preorder node/child topology, canonical leaves, NoMod chains and all lane
    sources. Compiler replay verifies the actual nested PHI expressions; the
    independent verifier checks a unique root, unit indegree, forward child
    references, topology ownership, lane completeness and fingerprint. A
    maximal little-endian tree agrees over 5120 executions and passes
    symbolized verification, Backsolver and both sealed replay paths. A
    big-endian two-latch case combines a three-leaf tree with an ordinary
    transfer; a fifth leaf is rejected. Topology and freshly sealed value
    tampering fail. This remains a three-node/four-leaf reducible-tree budget,
    not shared predicate DAG, arbitrary CFG, symbolic-address or general-heap
    loop summarization.
113. **Bounded ordered pointer/guard writer graphs.** Linear continuation
    MemorySSA paths now retain a single newest-first sequence of up to four
    conditional writers: at most two depth-two finite pointer partitions and
    two single-level guarded stores. Each guard layer binds per-byte source and
    polarity; each partition layer binds its complete select tree. Lanes hidden
    by newer unconditional definitions are masked from older layers. Lowering
    applies the sequence oldest-to-newest, making the most recent definition
    the outermost actual select; replay peels that expression in the opposite
    direction. V15 binds layer ordinals, kinds, writer identities, lane maps,
    partition topology and fingerprint, while v7/v8/v11 remain canonical for
    their older shapes. A maximum guard/partition/guard/partition example
    agrees for all 64 writer-condition combinations across three continuation
    selectors and passes symbolized verification, Backsolver, both sealed
    replay paths and tamper rejection. A big-endian two-partition proof also
    replays; a third partition is rejected. This is bounded constant-offset
    byte-lane recovery, not general points-to analysis, symbolic arrays,
    pointer-PHI or heap fixed-point reasoning.
114. **Bounded acyclic SESE-DAG control-flow predication.** The Hydra pass now
    has a separate v4 path for arms with real internal reconvergence rather
    than treating every region as a unique-parent tree. It uses the nearest
    common post-dominator as the outer merge, rejects external predecessors
    and cycles, and deterministically topologically orders up to ten blocks,
    four internal branches, eight outer-merge leaves and three local merges per
    arm. Each block guard is the disjunction of exact incoming-edge guards.
    Local PHIs are then folded into selects in topological order before the
    existing predicated LCS alignment handles ALU or aggressive readback-memory
    operations. V4 records complete predecessor/successor topology, local
    merge/PHI identities, alignment, outputs and a fingerprint; the independent
    verifier derives predecessor sets from successors and matches every local
    PHI site to an LLVM PHI opcode. A two-left/one-right local-merge example
    and an aggressive same-address store example agree with their original
    CFGs for all 256 byte inputs and pass symbolization and sealed replay.
    External-predecessor, cyclic and fourth-local-merge cases fail closed.
    This realizes a bounded reducible subset of Hydra-style compile-time state
    merging, not arbitrary control-flow melding, exception regions or a public
    equal-CPU performance result.
115. **Profile-bound and concurrent-safe transformation evidence.** Hydra
    profiles now have a v2 form that seals the resolved profiling command,
    stdin/file input mode, producing executable SHA-256 and command SHA-256.
    The text profile carries those identities into the compiler; malformed,
    duplicate, unreadable and valid-empty profiles fail closed instead of
    enabling an implicit site. Compiler records identify implicit, explicit,
    v1 and v2 selection, and v2 records bind the profile, executable, command
    and selected statistics. Replay-v2 independently verifies the profile
    artifact and requires the original authority command to match it exactly
    before executing inputs. The four compiler manifest producers also share
    one `flock`/`O_APPEND`/write-all/`fsync` publisher. Twenty-four concurrent
    compiler processes produce 24 complete independently verified records;
    replacing the original command is rejected even after resealing the outer
    campaign. V1 evidence remains compatible but cannot claim binary identity.
    This is local cooperative integrity, not a digital signature,
    append-only transparency service, network-filesystem proof or cross-host
    artifact transport.
116. **Signed Merkle-logged cross-host transformation bundles.** An F274 seal
    and every artifact role it names can now be packaged into a bounded ZIP64
    transport. A canonical descriptor binds roles, names, modes, sizes,
    content-addressed blobs and the embedded seal. OpenSSL Ed25519 signs it
    under an artifact-specific domain; verification accepts only an
    externally supplied public-key trust root. A separate log key signs each
    tree head. The append-only local log uses RFC 9162-style `0x00` leaf and
    `0x01` internal-node hashing, verifies all prior roots and signatures
    under a lock, and returns an offline inclusion path for the new leaf.
    Eight concurrent publishers obtain continuous indices and every bundle
    cross-checks against the log. Remote import rejects unexpected ZIP
    members, oversized or digest-mismatched blobs, invalid embedded seals,
    signature/proof/key/log rewrites and existing destinations before an
    fsync/rename publication. This enables independent verification after
    arbitrary external copying to a host without shared storage. It is not a
    public CT service, HSM or key-rotation system, trusted timestamp,
    reproducible-build proof, or gossip/checkpoint defense against rollback
    to a historically valid log prefix.
117. **Explicit LLVM refinement contracts and cross-major exact replay.**
    Hydra records now bind the actual LLVM version, a
    `poison`/`undef`/`freeze` refinement policy, inactive-operand safe constants,
    an exception-region rejection policy, and recomputable left/right/paired/
    extra freeze counts. One alignment slot preserves one dynamically executed
    freeze; different slots remain independent. A one-sided freeze is guarded
    by the outer path predicate. Inactive div/rem divisors become one and other
    operands become zero, preventing predication from introducing immediate
    divide-by-zero behavior. Invoke, EH-pad and non-branch-terminator regions
    remain untransformed. This contract follows the LLVM
    [undefined-behavior lattice](https://llvm.org/docs/UndefinedBehavior.html)
    and the LangRef definitions of
    [freeze](https://llvm.org/docs/LangRef.html#freeze-instruction),
    [select](https://llvm.org/docs/LangRef.html#select-instruction), and
    [branch](https://llvm.org/docs/LangRef.html#br-instruction).
    A new cross-major driver first independently replays the F274 sealed
    baseline, then runs an ABI-matched candidate plugin. It removes only LLVM
    version fields before requiring equal manifests and proof identities, and
    additionally requires candidate LLVM verification, candidate
    `llvm-diff`, and byte-identical textual IR. LLVM 18.1.3 and 17.0.6 pass the
    checked fixture. The same audit fixed QueryStore candidate admission:
    partial models and fixed/converter completions are now materialized only
    after replaying the complete Query IR prefix and target. This is a
    fixture-specific exact structural replay result. It is not a theorem for
    arbitrary IR, exception semantics, or unsupported LLVM releases; although
    LLVM [21.1.0](https://releases.llvm.org/21.1.0/docs/ReleaseNotes.html) is
    available, the project's declared matrix remains LLVM 8--18 and only
    17/18 were exercised here.
118. **Exact symbolic-index fixed heap-region writer graphs.** Continuation
    MemorySSA can now summarize a dynamic byte-address store when its base is
    one externally declared address-space-0 `malloc`/`calloc` with a constant
    extent, the read window is a fixed two- through eight-byte integer, and
    the store uses one pointer-width `inbounds gep i8` index. For load lane
    `l` and store source byte `s`, the compiler emits only
    `index = load_offset + l - s - writer_base_offset`; it does not enumerate
    the index domain. These equality/select layers compose newest-first with
    constant, guarded and finite-partition writers, while lowering applies
    them oldest-to-newest. V16 binds allocator identity, extent, index identity
    and width, signed offsets/cases, lane sources and up to eight writer
    layers. Compiler replay checks the actual `icmp/select` expression and
    MemorySSA/AA again; the independent verifier checks signed ranges,
    canonical case order and the same fingerprint. Malloc differential tests,
    a real Backsolver model, calloc with a nonzero window base, big-endian
    byte extraction, fail-closed unknown regions and an LLVM 17/18 exact replay
    certificate pass. This is a bounded single-object linear-memory result,
    not symbolic-base points-to analysis, pointer-PHI/strided array reasoning,
    realloc/lifetime semantics, cyclic region fixed points, general heap
    manipulation, or a public equal-CPU coverage result.
119. **Fixed-window symbolic-region loop recurrence.** A single-entry,
    single-unconditional-backedge loop can now carry a fixed integer read
    window through one symbolic-index heap write. Each address-order byte is a
    scalar PHI component:
    `X[t+1,l] = ite(index[t] = load_offset + l - source_byte - base_offset,
    byte(value[t], source_byte), X[t,l])`. This avoids trip-count unrolling
    while preserving every concrete iteration; IR size is bounded by load
    width times store width. V17 combines cycle topology, exact entry bytes,
    canonical all-carry lanes and the F291 allocator/index/case record.
    Compiler replay checks the actual PHI/equality/select recurrence and the
    independent verifier recomputes its signed fingerprint. Allocator
    recognition now also requires a non-variadic standard prototype whose
    size arguments match the DataLayout pointer-index width. Differential,
    Backsolver, big-endian, tamper, fail-closed and LLVM 17/18 exact-replay
    evidence passes. The v17 artifact itself is scalarized fixed-window
    reasoning, not an SMT array summary, general heap invariant,
    conditional/multi-latch or multi-writer loop model; F294's separate v18
    proof adds only the bounded conditional/multi-latch subset described
    below. Neither result is public performance evidence.
120. **Bounded shared-predicate CFG-DAG guard hash-consing.** Hydra candidate
    selection now preserves the v4 proof and lowered IR for regions inside its
    old budget, then tries a v5 SESE DAG with at most 14 blocks, five internal
    branches, ten merge-leaf edges, four local merges, twelve local PHIs and
    96 instructions per arm. V5 interns each edge guard by predecessor ordinal
    and successor index, so reachability, local-PHI predication and final-leaf
    reconstruction share one SSA expression. Both arms also share negations
    of an identical lowered predicate. The v5 manifest binds ordered predicate
    identities, the canonical guard graph, unique/reused counts, cache hits and
    the full v4 topology; the independent verifier recomputes these values and
    requires the record to genuinely exceed the v4 proof domain. A 13-block
    shared-predicate fixture passes all 256 concrete inputs, symbolization,
    seal/replay, tamper rejection and exact LLVM 17/18 replay; a 16-block,
    five-merge region fails closed. This is region-local common-subexpression
    elimination under a bounded acyclic proof, not multi-region/global
    hash-consing, critical-edge or switch/invoke/EH lowering, arbitrary CFG
    semantics, dynamic profitability proof, or public equal-CPU evidence.
121. **Conditional multi-latch symbolic-region fixed-window transfer.**
    Continuation MemorySSA now combines F292's symbolic-index byte recurrence
    with 2--4 mutually exclusive loop latches. A latch contains at most one
    fixed-region writer; it may be unconditional or guarded by one branch.
    The inner equality/select chain applies an overlap byte, while an outer
    guard/select carries the prior PHI byte when the store arm is not taken.
    Latches remain alternative MemoryPhi inputs and are never serialized as if
    multiple backedges executed in one iteration. V18 binds a per-transfer
    writer-presence bit, allocator/extent, store/index identity, signed cases,
    guard site/polarity and actual PHI/select/icmp structure. Compiler replay
    re-runs MemorySSA/AA; the independent verifier enforces all-carry symbolic
    bases and recomputes the presence-aware fingerprint. A two-latch
    conditional/unconditional fixture agrees for 126 executions and reaches a
    real Backsolver target; a PowerPC64 i16 fixture reaches the four-latch
    bound. Same-latch double writers, dynamic extent and five latches fail
    closed. LLVM 17/18 exact replay agrees on proof identity
    `2389278380394154034`, lowered IR and normalized manifest. This is not
    same-latch ordered multi-writer or nested-predicate transfer, symbolic
    base/strided array reasoning, dynamic allocation, general heap/SMT-array
    semantics, or public equal-CPU performance evidence. Full gates pass
    202/202 LLVM 18 tests, 201 LLVM 17 tests with one unsupported driver, and
    471/471 Python tests.
122. **Dominance-proven cross-selected-region guard hash-consing.** An
    explicit 2--4-site Hydra mode now prevalidates a same-function chain of
    v5 DAG regions before editing IR. Region ownership must be disjoint, the
    listed entries must form a dominance chain, and all regions must share at
    least one original SSA predicate identity. A function-scoped cache stores
    each predicate negation and its producer site. Reuse in a later region is
    permitted only when a freshly rebuilt LLVM DominatorTree proves that the
    cached instruction dominates the later insertion point; per-edge guards
    remain region-local. V6 binds ordered sites, common predicate identities,
    transaction ordinal, actual cross-region hit count and preceding producer
    sites on top of the complete v5 topology proof. The independent verifier
    checks both each structure fingerprint and the complete ordered JSONL
    transaction. Unified seal/replay now reconstructs the exact site array.
    Two serial 13-block/four-merge regions reduce eight independent predicate
    negations to five, agree for all 256 byte inputs, and pass tamper,
    symbolization and exact LLVM 17/18 replay. Reverse dominance, duplicate or
    missing sites, and a same-function pair without a common predicate fail
    closed. This is not cross-function or arbitrary-expression CSE,
    non-dominating/overlapping region melding, automatic multi-site
    profitability selection, or a public equal-CPU result. Full gates pass
    203/203 LLVM 18 tests, 202 LLVM 17 tests with one expected unsupported
    cross-major driver, and 471/471 Python tests.
123. **Same-latch ordered symbolic-region multiwriter fixed point.** A
    single unconditional latch may now summarize two through four relevant
    symbolic-index stores to one fixed heap allocation/window. MemorySSA
    traversal records ordinal zero as the newest definition. Lowering applies
    older writers first and wraps newer equality/select cases outside them, so
    the resulting lane expression implements last-writer-wins and ultimately
    falls through to the previous cycle-PHI byte. V19 binds writer count and
    ordered store/base/extent/index/signed-case records. Compiler replay peels
    the actual expression newest first; the independent verifier requires
    canonical ordinals, distinct stores, a shared region, all-carry base lanes,
    v17/v19 field exclusion, and recomputes the complete fingerprint. A
    three-writer little-endian fixture agrees for 105 executions and reaches a
    real Backsolver target; a two-writer PowerPC64 fixture verifies big-endian
    cases. Five writers and an unknown interposed ModRef fail closed. LLVM
    17/18 v16--v19 compatibility passes 12/12, and exact cross-major replay
    agrees on proof identity `3118488025292340061`, lowered IR, and normalized
    manifest. This is not nested/conditional predicate composition,
    multiwriter-per-latch multi-latch transfer, symbolic-base/general-heap
    reasoning, or public equal-CPU performance evidence. Full gates pass
    206/206 LLVM 18 tests, 205 LLVM 17 tests with one expected unsupported
    cross-major driver, and 471/471 Python tests.

The remaining work has two distinct layers. The six-arm empirical framework now
exists and its coverage/CPU join is executable, but the 20-repeat public
confirmatory campaign has not been run. The semantic
gap remains arbitrary-operation enabled sets and an unbounded Optimal-DPOR
proof, interpreter-level dynamic ConDPOR transition generation and consistency
proof, full C11/C++ executable semantics, and symbolic-base/general-heap/
nested-predicate symbolic region,
exception/external-state continuation lowering. Additional
engineering research remains around
non-shared-filesystem consensus/CAS, general exception semantics and LLVM
19--21 adaptation, cross-function/non-dominating structural reuse, critical-
edge/switch CFG lowering, general loop/memory region synthesis, global/full-DFSan
UCSan semantics, general effect models, richer UCSan object grammars, and calibrated
neural arithmetic proposal models. Grammar research still requires native
incremental GLR/GLL complete-forest parsing, multi-slot/arbitrary-ancestor
relation models, arbitrary-distance/bidirectional and cross-parent long-range factors,
execution of the sealed five-level public campaign, higher-power online
multiple-testing ablations, holdout-stable grammar convergence beyond the
implemented sample-conditioned symbol/production correspondence, and a
native/incremental GLR/GLL backend if its
measured cost justifies the additional implementation.

## Evaluation protocol

Use at least 10 independent repetitions for tuning and 20 for final comparisons, identical initial
corpora and CPU budgets, and randomized policy seeds recorded in each result. Generate the final
matrix with `benchmark/research_protocol.py`; confirmatory manifests below 20 repeats or with
unequal budgets are rejected. Report edge and bitmap-feature coverage over time, unique path
fingerprints, solver SAT/timeout ratios, generated-to-accepted funnel, worker utilization, coverage
per CPU-hour, failures, and right-censored target times. Compare AFL++ alone, the previous hybrid
baseline, feature ablations, interactions, and the full system. `benchmark/analyze_ablation.py`
performs paired bootstrap intervals, sign-flip randomization, A12/Cliff's delta, and Holm correction.
