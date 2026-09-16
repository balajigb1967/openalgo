# OpenAlgo — Agent Playbook (FNO customization line)

You may be Claude, Codex, OpenCode, Grok, DSH or Pi — the rules are identical for
all agents. The **main** branch of this repository is a **pristine mirror of
upstream `marketcalls/openalgo`**. NEVER commit to `main`.

## Golden rules

1. **Branch discipline.** `main` = upstream, fast-forward only. All custom work
   goes on `fno-capture` (or a new `feat/*` branch off it), then pushes to
   `origin` (github.com/balajigb1967/openalgo). Never push to upstream.
2. **Read before write.** Understand the surrounding code first; match OpenAlgo's
   existing style (Flask blueprints, per-broker plugins under `broker/`,
   REST under `blueprints/api_v1`).
3. **Broker-agnostic core.** Exchange/broker logic belongs in the broker plugin
   (`broker/fyers/...` etc.), never hardcoded in shared services.
4. **IST market hours.** NSE/BSE 09:15–15:30 IST; MCX 09:00–23:30 IST.
   Time-based logic must use Asia/Kolkata.
5. **Verify before claiming done.** Run `python -c "import ast; ast.parse(open('<file>').read())"`
   for any Python you touch. Frontend TS/TSX: `npm run build` in `frontend/`
   (only where node is available — the VM has no node; leave dist to upstream CI).
6. **Commits.** Conventional, one concern per commit
   (`fix(fyers): ...`, `feat(trading): ...`). Never mix generated `frontend/dist`
   with source changes.

## Architecture map

- `app.py` — Flask app factory entry; socketio; blueprint registration
- `blueprints/` — web + REST API (`/api/v1/*` broker-agnostic endpoints)
- `broker/<name>/` — one plugin per broker: `api/auth.py`, `api/data.py`
  (history/quotes/depth), `api/order_api.py`, mapping via `database/symbol_convert.py`
- `services/` — business layer (`history_service.py`, `order_service.py`, ...)
- `database/` — symtoken master-contract DB (`token_db.py`), app DB, session mgmt
- `websocket_proxy/` — broker WebSocket adapters → unified `/ws` stream (port 8765)
- `frontend/` — React+TS (Vite); built output is `frontend/dist` (built by upstream CI)
- `strategies/` — user strategy container; `upgrade/` — DB migrations

## FNO-custom additions (fno-capture line)

- `broker/fyers/api/data.py` — FNO underlying→active-contract auto-resolution and
  clear "symbol not found" errors
- `services/history_service.py` — actionable message when a contract has no
  intraday data (illiquid/far-month) but daily exists
- `blueprints/tv_watchlist.py`, `services/tv_watchlist_service.py`,
  `database/tv_watchlist_db.py` — TradingView watchlist sync plugin
- `frontend/src/components/trading/MarketDepthPanelContainer.tsx` + `Trading.tsx`
  wiring — Market Depth panel
- `utils/constants.py` — FOREX exchange constant

## Known data reality (do not "fix" these)

- Fyers serves **no intraday candles** for zero-volume contracts (COTTON, KAPAS,
  MCXBULLDEX, MCXMETLDEX, far-month NICKEL). Daily candles still work.
  The app now returns a clear 404 message for these — correct behavior.
- MCX near-month contracts move (gold Sep→Oct etc.); code must resolve
  active contracts dynamically, never hardcode expiry months.

## Useful verification commands (on the VM, ~/openalgo)

- App control: `bash ~/oa_control.sh {start|stop|restart|status}`
- Health: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5000/`
- History probe (replace KEY): see `docs/` or ask the user for the OpenAlgo API key
- Logs: `tail -40 log/server.log`

## MCP tool: openalgo (read-only)

Every FCC agent has an `openalgo` MCP server registered exposing:
`openalgo_status`, `openalgo_git`, `openalgo_logs`, `openalgo_history`,
`openalgo_health`. Use these instead of ad-hoc shell probing whenever possible.
