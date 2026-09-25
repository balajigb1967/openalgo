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

import glob
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
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


def _watchlist_symbols(user_id: str | None) -> list[tuple[str, str]]:
    """Symbol/exchange pairs from the operator's watchlists, deduped.

    The quotes block used to be a hardcoded handful of index/MCX roots, which
    is why the telemetry went deaf for any instrument outside it (NATURALGAS
    being the one that bit). The watchlist is the source of truth for what
    the operator actually trades.
    """
    if not user_id:
        return []
    try:
        from database.watchlist_db import get_watchlists
    except Exception:
        logger.exception("watchlist db unavailable for FCC context")
        return []
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    try:
        for wl in get_watchlists(user_id):
            for item in wl.get("items") or []:
                sym = str(item.get("symbol") or "").strip().upper()
                exch = str(item.get("exchange") or "").strip().upper()
                if not sym or not exch:
                    continue
                key = (sym, exch)
                if key in seen:
                    continue
                seen.add(key)
                out.append(key)
    except Exception:
        logger.exception("watchlist symbols for FCC context failed")
    return out


def _live_quote_lines(api_key: str | None, user_id: str | None = None,
                      focus: str | None = None) -> list[str]:
    lines = []
    try:
        from services.quotes_service import get_quotes
    except Exception:
        try:
            from restful_api_service import get_quotes  # type: ignore
        except Exception:
            return ["Live quotes unavailable (quotes service not importable)"]
    # Focus first, then everything the operator watches; the fixed roots are
    # only a fallback when no watchlist exists yet.
    rows: list[tuple[str, str]] = []
    if focus:
        f = focus.strip().upper()
        rows.append((f, _fo_exchange(f)))
    seen = {(s, e) for s, e in rows}
    for pair in _watchlist_symbols(user_id):
        if pair not in seen:
            seen.add(pair)
            rows.append(pair)
    if not rows:
        rows = list(_QUOTE_ROOTS)
    rows = rows[:16]
    for sym, exch in rows:
        try:
            ok, resp, _ = get_quotes(symbol=sym, exchange=exch, api_key=api_key or "")
            data = resp.get("data") if ok and isinstance(resp, dict) else None
            if isinstance(data, dict) and data.get("ltp") is not None:
                chp = float(data.get("pchg") or data.get("chp") or 0)
                lines.append(f"{sym} [{exch}] {_fmt(data.get('ltp'))} ({chp:+.2f}%)")
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


def _macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    if len(closes) < slow + signal:
        return None
    k_f = 2.0 / (fast + 1)
    k_s = 2.0 / (slow + 1)
    k_sig = 2.0 / (signal + 1)
    e_f = closes[0]
    e_s = closes[0]
    macd_series = []
    for c in closes:
        e_f = c * k_f + e_f * (1 - k_f)
        e_s = c * k_s + e_s * (1 - k_s)
        macd_series.append(e_f - e_s)
    sig_series = []
    e_sig = macd_series[slow - 1]
    for m in macd_series[slow - 1:]:
        e_sig = m * k_sig + e_sig * (1 - k_sig)
        sig_series.append(e_sig)
    cur_m = macd_series[-1]
    cur_s = sig_series[-1]
    hist = cur_m - cur_s
    prev_m = macd_series[-2] if len(macd_series) >= 2 else cur_m
    prev_s = sig_series[-2] if len(sig_series) >= 2 else cur_s
    cross = "BULLISH_CROSS" if cur_m > cur_s and prev_m <= prev_s else ("BEARISH_CROSS" if cur_m < cur_s and prev_m >= prev_s else ("BULLISH" if cur_m > cur_s else "BEARISH"))
    return {"macd": round(cur_m, 2), "signal": round(cur_s, 2), "hist": round(hist, 2), "status": cross}


def _bollinger_bands(closes: list[float], period: int = 20, mult: float = 2.0) -> dict | None:
    if len(closes) < period:
        return None
    c = closes[-period:]
    m = sum(c) / period
    var = sum((x - m) ** 2 for x in c) / period
    std = math.sqrt(var)
    u = m + mult * std
    l = m - mult * std
    bw = ((u - l) / m) * 100 if m else 0
    return {"upper": round(u, 2), "middle": round(m, 2), "lower": round(l, 2), "bandwidth_pct": round(bw, 2)}


def _moving_averages(closes: list[float], ltp: float) -> dict:
    out: dict[str, Any] = {}
    if not closes:
        return out
    ref = ltp or closes[-1]
    if len(closes) >= 5:
        out["sma20"] = round(sum(closes[-20:]) / min(20, len(closes)), 2)
    if len(closes) >= 50:
        out["sma50"] = round(sum(closes[-50:]) / 50, 2)
    if len(closes) >= 200:
        out["sma200"] = round(sum(closes[-200:]) / 200, 2)
    k = 2.0 / (20 + 1)
    e = closes[0]
    for c in closes[1:]:
        e = c * k + e * (1 - k)
    out["ema20"] = round(e, 2)
    return out


def _recent_candles_table(symbol: str, exchange: str, interval: str, count: int, api_key: str | None) -> list[str]:
    try:
        from services.history_service import get_history
        now = datetime.now()
        days = max(3, count // 10 + 2) if interval in ("1m", "3m", "5m", "15m", "30m", "1h") else max(30, count + 10)
        start = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        end = now.strftime("%Y-%m-%d")
        ok, resp, _ = get_history(symbol=symbol, exchange=exchange, interval=interval,
                                  start_date=start, end_date=end, api_key=api_key or "")
        if not ok or not isinstance(resp, dict):
            return []
        data = resp.get("data") or []
        if not isinstance(data, list) or not data:
            return []
        rows = data[-count:]
        lines = [f"{symbol} [{interval}] Recent OHLCV Candles (last {len(rows)} bars):"]
        lines.append("Time (IST) | Open | High | Low | Close | Volume")
        for r in rows:
            ts = r.get("timestamp") or 0
            try:
                dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=5, minutes=30)))
                t_str = dt.strftime("%H:%M" if interval != "D" else "%Y-%m-%d")
            except Exception:
                t_str = str(ts)
            o = _fmt(r.get("open"))
            h = _fmt(r.get("high"))
            l = _fmt(r.get("low"))
            c = _fmt(r.get("close"))
            v = f"{int(r.get('volume') or 0):,}"
            lines.append(f"{t_str} | {o} | {h} | {l} | {c} | {v}")
        return lines
    except Exception as e:
        logger.debug("recent candles table failed for %s %s: %s", symbol, interval, e)
        return []


def _depth_lines(symbol: str, exchange: str, api_key: str | None) -> list[str]:
    try:
        from services.depth_service import get_depth
        ok, resp, _ = get_depth(symbol=symbol, exchange=exchange, api_key=api_key or "")
        if not ok or not isinstance(resp, dict):
            return []
        d = resp.get("data") or {}
        bids = d.get("bids") or []
        asks = d.get("asks") or []
        tbq = float(d.get("totalbuyqty") or 0)
        tsq = float(d.get("totalsellqty") or 0)
        ratio = round(tbq / tsq, 2) if tsq > 0 else (1.0 if tbq == 0 else 99.0)
        lines = [f"{symbol} Market Depth (Order Book): Total Buy Qty={tbq:,.0f}, Total Sell Qty={tsq:,.0f} (Buyer/Seller Dominance: {ratio:.2f}x)"]
        b_top = [f"₹{_fmt(b.get('price'))} (qty {b.get('quantity', 0):,})" for b in bids[:3] if b.get('price')]
        a_top = [f"₹{_fmt(a.get('price'))} (qty {a.get('quantity', 0):,})" for a in asks[:3] if a.get('price')]
        if b_top:
            lines.append("  Top Bids: " + ", ".join(b_top))
        if a_top:
            lines.append("  Top Asks: " + ", ".join(a_top))
        return lines
    except Exception as e:
        logger.debug("depth lines %s: %s", symbol, e)
        return []


def _orderflow_lines(symbol: str) -> list[str]:
    try:
        from services.orderflow_service import get_orderflow
        res = get_orderflow(symbol=symbol, timeframe="5m", n_bars=15)
        if not res or not isinstance(res, dict):
            return []
        s = res.get("summary") or {}
        fut = res.get("target_symbol") or symbol
        lines = [
            f"{symbol} Orderflow [{fut}]: Delta={s.get('session_delta', 0):+,}, "
            f"CVD={s.get('session_cvd', 0):+,}, Bias={s.get('delta_bias', 'NEUTRAL')}, "
            f"POC=₹{_fmt(s.get('poc', 0))}, VAH=₹{_fmt(s.get('vah', 0))}, VAL=₹{_fmt(s.get('val', 0))}"
        ]
        if s.get("imbalance_summary"):
            lines.append(f"  Imbalance: {s.get('imbalance_summary')}")
        return lines
    except Exception as e:
        logger.debug("orderflow lines %s: %s", symbol, e)
        return []


def _market_brief_lines() -> list[str]:
    try:
        from services.market_brief_service import market_brief
        mb = market_brief()
        st = mb.get("session_stance") or {}
        phase = st.get("phase_label") or st.get("phase") or "LIVE"
        stance = st.get("stance") or "NEUTRAL"
        narrative = st.get("narrative") or ""
        return [f"Market Brief: Stance={stance} ({phase}). Narrative: {narrative}"]
    except Exception as e:
        logger.debug("market brief lines: %s", e)
        return []


def _news_lines(symbol: str) -> list[str]:
    try:
        from services.market_news_service import fetch_symbol_news
        res = fetch_symbol_news(symbol, limit=4)
        items = res.get("items") or []
        if items:
            formatted = [f"[{it.get('source', 'News')}]: {it.get('title', '')} ({it.get('sentiment', 'NEUTRAL')})" for it in items[:4]]
            return [f"{symbol} News & Catalysts: " + " | ".join(formatted)]
        return []
    except Exception as e:
        logger.debug("news lines %s: %s", symbol, e)
        return []


def _calendar_lines() -> list[str]:
    try:
        from services.plugin_calendar_service import economic_calendar
        cal = economic_calendar()
        evts = cal.get("events") or []
        if evts:
            formatted = [f"{e.get('time', '')} {e.get('currency', '')} {e.get('event', '')} [Impact: {e.get('impact', '')}]" for e in evts[:3]]
            return ["Economic Calendar: " + " | ".join(formatted)]
        return []
    except Exception as e:
        logger.debug("calendar lines: %s", e)
        return []


def _watchlist_lines(user_id: str | None, api_key: str | None) -> list[str]:
    pairs = _watchlist_symbols(user_id)
    if not pairs:
        return []
    try:
        from services.quotes_service import get_quotes
        advances = 0
        declines = 0
        quotes_str = []
        for s, e in pairs[:15]:
            try:
                ok, resp, _ = get_quotes(symbol=s, exchange=e, api_key=api_key or "")
                d = resp.get("data") if ok and isinstance(resp, dict) else None
                if isinstance(d, dict) and d.get("ltp") is not None:
                    chp = float(d.get("pchg") or d.get("chp") or 0)
                    if chp > 0:
                        advances += 1
                    elif chp < 0:
                        declines += 1
                    quotes_str.append(f"{s} {_fmt(d.get('ltp'))} ({chp:+.2f}%)")
            except Exception:
                continue
        breadth = f"Watchlist Breadth: {advances} Advancing, {declines} Declining (out of {len(pairs)} items)"
        return [breadth, "Watchlist Quotes: " + "; ".join(quotes_str)]
    except Exception as e:
        logger.debug("watchlist lines: %s", e)
        return []


def _chart_lines(symbol: str, ltp: float, api_key: str | None) -> list[str]:
    """Rich multi-timeframe chart snapshot for the focus symbol: OHLCV candles,
    trend, RSI, MACD, Moving Averages, Bollinger Bands, and classic floor pivots."""
    out: list[str] = []
    exch = _fo_exchange(symbol)
    try:
        daily = _daily_candles(symbol, api_key)
        closes_5m = _candles_for(symbol, exch, api_key)
        daily_closes = [c[4] for c in daily] if daily else []
        last = daily_closes[-1] if daily_closes else (closes_5m[-1] if closes_5m else 0.0)
        ref = ltp or last

        # Trend & Indicators
        mas = _moving_averages(daily_closes or closes_5m, ref)
        ema20 = mas.get("ema20", ref)
        slope5 = (daily_closes[-1] - daily_closes[-6]) / daily_closes[-6] * 100 if len(daily_closes) >= 6 and daily_closes[-6] else 0.0
        trend = "up" if ref > ema20 * 1.002 and slope5 > 0 else ("down" if ref < ema20 * 0.998 and slope5 < 0 else "range")
        rsi_val = _rsi((closes_5m or daily_closes)[-60:]) if (closes_5m or daily_closes) else None
        macd_val = _macd(closes_5m or daily_closes)

        bb_val = _bollinger_bands(closes_5m or daily_closes)

        # Pivots & Ranges
        week_hi = max(c[2] for c in daily[-5:]) if len(daily) >= 5 else None
        week_lo = min(c[3] for c in daily[-5:]) if len(daily) >= 5 else None
        month_hi = max(c[2] for c in daily[-22:]) if len(daily) >= 22 else None
        month_lo = min(c[3] for c in daily[-22:]) if len(daily) >= 22 else None

        tech_bits = [f"{symbol} Chart Trend: {trend.upper()} (LTP {'above' if ref >= ema20 else 'below'} 20-EMA ₹{_fmt(ema20)}, 5-day slope {slope5:+.1f}%)"]
        if rsi_val is not None:
            tech_bits.append(f"RSI(14): {rsi_val} ({_zone(rsi_val)})")
        if macd_val:
            tech_bits.append(f"MACD(12,26,9): {macd_val['status']} (MACD={macd_val['macd']}, Signal={macd_val['signal']}, Hist={macd_val['hist']:+.2f})")
        if mas.get("sma50"):
            tech_bits.append(f"SMA50=₹{_fmt(mas['sma50'])}, SMA200=₹{_fmt(mas.get('sma200', 0))}")
        if bb_val:
            tech_bits.append(f"Bollinger Bands: Upper=₹{_fmt(bb_val['upper'])}, Mid=₹{_fmt(bb_val['middle'])}, Lower=₹{_fmt(bb_val['lower'])} (Bandwidth: {bb_val['bandwidth_pct']:.2f}%)")
        if week_hi is not None and week_lo is not None:
            tech_bits.append(f"Week Range: ₹{_fmt(week_lo)} – ₹{_fmt(week_hi)}, Month Range: ₹{_fmt(month_lo)} – ₹{_fmt(month_hi)}")
        if len(daily) >= 2:
            _, po, ph, pl, pc = daily[-2]
            pp = (ph + pl + pc) / 3
            r1 = 2 * pp - pl
            s1 = 2 * pp - ph
            r2 = pp + (ph - pl)
            s2 = pp - (ph - pl)
            tech_bits.append(f"Classical Floor Pivots: PP=₹{_fmt(pp)}, R1=₹{_fmt(r1)}, S1=₹{_fmt(s1)}, R2=₹{_fmt(r2)}, S2=₹{_fmt(s2)}")
        out.append(" | ".join(tech_bits))

        # Add recent 5m and Daily OHLCV candle tables
        candles_5m_tbl = _recent_candles_table(symbol, exch, "5m", 10, api_key)
        if candles_5m_tbl:
            out.append("\n".join(candles_5m_tbl))
        candles_d_tbl = _recent_candles_table(symbol, exch, "D", 10, api_key)
        if candles_d_tbl:
            out.append("\n".join(candles_d_tbl))

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
            al_strs = []
            for a in alerts[:5]:
                sym = a.get("key") or a.get("symbol")
                side = a.get("side", "")
                stk = a.get("strike", "")
                entry = a.get("entry_premium", 0)
                pnl = a.get("pnl_pct", 0)
                risk = (a.get("reversal_risk") or {}).get("label", "")
                al_strs.append(f"{sym} {stk} {side} @₹{entry} (P&L {pnl:+.1f}%, {risk})")
            out.append(f"Active scalper alerts ({len(alerts)}): " + "; ".join(al_strs))
        if armed:
            out.append(f"Armed scalper monitors: {len(armed)}")
        if events:
            out.append(f"Latest scalper event: {events[0].get('msg', '')[:120]}")
        return out
    except Exception:
        return []


def _focus_ltp(symbol: str, api_key: str | None, user_id: str | None = None) -> float:
    """Live LTP for the focus symbol, trying every exchange it might live on.

    The watchlist's exchange for this symbol is tried first (the operator's
    own tagging beats any mapping), then the symbol-service guess. A symbol
    must never analyse without a price when a broker can quote it.
    """
    try:
        from services.quotes_service import get_quotes
        candidates: list[str] = []
        for s, e in _watchlist_symbols(user_id):
            if s.upper() == symbol.upper() and e not in candidates:
                candidates.append(e)
        mapped = _fo_exchange(symbol)
        if mapped not in candidates:
            candidates.append(mapped)
        for exch in candidates:
            try:
                ok, resp, _ = get_quotes(symbol=symbol, exchange=exch, api_key=api_key or "")
                data = resp.get("data") if ok and isinstance(resp, dict) else None
                ltp = float((data or {}).get("ltp") or 0)
                if ltp:
                    return ltp
            except Exception:
                continue
        return 0.0
    except Exception:
        return 0.0


def build_project_context(focus: str | None = None, api_key: str | None = None,
                          user_id: str | None = None) -> str:
    """Live OpenAlgo snapshot injected as the FCC system prompt."""
    if focus:
        focus = focus.split(":")[-1].strip().upper() or None
    if not focus:
        with _auto_lock:
            focus = _auto_state.get("symbol")
    if not focus:
        wl = _watchlist_symbols(user_id)
        if wl:
            focus = wl[0][0]
    if not focus:
        focus = "NIFTY"

    lines = [
        "You are the FCC AI analyst embedded in OpenAlgo, an open-source algo trading "
        "platform for Indian markets (NSE/BSE/MCX) with broker-neutral execution.",
        "You have complete live telemetry: multi-timeframe OHLCV candles, technical indicators "
        "(RSI, MACD, EMAs, Bollinger Bands, ATR), support/resistance pivots, market depth, "
        "orderflow, option chain with OI buildup, scalper alerts, market brief, news, and calendar.",
        "When the operator asks to analyse the chart or the market, analyse the focus symbol "
        "from the concrete telemetry below. Provide a clear technical breakdown (Trend, Key Levels, "
        "Indicators, Options Bias, Orderflow, Actionable Strategy).",
        "",
        "=== MARKET STANCE & BRIEF ===",
    ]
    lines.extend(_market_brief_lines())
    lines.append("")
    lines.append("Live quotes: " + "; ".join(_live_quote_lines(api_key, user_id, focus)))
    glob = _global_lines()
    if glob:
        lines.append("Global (dollar) references: " + "; ".join(glob))

    # Watchlist
    lines.append("")
    lines.append("=== WATCHLIST SNAPSHOT ===")
    lines.extend(_watchlist_lines(user_id, api_key))

    # Focus symbol telemetry
    lines.append("")
    lines.append(f"=== FOCUS SYMBOL: {focus} ===")
    ltp = _focus_ltp(focus, api_key, user_id)
    exch = _fo_exchange(focus)
    if ltp:
        lines.append(f"Focus price: {focus} [{exch}] LTP ₹{_fmt(ltp)}")
    lines.extend(_chart_lines(focus, ltp, api_key))
    lines.extend(_depth_lines(focus, exch, api_key))
    lines.extend(_orderflow_lines(focus))
    lines.extend(_chain_lines(focus, api_key))
    lines.extend(_news_lines(focus))

    # Scalper & Calendar
    lines.append("")
    lines.append("=== SCALPER & CALENDAR ===")
    lines.extend(_scalper_lines())
    lines.extend(_calendar_lines())

    lines.append(f"\nActive focus symbol for chart analysis: {focus}")
    return "\n".join(lines)


# ------------------------------------------------------------------------ chat

_KNOWN_ROOTS = [
    "NIFTY50", "NIFTYBANK", "BANKNIFTY", "NIFTY", "FINNIFTY", "MIDCPNIFTY",
    "SENSEX", "BANKEX", "CRUDEOIL", "NATURALGAS", "NATGASMINI", "GOLDMINI",
    "GOLD", "SILVER", "COPPER", "ZINC",
]

# English words and trading terms that uppercase to a symbol-shaped token.
# A bare caps match that lands here is ignored rather than chased as a ticker.
_STOP_TOKENS = {
    "WHAT", "WHATS", "WHY", "HOW", "WHEN", "WHERE", "WHO", "WHICH",
    "THE", "AND", "FOR", "ARE", "YOU", "YOUR", "NOW", "TODAY", "SEE",
    "CAN", "GET", "GIVE", "TELL", "SHOW", "WITH", "FROM", "ABOUT",
    "PLEASE", "ANALYSE", "ANALYZE", "ANALYSING", "ANALYZING", "ANALYSIS",
    "CHART", "CHARTS", "PRICE", "PRICES", "LEVEL", "LEVELS", "DATA",
    "TREND", "TRENDS", "VIEW", "VIEWS", "NEWS", "OM", "LOADED", "SYMBOL",
    "SYMBOLS", "CHECK", "CHECKING", "WATCHLIST", "LIST", "THIS", "THAT",
    "CURRENT", "ACTIVE", "SELECTED", "FOCUS", "FEED", "LIVE", "MARKET",
    "MARKETS", "STATUS", "UPDATE", "UPDATES", "REPORT", "REPORTS",
    "SUMMARY", "IDEA", "IDEAS", "SETUP", "SETUPS", "SIGNAL", "SIGNALS",
    "SCALPER", "SCALPING", "ADVISOR", "COMMENTARY", "TELEMETRY", "SQUAWK",
    "ORDER", "ORDERS", "POSITION", "POSITIONS", "TRADE", "TRADES", "TRADING",
    "BUY", "SELL", "LONG", "SHORT", "HOLD", "ENTRY", "EXIT", "TARGET",
    "STOPLOSS", "STOP", "LOSS", "PROFIT", "INDICATOR", "INDICATORS",
    "VOLUME", "VOLUMES", "TIMEFRAME", "TIMEFRAMES", "MINUTE", "MINUTES",
    "DAILY", "WEEKLY", "MONTHLY", "INTRADAY", "BREAKOUT", "REVERSAL",
    "SUPPORT", "RESISTANCE", "RANGE", "DEPTH", "ORDERFLOW", "OPTION",
    "OPTIONS", "CHAIN", "STRIKE", "STRIKES", "EXPIRY", "EXPIRIES",
    "CALL", "CALLS", "PUT", "PUTS", "ATM", "OTM", "ITM", "PCR", "DELTA",
    "CVD", "VWAP", "RSI", "MACD", "SMA", "EMA", "BOLLINGER", "PIVOT", "PIVOTS",
    "GOOD", "BAD", "BEST", "NEXT", "MOVE", "MOVES", "PREDICT", "PREDICTION",
    "FORECAST", "RECOMMEND", "RECOMMENDATION", "SUGGEST", "SUGGESTION",
    "HELLO", "HEY", "HI", "ASSISTANT", "AI", "FCC", "OPENALGO", "PLEASE",
    "INPUT", "INPUTS", "IMPROVE", "UPDATE", "DESKTOP", "MOBILE", "BOTH",
    "AUTO", "ARRANGE", "SECTIONS", "SECTION", "COLLAPSE", "DRAG", "DROP",
    "REARRANGE", "SYNC", "WIDGET", "WIDGETS", "SELECTION", "DEFAULT", "ALWAYS",
}

_EXPLICIT_LOADED_PHRASES = (
    "LOADED SYMBOL", "CURRENT SYMBOL", "ACTIVE SYMBOL", "THIS SYMBOL", "THE SYMBOL",
    "LOADED CHART", "CURRENT CHART", "ACTIVE CHART", "THIS CHART", "THE CHART",
    "SELECTED SYMBOL", "SELECTED CHART",
)


def resolve_focus(text: str, user_id: str | None = None) -> str | None:
    """Best-effort focus symbol from the user's message:
    1. If user explicitly refers to the loaded/current/active symbol or chart,
       return None so caller falls back to active loaded symbol.
    2. A known instrument root mentioned anywhere wins.
    3. Any symbol from operator's watchlist mentioned in text wins.
    4. A bare symbol-shaped token is accepted only when not a stop token."""
    up = (text or "").upper().strip()
    if not up:
        return None

    # 1. Explicit request for currently loaded/active chart symbol
    for phrase in _EXPLICIT_LOADED_PHRASES:
        if phrase in up:
            return None

    # 2. Known instrument roots
    for root in _KNOWN_ROOTS:
        if re.search(rf"\b{root}\b", up):
            return "NIFTY" if root == "NIFTY50" else ("BANKNIFTY" if root == "NIFTYBANK" else root)

    # 3. Watchlist symbols match
    if user_id:
        try:
            wl = _watchlist_symbols(user_id)
            for sym, _ in wl:
                s_up = sym.upper()
                if re.search(rf"\b{re.escape(s_up)}\b", up):
                    return sym
        except Exception:
            pass

    # 4. Bare candidate tokens
    tokens = re.findall(r"\b([A-Z][A-Z0-9&-]{2,14})\b", up)
    for tok in tokens:
        if tok not in _STOP_TOKENS:
            return tok
    return None


def chat(messages: list[dict], model: str | None = None,
         use_project_context: bool = True, focus: str | None = None,
         api_key: str | None = None, user_id: str | None = None) -> dict:
    """Grounded chat through the FCC proxy (Anthropic API with OpenAI fallback)."""
    if not FCC_ENABLED:
        raise RuntimeError("FCC integration is disabled (FCC_ENABLED=false)")

    if use_project_context and not focus and messages:
        focus = resolve_focus(next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""),
            user_id=user_id)

    if use_project_context and not focus:
        with _auto_lock:
            focus = _auto_state.get("symbol")
        if not focus:
            wl = _watchlist_symbols(user_id)
            if wl:
                focus = wl[0][0]
        if not focus:
            focus = "NIFTY"

    sys_prompt = build_project_context(focus, api_key, user_id) if use_project_context else ""
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
_AUTO_STATE_PATH = os.path.join(PROJECT_ROOT, ".fcc_squawk_state.json")
_auto_state: dict[str, Any] = {"enabled": True, "symbol": "NIFTY", "interval": 60,
                               "last_error": "", "last_ts": 0.0, "thread": None,
                               "stopped_reason": ""}

# Squawk runs until the operator switches it off — no session timer. The
# event engine keeps LLM spend gated (silent when nothing new, per-family
# cooldowns), and the market-hours guard pauses scans outside NSE hours.

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
    current_sym = ""
    try:
        while True:
            with _auto_lock:
                if not _auto_state["enabled"]:
                    return
                symbol = str(_auto_state["symbol"]).upper().split(":")[-1]
                interval = max(20, int(_auto_state["interval"] or 45))
            err = ""
            now = time.time()
            sym_changed = (symbol != current_sym)
            if sym_changed or (now - last_poll >= interval):
                last_poll = now
                current_sym = symbol
                try:
                    item = generate_commentary(symbol=symbol, api_key=_stored_api_key(), force=True)
                    if item:
                        logger.info("auto squawk produced bullet for %s: %s", symbol, item.get("headline"))
                except Exception as e:  # noqa: BLE001
                    err = str(e)[:200]
                    logger.warning("auto squawk %s: %s", symbol, err)
                with _auto_lock:
                    _auto_state["last_error"] = err
                    _auto_state["last_ts"] = time.time()

            # sleep in short slices so a disable or symbol change is honoured quickly
            deadline = time.time() + 1.0
            while time.time() < deadline:
                with _auto_lock:
                    if not _auto_state["enabled"]:
                        _persist_auto_state()
                        return
                    if str(_auto_state["symbol"]).upper().split(":")[-1] != current_sym:
                        break
                time.sleep(0.25)
    except Exception as e:  # noqa: BLE001
        logger.exception("unhandled error in _auto_loop: %s", e)
    finally:
        with _auto_lock:
            _auto_state["thread"] = None


def set_auto_squawk(enabled: bool, symbol: str | None = None,
                    interval: int | None = None) -> dict:
    """Start/stop the auto-squawk loop for a symbol.

    Always on: runs continuously for the active symbol and produces real-time
    institutional trading bullets on cadence and on every symbol switch."""
    with _auto_lock:
        _auto_state["enabled"] = bool(enabled)
        if symbol:
            _auto_state["symbol"] = str(symbol).upper().split(":")[-1]
        if interval:
            _auto_state["interval"] = max(20, int(interval))
        if enabled:
            _auto_state["stopped_reason"] = ""
        t = _auto_state.get("thread")
        if enabled and (not t or not t.is_alive()):
            t = threading.Thread(target=_auto_loop, name="fcc-auto-squawk", daemon=True)
            _auto_state["thread"] = t
            t.start()
        _persist_auto_state()
    return auto_squawk_status()


def _persist_auto_state() -> None:
    """Checkpoint the session so a restart can resume it (caller holds _auto_lock).
    Best effort — a dead disk never blocks the toggle."""
    try:
        snap = {k: v for k, v in _auto_state.items() if k in
                ("enabled", "symbol", "interval")}
        with open(_AUTO_STATE_PATH, "w") as f:
            json.dump(snap, f)
    except Exception as e:
        logger.debug("squawk state persist failed: %s", e)


def _resume_on_boot() -> None:
    """Re-arm or start a squawk session. Defaults to ON for NIFTY if no state persisted."""
    saved = {}
    try:
        if os.path.exists(_AUTO_STATE_PATH):
            with open(_AUTO_STATE_PATH, "r") as f:
                saved = json.load(f)
    except Exception:
        saved = {}
    enabled = saved.get("enabled", True) if isinstance(saved, dict) else True
    symbol = str(saved.get("symbol") or "NIFTY") if isinstance(saved, dict) else "NIFTY"
    interval = max(30, int(saved.get("interval") or 60)) if isinstance(saved, dict) else 60
    with _auto_lock:
        t = _auto_state.get("thread")
        if _auto_state.get("enabled") and t and t.is_alive():
            return  # this generation already runs a session
        if enabled:
            _auto_state.update({
                "enabled": True,
                "symbol": symbol,
                "interval": interval,
                "stopped_reason": "",
            })
            t = threading.Thread(target=_auto_loop, name="fcc-auto-squawk", daemon=True)
            _auto_state["thread"] = t
            t.start()
            logger.info("squawk loop running (default ON) for %s", symbol)


def auto_squawk_status() -> dict:
    _resume_on_boot()
    with _auto_lock:
        out = {k: v for k, v in _auto_state.items() if k != "thread"}
        t = _auto_state.get("thread")
        out["running"] = bool(t and t.is_alive())
        return out


# Start auto-squawk by default on boot
try:
    _resume_on_boot()
except Exception as _e:
    logger.debug("init auto-squawk failed: %s", _e)


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
                        api_key: str | None = None, force: bool = False) -> dict | None:
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
    # --- Classical Pivots & Support/Resistance levels -------------------
    if daily and len(daily) >= 2:
        _, po, ph, pl, pc = daily[-2]
        pp = (ph + pl + pc) / 3
        r1 = 2 * pp - pl
        s1 = 2 * pp - ph
        r2 = pp + (ph - pl)
        s2 = pp - (ph - pl)
        if ltp and r1 and abs(ltp - r1) / ltp <= 0.003:
            _evt(f"PIVOT LEVEL: Testing R1 resistance ₹{_fmt(r1, 0)} — watch for rejection or breakout", cooldown=900)
        elif ltp and s1 and abs(ltp - s1) / ltp <= 0.003:
            _evt(f"PIVOT LEVEL: Testing S1 support ₹{_fmt(s1, 0)} — watch for bounce or breakdown", cooldown=900)

    # --- Technical Indicators: MACD & Bollinger Bands -------------------
    macd_res = _macd(closes) if len(closes) >= 35 else None
    if macd_res:
        macd_st = macd_res.get("status")
        if macd_st in ("BULLISH_CROSS", "BEARISH_CROSS"):
            _evt(f"MACD 5M: {macd_st.replace('_', ' ')} (Hist: {macd_res['hist']:+.2f}) — momentum expanding", cooldown=1200)

    bb_res = _bollinger_bands(closes) if len(closes) >= 20 else None
    if bb_res and bb_res.get("bandwidth_pct", 99) < 0.6:
        _evt(f"BOLLINGER SQUEEZE: Volatility compressed ({bb_res['bandwidth_pct']:.2f}% bandwidth) — explosive move pending", cooldown=1800)

    # --- Depth (Order Book) signals -------------------------------------
    try:
        from services.depth_service import get_depth
        ok_d, resp_d, _ = get_depth(symbol=clean, exchange=exch, api_key=api_key or "")
        if ok_d and isinstance(resp_d, dict):
            dd = resp_d.get("data") or {}
            tbq = float(dd.get("totalbuyqty") or 0)
            tsq = float(dd.get("totalsellqty") or 0)
            if tbq > 0 and tsq > 0:
                d_ratio = tbq / tsq
                if d_ratio >= 1.65:
                    _evt(f"DEPTH PRESSURE: Strong buyer demand ({d_ratio:.1f}x bids over asks) — support cushion firming", cooldown=900)
                elif d_ratio <= 0.6:
                    _evt(f"DEPTH PRESSURE: Strong seller supply ({1/d_ratio:.1f}x asks over bids) — overhead supply active", cooldown=900)
    except Exception as e:
        logger.debug("depth event failed: %s", e)

    # --- Orderflow & Volume Delta signals -------------------------------
    try:
        from services.orderflow_service import get_orderflow
        res_of = get_orderflow(symbol=clean, timeframe="5m", n_bars=15)
        if res_of and isinstance(res_of, dict):
            ofs = res_of.get("summary") or {}
            s_delta = ofs.get("session_delta", 0)
            bias_of = ofs.get("delta_bias", "")
            poc_val = ofs.get("poc", 0)
            if "BUYER" in bias_of.upper():
                _evt(f"ORDERFLOW: Positive volume delta (+{s_delta:,}) with aggressive buyer flow near POC ₹{_fmt(poc_val, 0)}", cooldown=900)
            elif "SELLER" in bias_of.upper():
                _evt(f"ORDERFLOW: Negative volume delta ({s_delta:,}) with aggressive seller flow near POC ₹{_fmt(poc_val, 0)}", cooldown=900)
    except Exception as e:
        logger.debug("orderflow event failed: %s", e)

    # --- Scalper Advisor Signals ----------------------------------------
    try:
        from services.scalper_advisor_service import alert_history
        hist = alert_history()
        alerts = hist.get("alerts") or []
        sym_alerts = [a for a in alerts if clean in (a.get("key") or a.get("symbol") or "").upper() and (a.get("status") or "").lower() == "active"]
        if sym_alerts:
            sa = sym_alerts[0]
            al_side = sa.get("side") or "CE"
            al_stk = sa.get("strike") or "ATM"
            al_entry = float(sa.get("entry_premium") or 0)
            al_cur = float(sa.get("current_premium") or al_entry or 0)
            al_pnl = float(sa.get("pnl_pct") or 0)
            al_tgt = float(sa.get("target_premium") or 0)
            al_sl = float(sa.get("sl_premium") or 0)
            al_risk = (sa.get("reversal_risk") or {}).get("label")
            _evt(f"⚡ SCALPER ALERT: Active BUY {clean} {al_stk} {al_side} @ ₹{al_entry:.2f} (LTP ₹{al_cur:.2f}, P&L {al_pnl:+.1f}%) | Tgt ₹{al_tgt:.2f}, SL ₹{al_sl:.2f}", cooldown=600)
            if al_risk and ("RISK" in al_risk.upper() or "REVERSAL" in al_risk.upper()):
                _evt(f"⚠️ SCALPER WARNING: {al_risk} on {clean} {al_side} — trail stop loss closely", cooldown=900)
    except Exception as e:
        logger.debug("scalper event failed: %s", e)

    # --- Market Brief & Macro Stance Confluence -------------------------
    try:
        from services.market_brief_service import market_brief
        mb = market_brief()
        mst = mb.get("session_stance") or {}
        m_stance = (mst.get("stance") or "").upper()
        if m_stance in ("BULLISH", "BEARISH"):
            p_mst = prev.get("m_stance")
            if m_stance != p_mst:
                _evt(f"MACRO SESSION STANCE: Market overall stance is {m_stance} ({mst.get('phase_label') or mst.get('phase') or 'LIVE'})", cooldown=1800)
                st["sig"]["m_stance"] = m_stance
    except Exception as e:
        logger.debug("market brief stance event failed: %s", e)

    # --- Watchlist Breadth Context ---------------------------------------
    try:
        pairs = _watchlist_symbols(user_id)
        if pairs:
            from services.quotes_service import get_quotes
            adv = dec = 0
            for s_item, e_item in pairs[:15]:
                try:
                    ok_q, resp_q, _ = get_quotes(symbol=s_item, exchange=e_item, api_key=api_key or "")
                    qd = resp_q.get("data") if ok_q and isinstance(resp_q, dict) else None
                    if isinstance(qd, dict) and qd.get("ltp") is not None:
                        cp = float(qd.get("pchg") or qd.get("chp") or 0)
                        if cp > 0: adv += 1
                        elif cp < 0: dec += 1
                except Exception:
                    continue
            total_wl = adv + dec
            if total_wl >= 4:
                if adv / total_wl >= 0.75:
                    _evt(f"WATCHLIST BREADTH: Broad bullish market surge ({adv}/{total_wl} green across watchlist)", cooldown=1800)
                elif dec / total_wl >= 0.75:
                    _evt(f"WATCHLIST BREADTH: Broad market risk-off selloff ({dec}/{total_wl} red across watchlist)", cooldown=1800)
    except Exception as e:
        logger.debug("watchlist breadth event failed: %s", e)

    # --- News & Catalyst Drivers ----------------------------------------
    # Two wires: symbol headlines (TradingView + keyword RSS) and the general
    # TradingView wire. Either can raise an alert; both are cooldown-gated so
    # the same headline never spams the squawk.
    try:
        from services.market_news_service import fetch_symbol_news
        n_res = fetch_symbol_news(clean, limit=2)
        n_items = n_res.get("items") or []
        if n_items:
            first_n = n_items[0]
            n_title = first_n.get("title", "")
            if n_title:
                _evt(f"NEWS CATALYST [{clean}]: {n_title[:85]}", cooldown=1800)
    except Exception as e:
        logger.debug("news event failed: %s", e)
    try:
        from services.market_news_service import fetch_news
        general = (fetch_news(15).get("articles") or [])
        high_urg = [a for a in general if (a.get("urgency") or 2) <= 1 or (a.get("category") in ("", "markets"))]
        if high_urg:
            g = high_urg[0]
            g_title = (g.get("title") or "").strip()
            g_src = g.get("source") or "Wire"
            if g_title:
                sent = (g.get("sentiment") or "").upper()
                sent_tag = f" ({sent})" if sent in ("BULLISH", "BEARISH") else ""
                _evt(f"NEWS ALERT [{g_src}]{sent_tag}: {g_title[:80]}", cooldown=1200)
    except Exception as e:
        logger.debug("general news alert failed: %s", e)

    # --- Economic Calendar ----------------------------------------------
    try:
        from services.plugin_calendar_service import economic_calendar
        cal_res = economic_calendar()
        c_events = cal_res.get("events") or []
        for ce in c_events[:2]:
            if ce.get("impact") in ("HIGH", "MEDIUM") or ce.get("event"):
                _evt(f"CALENDAR EVENT: {ce.get('time', '')} {ce.get('currency', '')} {ce.get('event', '')} ({ce.get('impact', '')} Impact)", cooldown=3600)
                break
    except Exception as e:
        logger.debug("calendar event failed: %s", e)

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

    # If no discrete event occurred, stay silent unless forced (auto loop or manual button)
    if not signals:
        if force:
            signals = [
                f"{clean} tracking at ₹{_fmt(ltp)} ({chp:+.2f}%) · 5m RSI {rsi if rsi is not None else 'N/A'} · Day Range ₹{_fmt(low)}-₹{_fmt(high)}"
                + (f" · PCR {pcr}" if pcr is not None else "")
                + (f" · Max Pain ₹{_fmt(max_pain, 0)}" if max_pain else "")
            ]
        else:
            return None
    # Algorithmic tips: one bullet per NEW event, always actionable.
    tips = list(signals)

    # LLM tip polish — turns the raw events into crisp actionable bullets.
    # Falls back to the raw event bullets on any failure.
    if FCC_ENABLED:
        try:
            sys_p = ("You are a trading desk tip generator for Indian F&O markets. "
                     "You receive ONLY the NEW events for a symbol plus current "
                     "telemetry including Watchlist, Options Chain, Depth, Orderflow, "
                     "Scalper alerts, Macro Brief, News, Calendar, and Technical Levels. "
                     "Output 2-6 short trading-tip bullets (max 18 words each) with "
                     "concrete ₹ levels where possible: entry/avoid/exit hints, what "
                     "to watch next, risk note. Do NOT restate events that are not in the "
                     "list; no disclaimers, no filler. Output strictly valid JSON: "
                     "{\"headline\": str, \"tips\": [str], \"bias\": \"BULLISH\"|\"BEARISH\"|\"NEUTRAL\", \"tag\": str}.")
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


def scan_watchlist_commentary(user_id: str | None = None, api_key: str | None = None,
                              model: str | None = None) -> dict:
    """Scan all instruments across the operator's watchlist, synthesize their
    momentum, scalper signals, and key levels, and emit a unified squawk bullet."""
    pairs = _watchlist_symbols(user_id)
    if not pairs:
        pairs = list(_QUOTE_ROOTS)

    from services.quotes_service import get_quotes
    from services.scalper_advisor_service import alert_history

    alerts = (alert_history().get("alerts") or [])
    active_alerts = [a for a in alerts if (a.get("status") or "").lower() == "active"]
    active_keys = {str(a.get("key") or a.get("symbol") or "").upper(): a for a in active_alerts}

    adv = 0
    dec = 0
    bullets = []
    top_movers = []

    for sym, exch in pairs:
        try:
            ok, resp, _ = get_quotes(symbol=sym, exchange=exch, api_key=api_key or "")
            data = resp.get("data") if ok and isinstance(resp, dict) else None
            if isinstance(data, dict) and data.get("ltp") is not None:
                ltp = float(data.get("ltp") or 0)
                chp = float(data.get("pchg") or data.get("chp") or 0)
                if chp > 0:
                    adv += 1
                elif chp < 0:
                    dec += 1
                top_movers.append((abs(chp), sym, ltp, chp))
                # Check if scalper alert exists
                if sym in active_keys:
                    sa = active_keys[sym]
                    bullets.append(f"⚡ {sym} Scalper BUY {sa.get('strike')} {sa.get('side')} active (P&L {sa.get('pnl_pct', 0):+.1f}%)")
        except Exception:
            continue

    top_movers.sort(key=lambda x: x[0], reverse=True)
    for _, s, p, c in top_movers[:4]:
        bullets.append(f"{s}: ₹{_fmt(p)} ({c:+.2f}%)")

    breadth_note = f"Watchlist Breadth: {adv} Advancing, {dec} Declining out of {len(pairs)} instruments"
    bullets.insert(0, breadth_note)

    overall_bias = "BULLISH" if adv > dec * 1.3 else ("BEARISH" if dec > adv * 1.3 else "NEUTRAL")
    time_str = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%d %b %H:%M")
    headline = f"Watchlist Scan ({len(pairs)} Symbols) · {overall_bias.title()}"
    commentary = "\n".join("• " + b for b in bullets)

    return _store_commentary({
        "timestamp": time_str,
        "symbol": "WATCHLIST",
        "headline": headline,
        "commentary": commentary,
        "bias": overall_bias,
        "tag": "Watchlist Scan",
        "is_trigger": False,
        "triggers": [],
        "signals": bullets,
        "metrics": {"total": len(pairs), "advances": adv, "declines": dec},
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
    # node CLIs installed under ~/.local/opt/nodeXX/bin (dsh, pi, grok, opencode)
    for opt_dir in sorted(glob.glob(os.path.join(home, ".local", "opt", "node*", "bin")), reverse=True):
        extra.append(opt_dir)
    # nvm-managed node, if present
    nvm_bins = os.path.join(home, ".nvm", "versions", "node")
    if os.path.isdir(nvm_bins):
        for v in sorted(os.listdir(nvm_bins), reverse=True):
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
    # freebuff is listed only when its launcher exists AND it can actually run
    # headless — the TUI-only CLI cannot be driven by the agent runner.
    return {a: bool(_launcher_path(a)) and a != "freebuff" for a in FCC_AGENTS}


# Per-agent headless argv builders. Each FCC launcher wraps a different CLI
# with its own print-mode syntax; forcing one claude-style command line onto
# all of them broke everything except claude itself (codex needs a subcommand,
# antigravity's -p swallows the next flag, pi/dsh reject claude's permission
# flag, opencode v2 uses `run`). freebuff is interactive-only.
def _agent_argv(agent: str, launcher: str, prompt: str,
                model: str | None) -> list[str]:
    if agent == "claude":
        argv = [launcher, "-p", "--dangerously-skip-permissions"]
        if model:
            argv += ["--model", model]
        return argv + [prompt]
    if agent == "codex":
        argv = [launcher, "exec", "--dangerously-bypass-approvals-and-sandbox"]
        if model:
            argv += ["-m", model]
        return argv + [prompt]
    if agent == "opencode":
        return [launcher, "run", prompt]
    if agent == "grok":
        return [launcher, "-p", prompt]
    if agent == "dsh":
        return [launcher, "--profile", "headless", prompt]
    if agent == "pi":
        argv = [launcher, "--print"]
        if model:
            argv += ["--model", model]
        return argv + [prompt]
    if agent == "antigravity":
        argv = [launcher, "--dangerously-skip-permissions", "-p"]
        if model:
            argv += ["--model", model]
        return argv + [prompt]
    # Unknown agent: claude-style as a best effort.
    return [launcher, "-p", "--dangerously-skip-permissions", prompt]


def agent_status(run_id: str) -> dict:
    with _run_lock:
        run = _runs.get(run_id)
        if not run:
            raise KeyError(run_id)
        # _proc is a live Popen — never let it reach jsonify.
        return {k: v for k, v in run.items() if k != "_proc"}


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
    if agent == "freebuff":
        raise ValueError("freebuff runs interactively only and has no headless mode")
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

    cmd = _agent_argv(agent, launcher, prompt, model)

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
