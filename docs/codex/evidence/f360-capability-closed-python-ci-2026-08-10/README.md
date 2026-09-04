# F360 Capability-Closed Python CI Evidence

This directory records the capability-closed Python test gate introduced by
F360. The production gate is `util/python_test_gate.py`; it performs an explicit
command/module/library preflight, runs the repository-root pytest suite through
public pytest hooks, applies zero-degradation limits, and atomically writes a
machine-readable result.

## Recorded Evidence

- `python-test-gate.json`: complete local gate result for the CI capability set;
- `full-gate.log`: terminal output from the same 784-test run;
- `directed-tests.log`: six gate contract tests, including negative cases and
  repeated subtest-context accounting;
- `clean-venv.log`: package versions and smoke result from a fresh Python 3.12
  virtual environment installed only from the two requirements files;
- `clean-venv-gate.json`: machine-readable result from that clean environment;
- `static-checks.log`: Ruff, `py_compile`, actionlint 1.7.7, and whitespace
  validation results;
- `checks.txt`: bounded summary and explicit claim boundaries.

## Reproduction

Install the declared Python dependencies and the system capabilities listed in
`.github/workflows/run_tests.yml`, then run the exact `Run capability-closed
Python gate` command from that workflow. A smaller live smoke is:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 util/python_test_gate.py \
  --output /tmp/python-test-gate.json \
  --min-collected 2 \
  --require-command cc \
  --require-module pytest \
  --require-library z3 \
  -- -q -W error -p no:cacheprovider \
  test/test_pytest_discovery_contract.py
```

`SHA256SUMS.txt` covers every regular file in this directory except itself.
The snapshot proves the local production command and its declared Python
environment; actionlint proves workflow syntax and expression structure. It is
not a completed GitHub-hosted runner execution and does not run LLVM lit,
vendored QSYM/PIN tests, an MPI multi-process experiment, a solver campaign,
coverage measurement, a public benchmark, or LAVA-M.
