# Architecture_QA3 figure set

This directory contains the reproducible figures used by
`docs/Architecture_QA3.md`.

## Scope

- 25 named figures are exported as both SVG and PNG.
- Architecture, bitmap, data-flow, decision, and path diagrams are rendered from
  Graphviz DOT sources under `src/`.
- Quantitative figures are generated directly from the values reported in the
  document tables by `../render_qa3_diagrams.py`.
- The old ASCII blocks are not treated as canonical figures. Commands, source
  excerpts, SMT-LIB, telemetry, and raw experimental logs remain code blocks.

## Rendering

Prerequisites:

```bash
sudo apt-get install graphviz
```

Chrome is used only to rasterize the programmatically generated SVG charts:

```bash
python3 docs/diagrams/render_qa3_diagrams.py
python3 docs/diagrams/update_qa3_figure_links.py
```

The first command regenerates every SVG, PNG, and DOT file. The second command
idempotently replaces the named ASCII figure blocks in the Markdown document.

## Visual semantics

The palette is consistent across the set:

| Color | Meaning |
|---|---|
| Blue | AFL execution, instrumentation, or coverage measurement |
| Teal | MPI helper state, nominal solving, or accepted local state |
| Orange | Concolic execution, optimistic behavior, or a cautionary branch |
| Green | Successful delivery, accepted output, or validated progress |
| Red | Failed delivery, unsound/unreachable risk, or invalid measurement |
| Violet | Optional or opt-in subsystem |

Every quantitative chart includes its measurement scope and caveat in the image
itself so that an exported slide does not silently lose the report's
methodological qualification.
