# SymCC-Parallel — Distributed Concolic Execution & Hybrid Fuzzing

This repository is a fork of [**SymCC**](https://github.com/eurecom-s3/symcc)
(compiler-based *concolic* execution) extended into a **parallel, multi-core
vulnerability-finding toolkit**:

- 🧩 **Concolic execution** — a compiler pass injects symbolic tracking into your
  program at build time; at run time it solves branch conditions to generate new
  inputs that reach new code (SymCC + QSYM/Z3 backend).
- ⚡ **MPI parallelism** — a master/worker driver spreads concolic execution
  across as many CPU cores (or machines) as you have.
- 🔀 **Hybrid fuzzing** — combines [AFL++](https://github.com/AFLplusplus/AFLplusplus)
  coverage-guided fuzzing with SymCC concolic execution, with adaptive core
  allocation between the two.
- 📊 **Benchmark harness** — one command builds targets, runs every mode, and
  measures edge coverage so you can reproduce results.

> **CPU-only.** No GPU is required or used. More CPU cores ⇒ more parallelism.

---

## ⏱️ TL;DR — get it running in three commands

On a fresh **Ubuntu 22.04 / 24.04** machine:

```bash
git clone --recursive https://github.com/kilb/symcc.git
cd symcc
./setup.sh                       # installs all dependencies + builds everything (~10 min)
```

Then try it:

```bash
source .venv/bin/activate                                  # activate the Python env
python benchmark/run_benchmark.py --targets maze --np-list 1,4 \
       --rounds 1 --timeout 30 --no-public                 # runs a quick benchmark
```

That's it. The rest of this document explains each piece in detail — **you do not
need any prior knowledge of symbolic execution, MPI, or fuzzing to follow it.**

---

## Table of contents

1. [What this project does](#1-what-this-project-does)
2. [Requirements](#2-requirements)
3. [Installation](#3-installation)
4. [Verifying the install](#4-verifying-the-install)
5. [Running — four modes](#5-running--four-modes)
   - [Mode A — Compile & run one program](#mode-a--compile--run-one-program-the-basics)
   - [Mode B — Concolic loop on one core](#mode-b--concolic-loop-on-one-core)
   - [Mode C — MPI parallel concolic execution](#mode-c--mpi-parallel-concolic-execution)
   - [Mode D — Hybrid AFL++ + SymCC fuzzing](#mode-d--hybrid-afl--symcc-fuzzing)
6. [One-command benchmark](#6-one-command-benchmark)
7. [Environment-variable reference](#7-environment-variable-reference)
8. [Troubleshooting](#8-troubleshooting)
9. [Repository layout](#9-repository-layout)
10. [Packaging for distribution (no git)](#10-packaging-for-distribution-no-git)
11. [Further documentation](#11-further-documentation)
12. [Upstream, license & citation](#12-upstream-license--citation)

---

## 1. What this project does

Imagine you have a program that parses some input (a file format, a network
packet, etc.) and you want to find inputs that reach deep, hard-to-hit code
paths or trigger bugs.

- A plain **fuzzer** (like AFL++) mutates inputs randomly and keeps the ones that
  reach new code. It's fast but gets stuck on "magic" checks like
  `if (x == 0xDEADBEEF)`.
- A **concolic executor** (SymCC) runs the program on one concrete input while
  recording the exact mathematical conditions on each branch, then asks an SMT
  solver (Z3): *"what input would flip this branch?"* It's precise but slower.

**Hybrid fuzzing** runs both together: the fuzzer explores broadly and fast,
while concolic execution solves the hard checks the fuzzer can't. This project
makes that hybrid approach **run in parallel across all your CPU cores** via MPI,
plus a number of solver/scheduler optimizations on top of stock SymCC.

You end up with three things you can build and run:

| Artifact | What it is |
|----------|-----------|
| `build/symcc`, `build/sym++` | Drop-in replacements for `clang` / `clang++` that build concolic execution into your program. |
| `util/mpi_concolic_execution.py` | Runs pure concolic execution in parallel across cores (MPI). |
| `util/mpi_fuzzing_helper.py` | Runs the AFL++ + SymCC hybrid in parallel across cores (MPI). |

---

## 2. Requirements

**Operating system.** Ubuntu 22.04 or 24.04 (or another Debian-based distro).
`setup.sh` installs dependencies via `apt`. On other systems, install the
equivalents by hand (see [manual install](#manual-installation) below) and run
`./setup.sh --skip-apt`.

**Hardware.** Any x86-64 machine. Everything runs on CPU; the more cores you
have, the more parallel workers you can launch.

**Software** — all installed automatically by `setup.sh`:

| Dependency | Version | Used for |
|------------|---------|----------|
| clang / LLVM | 8–18 (18 recommended & tested) | the SymCC compiler pass |
| Z3 | ≥ 4.5 (`libz3-dev`) | the SMT solver backend |
| CMake | ≥ 3.16 | build system |
| Ninja | any | build system |
| OpenMPI + `mpi4py` | 4.x / ≥ 3.1 | MPI parallel drivers |
| Python | ≥ 3.10 | drivers & benchmark harness |
| AFL++ | ≥ 4.0 | hybrid fuzzing (optional but recommended) |

> Only **`mpi4py`** is a third-party Python package (plus `lit`/`ruff` for tests
> and linting). Everything else in the drivers uses the Python standard library.

---

## 3. Installation

> **Got a tarball instead of git access?** If a colleague sent you a
> `symcc-package.tar.gz` (produced by [`package.sh`](#10-packaging-for-distribution-no-git)),
> just `tar xzf symcc-package.tar.gz && cd symcc-package` — then the steps below
> work **identically, with no git required** (the runtime source is bundled).

### The one-command way (recommended)

```bash
git clone --recursive https://github.com/kilb/symcc.git
cd symcc
./setup.sh
```

`setup.sh` is **idempotent** — safe to re-run any time. It will:

1. Install the system packages listed above (`apt`, needs `sudo`).
2. Install **AFL++** from source if it isn't already present (skipped if found).
3. Create a Python virtual environment at `./.venv` and install
   `requirements.txt`.
4. Pull the git submodules (SymCC runtime + QSYM backend).
5. Build SymCC (via `build.sh`).
6. Run a smoke test to confirm everything works.

Useful flags:

```bash
./setup.sh --check       # only report what's installed/missing — changes nothing
./setup.sh --skip-apt    # you already have the system deps (or have no sudo)
./setup.sh --skip-afl    # you only need pure/MPI concolic, not hybrid fuzzing
./setup.sh --venv PATH   # put the virtualenv somewhere other than ./.venv
```

If you already have all dependencies and only want to (re)compile SymCC:

```bash
./build.sh               # incremental build into ./build
./build.sh --clean       # wipe ./build and rebuild from scratch
```

### Manual installation

If you can't use `setup.sh`, install the dependencies and build by hand:

```bash
# 1. System packages (Ubuntu 24.04)
sudo apt-get update
sudo apt-get install -y build-essential git curl cmake ninja-build \
    clang-18 llvm-18-dev llvm-18-tools libz3-dev zlib1g-dev \
    python3 python3-venv python3-pip libopenmpi-dev openmpi-bin

# 2. Python environment
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Submodules
git submodule update --init --recursive

# 4. Build SymCC (system Z3 needs Z3_TRUST_SYSTEM_VERSION=ON)
cmake -G Ninja -DSYMCC_RT_BACKEND=qsym -DZ3_TRUST_SYSTEM_VERSION=ON \
      -DLLVM_DIR=/usr/lib/llvm-18/lib/cmake/llvm -S . -B build
ninja -C build

# 5. (Optional) AFL++ for hybrid fuzzing
git clone --depth 1 https://github.com/AFLplusplus/AFLplusplus.git
make -C AFLplusplus -j"$(nproc)" all && sudo make -C AFLplusplus install
```

---

## 4. Verifying the install

```bash
./setup.sh --check
```

This prints a checklist of every dependency and whether `build/symcc` exists.
The build's own smoke test compiles a small program, runs it under SymCC, and
confirms it generates a new test case — you'll see `冒烟测试通过` (Chinese for
"smoke test passed") at the end of a successful `./setup.sh` or `./build.sh`.

---

## 5. Running — four modes

Pick the mode that matches what you want. **Remember to activate the environment
in every new shell:**

```bash
source .venv/bin/activate
```

### Mode A — Compile & run one program (the basics)

`build/symcc` is a drop-in replacement for `clang`. Compile any C program with
it and symbolic tracking is built in. Save this as `test.c`:

```c
#include <stdio.h>
#include <stdint.h>
#include <unistd.h>
int main(void) {
    int x;
    if (read(STDIN_FILENO, &x, sizeof(x)) != sizeof(x)) return -1;
    if (x == 0xCAFE) printf("secret path!\n");   // hard for a fuzzer, easy for concolic
    else             printf("normal path\n");
    return 0;
}
```

```bash
build/symcc test.c -o test               # compile with concolic execution built in
mkdir -p results
export SYMCC_OUTPUT_DIR="$PWD/results"    # where new inputs are written
echo 'aaaa' | ./test                      # run once; SymCC solves the branch on x
ls results/                               # -> a new input that makes x == 0xCAFE
```

SymCC treats data read from **stdin** as symbolic by default. To make a **file**
symbolic instead, set `SYMCC_INPUT_FILE=/path/to/file`. C++ programs: use
`build/sym++` in place of `clang++`.

### Mode B — Concolic loop on one core

`util/pure_concolic_execution.sh` repeatedly feeds newly generated inputs back
into the target — a self-contained concolic exploration loop:

```bash
build/symcc benchmark/targets/maze.c -o /tmp/maze
mkdir -p /tmp/seeds && printf 'aaaaaaaaaaaaaaaa' > /tmp/seeds/seed   # 16-byte seed
util/pure_concolic_execution.sh -i /tmp/seeds -o /tmp/out -- /tmp/maze @@
```

`@@` is replaced with the current input file. New inputs accumulate in `/tmp/out`.

### Mode C — MPI parallel concolic execution

The same idea as Mode B, but spread across many cores using MPI. The `-np N`
argument to `mpirun` sets the total number of MPI processes; the driver
automatically splits them into masters and workers.

```bash
build/symcc benchmark/targets/maze.c -o /tmp/maze
mkdir -p /tmp/seeds && printf 'aaaaaaaaaaaaaaaa' > /tmp/seeds/seed

mpirun -np 8 python3 util/mpi_concolic_execution.py \
    -i /tmp/seeds -o /tmp/out -t 15 --wall-timeout 60 -- /tmp/maze @@
```

> New inputs accumulate in `/tmp/out` as they are found. This example stops
> itself after 60s (`--wall-timeout 60`, with `-t 15` capping each execution);
> the first solved inputs take ~10–20s to appear, then throughput climbs to
> thousands per second. Omit the timeouts to run until no new input appears for
> `--max-idle` seconds (default 60).

Key options (`python3 util/mpi_concolic_execution.py --help`):

| Option | Meaning |
|--------|---------|
| `-i DIR` | directory of initial seed inputs (**required**) |
| `-o DIR` | where to store all generated test cases |
| `-t SEC` | timeout per SymCC execution (default 90) |
| `--wall-timeout SEC` | total wall-clock budget (0 = unlimited) |
| `--max-idle SEC` | stop after this long with no new inputs (default 60) |
| `-- TARGET [ARGS]` | the program to analyze; use `@@` for the input file, or omit it to feed via stdin |

> Rule of thumb: use `-np` up to your core count (`nproc`). The driver
> auto-scales the number of masters (~1 master per 90 workers).

### Mode D — Hybrid AFL++ + SymCC fuzzing

This is the most powerful mode: AFL++ and SymCC run **together**, sharing a
corpus. The **easiest and fully-wired way to run it is through the benchmark
harness** (next section) with `--hybrid`, which handles building the AFL and
SymCC binaries, launching everything, and cleaning up:

```bash
python benchmark/run_benchmark.py --hybrid --hybrid-adaptive \
    --targets maze --np-list 8 --rounds 1 --timeout 120 --no-public
```

Under the hood this uses `util/mpi_fuzzing_helper.py` (the MPI SymCC⨉AFL driver).
If you want to drive it directly, its interface is:

```bash
mpirun -np <N> python3 util/mpi_fuzzing_helper.py \
    -a <afl-fuzzer-name> -o <afl-output-dir> -n <symcc-instance-name> \
    -- <target> [args]
```

but note it expects an AFL++ session laid out the way `run_benchmark.py`'s
hybrid path sets it up (persistent-mode binaries, a running `afl-fuzz` main
node, matching cmplog companions). Reading
[`benchmark/run_benchmark.py`](benchmark/run_benchmark.py) (`run_hybrid`) is the
reference for a correct manual setup. **If in doubt, use the harness.**

---

## 6. One-command benchmark

The benchmark harness is the single best way to see the whole system working and
to reproduce results. It builds the synthetic targets, runs the selected modes,
measures **edge coverage**, and writes a report.

Quick run (a couple of minutes):

```bash
python benchmark/run_benchmark.py \
    --targets maze,deep_branches --np-list 1,4 \
    --rounds 1 --timeout 30 --no-public
```

Full default run (serial + MPI at 1/2/4/8 procs, 3 rounds, all synthetic
targets):

```bash
python benchmark/run_benchmark.py
```

Results are written to `benchmark_results/` (override with `--output DIR`):

- `benchmark_data.csv` / `benchmark_data.json` — raw per-run measurements
- `benchmark_report.txt` — human-readable summary (coverage, speedup, etc.)

Built-in synthetic targets: **`maze`**, **`parser`**, **`deep_branches`**,
**`crypto_check`** (source in `benchmark/targets/`). Useful flags:

| Flag | Effect |
|------|--------|
| `--targets a,b` | only run these targets |
| `--np-list 1,4,8` | MPI process counts to sweep |
| `--rounds N` | repeat each config N times (averages out noise) |
| `--timeout SEC` | per-run time budget |
| `--hybrid` `--hybrid-adaptive` | run the AFL++⨉SymCC hybrid with adaptive core split |
| `--afl-only` | AFL++ baseline only (for comparison) |
| `--no-serial` / `--no-mpi` / `--no-public` | skip a category of runs |
| `--skip-build` | reuse already-built targets |
| `--simulation` | dry-run wiring without real solving |

Run `python benchmark/run_benchmark.py --help` for the complete list.

### Real-world (public) benchmark targets

To benchmark on real software (LAVA-M, libarchive, pcre2, sqlite, …) instead of
the synthetic targets:

```bash
benchmark/setup_public_benchmarks.sh --all      # download & build target suites
benchmark/make_afl_targets_persistent.sh        # relink for fast persistent mode
python benchmark/run_benchmark.py               # public targets are auto-discovered
```

---

## 7. Environment-variable reference

You rarely need to set these by hand — the drivers and the benchmark harness set
them for you. They're documented here for tuning and for running the binaries
directly.

### Core SymCC (run time)

| Variable | Default | Meaning |
|----------|---------|---------|
| `SYMCC_OUTPUT_DIR` | `/tmp/output` | directory for newly generated test cases |
| `SYMCC_INPUT_FILE` | *(stdin)* | treat this file's contents as symbolic instead of stdin |
| `SYMCC_NO_SYMBOLIC_INPUT` | `0` | `1` = run like an uninstrumented program (no solving) |
| `SYMCC_ENABLE_LINEARIZATION` | `0` | `1` = QSYM basic-block pruning; recommended for fuzzing (the fuzzing helper enables it automatically) |
| `SYMCC_AFL_COVERAGE_MAP` | *(empty)* | path to a coverage map to skip already-covered paths |

(Full list, including compile-time options, in
[`docs/Configuration.txt`](docs/Configuration.txt).)

### This fork's optimizations (opt-in tuning)

| Variable | Meaning |
|----------|---------|
| `SYMCC_FAST_SOLVE` | enable the Fuzzy-Sat fast path for simple byte comparisons |
| `SYMCC_MULTI_SOLVE` | solve consecutive/related branch groups together |
| `SYMCC_EXPR_CACHE_SIZE` | expression hash-cons cache size (default 65536) |
| `SYMCC_KSCHED` | enable K-Scheduler rarity-weighted frontier scheduling |
| `SYMCC_TIMEOUT` | per-execution solver/exec timeout (seconds) |
| `SYMCC_BRANCH_SHARE` | share timed-out branches across workers (BSFuzz) |
| `SYMCC_FOCUS_BYTES` | restrict symbolization to specific input byte offsets |
| `SYMCC_DENSITY_OUT` / `SYMCC_DENSITY_BALANCE` | branch-density profiling & balancing |

### AFL++ (hybrid mode)

| Variable | Meaning |
|----------|---------|
| `AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1` | **often required on desktop Linux** — see Troubleshooting |
| `AFL_SKIP_CPUFREQ=1` | skip the CPU-frequency governor check |

---

## 8. Troubleshooting

**`afl-fuzz` aborts with "Pipe at the beginning of core_pattern" / missing
crashes.** Desktop Ubuntu routes core dumps to the `apport` crash handler via a
piped `core_pattern`, which AFL++ refuses by default. Either tell AFL to ignore
it (the harness does this automatically):

```bash
export AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1
export AFL_SKIP_CPUFREQ=1
```

or fix the system setting (needs `sudo`, resets on reboot):

```bash
echo core | sudo tee /proc/sys/kernel/core_pattern
```

**`afl-fuzz` aborts with "Fork server handshake failed"** in hybrid mode. The
main binary and its cmplog companion must have **matching persistence** (both
persistent or both fork-mode). Rebuild the persistent variants with
`benchmark/make_afl_targets_persistent.sh`.

**CMake can't find LLVM.** Point it at your install:
`export LLVM_DIR=/usr/lib/llvm-18/lib/cmake/llvm` and re-run `./build.sh` (or
pass `-DLLVM_DIR=...` to `cmake`).

**`pip install mpi4py` fails.** Install the OpenMPI development headers first:
`sudo apt-get install -y libopenmpi-dev openmpi-bin`, then re-run
`pip install -r requirements.txt`.

**SymCC generates no new test cases for some input.** This can be normal — the
QSYM backend prunes paths for speed and isn't exhaustive (see the FAQ in
[`docs/SymCC_Upstream_README.md`](docs/SymCC_Upstream_README.md)). Try a
different seed, or a target with reachable branches on the symbolic input.

**All input must be available up front.** Because of how the QSYM backend works,
when feeding symbolic data on stdin interactively you must terminate input with
`Ctrl+D` before the program executes.

---

## 9. Repository layout

```
symcc/
├── README.md                 ← you are here (project getting-started guide)
├── setup.sh                  ← one-command dependency install + build
├── build.sh                  ← build SymCC only (deps already present)
├── package.sh                ← make a self-contained tarball for offline (no-git) distribution
├── requirements.txt          ← Python dependencies
├── CMakeLists.txt            ← top-level build (compiler pass)
├── compiler/                 ← the LLVM compiler pass (injects symbolic tracking)
├── runtime/                  ← SymCC runtime support library (git submodule)
│   └── src/backends/qsym/qsym/  ← QSYM/Z3 solving backend (nested submodule)
├── util/
│   ├── pure_concolic_execution.sh    ← single-core concolic loop (Mode B)
│   ├── mpi_concolic_execution.py     ← MPI parallel concolic driver (Mode C)
│   ├── mpi_fuzzing_helper.py         ← MPI AFL++⨉SymCC hybrid driver (Mode D)
│   ├── grimoire_gen.py               ← GRIMOIRE structure-aware input generator
│   └── symcc_fuzzing_helper/         ← original single-node SymCC+AFL helper (Rust)
├── benchmark/
│   ├── run_benchmark.py              ← the benchmark harness (start here)
│   ├── targets/                      ← synthetic benchmark programs
│   ├── setup_public_benchmarks.sh    ← download real-world target suites
│   ├── make_afl_targets_persistent.sh← relink targets for fast persistent mode
│   └── compile_public_benchmarks.sh  ← build the real-world targets
├── docs/                     ← design notes, reports, upstream README
└── build/                    ← build output: symcc, sym++, libsymcc.so
                                 (runtime libsymcc-rt.so lives in a nested subdir)
```

---

## 10. Packaging for distribution (no git)

To hand this project to someone **without giving them git access**, use
[`package.sh`](package.sh) — it produces one self-contained tarball that builds
and runs with no git required:

```bash
./package.sh              # -> symcc-package.tar.gz (tracked source + all submodules)
./package.sh --all        # also include uncommitted-but-not-ignored files (docs, PDFs)
```

Why not a plain `tar` or `git archive`? This project nests git submodules (the
runtime, QSYM, and Z3 sources). `git archive` silently drops them, and a naïve
`tar` of the directory would sweep in the 1.6 GB of downloaded benchmark targets,
a stale machine-specific `build/`, and temporary files. `package.sh` uses
`git ls-files --recurse-submodules` to include **all source (submodules included)
+ docs + scripts**, while excluding `.git`, `build/`, `.venv/`, `third_party/`,
the `benchmark/public/` downloads, and caches. It also drops two large vendored
blobs the build never uses — the QSYM submodule's bundled Intel **PIN 2.14**
distribution (~200 MB; SymCC compiles against a stub `pin.H`, not real PIN) and
the bundled **Z3 source** (~27 MB; the build links system Z3 via
`Z3_TRUST_SYSTEM_VERSION`). The result is **~15 MB** and self-verifies. Pass
`--keep-vendored` if you specifically need to build PIN/Z3 from source.

The recipient then needs **no git at all**:

```bash
tar xzf symcc-package.tar.gz
cd symcc-package
./setup.sh                # fresh machine: installs dependencies + builds
# ...or, if the dependencies are already present:
./build.sh
```

`setup.sh`/`build.sh` detect that the runtime source is already bundled and skip
the git-submodule step automatically. For subsequent development the recipient
can run `git init` in the extracted tree if they want version control.

---

## 11. Further documentation

- [`docs/Configuration.txt`](docs/Configuration.txt) — every SymCC configuration option (compile-time & run-time).
- [`docs/Fuzzing.txt`](docs/Fuzzing.txt) — combining SymCC with a fuzzer (background).
- [`docs/MPI_Parallelization.txt`](docs/MPI_Parallelization.txt) — the MPI architecture.
- [`docs/Parallel_Architecture_Report.md`](docs/Parallel_Architecture_Report.md) — parallel design deep-dive.
- [`docs/SymCC_Upstream_README.md`](docs/SymCC_Upstream_README.md) — the original upstream SymCC README (build details, FAQ, C++/32-bit support, Docker).

---

## 12. Upstream, license & citation

This project builds on **SymCC** by Sebastian Poeplau and Aurélien Francillon
(EURECOM). SymCC and this fork are distributed under the **GNU General Public
License v3** (the runtime under the LGPL). See
[`docs/SymCC_Upstream_README.md`](docs/SymCC_Upstream_README.md) for full license
notes and the list of components with additional copyrights.

To cite SymCC in academic work:

```bibtex
@inproceedings {poeplau2020symcc,
  author =    {Sebastian Poeplau and Aurélien Francillon},
  title =     {Symbolic execution with {SymCC}: Don't interpret, compile!},
  booktitle = {29th {USENIX} Security Symposium ({USENIX} Security 20)},
  pages =     {181--198},
  year =      2020,
  url =       {https://www.usenix.org/conference/usenixsecurity20/presentation/poeplau},
  publisher = {{USENIX} Association},
  month =     aug,
}
```
