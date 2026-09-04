# F302 Online EVP MPI Evidence

This directory records one bounded mechanism run on 2026-08-04. It is not a
coverage or throughput benchmark.

## Configuration

- topology: one MPI master and one SymCC/QSYM worker;
- seed: one byte `0x01`;
- target: `test/empirical_value_profile.c` compiled by the current LLVM 18
  SymCC build;
- oracle: AFL++ `afl-showmap` over an independently AFL-instrumented copy;
- runtime: 8 seconds, stopped by external `timeout` (`124` is expected);
- online controls: publish interval 0, minimum observations 1, maximum
  distinct values 2; unrelated TACE, string, component, self-configuration and
  SMT-algorithm adaptation disabled.

## Result

The coordinator accepted 17 telemetry records and published three semantic
generations. The worker loaded empirical domains four times and performed four
domain attempts/queries: one SAT model was validated and three empirical UNSAT
results fell back. Once the rolling domain exceeded two values, generation 3
was the explicit `profile_count 0` tombstone. The peer queue retained three
interesting inputs. There were no parse, validation, publication or context
errors.

See `summary.json` for the compact result, `state.json` for bounded coordinator
state, `generations/` for the three sealed artifacts, `current.runtime` for the
withdrawal sidecar, and `mpi.log` for the process log.

## Interpretation

This proves collection -> aggregation -> versioned MPI transport -> worker
installation -> QSYM consumption -> telemetry -> withdrawal in a real process
topology. The short single-target run does not establish lower solver time,
higher coverage, scalability or superiority over a baseline.
