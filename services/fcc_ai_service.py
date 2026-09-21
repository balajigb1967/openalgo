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


def _fmt_oi(val: float) -> str:
    """Indian-market OI formatting: Cr / L abbreviations like the terminal."""
    try:
        val = float(val)
    except (TypeError, ValueError):
        return "0"
    sign = "+" if val > 0 else ""
    abs_v = abs(val)
    if abs_v >= 10000000:
        return f"{sign}{val / 10000000:.2f}Cr"
    if abs_v >= 100000:
        return f"{sign}{val / 100000:.1f}L"
    return f"{sign}{val:,.0f}"


def _classify_buildup(oi_chg: float, price_chg: float) -> str:
    """Classic F&O buildup taxonomy from OI and premium direction."""
    if oi_chg > 0:
        return "Long Buildup" if price_chg >= 0 else "Short Buildup"
    if oi_chg < 0:
        return "Short Covering" if price_chg >= 0 else "Long Unwinding"
    return "Neutral"


def _fo_exchange(symbol: str) -> str:
    up = symbol.upper()
    if up in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        return "NSE_INDEX"
    if up in ("SENSEX", "BANKEX"):
        return "BSE_INDEX"
    if up in ("CRUDEOIL", "GOLD", "SILVER", "NATURALGAS", "COPPER", "ZINC"):
        return "MCX"
    try:
        from services.symbol_service import get_exchange
        return get_exchange(symbol) or "NSE"
    except Exception:
        return "NSE"


def _chain_analytics(symbol: str, ltp: float, api_key: str | None) -> dict:
    """Deep option-chain telemetry for the focus symbol, fno-trader style:
    PCR(oi), max pain, top call/put OI walls with buildup classification,
    ATM IV and an OI-flow bias sentence. Tolerates thin broker payloads."""
    out: dict = {"has_options": False}
    try:
        from services.option_chain_service import get_option_chain
        from services.expiry_service import get_expiry_dates
        exch = _fo_exchange(symbol)
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
            return out
        ok, resp, _ = get_option_chain(underlying=symbol, exchange=exch, expiry_date=exp,
                                       strike_count=10, api_key=api_key or "")
        if not ok:
            return out
        data = resp.get("data") if isinstance(resp, dict) else resp
        chain = (data or {}).get("chain") or []
        if not chain:
            return out
        out["has_options"] = True
        rows = []
        tot_ce = tot_pe = chg_ce = chg_pe = 0.0
        for row in chain:
            strike = row.get("strike")
            ce, pe = row.get("ce") or {}, row.get("pe") or {}
            ce_oi = float(ce.get("oi") or 0)
            pe_oi = float(pe.get("oi") or 0)
            ce_chg = float(ce.get("oi_chg") or ce.get("changein_oi") or 0)
            pe_chg = float(pe.get("oi_chg") or pe.get("changein_oi") or 0)
            tot_ce += ce_oi
            tot_pe += pe_oi
            chg_ce += ce_chg
            chg_pe += pe_chg
            rows.append({"strike": strike, "ce": ce, "pe": pe,
                         "ce_oi": ce_oi, "pe_oi": pe_oi, "ce_chg": ce_chg, "pe_chg": pe_chg})
        out["pcr_oi"] = round(tot_pe / tot_ce, 2) if tot_ce else None
        out["tot_ce_chg"], out["tot_pe_chg"] = chg_ce, chg_pe

        # Max pain: the strike where option writers lose the least.
        if rows and ltp:
            try:
                pains = []
                for r in rows:
                    k = float(r["strike"] or 0)
                    pain = sum(float(x["ce_oi"]) * max(0.0, k - float(x["strike"] or 0))
                               + float(x["pe_oi"]) * max(0.0, float(x["strike"] or 0) - k)
                               for x in rows)
                    pains.append((pain, k))
                out["max_pain"] = min(pains)[1] if pains else None
            except Exception:
                out["max_pain"] = None

        # Top OI walls by absolute change, with buildup classification.
        def _top(rows_, side):
            cands = [r for r in rows_ if (r[f"{side}_chg"] or 0) != 0]
            if not cands:
                return None
            top = max(cands, key=lambda r: abs(r[f"{side}_chg"]))
            leg = top[side]
            chg = top[f"{side}_chg"]
            price_chg = float(leg.get("chp") or leg.get("pchg") or 0)
            kind = _classify_buildup(chg, price_chg)
            return {"strike": top["strike"], "chg": chg, "type": kind,
                    "label": f"{top['strike']} {side.upper()} ({_fmt_oi(chg)}, {kind})"}
        out["top_ce_buildup"] = _top(rows, "ce")
        out["top_pe_buildup"] = _top(rows, "pe")

        # ATM IV: average of the call/put IV at the strike nearest spot.
        if ltp and rows:
            atm_row = min(rows, key=lambda r: abs(float(r["strike"] or 0) - ltp))
            c_iv = float((atm_row["ce"] or {}).get("iv") or 0)
            p_iv = float((atm_row["pe"] or {}).get("iv") or 0)
            ivs = [v for v in (c_iv, p_iv) if v]
            out["atm_iv"] = round(sum(ivs) / len(ivs), 1) if ivs else None
            out["atm_strike"] = atm_row["strike"]

        # OI-flow bias sentence, fno-trader taxonomy.
        if chg_pe > 0 and chg_ce <= 0:
            out["oi_bias"] = f"Put Writing & Call Unwinding (Bullish Squeeze {_fmt_oi(chg_pe)} PE)"
        elif chg_ce > 0 and chg_pe <= 0:
            out["oi_bias"] = f"Call Writing & Put Unwinding (Bearish Supply {_fmt_oi(chg_ce)} CE)"
        elif chg_pe > chg_ce * 1.3:
            out["oi_bias"] = f"Put Writing Dominance ({_fmt_oi(chg_pe)} PE vs {_fmt_oi(chg_ce)} CE)"
        elif chg_ce > chg_pe * 1.3:
            out["oi_bias"] = f"Call Writing Dominance ({_fmt_oi(chg_ce)} CE vs {_fmt_oi(chg_pe)} PE)"
        elif chg_ce or chg_pe:
            out["oi_bias"] = f"Balanced OI Flow ({_fmt_oi(chg_pe)} PE vs {_fmt_oi(chg_ce)} CE)"
        out["strike_count"] = len(rows)
    except Exception as e:
        logger.debug("fcc chain analytics %s: %s", symbol, e)
    return out


def _chain_lines(symbol: str, api_key: str | None) -> list[str]:
    """Compact chain summary line for the grounded chat context."""
    try:
        from services.quotes_service import get_quotes
        exch = _fo_exchange(symbol)
        ltp = 0.0
        try:
            ok, resp, _ = get_quotes(symbol=symbol, exchange=exch, api_key=api_key or "")
            data = resp.get("data") if ok and isinstance(resp, dict) else None
            ltp = float((data or {}).get("ltp") or 0)
        except Exception:
            pass
        a = _chain_analytics(symbol, ltp, api_key)
        if not a.get("has_options"):
            return []
        bits = [f"{symbol} chain:"]
        if a.get("atm_strike") is not None:
            bits.append(f"ATM {_fmt(a['atm_strike'], 0)}")
        if a.get("pcr_oi") is not None:
            bits.append(f"PCR(oi) {a['pcr_oi']}")
        if a.get("max_pain") is not None:
            bits.append(f"max pain {_fmt(a['max_pain'], 0)}")
        if a.get("top_ce_buildup"):
            bits.append(f"call wall {a['top_ce_buildup']['label']}")
        if a.get("top_pe_buildup"):
            bits.append(f"put wall {a['top_pe_buildup']['label']}")
        if a.get("atm_iv"):
            bits.append(f"ATM IV {a['atm_iv']}%")
        return [" ".join(bits)]
    except Exception as e:
        logger.debug("fcc chain lines %s: %s", symbol, e)
    return []


def _daily_candles(symbol: str, api_key: str | None, days: int = 40) -> list[tuple]:
    """Daily OHLC rows (t, o, h, l, c) via the scalper advisor's broker-aware
    candle helper — the same source the terminal's own analytics trust."""
    try:
        from services.scalper_advisor_service import _candles as advisor_candles
        exch = _fo_exchange(symbol)
        rows = advisor_candles(symbol, exch, api_key or "", interval="D", days=days)
        out = []
        for r in rows or []:
            try:
                out.append((float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
            except (TypeError, ValueError, IndexError):
                continue
        return out
    except Exception as e:
        logger.debug("fcc daily candles %s: %s", symbol, e)
    return []


def _chart_lines(symbol: str, ltp: float, api_key: str | None) -> list[str]:
    """Daily chart snapshot for the focus symbol: trend, RSI, week/month range
    and classic floor pivots — the grounding behind 'analyse the chart'."""
    out: list[str] = []
    try:
        daily = _daily_candles(symbol, api_key)
        if not daily:
            return out
        closes = [c[4] for c in daily]
        last = closes[-1] if closes else 0.0
        ref = ltp or last
        # Trend: 20-EMA position plus short-slope.
        ema = last
        k = 2 / (20 + 1)
        for c in closes:
            ema = c * k + ema * (1 - k)
        slope5 = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 and closes[-6] else 0.0
        trend = "up" if ref > ema * 1.002 and slope5 > 0 else ("down" if ref < ema * 0.998 and slope5 < 0 else "range")
        rsi = _rsi(closes[-60:]) if closes else None
        week_hi = max(c[2] for c in daily[-5:])
        week_lo = min(c[3] for c in daily[-5:])
        month_hi = max(c[2] for c in daily[-22:])
        month_lo = min(c[3] for c in daily[-22:])
        bits = [f"{symbol} daily chart: trend {trend} (close {'above' if ref > ema else 'below'} 20-EMA, "
                f"5-day {slope5:+.1f}%)"]
        if rsi is not None:
            bits.append(f"RSI {rsi}")
        bits.append(f"week {_fmt(week_lo)}–{_fmt(week_hi)}, month {_fmt(month_lo)}–{_fmt(month_hi)}")
        if len(daily) >= 2:
            _, po, ph, pl, pc = daily[-2]
            pp = (ph + pl + pc) / 3
            bits.append(f"pivots PP {_fmt(pp)}, R1 {_fmt(2 * pp - pl)}, S1 {_fmt(2 * pp - ph)}")
        out.append(" ".join(bits))
    except Exception as e:
        logger.debug("fcc chart lines %s: %s", symbol, e)
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


def _focus_ltp(symbol: str, api_key: str | None) -> float:
    try:
        from services.quotes_service import get_quotes
        ok, resp, _ = get_quotes(symbol=symbol, exchange=_fo_exchange(symbol), api_key=api_key or "")
        data = resp.get("data") if ok and isinstance(resp, dict) else None
        return float((data or {}).get("ltp") or 0)
    except Exception:
        return 0.0


def build_project_context(focus: str | None = None, api_key: str | None = None) -> str:
    """Live OpenAlgo snapshot injected as the FCC system prompt."""
    if focus:
        focus = focus.split(":")[-1].strip().upper() or None
    lines = [
        "You are the FCC AI analyst embedded in OpenAlgo, an open-source algo trading "
        "platform for Indian markets (NSE/BSE/MCX) with broker-neutral execution.",
        "Answer crisply. When numbers are provided below, reason from them and say when "
        "data is unavailable instead of inventing it. When the operator asks to analyse "
        "the chart or the market, analyse the focus symbol from the telemetry below.",
        "",
        "Live quotes: " + "; ".join(_live_quote_lines(api_key)),
    ]
    glob = _global_lines()
    if glob:
        lines.append("Global (dollar) references: " + "; ".join(glob))
    if focus:
        ltp = _focus_ltp(focus, api_key)
        lines.extend(_chart_lines(focus, ltp, api_key))
        lines.extend(_chain_lines(focus, api_key))
    lines.extend(_scalper_lines())
    if focus:
        lines.append(f"User focus symbol: {focus} (the operator's active chart)")
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
_last_commentary_snapshots: dict[str, dict] = {}

# -------- event-driven commentary state (per symbol) ------------------------
# Each entry: {"events": [str,...], "sig": {"rsi": float, "above": bool,
# "trend": str, "ltp": float, "pcr": float}, "ts": float}
_event_state: dict[str, dict] = {}

# Cooldowns (seconds) so the same alert family cannot spam within a window.
_COOLDOWN_EVENT = 900       # same event text: 15 min
_COOLDOWN_HEARTBEAT = 900   # status heartbeat: at most every 15 min


def _mark_event(sym: str, text: str, sig: dict, *, heartbeat: bool = False,
                cooldown: int | None = None) -> str | None:
    """Record an event for a symbol; return the display text only when it is
    NEW (never seen, or past its cooldown). Status heartbeats additionally
    require a real change vs the recorded signature."""
    st = _event_state.setdefault(sym, {"events": {}, "sig": {}, "ts": 0.0})
    now = time.time()
    cd = _COOLDOWN_HEARTBEAT if heartbeat else (cooldown if cooldown is not None else _COOLDOWN_EVENT)
    key = text.lower()
    last = st["events"].get(key, 0.0)
    if now - last < cd:
        return None
    if heartbeat:
        prev = st.get("sig", {})
        watched = ("rsi_zone", "trend", "above")
        if prev and all(prev.get(k) == sig.get(k) for k in watched):
            return None
        # merge — never clobber sibling keys (oi walls, pcr, …)
        st["sig"] = {**st.get("sig", {}), **sig}
    st["events"][key] = now
    # prune stale entries (2 h) so the dict cannot grow unbounded
    if len(st["events"]) > 40:
        st["events"] = {k: t for k, t in st["events"].items() if now - t < 7200}
    return text


def _zone(rsi: float) -> str:
    if rsi <= 25:
        return "deep oversold"
    if rsi <= 35:
        return "oversold"
    if rsi >= 75:
        return "deep overbought"
    if rsi >= 65:
        return "overbought"
    if rsi >= 55:
        return "bullish zone"
    if rsi <= 45:
        return "bearish zone"
    return "neutral zone"


def get_commentary_history(limit: int = 30) -> list[dict]:
    with _commentary_lock:
        return list(_commentary_history[:limit])


# ------------------------------------------------------------ auto squawk loop

_auto_lock = threading.Lock()
_auto_state: dict[str, Any] = {"enabled": False, "symbol": "NIFTY", "interval": 60,
                               "last_error": "", "last_ts": 0.0, "thread": None,
                               "expires_ts": 0.0, "stopped_reason": ""}

# Squawk sessions self-expire so a forgotten toggle can't burn LLM tokens all
# day; the UI shows the countdown and the server flips itself off.
DEFAULT_SQUAWK_DURATION_MIN = 30
MAX_SQUAWK_DURATION_MIN = 240

# Event detection runs on a fast fixed cadence so bullets land as events
# happen; the event engine inside generate_commentary keeps LLM spend gated
# (silent when nothing new, per-family cooldowns otherwise).
SQUAWK_SCAN_CADENCE_S = 20


def _stored_api_key() -> str | None:
    """A usable OpenAlgo API key for background data calls (no request context)."""
    try:
        from database.auth_db import get_first_available_api_key
        k = get_first_available_api_key()
        if k:
            return k
    except Exception:
        pass
    return None


def _market_open_ist() -> bool:
    """NSE window 09:15–15:30 IST, Mon–Fri — no LLM spend outside it."""
    ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    if ist.weekday() >= 5:
        return False
    minutes = ist.hour * 60 + ist.minute
    return 9 * 60 + 15 <= minutes <= 15 * 60 + 30


def _auto_loop() -> None:
    last_poll = 0.0
    while True:
        with _auto_lock:
            if not _auto_state["enabled"]:
                _auto_state["thread"] = None
                return
            symbol = str(_auto_state["symbol"])
            interval = max(30, int(_auto_state["interval"] or 60))
            # Session expiry: flip off once the requested duration is spent.
            exp = float(_auto_state.get("expires_ts") or 0.0)
            if exp and time.time() >= exp:
                _auto_state["enabled"] = False
                _auto_state["thread"] = None
                _auto_state["stopped_reason"] = "auto-off after duration"
                return
        err = ""
        if _market_open_ist():
            # Fast event scan: check for events every SQUAWK_SCAN_CADENCE_S
            # independent of the LLM interval. The event engine inside
            # generate_commentary gates LLM spend (silent when nothing new,
            # per-family cooldowns otherwise), so bullets land as events happen.
            now = time.time()
            if now - last_poll >= SQUAWK_SCAN_CADENCE_S:
                last_poll = now
                try:
                    generate_commentary(symbol=symbol, api_key=_stored_api_key())
                except Exception as e:  # noqa: BLE001
                    err = str(e)[:200]
                    logger.warning("auto squawk %s: %s", symbol, err)
                with _auto_lock:
                    _auto_state["last_error"] = err
                    if not err:
                        _auto_state["last_ts"] = time.time()
        # sleep in short slices so a disable/expiry is honoured quickly
        deadline = time.time() + 1.0
        while time.time() < deadline:
            with _auto_lock:
                if not _auto_state["enabled"]:
                    _auto_state["thread"] = None
                    return
            time.sleep(0.25)


def set_auto_squawk(enabled: bool, symbol: str | None = None,
                    interval: int | None = None,
                    duration_min: int | None = None) -> dict:
    """Start/stop the auto-squawk loop for a symbol.

    Sessions auto-expire after ``duration_min`` minutes (default 30, cap 240)
    so a forgotten toggle cannot burn LLM tokens all day; status carries the
    countdown (``expires_in``) and ``stopped_reason`` when it flips off."""
    with _auto_lock:
        _auto_state["enabled"] = bool(enabled)
        if symbol:
            _auto_state["symbol"] = str(symbol).upper().split(":")[-1]
        if interval:
            _auto_state["interval"] = max(30, int(interval))
        if enabled:
            mins = int(duration_min or DEFAULT_SQUAWK_DURATION_MIN)
            _auto_state["expires_ts"] = time.time() + max(1, min(mins, MAX_SQUAWK_DURATION_MIN)) * 60
            _auto_state["stopped_reason"] = ""
        else:
            _auto_state["expires_ts"] = 0.0
        if enabled and not _auto_state["thread"]:
            t = threading.Thread(target=_auto_loop, name="fcc-auto-squawk", daemon=True)
            _auto_state["thread"] = t
            t.start()
    return auto_squawk_status()


def auto_squawk_status() -> dict:
    with _auto_lock:
        out = {k: v for k, v in _auto_state.items() if k != "thread"}
        out["running"] = bool(_auto_state.get("thread"))
        exp = float(out.get("expires_ts") or 0.0)
        out["expires_in"] = max(0, int(exp - time.time())) if (out.get("enabled") and exp) else 0
        if not out.get("enabled"):
            out["expires_ts"] = 0.0
        return out


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
    exch = _fo_exchange(clean)

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

    # Deep telemetry — chain analytics, daily chart snapshot, scalper state.
    opts = _chain_analytics(clean, ltp, api_key)
    chart_bits = _chart_lines(clean, ltp, api_key)
    pcr = opts.get("pcr_oi")
    max_pain = opts.get("max_pain")
    tcb, tpb = opts.get("top_ce_buildup"), opts.get("top_pe_buildup")

    daily = _daily_candles(clean, api_key)
    week_hi = max(c[2] for c in daily[-5:]) if daily else None
    week_lo = min(c[3] for c in daily[-5:]) if daily else None
    month_hi = max(c[2] for c in daily[-22:]) if daily else (max(closes[-6 * 75:]) if len(closes) >= 75 else None)
    month_lo = min(c[3] for c in daily[-22:]) if daily else (min(closes[-6 * 75:]) if len(closes) >= 75 else None)

    # ------------------------------------------------------------------
    # Event-driven signals: only NEW events make bullets; repeated reads
    # stay silent. Levels need TWO consecutive reads confirming.
    # ------------------------------------------------------------------
    st = _event_state.setdefault(clean, {"events": {}, "sig": {}, "ts": 0.0})
    prev = st.get("sig", {})
    level_hits: dict[str, dict] = st.setdefault("levels", {})  # name -> streak info
    prev_5m = prev.get("ltp") or ltp

    daily_hi = month_hi
    daily_lo = month_lo
    ev: list[str] = []

    def _evt(text: str, *, heartbeat: bool = False, cooldown: int | None = None) -> None:
        out = _mark_event(clean, text, {"rsi_zone": _zone(rsi) if rsi is not None else None,
                                        "trend": ("up" if closes and ltp and closes[-1] > (sum(closes[-20:]) / min(20, len(closes))) else "down") if closes else None,
                                        "above": bool(ltp and daily_hi and ltp >= daily_hi * 0.998)},
                          heartbeat=heartbeat, cooldown=cooldown)
        if out:
            ev.append(out)

    # --- RSI zone transitions -------------------------------------------
    if rsi is not None:
        z = _zone(rsi)
        pz = prev.get("rsi_zone")
        if z != pz:
            if z in ("deep oversold", "oversold"):
                _evt(f"RSI {rsi} → {z.upper()} — watch for a relief bounce; "
                     f"longs only above ₹{_fmt(ltp)}")
            elif z in ("deep overbought", "overbought"):
                _evt(f"RSI {rsi} → {z.upper()} — momentum stretched; "
                     f"book partials into strength")
            elif z == "bullish zone":
                _evt(f"RSI {rsi} — momentum turning UP into bullish zone")
            elif z == "bearish zone":
                _evt(f"RSI {rsi} — momentum rolling over into bearish zone")
        st["sig"]["rsi_zone"] = z

    # --- Support / resistance: nearing (warn once), then breach ----------
    for name, lvl, lo in (("monthly high", month_hi, False), ("monthly low", month_lo, True),
                          ("weekly high", week_hi, False), ("weekly low", week_lo, True),
                          ("day high", high or None, False), ("day low", low or None, True)):
        if not lvl or not ltp:
            continue
        near_pct = 0.25 if name.startswith("day") else 0.6
        dist = (lvl - ltp) / ltp * 100
        key = name.replace(" ", "_")
        lk = level_hits.setdefault(key, {"streak": 0, "near": False, "brk": False})
        near = abs(dist) <= near_pct
        brk = (ltp >= lvl) if not lo else (ltp <= lvl)
        lk["streak"] = lk["streak"] + 1 if near else 0
        if brk and not lk["brk"]:
            _evt(f"BREAKOUT: {name.capitalize()} ₹{_fmt(lvl, 0)} breached — "
                 f"{'breakout continuation' if not lo else 'breakdown'} watch")
            lk["brk"] = True
            lk["near"] = False
        elif lk["streak"] >= 2 and not lk["near"] and not brk:
            _evt(f"Nearing {name} ₹{_fmt(lvl, 0)} ({abs(dist):.1f}% away) — "
                 f"{'breakout' if not lo else 'breakdown'} alert zone")
            lk["near"] = True
        if not near and not brk and lk["brk"] and abs(dist) > near_pct:
            lk["brk"] = False  # fully reclaimed/lost again — re-arm

    # --- Trend: MA20 slope + higher-high structure ------------------------
    if len(closes) >= 25:
        ma20 = sum(closes[-20:]) / 20
        ma20_prev = sum(closes[-25:-5]) / 20
        trend = "UP" if ma20 > ma20_prev else "DOWN"
        p_trend = prev.get("trend")
        if trend != p_trend and p_trend is not None:
            _evt(f"TREND REVERSAL EXPECTED: 20-period MA slope flipped {p_trend}→{trend} "
                 f"— position accordingly")
        st["sig"]["trend"] = trend

    # --- OI / options flow events (only on change) -----------------------
    if opts.get("oi_bias") and opts.get("oi_bias") != prev.get("oi_bias"):
        _evt(f"OI FLOW SHIFT: {opts['oi_bias']}")
        st["sig"]["oi_bias"] = opts.get("oi_bias")
    if tcb and tcb.get("label") != prev.get("call_wall"):
        _evt(f"New CALL supply wall forming at {tcb['label']} — upside capped there")
        st["sig"]["call_wall"] = tcb.get("label")
    if tpb and tpb.get("label") != prev.get("put_wall"):
        _evt(f"New PUT support wall at {tpb['label']} — dips likely bought")
        st["sig"]["put_wall"] = tpb.get("label")
    if pcr is not None:
        p_pcr = prev.get("pcr")
        if p_pcr and abs(pcr - p_pcr) >= 0.1:
            d = pcr - p_pcr
            _evt(f"PCR swing {p_pcr} → {pcr} ({'bullish' if d > 0 else 'bearish'} tilt)")
        st["sig"]["pcr"] = pcr

    # --- Chart-pattern heuristics on 5m closes ---------------------------
    if len(closes) >= 40:
        seg = closes[-40:]
        hi, lo_ = max(seg), min(seg)
        rng = hi - lo_
        if rng > 0:
            pos = (seg[-1] - lo_) / rng
            if 0.45 <= pos <= 0.55 and rng / seg[-1] < 0.006:
                _evt("PATTERN: tight coil forming — compression breakout likely "
                     "(trade the direction of the pop)", cooldown=3600)
            elif pos >= 0.97:
                _evt("PATTERN: riding range top — strong momentum continuation "
                     "setup, trail stops", cooldown=3600)
            elif pos <= 0.03:
                _evt("PATTERN: at range bottom — oversold bounce setup, "
                     "wait for a green candle to enter", cooldown=3600)

    # --- Status heartbeat: only when something material changed ----------
    if not ev:
        _evt(f"{clean} steady at ₹{_fmt(ltp)} ({chp:+.2f}%) · RSI {rsi if rsi is not None else '-'} · "
             f"trend {st.get('sig', {}).get('trend', 'n/a')}", heartbeat=True)

    signals = ev

    scalper_lines = _scalper_lines()

    # Bullets are stamped in IST (the market's clock) — the VM runs UTC, so a
    # naive datetime.now() showed times 5.5h behind.
    time_str = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%d %b %H:%M")
    bias = "NEUTRAL"
    if chp >= 0.25:
        bias = "BULLISH"
    elif chp <= -0.25:
        bias = "BEARISH"
    if bias == "NEUTRAL" and pcr is not None:
        if pcr >= 1.25:
            bias = "BULLISH"
        elif pcr <= 0.85:
            bias = "BEARISH"

    headline = f"{clean} ₹{_fmt(ltp)} ({chp:+.2f}%) · {bias.title()}"

    # Nothing new? Stay SILENT — no bullet at all (the whole point of the
    # event engine; callers must handle item=None).
    if not signals:
        return None
    # Algorithmic tips: one bullet per NEW event, always actionable.
    tips = list(signals)

    # LLM tip polish — turns the raw events into crisp actionable bullets.
    # Falls back to the raw event bullets on any failure.
    if FCC_ENABLED:
        try:
            sys_p = ("You are a trading desk tip generator for Indian F&O markets. "
                     "You receive ONLY the NEW events for a symbol plus current "
                     "telemetry. Output 2-6 short trading-tip bullets (max 18 words "
                     "each) with concrete ₹ levels where possible: entry/avoid/exit "
                     "hints, what to watch next, risk note. Do NOT restate events "
                     "that are not in the list; no disclaimers, no filler. Output "
                     "strictly valid JSON: {\"headline\": str, \"tips\": [str], "
                     "\"bias\": \"BULLISH\"|\"BEARISH\"|\"NEUTRAL\", \"tag\": str}.")
            user_p = (f"Symbol: {clean} ({exch})\n"
                      + (f"Now: LTP ₹{_fmt(ltp)} ({chp:+.2f}%), day H ₹{_fmt(high)} L ₹{_fmt(low)}\n" if ltp
                         else "Quote feed unavailable right now — do NOT invent or mention ₹ price levels; keep tips qualitative.\n")
                      + (f"5m RSI {rsi} ({_zone(rsi)})\n" if rsi is not None else "")
                      + (f"PCR(oi) {pcr}" + (f", max pain {_fmt(max_pain, 0)}" if max_pain else "") + "\n" if pcr is not None else "")
                      + (f"ATM IV {opts['atm_iv']}%\n" if opts.get("atm_iv") else "")
                      + "NEW EVENTS:\n" + "\n".join(f"- {s}" for s in signals)
                      + ("\nScalper context: " + " | ".join(scalper_lines[:2]) if scalper_lines else "")
                      + "\nTips JSON:")
            raw = _fcc_request("POST", "/v1/messages", timeout=60.0, body={
                "model": model or _default_model() or "claude-haiku-4-20250514",
                "max_tokens": 700, "system": sys_p, "stream": False,
                "messages": [{"role": "user", "content": user_p}]})
            text = "".join(b.get("text", "") for b in (raw.get("content") or [])
                           if b.get("type") == "text").strip()
            text = re.sub(r"^```(json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("tips"):
                tips = [str(t) for t in parsed["tips"]][:6]
                headline = parsed.get("headline") or headline
                bias = (parsed.get("bias") or bias).upper()
        except Exception as e:
            logger.info("FCC tips LLM failed (%s); using raw event bullets", e)

    commentary = "\n".join("• " + t for t in tips)

    return _store_commentary({
        "timestamp": time_str, "symbol": clean, "headline": headline,
        "commentary": commentary, "bias": bias, "tag": "Trading Tips",
        "is_trigger": False, "triggers": [], "signals": signals,
        "metrics": {"ltp": ltp, "chp": chp, "rsi": rsi, "pcr": pcr},
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
