# F362 Single-Snapshot Pytest Manifest Evidence

This directory records the manifest-admission hardening introduced by F362.
The production reader now consumes at most `limit + 1` bytes from one open
descriptor before UTF-8/JSON parsing, rejects duplicate JSON object members,
and forces pytest third-party plugin autoload isolation even if the inherited
environment variable exists but is empty.

## Recorded Evidence

- `run_adversarial_manifest_checks.py`: executable counterexample driver using
  the production reader and gate;
- `adversarial-cases.json`: machine-readable results for an understated
  pre-read `stat`, duplicate `count` members, and an empty plugin-isolation
  environment variable;
- `adversarial-cases.log`: driver result;
- `inventory-rebuild.log`: production inventory rebuilt with the isolation
  variable initially empty; the 787-node output remains byte-identical;
- `directed-tests.log`: the existing nine node identities with expanded F362
  assertions;
- `full-gate.json` and `full-gate.log`: complete capability and identity gate,
  also started with an empty isolation variable;
- `static-checks.log`: Ruff, `py_compile`, actionlint, whitespace, hash, and
  delivery checks.

## Interpretation

The size counterexample uses a valid 226-byte manifest, sets the production
limit to 32 bytes, and replaces `Path.stat()` with a forged one-byte result.
The reader performs zero path-stat calls and rejects after reading 33 bytes
from one descriptor. The duplicate-member case returns 2 before pytest starts
and records `collected=0`. The empty-environment case executes one test and the
gate records `plugin_autoload_disabled=true`.

`SHA256SUMS.txt` covers every regular file in this directory except itself.
These are local correctness and reproducibility results. They are not a
GitHub-hosted run, a hostile-filesystem proof, a digital signature, a solver or
coverage campaign, a public benchmark, LAVA-M, or a performance result.
