# F361 Canonical Pytest Inventory Evidence

This directory records the canonical pytest node-id inventory and exact
identity gate introduced by F361. The production collector disables automatic
third-party plugin loading, obtains node IDs from pytest's collection hook,
sorts and deduplicates them, and writes a versioned manifest with a canonical
SHA-256 digest. The production gate compares the complete observed identity set
with that committed manifest.

## Recorded Evidence

- `pytest-nodeids.snapshot.json`: exact snapshot of the committed 787-node
  manifest;
- `inventory-rebuild.log`: an independent production rebuild that is
  byte-identical to the committed file;
- `exact-collection-gate.json`: collect-only gate against the committed
  inventory;
- `equal-count-mutated-manifest.json`: a valid 787-node counterfactual that
  replaces one expected node ID with one nonexistent node ID;
- `equal-count-mutated-gate.json`: machine-readable rejection of that
  counterfactual as one missing plus one unexpected node ID;
- `equal-count-counterfactual.log`: exact and mutated production gate exits;
- `clean-venv-inventory-gate.json` and `clean-venv.log`: full collect-only
  identity check from the clean Python 3.12 environment created for F360;
- `full-gate.json` and `full-gate.log`: complete capability-closed execution;
- `directed-tests.log`: nine generator/gate contracts;
- `static-checks.log`: Ruff, `py_compile`, actionlint, and whitespace checks.

## Reproduction

Regenerate to a temporary path and compare before intentionally updating the
committed review boundary:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
  python3 util/python_test_inventory.py \
  --output /tmp/pytest-nodeids.json \
  --root test \
  --min-collected 778
cmp test/pytest-nodeids.json /tmp/pytest-nodeids.json
```

`SHA256SUMS.txt` covers every regular file in this directory except itself.
The node-list digest `18596f...33c00` binds canonical node-id lines; the outer
snapshot file has a different SHA-256 because it also contains schema, count,
root, JSON syntax, and the embedded digest.

This inventory detects test removal, rename, replacement, deselection, and
collection drift. It does not prove that an unchanged test body still asserts
the same behavior, and its digest is not a signature. This evidence does not
represent a GitHub-hosted run, LLVM lit or QSYM/PIN execution, an MPI campaign,
coverage measurement, a public benchmark, LAVA-M, or a performance result.
