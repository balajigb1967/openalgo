---
name: openalgo
description: FNO Trader Pro user's OpenAlgo fork — project knowledge, live app ops and market-data access for the Pi coding agent.
---

# OpenAlgo — Pi skill

Use this skill whenever the user asks about the OpenAlgo project on this VM
(`/home/ubuntu/openalgo`), its market data, or asks you to work on it.

You are one of several interchangeable agents (claude, codex, opencode, grok,
dsh); results must not depend on which one runs. The shared playbook
`AGENTS.md` at the repo root (`/home/ubuntu/openalgo/AGENTS.md`) is the
authority — read it first and follow it exactly.

## Fast facts

- Fork: github.com/balajigb1967/openalgo — `main` = pristine upstream
  (never commit), custom line = `fno-capture` branch.
- App runs at 127.0.0.1:5000 (web) and 8765 (websocket).
- Control: `bash /home/ubuntu/oa_control.sh {start|stop|restart|status}`
- Logs: `tail -40 /home/ubuntu/openalgo/log/server.log`
- Health: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5000/`

## Live data without MCP (Pi has no MCP by design)

Fetch candles through OpenAlgo's REST API. The API key lives in the app DB:

```bash
KEY=$(python3 -c "
import sqlite3,glob
for db in sorted(glob.glob('/home/ubuntu/openalgo/db/*.db')):
    c=sqlite3.connect(db)
    ts={r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")}
    if 'api_keys' in ts:
        row=c.execute('SELECT api_key FROM api_keys LIMIT 1').fetchone()
        if row and row[0]: print(row[0]); break
")
curl -s -X POST http://127.0.0.1:5000/api/v1/history \
  -H 'Content-Type: application/json' \
  -d "{\"apikey\":\"$KEY\",\"symbol\":\"CRUDEOIL\",\"exchange\":\"MCX\",\"interval\":\"1m\",\"start_date\":\"$(date -d '-2 days' +%F)\",\"end_date\":\"$(date +%F)\"}"
```

## Data reality (do not "fix")

- Fyers has no intraday candles for zero-volume contracts (COTTON, KAPAS,
  MCXBULLDEX, MCXMETLDEX, far-month NICKEL); daily works. The app returns a
  clear 404 message — that is correct behavior.
- MCX near-months move; resolve active contracts dynamically, never hardcode.
