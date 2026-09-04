# REQUIRES: qsym
# RUN: python3 %s %querysolver %S/../util/self_config.py

import json
from pathlib import Path
import subprocess
import sys


solver = Path(sys.argv[1])
self_config = Path(sys.argv[2])
sys.path.insert(0, str(self_config.parent))

from self_config import SelfConfiguringPolicy, discover_parameter_registry  # noqa: E402


raw = subprocess.run(
    [str(solver), "--print-parameters"],
    check=True,
    capture_output=True,
    text=True,
)
provider = json.loads(raw.stdout)
assert provider["schema"] == "symcc-parameter-provider-v1"
assert provider["provider"] == "symcc-query-solver"
assert len(provider["parameters"]) == 25

registry = discover_parameter_registry((), provider_commands=[[str(solver)]])
assert len(registry.specs) == 57
assert registry.errors == []
assert registry.conflicts == []
assert [item.name for item in registry.providers] == [
    "symcc-coordinator",
    "symcc-query-solver",
]
assert registry.specs["SYMCC_SOLVER_PSCACHE_CONFLICT_TIMEOUT"].active_when == {
    "SYMCC_SOLVER_PSCACHE": ("1",),
    "SYMCC_SOLVER_PSCACHE_CONFLICTS": ("1",),
}
assert registry.specs["SYMCC_SOLVER_PSCACHE_CONFLICT_TIMEOUT"].scope == (
    "query-service"
)

policy = SelfConfiguringPolicy(None, (), provider_commands=[[str(solver)]], seed=1)
assert policy.registry_parameter_count == 57
assert len(policy.parameters) == 28
assert "SYMCC_SELECTIVE_QUERY" not in policy.parameters
assert "SYMCC_SOLVER_PSCACHE" not in policy.parameters

cli = subprocess.run(
    [
        sys.executable,
        str(self_config),
        "--print-schema",
        "--provider-command",
        str(solver),
    ],
    check=True,
    capture_output=True,
    text=True,
)
payload = json.loads(cli.stdout)
assert len(payload["parameters"]) == 57
assert payload["provenance"]["errors"] == []
assert payload["provenance"]["conflicts"] == []
assert len(payload["provenance"]["providers"]) == 2
