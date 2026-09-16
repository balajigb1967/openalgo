# OpenAlgo — Grok-specific notes

Read `AGENTS.md` first — it is the authority for all agents. The condensed rules:

- `main` is a pristine upstream mirror: **never commit to it**. Work on
  `fno-capture` (or `feat/*` off it); push to origin = balajigb1967/openalgo.
- Broker logic lives in broker plugins (`broker/<name>/...`); shared services
  stay broker-agnostic. IST market hours (NSE/BSE 09:15–15:30, MCX 09:00–23:30).
- Fyers serves no intraday for zero-volume contracts (COTTON, KAPAS, MCXBULLDEX,
  MCXMETLDEX, far-month NICKEL) — that is data reality, not a bug.
- MCX near-months move; resolve active contracts dynamically, never hardcode.
- Verify python with `python -c "import ast; ast.parse(open('<file>').read())"`.
- Commits: conventional style, one concern per commit.

MCP servers registered for you: `openalgo` (trading + market data, upstream
mcp/mcpserver.py) and `openalgo-ops` (app ops: status/git/logs/health/history).
