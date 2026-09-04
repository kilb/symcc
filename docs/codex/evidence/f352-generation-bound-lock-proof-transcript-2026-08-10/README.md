# F352 Generation-Bound Lock-Proof Transcript Evidence

This directory preserves local production-mechanism evidence for the
generation-bound cluster-lock proof transcript, the all-master transcript
commit phase, and strict renewal-result admission.

## Reproduction

From the repository root:

```bash
python3 docs/codex/evidence/f352-generation-bound-lock-proof-transcript-2026-08-10/run_generation_bound_transcript_integration.py
```

The driver invokes the production local capability probe and
`qualify_mpi_cluster_advisory_lock()`. It uses real directories, regular files,
`openat`, `flock`, and namespace-identity closure. A thread message bus replaces
MPI transport, while synthetic processor names activate the multi-processor
path. Only the transcript calculation is fault-injected for one participant;
the commit and rejection paths remain production code.

The byte-identical JSON and log preserve ten exact checks. They cover one
normal `M=3`, `H=2`, generation-7 proof, one participant's transcript
divergence, a previous-generation replay, a zero-round result splice, a
foreign-root capability splice, and field-sensitivity of the canonical
transcript. The three test logs preserve the directed, six-module, and complete
warnings-as-errors Python results. `SHA256SUMS.txt` covers every evidence file
except itself.

## Interpretation

The normal production result commits the same SHA-256 transcript on all three
participants. The transcript binds the epoch, generation, ordered members,
processor representatives, holder rounds, contention checks, release checks,
and namespace-identity checks. Renewal completion separately requires the
result capability to equal the controller's startup capability and, in the
production path, requires the ordered result ranks to equal the configured
master ranks.

This is fail-closed integrity evidence for trusted framework components. The
transcript is an unkeyed digest, not a signature or MAC. It detects accidental
or fault-injected replay, splicing, and disagreement; it does not provide
Byzantine authentication against a malicious master that can fabricate all
inputs. The experiment does not run `mpirun`, another physical host, NFS,
Lustre, CephFS, server failover, a network partition, a target, a solver, or a
fuzzing campaign. It makes no throughput, coverage, bug-discovery, or LAVA-M
uplift claim.

