#!/usr/bin/env bash
# oa_mcp_launch.sh - shared launcher for OpenAlgo's upstream MCP server.
# Reads the API key from the app DB (no secrets in agent configs) and execs
#   python3 mcp/mcpserver.py <api_key> <host>
# Used by every FCC agent as the `openalgo` MCP server command.
set -eu
OA_DIR="${OPENALGO_DIR:-/home/ubuntu/openalgo}"
HOST="${OPENALGO_URL:-http://127.0.0.1:5000}"
PY="$OA_DIR/.venv/bin/python"
[ -x "$PY" ] || PY=python3

KEY=$(python3 - <<'PYEOF'
import sqlite3, glob, os
oa = os.environ.get("OPENALGO_DIR", "/home/ubuntu/openalgo")
for db in sorted(glob.glob(os.path.join(oa, "db", "*.db"))):
    try:
        c = sqlite3.connect(db)
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "api_keys" in tables:
            row = c.execute("SELECT api_key FROM api_keys LIMIT 1").fetchone()
            c.close()
            if row and row[0]:
                print(row[0]); raise SystemExit
        c.close()
    except Exception:
        continue
raise SystemExit("NO_KEY")
PYEOF
) || { echo "oa_mcp_launch: no OpenAlgo API key found in $OA_DIR/db/*.db" >&2; exit 1; }

cd "$OA_DIR"
exec "$PY" mcp/mcpserver.py "$KEY" "$HOST"
