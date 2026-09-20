# FCC AI service for OpenAlgo.
#
# Bridges the platform to a locally running fcc-server proxy (free-claude-code
# project, OpenAI/Anthropic-compatible, default http://127.0.0.1:8082) and
# grounds every turn with live OpenAlgo data: broker quotes, option-chain
# analytics, global (dollar) references and the scalper advisor state.
#
# Surfaces served (blueprints/fcc_ai.py):
# - chat            grounded multi-turn chat for desktop panel + mobile app
# - live commentary institutional squawk bullets, algorithmic fallback included
# - agent runner    fcc-claude / fcc-codex / ... coding agents against THIS
#                   project, streaming output, stoppable — the same feature
#                   fno-trader ships, pointed at the openalgo checkout.
#
# The proxy is a local service; it is never exposed by the app itself.

import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FCC_BASE_URL = os.getenv("FCC_BASE_URL", "http://127.0.0.1:8082").rstrip("/")
FCC_AUTH_TOKEN = os.getenv("FCC_AUTH_TOKEN", "freecc")
FCC_ENABLED = os.getenv("FCC_ENABLED", "1") not in ("0", "false", "False")
FCC_TIMEOUT = float(os.getenv("FCC_TIMEOUT", "45"))
FCC_AGENT_TIMEOUT = float(os.getenv("FCC_AGENT_TIMEOUT", "1800"))
FCC_AGENTS = ("claude", "codex", "opencode", "grok", "dsh", "pi", "freebuff", "antigravity")

# The project the coding agents work on: this checkout.
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

_status_cache: dict[str, Any] = {"ts": 0.0, "data": None}
_status_lock = threading.Lock()

_run_lock = threading.Lock()
_runs: dict[str, dict[str, Any]] = {}
_run_seq = [0]


# ---------------------------------------------------------------- proxy client

def _fcc_request(method: str, path: str, body: dict | None = None,
                 timeout: float | None = None) -> dict:
    url = f"{FCC_BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if FCC_AUTH_TOKEN:
        req.add_header("Authorization", f"Bearer {FCC_AUTH_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=timeout or FCC_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore") or "{}")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:300]
        except Exception:
            pass
        raise RuntimeError(f"FCC HTTP {e.code}: {detail or e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"FCC proxy unreachable at {FCC_BASE_URL}: {e}") from e


def get_status(force: bool = False) -> dict:
    """Proxy health + model catalog, cached briefly so UI polling stays cheap."""
    now = time.time()
    with _status_lock:
        if not force and _status_cache["data"]:
            if now - _status_cache["ts"] < (10.0 if _status_cache["data"].get("connected") else 2.0):
                return _status_cache["data"]

    def _launcher(name: str) -> str | None:
        for d in (os.path.expanduser("~/.local/bin"), "/usr/local/bin", "/usr/bin"):
            p = os.path.join(d, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        return shutil_which(name)

    def shutil_which(name: str) -> str | None:
        try:
            return subprocess.run(["which", name], capture_output=True, text=True,
                                  timeout=5).stdout.strip() or None
        except Exception:
            return None

    status: dict[str, Any] = {
        "enabled": FCC_ENABLED,
        "connected": False,
        "base_url": FCC_BASE_URL,
        "models": [],
        "model_count": 0,
        "project_root": PROJECT_ROOT,
        "agents_available": {a: bool(_launcher(f"fcc-{a}") or (a == "freebuff" and _launcher("freebuff")))
                             for a in FCC_AGENTS},
        "error": None,
    }
    if FCC_ENABLED:
        try:
            raw = _fcc_request("GET", "/v1/models", timeout=6.0)
            items = raw.get("data") or []
            status["models"] = sorted({(m.get("id") or "").strip() for m in items if m.get("id")})
            status["model_count"] = len(status["models"])
            status["connected"] = True
        except Exception as e:
            status["error"] = str(e)

    with _status_lock:
        _status_cache["ts"] = now
        _status_cache["data"] = status
    return status


def _default_model() -> str | None:
    try:
        root = _fcc_request("GET", "/", timeout=5.0)
        return root.get("model")
    except Exception:
        return None


# ------------------------------------------------------------ openalgo grounding

_QUOTE_ROOTS = [
    ("NIFTY", "NSE_INDEX"),
    ("BANKNIFTY", "NSE_INDEX"),
    ("SENSEX", "BSE_INDEX"),
    ("CRUDEOIL", "MCX"),
    ("GOLD", "MCX"),
    ("SILVER", "MCX"),
]


def _fmt(v: Any, dp: int = 2) -> str:
    try:
        return f"{float(v):,.{dp}f}"
    except (TypeError, ValueError):
        return "N/A"


def _live_quote_lines(api_key: str | None) -> list[str]:
    lines = []
    try:
        from services.quotes_service import get_quotes
    except Exception:
        try:
            from restful_api_service import get_quotes  # type: ignore
        except Exception:
            return ["Live quotes unavailable (quotes service not importable)"]
    for sym, exch in _QUOTE_ROOTS:
        try:
            ok, resp, _ = get_quotes(symbol=sym, exchange=exch, api_key=api_key or "")
            data = resp.get("data") if ok and isinstance(resp, dict) else None
            if isinstance(data, dict) and data.get("ltp") is not None:
                chp = float(data.get("pchg") or data.get("chp") or 0)
                lines.append(f"{sym} {_fmt(data.get('ltp'))} ({chp:+.2f}%)")
        except Exception:
            continue
    return lines or ["Live quotes unavailable (no data)"]


def _global_lines() -> list[str]:
    try:
        from services.global_quotes_service import get_global_quotes
        rows = get_global_quotes()
        out = []
        for k, v in list((rows or {}).items())[:8]:
            if isinstance(v, dict) and v.get("ltp") is not None:
                out.append(f"{k} {_fmt(v.get('ltp'))} ({float(v.get('chp') or 0):+.2f}%)")
        return out
    except Exception:
        return []


def _chain_lines(symbol: str, api_key: str | None) -> list[str]:
    """Option-chain analytics for the focus symbol: PCR, max pain, OI walls."""
    out = []
    try:
        from services.option_chain_service import get_option_chain
        from services.expiry_service import get_expiry_dates
        exch = "NSE_INDEX" if symbol.upper() in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY") else None
        if exch is None:
            try:
                from services.symbol_service import get_exchange
                exch = get_exchange(symbol) or "NSE_INDEX"
            except Exception:
                exch = "NSE_INDEX"
        fo_exch = {"NSE_INDEX": "NFO", "BSE_INDEX": "BFO"}.get(exch, exch)
        try:
            ok, resp, _ = get_expiry_dates(symbol=symbol, exchange=fo_exch,
                                           instrumenttype="options", api_key=api_key or "")
            data = resp.get("data") if ok and isinstance(resp, dict) else None
            exps = (data or {}).get("expiry") or (data or {}).get("dates") or []
            exp = exps[0] if exps else None
        except Exception:
            exp = None
        if not exp:
            return []
        ok, resp, _ = get_option_chain(underlying=symbol, exchange=exch, expiry_date=exp,
                                       strike_count=5, api_key=api_key or "")
        if not ok:
            return []
        data = resp.get("data") if isinstance(resp, dict) else resp
        chain = (data or {}).get("chain") or []
        atm = (data or {}).get("atm_strike")
        tot_ce = tot_pe = chg_ce = chg_pe = 0
        top_ce = top_pe = None
        for row in chain:
            ce, pe = row.get("ce") or {}, row.get("pe") or {}
            tot_ce += ce.get("oi") or 0
            tot_pe += pe.get("oi") or 0
            chg_ce += ce.get("oi_chg") or ce.get("changein_oi") or 0
            chg_pe += pe.get("oi_chg") or pe.get("changein_oi") or 0
            if top_ce is None or (ce.get("oi") or 0) > (top_ce[1] or 0):
                top_ce = (row.get("strike"), ce.get("oi"))
            if top_pe is None or (pe.get("oi") or 0) > (top_pe[1] or 0):
                top_pe = (row.get("strike"), pe.get("oi"))
        if tot_ce or tot_pe:
            pcr = round(tot_pe / tot_ce, 2) if tot_ce else 0
            out.append(f"{symbol} chain: ATM {_fmt(atm, 0)}, PCR(oi) {pcr}, "
                       f"Call wall {top_ce[0] if top_ce else 'N/A'}, Put wall {top_pe[0] if top_pe else 'N/A'}, "
                       f"OI dCE {_fmt(chg_ce, 0)} vs dPE {_fmt(chg_pe, 0)}")
    except Exception as e:
        logger.debug("fcc chain lines %s: %s", symbol, e)
    return out


def _scalper_lines() -> list[str]:
    try:
        from services.scalper_advisor_service import alert_history, get_monitor
        hist = alert_history()
        alerts = [a for a in (hist.get("alerts") or [])
                  if (a.get("status") or "").lower() == "active"]
        mon = get_monitor() or {}
        armed = mon.get("armed") or {}
        events = mon.get("events") or []
        out = []
        if alerts:
            names = ", ".join(f"{a.get('key')} {a.get('side')}" for a in alerts[:5])
            out.append(f"Active scalper alerts: {len(alerts)} ({names})")
        if armed:
            out.append(f"Armed monitors: {len(armed)}")
        if events:
            out.append(f"Latest monitor event: {events[0].get('msg', '')[:120]}")
        return out
    except Exception:
        return []


def build_project_context(focus: str | None = None, api_key: str | None = None) -> str:
    """Live OpenAlgo snapshot injected as the FCC system prompt."""
    lines = [
        "You are the FCC AI analyst embedded in OpenAlgo, an open-source algo trading "
        "platform for Indian markets (NSE/BSE/MCX) with broker-neutral execution.",
        "Answer crisply. When numbers are provided below, reason from them and say when "
        "data is unavailable instead of inventing it.",
        "",
        "Live quotes: " + "; ".join(_live_quote_lines(api_key)),
    ]
    glob = _global_lines()
    if glob:
        lines.append("Global (dollar) references: " + "; ".join(glob))
    if focus:
        lines.extend(_chain_lines(focus, api_key))
    lines.extend(_scalper_lines())
    if focus:
        lines.append(f"User focus symbol: {focus}")
    return "\n".join(lines)


# ------------------------------------------------------------------------ chat

_KNOWN_ROOTS = [
    "NIFTY50", "NIFTYBANK", "BANKNIFTY", "NIFTY", "FINNIFTY", "MIDCPNIFTY",
    "SENSEX", "BANKEX", "CRUDEOIL", "NATURALGAS", "NATGASMINI", "GOLDMINI",
    "GOLD", "SILVER", "COPPER", "ZINC",
]

# English words that uppercase to a symbol-shaped token (WHAT, ARE, THE…).
# A bare caps match that lands here is ignored rather than chased as a chain.
_STOP_TOKENS = {
    "WHAT", "WHATS", "WHY", "HOW", "THE", "AND", "FOR", "ARE", "YOU",
    "YOUR", "NOW", "TODAY", "SEE", "CAN", "GET", "GIVE", "TELL", "SHOW",
    "WITH", "FROM", "ABOUT", "PLEASE", "ANALYSE", "ANALYZE", "CHART",
    "PRICE", "LEVEL", "LEVELS", "DATA", "TREND", "VIEW", "NEWS", "OM",
}


def resolve_focus(text: str) -> str | None:
    """Best-effort focus symbol from the user's message: a known instrument
    root mentioned anywhere wins; a bare symbol-shaped token is accepted only
    when it is not a common English word."""
    up = (text or "").upper()
    for root in _KNOWN_ROOTS:
        if re.search(rf"\b{root}\b", up):
            return "NIFTY" if root == "NIFTY50" else ("BANKNIFTY" if root == "NIFTYBANK" else root)
    m = re.search(r"\b([A-Z][A-Z&-]{2,14})\b", up)
    if m and m.group(1) not in _STOP_TOKENS:
        return m.group(1)
    return None


def chat(messages: list[dict], model: str | None = None,
         use_project_context: bool = True, focus: str | None = None,
         api_key: str | None = None) -> dict:
    """Grounded chat through the FCC proxy (Anthropic API with OpenAI fallback)."""
    if not FCC_ENABLED:
        raise RuntimeError("FCC integration is disabled (FCC_ENABLED=false)")

    if use_project_context and not focus and messages:
        focus = resolve_focus(next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""))

    sys_prompt = build_project_context(focus, api_key) if use_project_context else ""
    payload = [{"role": (m.get("role") or "user"), "content": (m.get("content") or "")}
               for m in (messages or [])]
    model_id = model or _default_model() or "claude-haiku-4-20250514"

    try:
        body: dict[str, Any] = {"model": model_id, "max_tokens": 2048,
                                "messages": payload, "stream": False}
        if sys_prompt:
            body["system"] = sys_prompt
        raw = _fcc_request("POST", "/v1/messages", body=body)
        content = "".join(b.get("text", "") for b in (raw.get("content") or [])
                          if b.get("type") == "text")
        used_model = raw.get("model") or model_id
    except RuntimeError:
        msgs = ([{"role": "system", "content": sys_prompt}] if sys_prompt else []) + payload
        raw = _fcc_request("POST", "/v1/chat/completions",
                           body={"model": model, "messages": msgs, "stream": False})
        content = (raw.get("choices") or [{}])[0].get("message", {}).get("content", "")
        used_model = raw.get("model") or model or ""

    return {"content": content, "model": used_model,
            "context_used": bool(sys_prompt), "focus": focus or None}


# ------------------------------------------------------------ live commentary

_commentary_history: list[dict] = []
_commentary_lock = threading.Lock()


def get_commentary_history(limit: int = 30) -> list[dict]:
    with _commentary_lock:
        return list(_commentary_history[:limit])


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    return 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 1)


def _candles_for(symbol: str, exchange: str, api_key: str | None) -> list[float]:
    """Recent 5m closes via the scalper advisor's broker-aware candle helper."""
    try:
        from services.scalper_advisor_service import _candles as advisor_candles
        rows = advisor_candles(symbol, exchange, api_key or "", interval="5m", days=5)
        return [float(c[4]) for c in rows if c and c[4]]
    except Exception as e:
        logger.debug("fcc candles %s: %s", symbol, e)
    return []


def generate_commentary(symbol: str | None = None, model: str | None = None,
                        api_key: str | None = None) -> dict:
    """Institutional squawk bullet for a symbol — telemetry first, LLM polish."""
    clean = (symbol or "NIFTY").upper().split(":")[-1]
    is_index = clean in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX")
    exch = "NSE_INDEX" if is_index and clean != "SENSEX" else ("BSE_INDEX" if clean in ("SENSEX", "BANKEX") else ("MCX" if clean in ("CRUDEOIL", "GOLD", "SILVER", "NATURALGAS", "COPPER", "ZINC") else "NSE"))

    ltp = high = low = open_ = 0.0
    chp = 0.0
    try:
        from services.quotes_service import get_quotes
        ok, resp, _ = get_quotes(symbol=clean, exchange=exch, api_key=api_key or "")
        data = resp.get("data") if ok and isinstance(resp, dict) else None
        if isinstance(data, dict):
            ltp = float(data.get("ltp") or 0)
            high = float(data.get("high") or 0)
            low = float(data.get("low") or 0)
            open_ = float(data.get("open") or 0)
            chp = float(data.get("pchg") or data.get("chp") or 0)
    except Exception:
        pass

    closes = _candles_for(clean, exch, api_key)
    if closes and not ltp:
        ltp = closes[-1]
    rsi = _rsi(closes[-60:]) if closes else None

    chain = _chain_lines(clean, api_key)
    pcr_txt = next((l for l in chain if "PCR" in l), "")
    month_low = min(closes[-6 * 75:]) if len(closes) >= 75 else None
    month_high = max(closes[-6 * 75:]) if len(closes) >= 75 else None

    bias = "NEUTRAL"
    if chp >= 0.25:
        bias = "BULLISH"
    elif chp <= -0.25:
        bias = "BEARISH"

    pos_phrase = []
    if month_high and ltp >= month_high * 0.995:
        pos_phrase.append("nearing monthly high")
    if month_low and ltp <= month_low * 1.005:
        pos_phrase.append("nearing monthly low")
    if rsi is not None:
        pos_phrase.append("RSI " + ("overbought" if rsi > 70 else "oversold" if rsi < 30 else f"{rsi}"))

    time_str = datetime.now().strftime("%d %b %H:%M")
    headline = f"{clean} at ₹{_fmt(ltp)} ({chp:+.2f}%) · {bias.title()}"
    algo = (f"{clean} is trading at ₹{_fmt(ltp)} ({chp:+.2f}%). "
            + (pcr_txt + ". " if pcr_txt else "")
            + (f"Technicals: {', '.join(pos_phrase)}. " if pos_phrase else ""))

    # LLM polish — short, spoken-style; falls back to the algorithmic bullet.
    if FCC_ENABLED:
        try:
            sys_p = ("You are an institutional floor trading squawk analyst for Indian "
                     "markets (NSE/BSE/MCX). Punchy, factual, broadcast spoken style, "
                     "under 40 words. Output strictly valid JSON with keys: "
                     "headline, commentary, bias, tag.")
            user_p = (f"Asset: {clean} ({exch})\nQuote: LTP=₹{_fmt(ltp)} ({chp:+.2f}%), "
                      f"H=₹{_fmt(high)}, L=₹{_fmt(low)}, O=₹{_fmt(open_)}\n"
                      f"RSI: {rsi if rsi is not None else 'N/A'}\n"
                      f"Month range: {_fmt(month_low)}–{_fmt(month_high)}\n"
                      f"Chain: {pcr_txt or 'N/A'}\nProduce the JSON.")
            raw = _fcc_request("POST", "/v1/messages", timeout=15.0, body={
                "model": model or _default_model() or "claude-haiku-4-20250514",
                "max_tokens": 250, "system": sys_p, "stream": False,
                "messages": [{"role": "user", "content": user_p}]})
            text = "".join(b.get("text", "") for b in (raw.get("content") or [])
                           if b.get("type") == "text").strip()
            text = re.sub(r"^```(json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("commentary"):
                return _store_commentary({
                    "timestamp": time_str, "symbol": clean,
                    "headline": parsed.get("headline") or headline,
                    "commentary": parsed["commentary"],
                    "bias": (parsed.get("bias") or bias).upper(),
                    "tag": parsed.get("tag") or "Live Squawk",
                    "is_trigger": False, "triggers": [],
                    "metrics": {"ltp": ltp, "chp": chp, "rsi": rsi},
                })
        except Exception as e:
            logger.info("FCC commentary LLM failed (%s); using algorithmic bullet", e)

    return _store_commentary({
        "timestamp": time_str, "symbol": clean, "headline": headline,
        "commentary": algo.strip(), "bias": bias, "tag": "Live Squawk",
        "is_trigger": False, "triggers": [],
        "metrics": {"ltp": ltp, "chp": chp, "rsi": rsi},
    })


def _store_commentary(item: dict) -> dict:
    item = {"id": f"comm_{int(time.time() * 1000)}", **item}
    with _commentary_lock:
        _commentary_history.insert(0, item)
        if len(_commentary_history) > 60:
            _commentary_history.pop()
    return item


# ---------------------------------------------------------------- agent runner

def _child_env() -> dict[str, str]:
    """Env for spawned fcc-* agent processes so they can find the CLIs they wrap
    (claude, codex, ...) in ~/.local/bin or nvm dirs even when the app runs with
    a minimal service PATH. Also scrubs the app's own PORT/HOST — the fcc-*
    launchers fall back to a generic PORT and would otherwise probe OpenAlgo's
    port instead of the FCC proxy."""
    env = dict(os.environ)
    home = os.path.expanduser("~")
    extra = [os.path.expanduser("~/.local/bin")]
    nvm_bins = os.path.join(home, ".nvm", "versions", "node")
    if os.path.isdir(nvm_bins):
        for v in sorted(os.listdir(nvm_bins)):
            extra.append(os.path.join(nvm_bins, v, "bin"))
    seen: set[str] = set()
    parts: list[str] = []
    for p in extra + env.get("PATH", os.defpath).split(os.pathsep):
        if p and p not in seen:
            seen.add(p)
            parts.append(p)
    env["PATH"] = os.pathsep.join(parts)
    env.setdefault("NO_COLOR", "1")
    env.pop("PORT", None)
    env.pop("HOST", None)
    try:
        from urllib.parse import urlparse
        parsed = urlparse(FCC_BASE_URL)
        if parsed.port:
            env["FCC_PORT"] = str(parsed.port)
        if parsed.hostname:
            env.setdefault("FCC_HOST", parsed.hostname)
    except Exception:
        pass
    env.setdefault("FCC_BASE_URL", FCC_BASE_URL)
    return env


def _launcher_path(agent: str) -> str | None:
    name = "freebuff" if agent == "freebuff" else f"fcc-{agent}"
    for d in (os.path.expanduser("~/.local/bin"), "/usr/local/bin", "/usr/bin"):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    try:
        w = subprocess.run(["which", name], capture_output=True, text=True,
                           timeout=5, env=_child_env()).stdout.strip()
        return w or None
    except Exception:
        return None


def list_agents() -> dict:
    return {a: bool(_launcher_path(a)) for a in FCC_AGENTS}


def agent_status(run_id: str) -> dict:
    with _run_lock:
        run = _runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        return dict(run)


def list_runs(limit: int = 20) -> list[dict]:
    with _run_lock:
        runs = sorted(_runs.values(), key=lambda r: r.get("started_ts") or 0, reverse=True)
        return [{k: r.get(k) for k in ("run_id", "agent", "prompt", "status", "started_ts",
                                       "ended_ts", "error")} for r in runs[:limit]]


def stop_agent(run_id: str) -> None:
    with _run_lock:
        run = _runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        proc = run.get("_proc")
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    with _run_lock:
        if _runs.get(run_id, {}).get("status") == "running":
            _runs[run_id]["status"] = "stopped"


def run_agent(agent: str, prompt: str, cwd: str | None = None,
              timeout: float | None = None, model: str | None = None) -> dict:
    """Launch an FCC coding agent against this project, non-interactive."""
    agent = (agent or "").strip()
    if agent.startswith("fcc-"):  # accept the full launcher name too
        agent = agent[4:]
    launcher = _launcher_path(agent)
    if not launcher:
        raise FileNotFoundError(f"fcc-{agent} launcher not found on this machine")
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")

    with _run_lock:
        _run_seq[0] += 1
        run_id = f"fcc_{int(time.time())}_{_run_seq[0]}"
        _runs[run_id] = {
            "run_id": run_id, "agent": agent, "prompt": prompt[:200],
            "status": "running", "output": "", "started_ts": time.time(),
            "ended_ts": None, "error": None,
        }

    cmd = [launcher]
    if agent not in ("dsh", "freebuff"):
        cmd += ["-p", "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
    cmd += [prompt]

    def _worker():
        try:
            proc = subprocess.Popen(
                cmd, cwd=cwd or PROJECT_ROOT, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, env=_child_env(),
            )
            with _run_lock:
                _runs[run_id]["_proc"] = proc
            buf: list[str] = []
            for line in proc.stdout:  # type: ignore[union-attr]
                buf.append(line)
                with _run_lock:
                    _runs[run_id]["output"] = "".join(buf)[-40_000:]
            try:
                rc = proc.wait(timeout=timeout or FCC_AGENT_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except Exception:
                    proc.kill()
                with _run_lock:
                    _runs[run_id].update(status="failed", ended_ts=time.time(),
                                         error="timeout")
                return
            with _run_lock:
                _runs[run_id].update(status="done" if rc == 0 else "failed",
                                     ended_ts=time.time(),
                                     error=None if rc == 0 else f"exit {rc}")
        except Exception as e:
            with _run_lock:
                _runs[run_id].update(status="failed", ended_ts=time.time(), error=str(e))

    threading.Thread(target=_worker, daemon=True, name=f"fcc-agent-{agent}").start()
    with _run_lock:
        return {k: v for k, v in _runs[run_id].items() if k != "_proc"}
