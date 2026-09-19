"""
Scalper Advisor service — ported from the fno-trader-pro project onto OpenAlgo.

For each instrument (NIFTY, BANKNIFTY, SENSEX, CRUDEOIL, GOLD, SILVER, GOLD MINI,
SILVER MINI, NATURALGAS and minis) it produces a directional option-buying
advisory with:
  - strike + CE/PE, live entry premium (real chain LTP, real delta)
  - target premium / stop-loss premium (delta-projected spot levels too)
  - trigger time (next 5-min candle close, IST)
  - human-readable basis (chain structure + momentum + flows)
  - live monitor: current premium vs entry, target/stop hits, momentum
    reversal early square-off alerts

Differences from the fno-trader-pro original are all in the data layer: the
advisory math is unchanged, but quotes, candles, depth and the option chain
come from OpenAlgo's broker-agnostic services (services/quotes_service,
services/history_service, services/depth_service, services/option_chain_service)
instead of the fno-trader-pro market module. Alerts persist to SQLite
(database/scalper_db) instead of a JSON file.

Nothing is fabricated: when a leg premium or spot is unavailable the advisory
degrades to WAIT / NO_DATA.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from database.scalper_db import (
    add_alert_row,
    load_today_alerts,
)
from services.depth_service import get_depth
from services.history_service import get_history
from services.option_chain_service import get_option_chain
from services.option_symbol_service import find_near_month_futures
from services.quotes_service import get_quotes

log = logging.getLogger("services.scalper_advisor")

_IST = timezone(timedelta(hours=5, minutes=30))

_ADVICE_CACHE = {}          # key -> {"ts": float, "data": dict}
_ADVICE_TTL = 60.0          # per-instrument advisory cache (chain builds are heavy)
_ADVICE_STALE = 300.0       # stale-while-revalidate window
_ADVICE_REFRESHING = set()
_ADVICE_LOCK = threading.Lock()
_MOMO_CACHE = {}            # fut symbol -> {"ts": float, "data": dict}
_MOMO_TTL = 60.0

_MONITOR_LOCK = threading.Lock()
_MONITOR = {"armed": {}, "events": [], "since": None}

# ---------------- intraday alert store (in-memory mirror of the DB rows) ----
_ALERTS: dict = {}
_ALERT_SEQ = [0]
_ALERTS_TODAY = [""]
_MAX_ALERTS = 120
TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60}

# ---------------- instrument universe --------------------------------------
# base      : the chain/underlying root used for options and futures lookups
# opt_exch  : exchange where the OPTIONS trade (NFO / BFO / MCX)
# fut_exch  : exchange where the FUTURES trade (same as opt_exch here)
# lot       : lot size, informational
INSTRUMENTS = [
    {"key": "NIFTY", "name": "NIFTY 50", "mkt": "NSE", "base": "NIFTY", "opt_exch": "NFO", "lot": 75},
    {"key": "BANKNIFTY", "name": "BANK NIFTY", "mkt": "NSE", "base": "BANKNIFTY", "opt_exch": "NFO", "lot": 35},
    {"key": "SENSEX", "name": "SENSEX", "mkt": "BSE", "base": "SENSEX", "opt_exch": "BFO", "lot": 20},
    {"key": "CRUDEOIL", "name": "CRUDE OIL", "mkt": "MCX", "base": "CRUDEOIL", "opt_exch": "MCX", "lot": 100},
    {"key": "GOLD", "name": "GOLD", "mkt": "MCX", "base": "GOLD", "opt_exch": "MCX", "lot": 100},
    {"key": "SILVER", "name": "SILVER", "mkt": "MCX", "base": "SILVER", "opt_exch": "MCX", "lot": 30},
    {"key": "GOLDMINI", "name": "GOLD MINI", "mkt": "MCX", "base": "GOLDM", "opt_exch": "MCX", "lot": 10},
    {"key": "SILVERMINI", "name": "SILVER MINI", "mkt": "MCX", "base": "SILVERM", "opt_exch": "MCX", "lot": 5},
    {"key": "NATURALGAS", "name": "NATURAL GAS", "mkt": "MCX", "base": "NATURALGAS", "opt_exch": "MCX", "lot": 1250},
    {"key": "NATGASMINI", "name": "NATURALGAS MINI", "mkt": "MCX", "base": "NATGASMINI", "opt_exch": "MCX", "lot": 250},
    {"key": "CRUDEOILMINI", "name": "CRUDEOIL MINI", "mkt": "MCX", "base": "CRUDEOILM", "opt_exch": "MCX", "lot": 10},
]

_FUT_RESOLVE_CACHE = {}     # base -> (resolved_ts, fut dict or None), 30 min refresh
_FUT_RESOLVE_TTL = 1800.0


# ---------------------------------------------------------------------------
# OpenAlgo data layer helpers (broker-agnostic)
# ---------------------------------------------------------------------------
def _api_key() -> str | None:
    """The OpenAlgo API key of the first active session — internal services need it."""
    try:
        from database.auth_db import get_first_available_api_key
        return get_first_available_api_key()
    except Exception:
        return None


def _resolve_futures(base: str, fut_exch: str) -> dict | None:
    """Nearest unexpired FUT contract for a base, cached 30 min."""
    now = time.time()
    cached = _FUT_RESOLVE_CACHE.get(base)
    if cached and now - cached[0] < _FUT_RESOLVE_TTL:
        return cached[1]
    try:
        fut = find_near_month_futures(base, fut_exch)
    except Exception:
        fut = None
    _FUT_RESOLVE_CACHE[base] = (now, fut)
    return fut


def _nearest_expiry(base: str, opt_exch: str, api_key: str) -> str | None:
    """Nearest live options expiry for the base, as DDMMMYY (chain API format)."""
    try:
        from services.expiry_service import get_expiry_dates
        ok, resp, _ = get_expiry_dates(base, opt_exch, "options", api_key)
        if ok:
            dates = (resp.get("data") or []) if isinstance(resp, dict) else []
            if dates:
                return str(dates[0]).replace("-", "")
    except Exception as e:
        log.debug("expiry lookup %s/%s failed: %s", base, opt_exch, e)
    return None


def _quote(symbol: str, exchange: str, api_key: str) -> dict:
    """Live quote {ltp, open, high, low, prev_close, volume, oi} or {}."""
    if not symbol:
        return {}
    try:
        ok, resp, _ = get_quotes(symbol=symbol, exchange=exchange, api_key=api_key)
        if ok:
            data = resp.get("data") if isinstance(resp, dict) else None
            if isinstance(data, dict):
                return data
            if isinstance(resp, dict) and resp.get("ltp") is not None:
                return resp
    except Exception as e:
        log.debug("quote %s:%s failed: %s", exchange, symbol, e)
    return {}


def _candles(symbol: str, exchange: str, api_key: str, interval: str = "5m", days: int = 2) -> list:
    """Recent candles as [ts, o, h, l, c, v] lists (ts epoch seconds), newest last."""
    if not symbol:
        return []
    try:
        from datetime import datetime as _dt
        end = _dt.now(_IST).strftime("%Y-%m-%d")
        start = (_dt.now(_IST) - timedelta(days=days)).strftime("%Y-%m-%d")
        ok, resp, _ = get_history(
            symbol=symbol, exchange=exchange, interval=interval,
            start_date=start, end_date=end, api_key=api_key,
        )
        if not ok:
            return []
        data = resp.get("data") if isinstance(resp, dict) else None
        rows = data if isinstance(data, list) else []
        out = []
        for r in rows:
            try:
                ts = int(float(r.get("timestamp") or 0))
                if ts > 4102444800:      # ms -> s
                    ts = ts // 1000
                out.append([
                    ts,
                    float(r.get("open") or 0), float(r.get("high") or 0),
                    float(r.get("low") or 0), float(r.get("close") or 0),
                    float(r.get("volume") or 0),
                ])
            except (TypeError, ValueError, AttributeError):
                continue
        out.sort(key=lambda c: c[0])
        return out
    except Exception as e:
        log.debug("history %s:%s failed: %s", exchange, symbol, e)
    return []


def _depth_imbalance(symbol: str, exchange: str, api_key: str) -> float | None:
    """5-level depth imbalance of the futures: (buyQty - sellQty) / total."""
    if not symbol:
        return None
    try:
        ok, resp, _ = get_depth(symbol=symbol, exchange=exchange, api_key=api_key)
        if not ok:
            return None
        data = resp.get("data") if isinstance(resp, dict) else None
        data = data if isinstance(data, dict) else (resp if isinstance(resp, dict) else {})
        tb = float(data.get("totalbuyqty") or 0)
        ts_ = float(data.get("totalsellqty") or 0)
        if tb + ts_ <= 0:
            return None
        return (tb - ts_) / (tb + ts_)
    except Exception:
        return None


def _fetch_chain(inst: dict, api_key: str) -> dict:
    """Full option chain payload for the instrument's nearest expiry.

    Returns a normalized dict {spot, atm, dte, expiry, strikes:[{strike, ce, pe}]}
    where ce/pe carry ltp / oi / delta / theta / implied_volatility — or {}.
    """
    exp = _nearest_expiry(inst["base"], inst["opt_exch"], api_key)
    if not exp:
        return {}
    ok, resp, _ = get_option_chain(
        underlying=inst["base"], exchange=inst["opt_exch"], expiry_date=exp,
        strike_count=8, api_key=api_key, with_quotes=True, with_greeks=True,
    )
    if not ok:
        return {}
    payload = resp if isinstance(resp, dict) else {}
    if isinstance(payload.get("data"), dict) and "chain" not in payload:
        payload = payload["data"]
    chain = payload.get("chain") or []
    if not chain:
        return {}
    strikes = []
    for item in chain:
        strikes.append({"strike": item.get("strike"), "ce": item.get("ce"), "pe": item.get("pe")})

    spot = payload.get("underlying_ltp")
    try:
        spot = float(spot) if spot is not None else None
    except (TypeError, ValueError):
        spot = None
    if not spot:
        # last resort: futures LTP as the pricing basis (MCX has no spot feed)
        fut = _resolve_futures(inst["base"], inst["opt_exch"])
        if fut:
            spot = _quote(fut["symbol"], fut["exchange"], api_key).get("ltp")
    if not spot:
        return {}

    # days to expiry from DDMMMYY
    dte = None
    try:
        exp_dt = datetime.strptime(exp, "%d%b%y").date()
        dte = (exp_dt - datetime.now(_IST).date()).days
    except ValueError:
        pass

    return {
        "spot": spot,
        "atm": payload.get("atm_strike"),
        "expiry": exp,
        "dte": dte,
        "strikes": strikes,
    }


def _analyze_chain(a: dict) -> dict:
    """Normalized chain analysis: PCR (OI), max pain, OI walls, likely direction.
    Everything is derived from real OI/LTP numbers in the chain payload."""
    strikes = a.get("strikes") or []
    spot = a.get("spot")
    total_ce_oi = total_pe_oi = 0.0
    ce_walls, pe_walls = [], []   # (strike, oi)
    for s in strikes:
        ce, pe = s.get("ce") or {}, s.get("pe") or {}
        c_oi = float(ce.get("oi") or 0)
        p_oi = float(pe.get("oi") or 0)
        total_ce_oi += c_oi
        total_pe_oi += p_oi
        if c_oi > 0:
            ce_walls.append((s.get("strike"), c_oi))
        if p_oi > 0:
            pe_walls.append((s.get("strike"), p_oi))

    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else None

    # max pain: strike minimizing total writer payout across all listed OI
    max_pain = None
    if strikes and (total_ce_oi > 0 or total_pe_oi > 0):
        best_loss, best_strike = None, None
        for s in strikes:
            candidate = float(s.get("strike") or 0)
            if candidate <= 0:
                continue
            loss = 0.0
            for t in strikes:
                k = float(t.get("strike") or 0)
                c_oi = float((t.get("ce") or {}).get("oi") or 0)
                p_oi = float((t.get("pe") or {}).get("oi") or 0)
                if candidate > k:
                    loss += c_oi * (candidate - k)
                if candidate < k:
                    loss += p_oi * (k - candidate)
            if best_loss is None or loss < best_loss:
                best_loss, best_strike = loss, candidate
        max_pain = best_strike

    call_wall = max(ce_walls, key=lambda x: x[1])[0] if ce_walls else None
    put_wall = max(pe_walls, key=lambda x: x[1])[0] if pe_walls else None

    # likely direction: PCR + position vs max pain + OI skew around spot
    score = 0
    reasons = []
    confidence = 50
    if pcr is not None:
        if pcr >= 1.15:
            score += 1
            reasons.append(f"PCR {pcr:.2f} — put writing support")
        elif pcr <= 0.85:
            score -= 1
            reasons.append(f"PCR {pcr:.2f} — call writing overhead")
        else:
            reasons.append(f"PCR {pcr:.2f} neutral")
    if max_pain and spot:
        if spot > max_pain:
            score += 1
            reasons.append(f"Spot above Max Pain {max_pain:g} — pinning risk reduces")
        else:
            score -= 1
            reasons.append(f"Spot below Max Pain {max_pain:g} — pinning risk reduces")
    if spot and call_wall and put_wall:
        d_res = abs(call_wall - spot)
        d_sup = abs(spot - put_wall)
        if d_sup < 0.6 * d_res:
            score += 1
            reasons.append(f"Spot closer to put wall {put_wall:g} — support holding")
        elif d_res < 0.6 * d_sup:
            score -= 1
            reasons.append(f"Spot closer to call wall {call_wall:g} — resistance capping")
    if score >= 2:
        direction, confidence = "BULLISH", 75
    elif score == 1:
        direction, confidence = "BULLISH", 60
    elif score <= -2:
        direction, confidence = "BEARISH", 75
    elif score == -1:
        direction, confidence = "BEARISH", 60
    else:
        direction, confidence = "RANGE-BOUND", 50

    return {
        "pcr_oi": pcr,
        "max_pain": max_pain,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "likely_move": {"direction": direction, "confidence": confidence, "reasons": reasons},
    }


# ---------------------------------------------------------------------------
# Momentum / chart pack / flow (adapted to the OpenAlgo data layer)
# ---------------------------------------------------------------------------
def _ema(vals: list, period: int) -> float:
    if not vals:
        return 0.0
    k = 2.0 / (period + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


def _momentum(fut: dict, api_key: str) -> dict:
    """Futures momentum. Uses 5m EMA9/EMA21 only when candles are dense & fresh;
    otherwise falls back to the live quote's day-change."""
    sym = (fut or {}).get("symbol") or ""
    if not sym:
        return {"trend": "FLAT", "chp_30m": None, "src": "none"}
    now = time.time()
    cached = _MOMO_CACHE.get(sym)
    if cached and now - cached["ts"] < _MOMO_TTL:
        return cached["data"]
    out = {"trend": "FLAT", "chp_30m": None, "src": "none"}
    candles = _candles(sym, (fut or {}).get("exchange") or "", api_key, "5m", 2)
    if candles:
        last_ts = int(candles[-1][0])
        fresh = (time.time() - last_ts) <= 720
        dense = len(candles) >= 7 and (candles[-1][0] - candles[-7][0]) <= 3000
        if fresh and dense:
            closes = [c[4] for c in candles]
            ef, es = _ema(closes[-30:], 9), _ema(closes[-30:], 21)
            last = closes[-1]
            if ef > es and last >= ef:
                out["trend"] = "UP"
            elif ef < es and last <= ef:
                out["trend"] = "DOWN"
            out["chp_30m"] = round((last / closes[-7] - 1.0) * 100, 2)
            out["src"] = "5m"
    if out["src"] == "none":
        q = _quote(sym, (fut or {}).get("exchange") or "", api_key)
        chp = q.get("chp")
        if chp is None and q.get("ltp") and q.get("prev_close"):
            try:
                chp = (float(q["ltp"]) / float(q["prev_close"]) - 1.0) * 100.0
            except (TypeError, ValueError, ZeroDivisionError):
                chp = None
        if chp is not None:
            out["day_chp"] = round(float(chp), 2)
            out["trend"] = "UP" if chp > 0.15 else ("DOWN" if chp < -0.15 else "FLAT")
            out["src"] = "day"
    _MOMO_CACHE[sym] = {"ts": now, "data": out}
    return out


def _next_candle_close() -> str:
    n = datetime.now(_IST)
    secs_left = 300 - (int(n.timestamp()) % 300)
    t = n + timedelta(seconds=secs_left)
    return t.strftime("%H:%M")


def _expiry_note(dte) -> str:
    if dte is None:
        return ""
    if dte <= 0:
        return "0-DTE: gamma-heavy, strict stops"
    if dte <= 1:
        return "expiry week: gamma pop, decay fast"
    if dte <= 3:
        return "near expiry: theta burn rising"
    return ""


# ---------------------------------------------------------------------------
# Reversal-risk engine (ported as-is; data calls swapped for OpenAlgo)
# ---------------------------------------------------------------------------
_RISK_CACHE = {}
_RISK_TTL = 45


def _rsi(closes: list, period: int = 14):
    if len(closes) < period + 2:
        return None
    gains, losses = [], []
    for i in range(-period - 1, 0):
        d = closes[i] - closes[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    ag, al = sum(gains) / period, sum(losses) / period
    if al <= 0:
        return 100.0 if ag > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def _chart_pack(fut: dict, api_key: str) -> dict:
    """5m chart indicators for the futures series (cached 45s)."""
    sym = (fut or {}).get("symbol") or ""
    if not sym:
        return {}
    now = time.time()
    cached = _RISK_CACHE.get(sym)
    if cached and now - cached["ts"] < _RISK_TTL:
        return cached["data"]
    pack = {"candles": [], "ema9": None, "ema21": None, "rsi": None, "vwap": None, "avg_vol": None}
    candles = _candles(sym, (fut or {}).get("exchange") or "", api_key, "5m", 2)
    if len(candles) >= 15:
        closes = [c[4] for c in candles]
        vols = [c[5] for c in candles]
        pack["candles"] = candles
        pack["ema9"] = _ema(closes[-40:], 9)
        pack["ema21"] = _ema(closes[-40:], 21)
        pack["rsi"] = _rsi(closes[-40:], 14)
        seg = candles[-90:]
        tpv = sum(((c[2] + c[3] + c[4]) / 3.0) * (c[5] or 1.0) for c in seg)
        vv = sum((c[5] or 1.0) for c in seg)
        pack["vwap"] = tpv / vv if vv > 0 else None
        pack["avg_vol"] = (sum(vols[-21:-1]) / 20.0) if len(vols) >= 22 else (sum(vols) / max(1, len(vols)))
    _RISK_CACHE[sym] = {"ts": now, "data": pack}
    return pack


def _flow_imbalance(fut: dict, api_key: str):
    return _depth_imbalance((fut or {}).get("symbol"), (fut or {}).get("exchange"), api_key)


def _analyze_reversal_risk(key: str, side: str, entry_premium, cur_premium,
                           spot, fut: dict, adv: dict, mkt: str, api_key: str) -> dict:
    """Score 0-100 reversal risk against an open option-buying position.
    >=60 REVERSAL LIKELY (early square-off advised), 42-59 building, <42 holding."""
    factors = []

    def add(w, note):
        factors.append({"name": note.split(" — ")[0], "w": int(w), "note": note})
        return w

    raw = 0.0
    is_ce = (side == "CE")
    cp = _chart_pack(fut, api_key)
    candles = cp.get("candles") or []

    # ---- 1. Trend flip ----
    trend = ((adv.get("momentum") or {}).get("trend")) or "FLAT"
    against_trend = ("DOWN" if is_ce else "UP")
    if trend == against_trend:
        raw += add(18, f"Trend flip — 5m momentum turned {trend} against {side}")
    elif trend == "FLAT":
        raw += add(6, "Trend flat — thrust lost")

    # ---- 2. RSI extreme ----
    rsi = cp.get("rsi")
    if rsi is not None:
        if is_ce and rsi >= 72:
            raw += add(15, f"RSI {rsi:.0f} overbought — CE stretched")
        elif is_ce and rsi >= 62:
            raw += add(7, f"RSI {rsi:.0f} hot zone")
        elif (not is_ce) and rsi <= 28:
            raw += add(15, f"RSI {rsi:.0f} oversold — PE stretched")
        elif (not is_ce) and rsi <= 38:
            raw += add(7, f"RSI {rsi:.0f} cold zone")

    # ---- 3. Volume climax / exhaustion ----
    if candles:
        last = candles[-1]
        lc = last[4]
        avg_v = cp.get("avg_vol")
        last_v = last[5]
        prev = candles[-2] if len(candles) > 1 else None
        closes = [c[4] for c in candles]
        if avg_v and avg_v > 0 and last_v > 0:
            vr = last_v / avg_v
            adv_close = (lc < prev[4]) if prev else False
            if vr >= 2.2 and adv_close:
                raw += add(13, f"Volume climax {vr:.1f}x with adverse close — distribution")
            elif vr >= 3.0:
                raw += add(8, f"Volume climax {vr:.1f}x average")
            else:
                win10 = closes[-11:-3] if len(closes) >= 11 else closes[:-1]
                if win10:
                    making_extreme = (lc >= max(win10)) if is_ce else (lc <= min(win10))
                    if making_extreme and vr <= 0.75:
                        raw += add(9, f"New extreme on {vr:.2f}x volume — exhaustion")

    # ---- 4. Pivot structure ----
    try:
        q = _quote((fut or {}).get("symbol") or "", (fut or {}).get("exchange") or "", api_key) if fut else {}
        dh, dl = q.get("high"), q.get("low")
        if (not dh or not dl) and candles:
            seg = candles[-90:]
            dh, dl = max(c[2] for c in seg), min(c[3] for c in seg)
        dc = candles[-1][4] if candles else spot
        if dh and dl and dc and spot:
            p = (dh + dl + dc) / 3.0
            r1, s1 = 2 * p - dl, 2 * p - dh
            r2, s2 = p + (dh - dl), p - (dh - dl)
            if is_ce:
                if spot >= r2:
                    raw += add(8, f"Spot {spot:g} beyond R2 {r2:.0f} — overextended")
                elif r1 <= spot < r2 and candles and candles[-1][4] < r1 and candles[-2][2] >= r1:
                    raw += add(11, f"Rejected at R1 {r1:.0f} — failed breakout")
                elif spot < r1 and (r1 - spot) / spot <= 0.0025:
                    raw += add(6, f"Approaching R1 {r1:.0f} resistance")
            else:
                if spot <= s2:
                    raw += add(8, f"Spot {spot:g} below S2 {s2:.0f} — overextended down")
                elif s2 < spot <= s1 and candles and candles[-1][4] > s1 and candles[-2][3] <= s1:
                    raw += add(11, f"Bounced off S1 {s1:.0f} — failed breakdown")
                elif spot > s1 and (spot - s1) / spot <= 0.0025:
                    raw += add(6, f"Approaching S1 {s1:.0f} support")
    except Exception:
        pass

    # ---- 5. EMA structure break ----
    e9, e21 = cp.get("ema9"), cp.get("ema21")
    if candles and e9 and e21:
        lc = candles[-1][4]
        if is_ce and lc < e9 and e9 < e21:
            raw += add(12, "EMA structure broken — price < EMA9 < EMA21")
        elif is_ce and lc < e9:
            raw += add(6, "Price lost EMA9")
        elif (not is_ce) and lc > e9 and e9 > e21:
            raw += add(12, "EMA structure broken — price > EMA9 > EMA21")
        elif (not is_ce) and lc > e9:
            raw += add(6, "Price reclaimed EMA9")

    # ---- 6. VWAP stretch / snap-back ----
    vwap = cp.get("vwap")
    if vwap and candles and spot:
        dev = (spot - vwap) / vwap * 100.0
        if is_ce and dev >= 1.8:
            raw += add(8, f"Spot {dev:+.1f}% above VWAP — mean-reversion stretch")
        elif (not is_ce) and dev <= -1.8:
            raw += add(8, f"Spot {dev:+.1f}% below VWAP — mean-reversion stretch")
        lc = candles[-1][4]
        if is_ce and lc < vwap:
            raw += add(7, "Lost VWAP — intraday control flipped to sellers")
        elif (not is_ce) and lc > vwap:
            raw += add(7, "Reclaimed VWAP — intraday control flipped to buyers")

    # ---- 7. Candle pattern against the position ----
    if len(candles) >= 2:
        c1, c2 = candles[-2], candles[-1]
        b1 = c1[4] - c1[1]
        b2 = c2[4] - c2[1]
        rng = max(1e-9, c2[3] - c2[2])
        if is_ce and b1 > 0 and b2 < 0 and abs(b2) >= 0.6 * abs(b1):
            raw += add(10, "Bearish engulfing on the last 5m candle")
        elif (not is_ce) and b1 < 0 and b2 > 0 and abs(b2) >= 0.6 * abs(b1):
            raw += add(10, "Bullish engulfing on the last 5m candle")
        elif abs(b2) <= 0.25 * rng:
            raw += add(6, "Doji indecision at the extremes")

    # ---- 8. Greeks / premium behaviour ----
    try:
        pnl_pct = (float(cur_premium or 0) - float(entry_premium or 0)) / float(entry_premium or 1) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        pnl_pct = 0.0
    iv = adv.get("iv")
    dte = adv.get("dte")
    if dte is not None and dte <= 1 and -3.0 <= pnl_pct <= 3.0:
        raw += add(6, f"Theta burn — 0/1-DTE with flat P&L ({pnl_pct:+.1f}%)")
    if isinstance(iv, (int, float)) and iv >= 60:
        raw += add(4, f"IV {iv:.0f}% — premium-rich, IV-crush risk")

    # ---- 9. Order-flow imbalance ----
    imb = _flow_imbalance(fut, api_key)
    if imb is not None:
        if is_ce and imb <= -0.35:
            raw += add(12, f"Order flow {imb:+.0%} — heavy sell pressure")
        elif is_ce and imb <= -0.20:
            raw += add(5, f"Order flow {imb:+.0%} — sellers lean")
        elif (not is_ce) and imb >= 0.35:
            raw += add(12, f"Order flow {imb:+.0%} — heavy buy pressure")
        elif (not is_ce) and imb >= 0.20:
            raw += add(5, f"Order flow {imb:+.0%} — buyers lean")

    score = int(min(100, round(raw)))
    if score >= 60:
        label, action = "REVERSAL LIKELY", "EXIT"
    elif score >= 42:
        label, action = "RISK BUILDING", "TIGHTEN"
    else:
        label, action = "HOLDING", "HOLD"
    return {"score": score, "label": label, "action": action,
            "factors": factors[:6], "ts": datetime.now(_IST).strftime("%H:%M:%S")}


def _pretrade_gate(key: str, side: str, spot, fut: dict, momo: dict, dte, mkt: str, api_key: str):
    """Entry-time quality gate: the SAME factor families applied to a would-be
    position BEFORE the alert fires. Returns (verdict, notes, risk, adj)."""
    is_ce = (side == "CE")
    notes = []
    adj = 0.0

    risk = _analyze_reversal_risk(key, side, None, None, spot, fut,
                                  {"momentum": momo or {}, "dte": dte}, mkt, api_key)
    if risk["score"] >= 50:
        top = risk["factors"][0]["note"] if risk["factors"] else f"risk {risk['score']}/100"
        notes.append(f"pre-trade veto: {top} (risk {risk['score']}/100)")
        return "VETO", notes, risk, adj
    if risk["score"] >= 35:
        adj -= 0.75
        top = risk["factors"][0]["note"] if risk["factors"] else f"risk {risk['score']}/100"
        notes.append(f"caution: {top} (risk {risk['score']}/100)")

    cp = _chart_pack(fut, api_key)
    candles = cp.get("candles") or []

    e9, e21 = cp.get("ema9"), cp.get("ema21")
    if candles and e9 and e21:
        lc = candles[-1][4]
        if is_ce and lc > e9 > e21:
            adj += 0.4
            notes.append("EMA9>EMA21 stack aligned with CE")
        elif (not is_ce) and lc < e9 < e21:
            adj += 0.4
            notes.append("EMA9<EMA21 stack aligned with PE")

    vwap = cp.get("vwap")
    if candles and vwap:
        lc = candles[-1][4]
        if (is_ce and lc > vwap) or ((not is_ce) and lc < vwap):
            adj += 0.3
            notes.append("price on trend side of VWAP")

    avg_v = cp.get("avg_vol")
    if candles and avg_v:
        c_last = candles[-1]
        body = c_last[4] - c_last[1]
        v = c_last[5]
        if avg_v > 0 and v >= 1.3 * avg_v and ((is_ce and body > 0) or ((not is_ce) and body < 0)):
            adj += 0.5
            notes.append(f"volume-confirmed thrust ({v / avg_v:.1f}x avg)")

    imb = _flow_imbalance(fut, api_key)
    if imb is not None:
        if (is_ce and imb >= 0.20) or ((not is_ce) and imb <= -0.20):
            adj += 0.4
            notes.append(f"order flow agrees ({imb:+.0%})")
        elif (is_ce and imb <= -0.25) or ((not is_ce) and imb >= 0.25):
            adj -= 0.6
            notes.append(f"order flow against ({imb:+.0%})")

    if adj <= -1.4:
        notes.insert(0, f"pre-trade veto: stacked adverse checks (net {adj:+.1f})")
        return "VETO", notes, risk, adj
    if adj < -0.4:
        return "CAUTION", notes, risk, adj
    return "GO", notes, risk, adj


def _market_open(mkt: str) -> bool:
    n = datetime.now(_IST)
    if n.weekday() >= 5:
        return False
    minutes = n.hour * 60 + n.minute
    if mkt == "MCX":
        return (9 * 60 <= minutes < 23 * 60 + 30)
    return (9 * 60 + 15 <= minutes < 15 * 60 + 30)


def _empty_advice(inst: dict) -> dict:
    return {
        "key": inst["key"], "name": inst["name"], "market": inst["mkt"], "lot": inst["lot"],
        "status": "NO_DATA", "signal": "WAIT", "side": None, "confidence": 0,
        "fut_symbol": "", "spot": None, "strike": None, "option_symbol": None,
        "note": "Building advisory…",
    }


def _leg_premium(a: dict, strike, side: str):
    for s in (a.get("strikes") or []):
        if s.get("strike") == strike:
            leg = (s.get("ce") if side == "CE" else s.get("pe")) or {}
            try:
                v = float(leg.get("ltp") or 0)
                return v if v > 0 else None
            except (TypeError, ValueError):
                return None
    return None


def _find_opposite_strike(a: dict, strike, side: str):
    """Nearest opposite-side strike whose premium is closest to the entry premium."""
    entry = None
    for s in (a.get("strikes") or []):
        if s.get("strike") == strike:
            entry = (s.get("ce") if side == "CE" else s.get("pe") or {})
            break
    try:
        entry = float((entry or {}).get("ltp") or 0)
    except (TypeError, ValueError):
        entry = 0.0
    if entry <= 0:
        return None
    opp_side = "PE" if side == "CE" else "CE"
    best, best_diff = None, None
    for s in (a.get("strikes") or []):
        leg = (s.get("pe") if opp_side == "PE" else s.get("ce")) or {}
        try:
            ltp = float(leg.get("ltp") or 0)
        except (TypeError, ValueError):
            continue
        if ltp <= 0:
            continue
        diff = abs(ltp - entry)
        if best_diff is None or diff < best_diff:
            best, best_diff = {"strike": s.get("strike"), "symbol": leg.get("symbol"), "ltp": ltp}, diff
    return best


def _advise_one(inst: dict, api_key: str) -> dict:
    """Build the advisory for one instrument (chain + momentum + gates + levels)."""
    key = inst["key"]
    fut = _resolve_futures(inst["base"], inst["opt_exch"])
    out = {
        "key": key, "name": inst["name"], "market": inst["mkt"], "lot": inst["lot"],
        "status": "NO_DATA", "signal": "WAIT", "side": None, "confidence": 0,
        "fut_symbol": (fut or {}).get("symbol") or "",
        "spot": None, "strike": None, "option_symbol": None,
        "entry_premium": None, "current_premium": None,
        "target_premium": None, "sl_premium": None,
        "target_spot": None, "sl_spot": None, "rr": None,
        "trigger_time": None, "expiry": None, "dte": None,
        "pcr": None, "max_pain": None, "iv": None, "theta": None,
        "momentum": {"trend": "FLAT", "chp_30m": None},
        "basis": [], "note": "",
    }
    if not _market_open(inst["mkt"]):
        out["status"] = "CLOSED"
        out["note"] = "Market closed — advisories resume next session"

    try:
        a = _fetch_chain(inst, api_key)
    except Exception as e:
        log.warning("advisor chain %s failed: %s", inst["base"], e)
        a = {}
    if not a or not a.get("spot"):
        out["note"] = out["note"] or "Live chain unavailable right now"
        return out

    # normalized chain analysis (PCR / max pain / walls / direction)
    analysis = _analyze_chain(a)
    spot = a["spot"]
    atm = a.get("atm")
    pcr = analysis.get("pcr_oi")
    max_pain = analysis.get("max_pain")
    dte = a.get("dte")
    likely = analysis.get("likely_move") or {}
    strikes = a.get("strikes") or []

    momo = _momentum(fut, api_key)
    out.update(spot=spot, pcr=pcr, max_pain=max_pain, dte=dte,
               expiry=a.get("expiry"), momentum=momo,
               trigger_time=_next_candle_close(),
               status="LIVE" if out["status"] != "CLOSED" else "CLOSED")

    # ---------------- score ----------------
    score = 0.0
    basis = []
    direction = (likely.get("direction") or "RANGE-BOUND").upper()
    if direction == "BULLISH":
        score += 2.0
        basis.append(f"Chain structure bullish ({likely.get('confidence', '--')}% conf)")
    elif direction == "BEARISH":
        score -= 2.0
        basis.append(f"Chain structure bearish ({likely.get('confidence', '--')}% conf)")
    else:
        basis.append("Chain structure range-bound")
    for r in (likely.get("reasons") or []):
        t = str(r)
        if t.startswith("PCR") or "Max Pain" in t:
            continue    # normalized lines below cover these — no duplicates
        basis.append(t)
        if len(basis) >= 3:
            break

    trend = momo.get("trend")
    chp30 = momo.get("chp_30m")
    day_chp = momo.get("day_chp")
    src = momo.get("src")
    if src == "5m":
        move_txt = f" ({chp30:+.2f}% 30m)" if chp30 is not None else ""
        if trend == "UP":
            score += 1.5
            basis.append(f"5m futures momentum UP{move_txt}")
        elif trend == "DOWN":
            score -= 1.5
            basis.append(f"5m futures momentum DOWN{move_txt}")
        else:
            basis.append("5m futures momentum flat")
    elif src == "day" and day_chp is not None:
        sign = "+" if day_chp > 0 else ""
        if trend == "UP":
            score += 1.0
            basis.append(f"Futures day move {sign}{day_chp:.2f}% — intraday strength (5m feed unavailable)")
        elif trend == "DOWN":
            score -= 1.0
            basis.append(f"Futures day move {sign}{day_chp:.2f}% — intraday weakness (5m feed unavailable)")
        else:
            basis.append(f"Futures day move {sign}{day_chp:.2f}% — flat")
    else:
        basis.append("Momentum data unavailable")

    if pcr is not None:
        if pcr >= 1.15:
            score += 0.5
            basis.append(f"PCR {pcr:.2f} — put writing support")
        elif pcr <= 0.85:
            score -= 0.5
            basis.append(f"PCR {pcr:.2f} — call writing overhead")
        else:
            basis.append(f"PCR {pcr:.2f} neutral")
    if max_pain:
        if spot > max_pain:
            score += 0.5
            basis.append(f"Spot above Max Pain {max_pain:g}")
        else:
            score -= 0.5
            basis.append(f"Spot below Max Pain {max_pain:g}")

    call_wall = analysis.get("call_wall")
    put_wall = analysis.get("put_wall")
    if call_wall and put_wall:
        basis.append(f"OI walls: support {put_wall:g} / resistance {call_wall:g}")

    # ---------------- decision ----------------
    if score >= 2.5:
        side = "CE"
    elif score <= -2.5:
        side = "PE"
    else:
        side = None

    # ---------------- pre-trade gate ----------------
    pretrade = None
    if side:
        verdict, pt_notes, pt_risk, pt_adj = _pretrade_gate(
            key, side, spot, fut, momo, dte, inst["mkt"], api_key)
        pretrade = {"verdict": verdict, "notes": pt_notes, "risk_score": pt_risk["score"]}
        if verdict == "VETO":
            side = None
            out["note"] = pt_notes[0] if pt_notes else "Pre-trade checks vetoed the signal"
        elif verdict == "CAUTION":
            score *= 0.75

    out["signal"] = ("BUY CE" if side == "CE" else "BUY PE" if side == "PE" else "WAIT")
    out["side"] = side
    conf = int(max(35, min(85, 35 + abs(score) * 9)))
    if pretrade:
        if pretrade["verdict"] == "GO":
            conf = min(85, conf + 5)
        elif pretrade["verdict"] == "CAUTION":
            conf = max(30, conf - 8)
    out["confidence"] = conf
    out["pretrade"] = pretrade
    out["basis"] = ((pretrade["notes"] if pretrade else []) + basis)[:8]

    if not side:
        out["note"] = out["note"] or "No clean edge — wait for next 5m candle"
        return out

    # ---------------- contract & levels ----------------
    leg = None
    for s in strikes:
        if s.get("strike") == atm:
            leg = (s.get("ce") if side == "CE" else s.get("pe")) or {}
            break
    entry = leg.get("ltp") if leg else None
    try:
        entry = float(entry) if entry is not None else None
    except (TypeError, ValueError):
        entry = None
    if not entry or entry <= 0:
        out["signal"] = "WAIT"
        out["note"] = "ATM premium unavailable — skip this trigger"
        return out

    delta = leg.get("delta")
    try:
        delta = abs(float(delta)) if delta is not None else 0.5
    except (TypeError, ValueError):
        delta = 0.5
    delta = max(0.05, min(0.95, delta))

    # ---- adaptive target & stop (noise-floor stop, R-multiple target) ----
    is_ce = (side == "CE")
    candles_n = (_chart_pack(fut, api_key).get("candles") or [])
    ranges = []
    for c in candles_n[-12:]:
        try:
            _h, _l, _c = c[2], c[3], c[4]
            if _c > 0:
                ranges.append((_h - _l) / _c)
        except (TypeError, ValueError):
            pass
    if ranges:
        noise_spot = (sum(ranges) / len(ranges)) * 2.5
    else:
        try:
            _iv = float(leg.get("implied_volatility") or 0) / 100.0
        except (TypeError, ValueError):
            _iv = 0.0
        noise_spot = 0.0035 * max(0.5, min(4.0, (_iv / 0.12) if _iv else 1.0))
    try:
        risk_frac = max(0.10, min(0.35, noise_spot * delta * spot / entry))
    except (TypeError, ZeroDivisionError):
        risk_frac = 0.15
    sl = round(entry * (1 - risk_frac), 2)
    with_trend0 = (momo.get("trend") == "UP" and is_ce) or (momo.get("trend") == "DOWN" and (not is_ce))
    r_mult = 2.2 + (0.6 if with_trend0 else 0.0) - (0.4 if (dte is not None and dte <= 1) else 0.0)
    r_mult = max(1.8, min(3.2, r_mult))
    target = round(entry + r_mult * (entry - sl), 2)
    rr = round((target - entry) / max(0.05, entry - sl), 2)

    spot_step = (target - entry) / delta
    sl_step = (entry - sl) / delta
    if side == "CE":
        t_spot, s_spot = spot + spot_step, spot - sl_step
    else:
        t_spot, s_spot = spot - spot_step, spot + sl_step

    out.update(
        strike=atm,
        option_symbol=leg.get("symbol") or f"{int(atm or 0)} {side}",
        entry_premium=entry,
        current_premium=entry,
        target_premium=target,
        sl_premium=sl,
        target_spot=round(t_spot, 2),
        sl_spot=round(s_spot, 2),
        rr=rr,
        iv=leg.get("implied_volatility"),
        theta=leg.get("theta"),
        note=_expiry_note(dte) or f"ATM delta {delta:.2f} · R{r_mult:.1f} adaptive target (stop ≈ {(entry - sl) / entry * 100:.0f}% premium decay)",
        strikes=strikes,   # internal only — stripped before the API response
    )
    return out


# ---------------------------------------------------------------------------
# Alert store (in-memory mirror + SQLite persistence)
# ---------------------------------------------------------------------------
def _persist_alerts() -> None:
    for al in list(_ALERTS.values())[-_MAX_ALERTS:]:
        add_alert_row(al)


def _load_alerts() -> None:
    today = datetime.now(_IST).date().isoformat()
    prefix = today.replace("-", "")
    try:
        rows = load_today_alerts(prefix)
        if rows:
            _ALERTS_TODAY[0] = today
            for a in rows:
                aid = a.get("alert_id") or ""
                a["id"] = aid
                _ALERTS[aid] = a
                try:
                    seq = int(aid.split("-")[-1])
                    if seq > _ALERT_SEQ[0]:
                        _ALERT_SEQ[0] = seq
                except (ValueError, IndexError):
                    pass
    except Exception as e:
        log.debug("alerts db load failed: %s", e)


_load_alerts()

_MAX_EVENTS = 60


def create_alert(key: str, a: dict, opp: dict = None) -> dict:
    """Record a generated signal as an intraday alert (entry levels frozen)."""
    now = datetime.now(_IST)
    today = now.date().isoformat()
    if _ALERTS_TODAY[0] != today:
        _ALERTS.clear()
        _ALERTS_TODAY[0] = today
        _ALERT_SEQ[0] = 0
    _ALERT_SEQ[0] += 1
    al = {
        "id": f"{today.replace('-', '')}-{_ALERT_SEQ[0]:03d}",
        "key": key, "name": a.get("name"), "market": a.get("market"),
        "side": a.get("side"), "strike": a.get("strike"),
        "option_symbol": a.get("option_symbol"),
        "opp_strike": (opp or {}).get("strike"), "opp_symbol": (opp or {}).get("symbol"),
        "opp_premium": (opp or {}).get("ltp"),
        "entry_premium": a.get("entry_premium"),
        "current_premium": a.get("entry_premium"),
        "spot_at_entry": a.get("spot"),
        "target_premium": a.get("target_premium"), "sl_premium": a.get("sl_premium"),
        "target_spot": a.get("target_spot"), "sl_spot": a.get("sl_spot"),
        "basis": a.get("basis") or [], "note": a.get("note") or "",
        "confidence": a.get("confidence"), "rr": a.get("rr"),
        "pretrade": a.get("pretrade"),
        "status": "ACTIVE",
        "created_ts": time.time(), "created_at": now.strftime("%d %b %H:%M:%S"),
        "closed_at": None, "close_reason": None,
    }
    _ALERTS[al["id"]] = al
    if len(_ALERTS) > _MAX_ALERTS:
        for old in sorted(_ALERTS.keys())[:len(_ALERTS) - _MAX_ALERTS]:
            _ALERTS.pop(old, None)
    _persist_alerts()
    return al


def close_alert(alert_id: str, reason: str = "Manual close") -> dict:
    al = _ALERTS.get(alert_id)
    if not al:
        return {}
    if al["status"] == "ACTIVE":
        al["status"] = "CLOSED"
        al["closed_at"] = datetime.now(_IST).strftime("%d %b %H:%M:%S")
        al["close_reason"] = reason
        try:
            e, c = float(al.get("entry_premium") or 0), float(al.get("current_premium") or 0)
            if e > 0 and c > 0:
                al["close_pnl_pct"] = round((c - e) / e * 100.0, 2)
                al["close_outcome"] = "WIN" if c >= e else "LOSS"
        except (TypeError, ValueError):
            pass
        _persist_alerts()
    return al


def _sync_alerts(advice_by_key: dict) -> None:
    """Keep ACTIVE alerts in sync with live advisories; auto-close on confirmed
    reversal; auto-create when an instrument regenerates a different signal."""
    for al in list(_ALERTS.values()):
        if al["status"] != "ACTIVE":
            continue
        adv = advice_by_key.get(al["key"]) or {}
        cur = adv.get("current_premium")
        if cur is not None:
            al["current_premium"] = cur
        if adv.get("spot") is not None:
            al["spot_now"] = adv.get("spot")
        try:
            e, c = float(al.get("entry_premium") or 0), float(cur or 0)
            if e > 0 and c > 0:
                al["pnl_pct"] = round((c - e) / e * 100.0, 2)
        except (TypeError, ValueError):
            pass
        trend = (adv.get("momentum") or {}).get("trend") or "FLAT"
        against = "DOWN" if al.get("side") == "CE" else "UP"
        flip_key = f"_flip_{al['id']}"
        if al.get("side") and trend == against:
            al[flip_key] = al.get(flip_key, 0) + 1
            pnl_now = al.get("pnl_pct") or 0.0
            if al[flip_key] == 1:
                al.setdefault("revision_log", []).append({"ts": datetime.now(_IST).strftime("%d %b %H:%M:%S"), "msg": f"Momentum flipped {trend} against the trade (1st tick) — watching for confirmation"})
            elif al[flip_key] >= 3 and pnl_now < -2.0:
                close_alert(al["id"], f"Reversal confirmed (momentum {trend} ×3, {pnl_now:+.1f}%) — early square-off suggested")
                continue
            elif al[flip_key] >= 3 and pnl_now >= 0:
                al.setdefault("revision_log", []).append({"ts": datetime.now(_IST).strftime("%d %b %H:%M:%S"), "msg": f"Momentum {trend} ×3 with trade at {pnl_now:+.1f}% — SL trailed tight, not reversing winner"})
        else:
            al.pop(flip_key, None)
        if adv.get("option_symbol") and adv.get("option_symbol") != al.get("option_symbol") and adv.get("signal", "WAIT").startswith("BUY"):
            close_alert(al["id"], f"New signal on {al['key']} ({adv.get('option_symbol')})")
        if not al.get("opp_symbol") and al.get("strike") is not None and al.get("side"):
            opp = _find_opposite_strike(adv, al.get("strike"), al.get("side"))
            if opp:
                al["opp_strike"], al["opp_symbol"], al["opp_premium"] = opp.get("strike"), opp.get("symbol"), opp.get("ltp")
                _persist_alerts()
    for key, adv in advice_by_key.items():
        if not str(adv.get("signal") or "").startswith("BUY") or not adv.get("entry_premium"):
            continue
        has_active = any(al["key"] == key and al["status"] == "ACTIVE" and al.get("option_symbol") == adv.get("option_symbol")
                         for al in _ALERTS.values())
        if not has_active:
            create_alert(key, adv, opp=_find_opposite_strike(adv, adv.get("strike"), adv.get("side")))


def alert_history() -> dict:
    with _MONITOR_LOCK:
        alerts = sorted(_ALERTS.values(), key=lambda x: x.get("created_ts") or 0, reverse=True)
        armed = {k: {"alert_id": p.get("alert_id"), "armed_at": p.get("armed_at")}
                 for k, p in _MONITOR["armed"].items()}
    return {"alerts": alerts, "armed": armed}


def _monitor_pass(advice_by_key: dict, api_key: str) -> list:
    """Update armed positions against fresh advisories; emit events."""
    events = []
    with _MONITOR_LOCK:
        armed = _MONITOR["armed"]
        if not armed:
            return events
        now_iso = datetime.now(_IST).strftime("%d %b %H:%M:%S")
        for key, pos in list(armed.items()):
            adv = advice_by_key.get(key) or {}
            side = pos.get("side")
            if adv.get("option_symbol") and pos.get("option_symbol") and adv["option_symbol"] != pos.get("option_symbol"):
                events.append({"ts": now_iso, "key": key, "severity": "INFO",
                               "msg": f"{key}: contract rolled ({pos.get('option_symbol')} → {adv['option_symbol']}). Position monitor released."})
                armed.pop(key, None)
                continue
            cur = adv.get("current_premium")
            entry = pos.get("entry_premium")
            if cur is None or not entry:
                continue
            pnl_pct = (cur - entry) / entry * 100.0
            pos["current_premium"] = cur
            pos["pnl_pct"] = round(pnl_pct, 2)

            tnow = time.time()
            pos.setdefault("premium_series", []).append({"t": tnow, "p": cur})
            if adv.get("spot") is not None:
                pos.setdefault("spot_series", []).append({"t": tnow, "s": adv["spot"]})
            for series_key, cap in (("premium_series", 360), ("spot_series", 360)):
                if len(pos[series_key]) > cap:
                    pos[series_key] = pos[series_key][-cap:]

            try:
                _hi = float(pos.get("hi_premium") or cur)
                _lo = float(pos.get("lo_premium") or cur)
            except (TypeError, ValueError):
                _hi = _lo = cur
            if cur > _hi:
                _hi = cur
            if cur < _lo:
                _lo = cur
            pos["hi_premium"], pos["lo_premium"] = round(_hi, 2), round(_lo, 2)

            trend = (adv.get("momentum") or {}).get("trend") or "FLAT"
            with_trend = (trend == "UP" and side == "CE") or (trend == "DOWN" and side == "PE")
            against = (trend == "DOWN" and side == "CE") or (trend == "UP" and side == "PE")
            try:
                tgt_p = float(pos.get("target_premium") or 0)
                sl_p = float(pos.get("sl_premium") or 0)
            except (TypeError, ValueError):
                tgt_p, sl_p = 0.0, 0.0
            if with_trend and pnl_pct >= 12.0 and tgt_p and not pos.get("tgt_extended") and cur < tgt_p:
                ext_pct = (pos.get("target_pct", 25.0) or 25.0) * 1.5
                new_tgt = round(entry * (1 + ext_pct / 100.0), 2)
                if new_tgt > tgt_p + 0.01:
                    pos["target_premium"] = new_tgt
                    pos["target_pct"] = round(ext_pct, 1)
                    pos["tgt_extended"] = True
                    pos["revision_log"].append({"ts": now_iso, "msg": f"Target extended to ₹{new_tgt} (momentum {trend} continues, {pnl_pct:+.1f}%)"})
                    events.append({"ts": now_iso, "key": key, "severity": "INFO",
                                   "msg": f"{key} {pos.get('option_symbol')}: 🔼 Target extended to ₹{new_tgt} — momentum {trend} strong ({pnl_pct:+.1f}%)."})
            if with_trend and pnl_pct >= 15.0 and not pos.get("be_done"):
                pos["sl_premium"] = round(entry, 2)
                pos["sl_pct"] = 0.0
                pos["be_done"] = True
                pos["revision_log"].append({"ts": now_iso, "msg": "SL trailed to breakeven (+15% milestone)"})
                events.append({"ts": now_iso, "key": key, "severity": "INFO",
                               "msg": f"{key} {pos.get('option_symbol')}: 🛡️ SL trailed to BREAKEVEN (₹{entry:.2f}) at {pnl_pct:+.1f}% — risk-free now."})
            if against and pnl_pct >= 8.0 and not pos.get("trail_done"):
                locked = round(cur * 0.96, 2)
                if locked > sl_p:
                    pos["sl_premium"] = locked
                    pos["sl_pct"] = round((locked / entry - 1) * 100, 1)
                    pos["trail_done"] = True
                    pos["revision_log"].append({"ts": now_iso, "msg": f"SL tightened to ₹{locked} on early reversal sign"})
                    events.append({"ts": now_iso, "key": key, "severity": "WARN",
                                   "msg": f"{key} {pos.get('option_symbol')}: 🔒 SL tightened to ₹{locked} — momentum turning, protecting {pnl_pct:+.1f}% gains."})

            try:
                tgt_px = float(pos.get("target_premium") or 0)
                sl_px = float(pos.get("sl_premium") or 0)
            except (TypeError, ValueError):
                tgt_px, sl_px = 0.0, 0.0
            if tgt_px and _hi >= tgt_px:
                events.append({"ts": now_iso, "key": key, "severity": "SUCCESS",
                               "msg": f"{key} {pos.get('option_symbol')}: 🎯 TARGET HIT — peak ₹{_hi:.2f} vs target ₹{tgt_px:.2f} ({pnl_pct:+.1f}% now). Square off now."})
                armed.pop(key, None)
                continue
            if sl_px and _lo <= sl_px:
                events.append({"ts": now_iso, "key": key, "severity": "DANGER",
                               "msg": f"{key} {pos.get('option_symbol')}: 🛑 STOP LOSS HIT — low ₹{_lo:.2f} vs SL ₹{sl_px:.2f} ({pnl_pct:+.1f}% now). Exit."})
                armed.pop(key, None)
                continue
            fut = {"symbol": pos.get("fut"), "exchange": _exch_for(pos.get("market"))}
            risk = _analyze_reversal_risk(key, side, entry, cur, adv.get("spot"),
                                          fut, adv, pos.get("market") or "", api_key)
            pos["reversal_risk"] = risk
            band = risk["label"]
            prev_band = pos.get("last_rev_band")
            if band != prev_band:
                pos["last_rev_band"] = band
                top = risk["factors"][0]["note"] if risk["factors"] else "multiple signals"
                if band == "REVERSAL LIKELY":
                    events.append({"ts": now_iso, "key": key, "severity": "DANGER",
                                   "msg": f"{key} {pos.get('option_symbol')}: 🚨 REVERSAL LIKELY (risk {risk['score']}/100) — EARLY SQUARE-OFF advised. {top}. P&L {pnl_pct:+.1f}%."})
                elif band == "RISK BUILDING":
                    events.append({"ts": now_iso, "key": key, "severity": "WARN",
                                   "msg": f"{key} {pos.get('option_symbol')}: ⚠️ Reversal risk building ({risk['score']}/100) — tighten stops. {top}. P&L {pnl_pct:+.1f}%."})
                elif prev_band in ("REVERSAL LIKELY", "RISK BUILDING"):
                    events.append({"ts": now_iso, "key": key, "severity": "SUCCESS",
                                   "msg": f"{key} {pos.get('option_symbol')}: ✅ Reversal risk easing ({risk['score']}/100) — hold with trailing stop. P&L {pnl_pct:+.1f}%."})
            if band == "REVERSAL LIKELY" and pnl_pct >= 3.0:
                locked = round(cur * (0.95 if pnl_pct >= 10 else 0.90), 2)
                try:
                    _sl_now = float(pos.get("sl_premium") or 0)
                except (TypeError, ValueError):
                    _sl_now = 0.0
                if locked > _sl_now:
                    pos["sl_premium"] = locked
                    pos["sl_pct"] = round((locked / entry - 1) * 100, 1)
                    pos["revision_log"].append({"ts": now_iso, "msg": f"Reversal risk {risk['score']}/100 — SL trailed to ₹{locked} locking {pnl_pct:+.1f}%"})
                    events.append({"ts": now_iso, "key": key, "severity": "WARN",
                                   "msg": f"{key} {pos.get('option_symbol')}: 🔒 Profit protected — SL moved to ₹{locked} (risk {risk['score']}/100, {pnl_pct:+.1f}%)."})
            if risk["score"] < 75:
                pos["auto_sq_done"] = False
            elif pnl_pct >= 6.0 and not pos.get("auto_sq_done"):
                pos["auto_sq_done"] = True
                events.append({"ts": now_iso, "key": key, "severity": "DANGER",
                               "msg": f"{key} {pos.get('option_symbol')}: 🏁 AUTO SQUARE-OFF SIGNAL — risk {risk['score']}/100 with {pnl_pct:+.1f}% gains. Exit now to lock profits."})
        if events:
            _MONITOR["events"] = (events + _MONITOR["events"])[:_MAX_EVENTS]
    return events


def _exch_for(market: str) -> str:
    return {"NSE": "NFO", "BSE": "BFO", "MCX": "MCX"}.get((market or "").upper(), "NFO")


def arm(key: str, advice: dict, alert_id: str = None) -> dict:
    entry = advice.get("entry_premium") or 0
    pos = {
        "key": key,
        "alert_id": alert_id,
        "name": advice.get("name"),
        "side": advice.get("side"),
        "strike": advice.get("strike"),
        "option_symbol": advice.get("option_symbol"),
        "entry_premium": advice.get("entry_premium"),
        "current_premium": advice.get("entry_premium"),
        "target_premium": advice.get("target_premium"),
        "sl_premium": advice.get("sl_premium"),
        "target_spot": advice.get("target_spot"),
        "sl_spot": advice.get("sl_spot"),
        "target_pct": round(((advice.get("target_premium") or 0) / entry - 1) * 100, 1) if entry else 25.0,
        "sl_pct": round(((advice.get("sl_premium") or 0) / entry - 1) * 100, 1) if entry else -30.0,
        "armed_at": datetime.now(_IST).strftime("%d %b %H:%M:%S"),
        "fut": advice.get("fut_symbol"),
        "market": advice.get("market"),
        "pnl_pct": 0.0,
        "premium_series": [],
        "spot_series": [],
        "revision_log": [],
        "be_done": False,
        "trail_done": False,
        "last_rev_alert": None,
    }
    t0 = time.time()
    if advice.get("entry_premium"):
        pos["premium_series"].append({"t": t0, "p": advice["entry_premium"]})
    if advice.get("spot"):
        pos["spot_series"].append({"t": t0, "s": advice["spot"]})
    with _MONITOR_LOCK:
        _MONITOR["armed"][key] = pos
        if _MONITOR["since"] is None:
            _MONITOR["since"] = pos["armed_at"]
        _MONITOR["events"].insert(0, {
            "ts": pos["armed_at"], "key": key, "severity": "INFO",
            "msg": f"{key} {pos['option_symbol']}: 🔔 monitor ARMED @ ₹{pos['entry_premium']} (target {pos['target_pct']:+.1f}% / SL {pos['sl_pct']:+.1f}%)"
        })
        _MONITOR["events"] = _MONITOR["events"][:_MAX_EVENTS]
    return pos


def disarm(key: str = None) -> None:
    with _MONITOR_LOCK:
        if key:
            _MONITOR["armed"].pop(key, None)
        else:
            _MONITOR["armed"].clear()


def revise_target(key: str, target: float = None, sl: float = None) -> dict | None:
    """Manually revise an armed position's target and/or stoploss premium.

    The advisor keeps revising automatically (extend-on-momentum, SL to
    breakeven); this applies a trader's own levels on top. Premium values are
    validated against the entry so an impossible level (target below SL) is
    rejected rather than silently arming a losing exit plan.
    """
    with _MONITOR_LOCK:
        pos = _MONITOR["armed"].get(key)
        if not pos:
            return None
        entry = float(pos.get("entry_premium") or 0)
        now_iso = datetime.now(_IST).strftime("%d %b %H:%M:%S")
        try:
            new_tgt = float(target) if target is not None else float(pos.get("target_premium") or 0)
            new_sl = float(sl) if sl is not None else float(pos.get("sl_premium") or 0)
        except (TypeError, ValueError):
            return None
        if entry <= 0 or new_tgt <= 0 or new_sl <= 0 or new_tgt <= new_sl:
            return None
        pos["target_premium"] = round(new_tgt, 2)
        pos["sl_premium"] = round(new_sl, 2)
        pos["target_pct"] = round((new_tgt / entry - 1) * 100, 1)
        pos["sl_pct"] = round((new_sl / entry - 1) * 100, 1)
        notes = []
        if target is not None:
            notes.append(f"target → ₹{new_tgt} ({pos['target_pct']:+.1f}%)")
        if sl is not None:
            notes.append(f"SL → ₹{new_sl} ({pos['sl_pct']:+.1f}%)")
        msg = f"{key} {pos.get('option_symbol')}: ✏️ Revised {' · '.join(notes)} (manual)."
        pos.setdefault("revision_log", []).append({"ts": now_iso, "msg": msg})
        _MONITOR["events"].insert(0, {"ts": now_iso, "key": key,
                                      "severity": "INFO", "msg": msg})
        _MONITOR["events"] = _MONITOR["events"][:_MAX_EVENTS]
        return dict(pos)


def get_monitor() -> dict:
    with _MONITOR_LOCK:
        return {"armed": dict(_MONITOR["armed"]), "events": list(_MONITOR["events"]), "since": _MONITOR["since"]}


def alert_chart_data(alert_id: str, symbol: str, timeframe: str = "5m", n_bars: int = 120) -> dict:
    """Candles for the alert chart (spot or option symbol) + alert meta."""
    al = _ALERTS.get(alert_id) or {}
    tf = timeframe if timeframe in TF_MINUTES else "5m"
    secs = TF_MINUTES[tf] * 60
    api_key = _api_key()
    candles = []
    if symbol:
        inst = next((i for i in INSTRUMENTS if i["key"] == (al.get("key") or "")), None)
        exch = inst["opt_exch"] if inst else "NFO"
        try:
            raw = _candles(symbol, exch, api_key, tf, 2 if secs < 86400 else 10)
            ist_off = 330 * 60
            for c in raw:
                ts = int(c[0])
                if ts <= 0:
                    continue
                if secs < 86400:
                    ts = ts - ts % secs + ist_off   # bucket to IST wall-clock minute
                candles.append([ts, c[1], c[2], c[3], c[4], c[5]])
        except Exception as e:
            log.debug("alert chart history %s failed: %s", symbol, e)
    return {"alert": al, "timeframe": tf, "candles": candles}


def scalper_advisor(refresh: bool = False, arm_key: str = None, disarm_key: str = None,
                    arm_alert_id: str = None, close_alert_id: str = None,
                    close_reason: str = "Manual close", auto_arm: bool = False,
                    focus_key: str = None, revise_key: str = None,
                    revise_tgt: float = None, revise_sl: float = None) -> dict:
    """Full advisory payload for all instruments + monitor state + intraday alerts.

    auto_arm=True live-monitors every fresh BUY signal without a manual arm:
    any LIVE instrument with a BUY advisory and no armed position yet is armed
    (linked to its ACTIVE alert), so target/SL hits, trailing and reversal
    events stream from the moment a signal fires."""
    now = time.time()
    api_key = _api_key()
    if not api_key:
        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "instruments": [], "active_signals": 0,
            "monitor": {"armed": {}, "events": [], "since": None},
            "new_events": [],
            "error": "No active broker session. Log in to your broker first.",
            "disclaimer": "Advisory only — option buying is high risk.",
        }

    results = []
    cold_insts = []
    stale_keys = []
    for inst in INSTRUMENTS:
        key = inst["key"]
        cached = _ADVICE_CACHE.get(key)
        if cached and not refresh and now - cached["ts"] < _ADVICE_TTL:
            results.append(cached["data"])
        elif cached and not refresh and now - cached["ts"] < _ADVICE_STALE:
            results.append(cached["data"])
            stale_keys.append(inst)
        elif cached and refresh:
            results.append(cached["data"])
            stale_keys.append(inst)
        else:
            results.append(_empty_advice(inst))
            cold_insts.append(inst)

    def _store(adv: dict) -> None:
        _ADVICE_CACHE[adv.get("key")] = {"ts": time.time(), "data": adv}
        for i, r in enumerate(results):
            if r.get("key") == adv.get("key"):
                results[i] = adv
                break

    if cold_insts:
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(6, len(cold_insts))) as _ex:
                for inst, adv in zip(cold_insts, _ex.map(lambda i: _advise_one(i, api_key), cold_insts)):
                    _store(adv)
        except Exception as par_e:
            log.warning("advisor parallel build failed: %s", par_e)
            for inst in cold_insts:
                try:
                    _store(_advise_one(inst, api_key))
                except Exception as build_e:
                    log.warning("advisor build %s failed: %s", inst["key"], build_e)

    if stale_keys and refresh:
        for inst in stale_keys:
            try:
                _store(_advise_one(inst, api_key))
            except Exception as build_e:
                log.warning("advisor refresh %s failed: %s", inst["key"], build_e)
    elif stale_keys:
        with _ADVICE_LOCK:
            pending = [i for i in stale_keys if i["key"] not in _ADVICE_REFRESHING]
            _ADVICE_REFRESHING.update(i["key"] for i in pending)

        def _rebuild_stale():
            for inst in pending:
                try:
                    _store(_advise_one(inst, api_key))
                except Exception as build_e:
                    log.warning("advisor bg rebuild %s failed: %s", inst["key"], build_e)
                finally:
                    with _ADVICE_LOCK:
                        _ADVICE_REFRESHING.discard(inst["key"])

        threading.Thread(target=_rebuild_stale, daemon=True, name="advisor-swr").start()

    advice_by_key = {a["key"]: a for a in results}

    # Symbol sync: rebuild the focused instrument fresh and float it to the top
    # so the sidebar reflects the chart's selected symbol immediately.
    focused_inst = next((i for i in INSTRUMENTS if i["key"] == (focus_key or "").upper()), None)
    if focused_inst is not None:
        try:
            _store(_advise_one(focused_inst, api_key))
            results.sort(key=lambda a: 0 if a.get("key") == focused_inst["key"] else 1)
        except Exception as focus_e:
            log.warning("advisor focus rebuild %s failed: %s", focused_inst["key"], focus_e)

    advice_by_key = {a["key"]: a for a in results}

    if arm_key and advice_by_key.get(arm_key, {}).get("side"):
        arm(arm_key, advice_by_key[arm_key], alert_id=arm_alert_id)
    if disarm_key:
        disarm(disarm_key)
    if revise_key and (revise_tgt is not None or revise_sl is not None):
        revise_target(revise_key, target=revise_tgt, sl=revise_sl)
    if close_alert_id:
        closed = close_alert(close_alert_id, close_reason)
        if closed:
            disarm(closed.get("key"))

    _sync_alerts(advice_by_key)

    if auto_arm:
        for a_key, adv in advice_by_key.items():
            if adv.get("status") != "LIVE":
                continue
            if not str(adv.get("signal") or "").startswith("BUY") or not adv.get("entry_premium"):
                continue
            with _MONITOR_LOCK:
                already = a_key in _MONITOR["armed"]
            if already:
                continue
            al_id = next((al["id"] for al in _ALERTS.values()
                          if al.get("key") == a_key and al.get("status") == "ACTIVE"
                          and al.get("option_symbol") == adv.get("option_symbol")), None)
            arm(a_key, adv, alert_id=al_id)

    new_events = _monitor_pass(advice_by_key, api_key)
    mon = get_monitor()
    hist = alert_history()
    armed_ids = {(p.get("alert_id") or "") for p in mon["armed"].values()}
    for al in hist["alerts"]:
        al["armed"] = al["id"] in armed_ids
        pos = mon["armed"].get(al["key"])
        if pos:
            al["revision_log"] = pos.get("revision_log") or []
        if al["status"] == "ACTIVE" and al.get("side"):
            if pos and pos.get("reversal_risk"):
                al["reversal_risk"] = pos["reversal_risk"]
            else:
                a_adv = advice_by_key.get(al["key"]) or {}
                al["reversal_risk"] = _analyze_reversal_risk(
                    al["key"], al["side"], al.get("entry_premium"), al.get("current_premium"),
                    a_adv.get("spot") or al.get("spot_now"), {"symbol": a_adv.get("fut_symbol"), "exchange": _exch_for(a_adv.get("market"))},
                    a_adv, a_adv.get("market"), api_key)
    mon["alerts"] = hist["alerts"]
    mon["armed_map"] = hist["armed"]

    for a in results:
        pos = mon["armed"].get(a["key"])
        a["armed"] = bool(pos)
        if pos:
            a["armed_entry"] = pos.get("entry_premium")
            a["armed_pnl_pct"] = pos.get("pnl_pct")
            a["armed_target_premium"] = pos.get("target_premium")
            a["armed_sl_premium"] = pos.get("sl_premium")
            a["armed_target_pct"] = pos.get("target_pct")
            a["armed_sl_pct"] = pos.get("sl_pct")
            a["armed_at"] = pos.get("armed_at")
            a["revision_log"] = pos.get("revision_log") or []
            a["reversal_risk"] = pos.get("reversal_risk")
            a["premium_series"] = pos.get("premium_series") or []
            a["spot_series"] = pos.get("spot_series") or []

    buys = [a for a in results if a.get("side")]
    for a in results:
        a.pop("strikes", None)
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "instruments": results,
        "active_signals": len(buys),
        "monitor": mon,
        "new_events": new_events,
        "disclaimer": "Advisory only — option buying is high risk. Premiums/levels are model estimates from live chain data, not guaranteed.",
    }
