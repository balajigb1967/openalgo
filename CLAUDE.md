@AGENTS.md

# OpenAlgo — Claude-specific notes

- AGENTS.md (imported above) is the authority for architecture, branch
  discipline and the FNO-custom map. Follow it exactly.
- `main` is upstream-pristine: read-only for you. Custom work happens on
  `fno-capture` or `feat/*` branches.
- When asked to "sync upstream": fetch upstream, fast-forward `main` only,
  never merge custom work into `main`.
