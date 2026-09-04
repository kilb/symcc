# F455 evidence bundle

This directory records the 2026-08-26 mechanism acceptance evidence for the
structured, reactive agentic concolic control loop.

## Contents

- `oracle.json` and `oracle.stdout.json`: exact online/shadow/fallback
  fixed-backend oracle output;
- `focused-tests.log`: structured protocol, persistence, identity, budget and
  backend tests (`28 passed + 6 subtests`);
- `coupled-tests.log`: the focused set plus the full MPI AFL triage regression
  file (`88 passed + 43 subtests`);
- `full-python-gate.log`, `.json`, `.time.txt`: capability-closed repository
  gate (`1517 passed + 323 subtests`, 16/16 capabilities, exact node IDs);
- `static-checks.txt`: Python compile/lint, diff, SVG/XML and oracle checks;
- `environment.txt`: execution environment and tool versions;
- `source-research.txt`: primary papers/artifacts and pinned public HEADs;
- `source-manifest.txt`: SHA-256 identities of implementation, tests and docs;
- `review.txt`: twelve review rounds and the corrections they produced;
- `SHA256SUMS.txt`: hashes for every evidence file except itself.

## Measured mechanism result

Each arm evaluated four tasks with the production-default reactive gate. One
productive task was suppressed and three solver-barrier tasks triggered in all
arms. Online and shadow each completed three schema-valid backend requests;
fallback made zero requests. Online admitted three candidate actions. Shadow
observed the same three proposed candidates but admitted none. Conservative
input/output token accounting was 999/354 in online and shadow despite a
backend declaration of zero.

The exact triggered task set contained three entries with digest
`3b87996b341910d17f3c515d6ae94fba59ed5faced4186685e0dc7bf0e033a51`.
The MPI triage regression gave authoritative coverage deltas 1 then 0 when two
workers reported the same edge.

## Claim boundary

The backend is a fixed local command, not a language model. The result proves
schema enforcement, reactive triggering, resource accounting, persistence,
real-execution feedback binding and ablation isolation. It does not establish
LLM quality, coverage improvement, solver speedup, defect yield, or reproduce
the performance claims of Cottontail, HyLLfuzz, ConcoLixir or Agolic.

Primary report:
`../../research-progress/Structured_Agentic_Concolic_Closed_Loop_F455_2026-08-26.md`.
