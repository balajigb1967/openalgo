#!/usr/bin/env python3
"""Standalone read-only MCP server for the OpenAlgo app on this VM.

Gives every FCC agent (claude, codex, opencode, grok, dsh) identical live
access to the local OpenAlgo instance:

  tools:
    openalgo_status   - app process/port state
    openalgo_git      - whitelisted git inspection (status/log/diff/branch)
    openalgo_logs     - tail log/server.log
    openalgo_health   - HTTP health of 127.0.0.1:5000
    openalgo_history  - fetch candles via OpenAlgo's own /api/v1/history

  resources:
    openalgo://file/<relative-path> - read repo files (path-traversal guarded)

Register with (stdio):
  fcc-claude mcp add openalgo -- python3 /home/ubuntu/openalgo/mcp_openalgo_server.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

OA_DIR = Path(os.environ.get("OPENALGO_DIR", "/home/ubuntu/openalgo"))
APP_PORT = int(os.environ.get("OPENALGO_PORT", "5000"))
WS_PORT = int(os.environ.get("OPENALGO_WS_PORT", "8765"))

# ---------------------------------------------------------------- utilities


def sh(args, cwd=None, timeout=15):
    try:
        out = subprocess.run(
            args, cwd=str(cwd or OA_DIR), capture_output=True, text=True,
            timeout=timeout,
        )
        return (out.stdout + out.stderr).strip() or "(no output)"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


def _safe_repo_path(rel):
    root = OA_DIR.resolve()
    p = (root / rel.lstrip("/")).resolve()
    if root != p and root not in p.parents:
        return None
    return p


def _find_api_key():
    """Decrypt the OpenAlgo API key from its DB using the app's own KDF
    (PBKDF2 over API_KEY_PEPPER + FERNET_SALT from .env). Values are never
    printed or logged."""
    try:
        env = {}
        for line in (OA_DIR / ".env").read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("\"'")
        pepper = os.environ.get("API_KEY_PEPPER") or env.get("API_KEY_PEPPER")
        salt_hex = os.environ.get("FERNET_SALT") or env.get("FERNET_SALT")
        if not pepper or not salt_hex:
            return None
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        import base64
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                         salt=bytes.fromhex(salt_hex), iterations=100000)
        fernet = Fernet(base64.urlsafe_b64encode(kdf.derive(pepper.encode())))
        db_url = env.get("DATABASE_URL", "sqlite:///db/openalgo.db")
        if db_url.startswith("sqlite:///"):
            rel = db_url[len("sqlite:///"):]
            db_path = rel if rel.startswith("/") else str(OA_DIR / rel)
        else:
            db_path = str(OA_DIR / "db" / "openalgo.db")
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT api_key_encrypted FROM api_keys "
            "WHERE api_key_encrypted IS NOT NULL AND api_key_encrypted != '' "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            return fernet.decrypt(row[0].encode()).decode()
    except Exception:  # noqa: BLE001, S110
        return None
    return None


# ------------------------------------------------------------------- tools


def tool_status(_args):
    procs = sh(["bash", "-c", "pgrep -af 'app[.]py' | head -3"])
    ports = sh(
        ["bash", "-c",
         f"ss -tln | grep -E ':{APP_PORT} |:{WS_PORT} ' || echo 'no listeners'"]
    )
    return {"processes": procs, "ports": ports,
            "dir": str(OA_DIR)}


def tool_git(args):
    sub = args.get("subcommand", "status")
    allowed = {
        "status": ["git", "status", "--short", "--", "."],
        "log": ["git", "log", "--oneline", "-15"],
        "branch": ["git", "branch", "-v"],
        "diff": ["git", "diff", "--stat"],
    }
    if sub not in allowed:
        return {"error": f"subcommand must be one of {sorted(allowed)}"}
    return {"output": sh(allowed[sub], timeout=20)}


def tool_logs(args):
    n = min(int(args.get("lines", 60)), 200)
    log = OA_DIR / "log" / "server.log"
    if not log.exists():
        return {"output": "(no log file yet)"}
    lines = log.read_text(errors="replace").splitlines()[-n:]
    return {"output": "\n".join(lines)}


def tool_health(_args):
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{APP_PORT}/", timeout=5
        ) as resp:
            return {"http_code": resp.status, "app": "up"}
    except Exception as exc:  # noqa: BLE001
        return {"http_code": None, "app": "down", "error": str(exc)}


def tool_history(args):
    symbol = args.get("symbol")
    exchange = args.get("exchange", "MCX")
    interval = args.get("interval", "1m")
    days = min(int(args.get("days", 1)), 7)
    if not symbol:
        return {"error": "symbol is required (e.g. CRUDEOIL, GOLDM, NATURALGAS)"}
    api_key = _find_api_key()
    if not api_key:
        return {"error": "no OpenAlgo API key found in db/*.db"}
    from datetime import date, timedelta
    end = date.today()
    start = end - timedelta(days=days)
    payload = {
        "apikey": api_key, "symbol": symbol, "exchange": exchange,
        "interval": interval,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{APP_PORT}/api/v1/history",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:400]
        return {"error": f"HTTP {exc.code}: {body}"}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    candles = data.get("data") or []
    return {
        "status": data.get("status"),
        "candles": len(candles) if isinstance(candles, list) else candles,
        "preview": (candles[:2] if isinstance(candles, list) else None),
    }


TOOLS = {
    "openalgo_status": tool_status,
    "openalgo_git": tool_git,
    "openalgo_logs": tool_logs,
    "openalgo_health": tool_health,
    "openalgo_history": tool_history,
}

TOOL_SPECS = [
    {"name": "openalgo_status", "description": "OpenAlgo app process and port state on this VM",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "openalgo_git", "description": "Whitelisted git inspection of the openalgo repo (status|log|branch|diff)",
     "inputSchema": {"type": "object", "properties": {"subcommand": {"type": "string", "enum": ["status", "log", "branch", "diff"]}}}},
    {"name": "openalgo_logs", "description": "Tail log/server.log of the OpenAlgo app",
     "inputSchema": {"type": "object", "properties": {"lines": {"type": "integer"}}}},
    {"name": "openalgo_health", "description": "HTTP health check of the OpenAlgo web app (127.0.0.1:5000)",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "openalgo_history", "description": "Fetch OHLC candles via OpenAlgo REST (/api/v1/history). Args: symbol, exchange, interval, days",
     "inputSchema": {"type": "object",
                     "properties": {"symbol": {"type": "string"}, "exchange": {"type": "string"},
                                    "interval": {"type": "string"}, "days": {"type": "integer"}},
                     "required": ["symbol"]}},
]


def list_resources():
    out = []
    for rel in ["AGENTS.md", "CLAUDE.md", "SYNC.md", "app.py",
                "services/history_service.py", "broker/fyers/api/data.py"]:
        if (OA_DIR / rel).exists():
            out.append({"uri": f"openalgo://file/{rel}", "name": rel})
    return out


def read_resource(uri):
    rel = uri.split("openalgo://file/", 1)[-1]
    p = _safe_repo_path(rel)
    if not p or not p.is_file():
        return {"contents": [], "error": f"not found: {rel}"}
    text = p.read_text(errors="replace")[:60000]
    return {"contents": [{"uri": uri, "text": text}]}


# ------------------------------------------------------------------ server


def reply(id_, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method", "")
        id_ = msg.get("id")
        if method == "initialize":
            reply(id_, {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "openalgo", "version": "1.0.0"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(id_, {"tools": TOOL_SPECS})
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            fn = TOOLS.get(name)
            if not fn:
                reply(id_, {"content": [{"type": "text", "text": f"unknown tool {name}"}],
                            "isError": True})
                continue
            try:
                result = fn(params.get("arguments") or {})
                reply(id_, {"content": [{"type": "text",
                                         "text": json.dumps(result, indent=2, default=str)}]})
            except Exception as exc:  # noqa: BLE001
                reply(id_, {"content": [{"type": "text", "text": f"error: {exc}"}],
                            "isError": True})
        elif method == "resources/list":
            reply(id_, {"resources": list_resources()})
        elif method == "resources/read":
            uri = msg.get("params", {}).get("uri", "")
            reply(id_, read_resource(uri))
        elif method == "ping":
            reply(id_, {})
        else:
            if id_ is not None:
                reply(id_, {"error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
