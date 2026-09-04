# F359 Hermetic Pytest Discovery Evidence

This directory records the repository-root pytest discovery boundary introduced
by F359. The integration driver invokes the real pytest command in four modes:

1. default root discovery with `pytest.ini`;
2. a counterfactual invocation that clears `testpaths` from the command line;
3. an explicit path to the vendored QSYM test suite;
4. an explicit path to one SymCC-Parallel project test module.

The evidence distinguishes discovery isolation from test suppression. Default
root discovery must contain the project suite and exclude vendored QSYM. The
counterfactual must reproduce the six QSYM collection errors in the current
environment, while the explicit QSYM path must still enter that suite. The
current QSYM result is a transparent prerequisite failure because its
separately built `qsym`/PIN environment is absent; this directory does not claim
that the upstream QSYM native tests pass.

## Reproduction

```bash
PYTHONDONTWRITEBYTECODE=1 python3 \
  docs/codex/evidence/f359-hermetic-pytest-discovery-2026-08-10/\
run_pytest_discovery_integration.py
```

`pytest-discovery-integration.json` is the current-state snapshot. Replays may
legitimately report a larger project test count after new tests are added; the
stable contract is the 12 semantic checks, not a permanently frozen count.
`SHA256SUMS.txt` covers every regular file in this directory except itself.

This is test-infrastructure evidence. It does not run compiler lit tests, build
QSYM/PIN, execute a solver, run a fuzzing campaign, measure coverage, or support
performance, bug-finding, LAVA-M, MPI, multi-host, or SOTA claims.
