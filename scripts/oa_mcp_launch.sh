#!/usr/bin/env bash
# oa_mcp_launch.sh - shared launcher for OpenAlgo's upstream MCP server.
# Resolves the OpenAlgo API key (Fernet-encrypted in the app DB) by deriving
# the app's own KDF (PBKDF2 over API_KEY_PEPPER + FERNET_SALT), then execs
#   python3 mcp/mcpserver.py <api_key> <host>
# Used by every FCC agent as the `openalgo` MCP server command.
# No Flask app boot; reads only .env + sqlite.
set -eu
OA_DIR="${OPENALGO_DIR:-/home/ubuntu/openalgo}"
HOST="${OPENALGO_URL:-http://127.0.0.1:5000}"
PY="$OA_DIR/.venv/bin/python"
[ -x "$PY" ] || PY=python3

KEY=$("$PY" - <<'PYEOF'
import os, sqlite3, sys
from pathlib import Path

OA = Path("/home/ubuntu/openalgo")

# --- load the two KDF inputs from .env (values never printed) ---
env = {}
for line in (OA / ".env").read_text().splitlines():
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip("'\"")

pepper = os.environ.get("API_KEY_PEPPER") or env.get("API_KEY_PEPPER")
salt_hex = os.environ.get("FERNET_SALT") or env.get("FERNET_SALT")
if not pepper or not salt_hex:
    sys.exit("NO_KDF_INPUTS")

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64

kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                 salt=bytes.fromhex(salt_hex), iterations=100000)
fernet = Fernet(base64.urlsafe_b64encode(kdf.derive(pepper.encode())))

# --- locate the app DB from DATABASE_URL (default db/openalgo.db) ---
db_url = env.get("DATABASE_URL", "sqlite:///db/openalgo.db")
if db_url.startswith("sqlite:///"):
    rel = db_url[len("sqlite:///"):]
    db_path = rel if rel.startswith("/") else str(OA / rel)
else:
    db_path = str(OA / "db" / "openalgo.db")

conn = sqlite3.connect(db_path)
row = conn.execute(
    "SELECT api_key_encrypted FROM api_keys "
    "WHERE api_key_encrypted IS NOT NULL AND api_key_encrypted != '' "
    "ORDER BY id LIMIT 1"
).fetchone()
conn.close()
if not row:
    sys.exit("NO_KEY_ROW")
try:
    print(fernet.decrypt(row[0].encode()).decode())
except Exception:
    sys.exit("DECRYPT_FAILED")
PYEOF
) || { echo "oa_mcp_launch: could not resolve the OpenAlgo API key" >&2; exit 1; }

cd "$OA_DIR"
exec "$PY" mcp/mcpserver.py "$KEY" "$HOST"
