"""
Orderflow & Volume Delta service — ported from the fno-trader-pro project
onto OpenAlgo's data layer.

Computes timewise orderflow metrics including aggressive Buy/Sell Volume,
Intra-bar Delta, Cumulative Volume Delta (CVD), and Diagonal Imbalances.
Index symbols auto-resolve to their near-month futures contract via OpenAlgo's
master contracts (find_near_month_futures); candles come from OpenAlgo's
history service, quotes from its quotes service — so any configured broker
works, not just one feed.

The bar-decomposition math (directional weight model, imbalance thresholds,
POC/VA calculation) is unchanged from the original.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List

from database.orderflow_db import upsert_instrument_row
from services.history_service import get_history
from services.option_symbol_service import find_near_month_futures
from services.quotes_service import get_quotes

log = logging.getLogger("services.orderflow")

INDEX_NAME_MAP = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "BANK NIFTY",
    "FINNIFTY": "FIN NIFTY",
    "MIDCPNIFTY": "MIDCAP NIFTY",
    "SENSEX": "BSE SENSEX",
    "BANKEX": "BSE BANKEX",
}

# root -> (display name, options exchange for the futures lookup)
ROOT_EXCH = {
    "NIFTY": ("NIFTY 50", "NFO"),
    "BANKNIFTY": ("BANK NIFTY", "NFO"),
    "FINNIFTY": ("FIN NIFTY", "NFO"),
    "MIDCPNIFTY": ("MIDCAP NIFTY", "NFO"),
    "SENSEX": ("BSE SENSEX", "BFO"),
    "BANKEX": ("BSE BANKEX", "BFO"),
    "CRUDEOIL": ("CRUDE OIL", "MCX"),
    "NATURALGAS": ("NATURAL GAS", "MCX"),
    "GOLD": ("GOLD", "MCX"),
    "SILVER": ("SILVER", "MCX"),
    "GOLDM": ("GOLD MINI", "MCX"),
    "SILVERM": ("SILVER MINI", "MCX"),
    "COPPER": ("COPPER", "MCX"),
    "ZINC": ("ZINC", "MCX"),
    "LEAD": ("LEAD", "MCX"),
}

_FUT_RESOLVE_CACHE = {}     # root -> (ts, fut dict or None)
_FUT_RESOLVE_TTL = 1800.0


def _api_key() -> str | None:
    try:
        from database.auth_db import get_first_available_api_key
        return get_first_available_api_key()
    except Exception:
        return None


def _resolve_futures_cached(root: str, exchange: str) -> dict | None:
    now = time.time()
    cached = _FUT_RESOLVE_CACHE.get(root)
    if cached and now - cached[0] < _FUT_RESOLVE_TTL:
        return cached[1]
    try:
        fut = find_near_month_futures(root, exchange)
    except Exception:
        fut = None
    _FUT_RESOLVE_CACHE[root] = (now, fut)
    return fut


def resolve_orderflow_target(symbol: str) -> Dict[str, Any]:
    """Resolve an incoming symbol, auto-selecting the corresponding future
    contract for indices and MCX roots via OpenAlgo's master contracts."""
    sym_u = (symbol or "NSE:NIFTY 50").strip().upper()

    # Strip the exchange prefix, keep the root
    clean = sym_u.split(":")[-1].replace("-INDEX", "").replace("-EQ", "").replace(" 50", "").replace(" 25", "")
    clean = clean.replace("BANK NIFTY", "BANKNIFTY").replace("FIN NIFTY", "FINNIFTY")
    clean = clean.replace("MIDCAP NIFTY", "MIDCPNIFTY").replace("BSE SENSEX", "SENSEX")
    clean = clean.replace("BSE BANKEX", "BANKEX").replace(" SENSEX", "SENSEX")
    clean = clean.replace("-FUT", "").strip()

    root = clean
    for k in ("BANKNIFTY", "NIFTYBANK", "FINNIFTY", "MIDCPNIFTY", "NIFTY", "SENSEX", "BANKEX",
              "CRUDEOILM", "CRUDEOIL", "NATURALGAS", "NATGASMINI", "GOLDM", "GOLDPETAL",
              "GOLDGUINEA", "GOLD", "SILVERM", "SILVER", "COPPER", "ZINC", "LEAD", "ALUMINIUM", "MENTHAOIL"):
        if clean.startswith(k):
            root = k
            break

    name, fut_exch = ROOT_EXCH.get(root, (root, None))
    is_index = root in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX")

    future_sym = None
    if fut_exch:
        fut = _resolve_futures_cached(root, fut_exch)
        if fut:
            future_sym = fut.get("symbol")
            name = ROOT_EXCH.get(root, (root, None))[0]

    target_sym = future_sym if (future_sym and (is_index or fut_exch == "MCX")) else sym_u

    return {
        "input_symbol": sym_u,
        "target_symbol": target_sym,
        "future_contract": future_sym or target_sym,
        "is_index": is_index,
        "root": root,
        "name": name,
        "fut_exchange": fut_exch or sym_u.split(":")[0] if ":" in sym_u else "NSE",
    }


def compute_bar_orderflow(c: list) -> Dict[str, Any]:
    """Decompose a candle [ts, open, high, low, close, volume] into orderflow metrics."""
    ts = int(c[0])
    o, hi, lo, cl, v = float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])

    rng = max(0.01, hi - lo)
    body = cl - o
    dir_ratio = body / rng  # -1.0 to +1.0
    loc_ratio = (cl - lo) / rng  # 0.0 to 1.0

    # Weight directional conviction + close location relative to candle extremes
    weight = 0.55 * dir_ratio + 0.45 * (loc_ratio - 0.5) * 2.0
    weight = max(-0.92, min(0.92, weight))

    buy_ratio = 0.5 + 0.5 * weight
    sell_ratio = 1.0 - buy_ratio

    buy_vol = round(v * buy_ratio)
    sell_vol = max(0, int(v - buy_vol))
    delta = buy_vol - sell_vol
    delta_pct = round((delta / v * 100.0), 1) if v > 0 else 0.0

    # Imbalance calculation
    imbalance_type = "NEUTRAL"
    ratio = 1.0
    is_stacked = False

    if sell_vol > 0 and buy_vol >= sell_vol * 1.4:
        ratio = round(buy_vol / max(1, sell_vol), 1)
        imbalance_type = "BUY"
        is_stacked = ratio >= 2.5
        imbalance_label = f"🔥 {ratio}x Buy" if is_stacked else f"🟢 {ratio}x Buy"
    elif buy_vol > 0 and sell_vol >= buy_vol * 1.4:
        ratio = round(sell_vol / max(1, buy_vol), 1)
        imbalance_type = "SELL"
        is_stacked = ratio >= 2.5
        imbalance_label = f"🔥 {ratio}x Sell" if is_stacked else f"🔴 {ratio}x Sell"
    else:
        imbalance_label = "⚖️ Balanced"

    dt = datetime.fromtimestamp(ts)
    time_str = dt.strftime("%H:%M")
    date_str = dt.strftime("%d %b")

    return {
        "timestamp": ts,
        "time": time_str,
        "date": date_str,
        "open": round(o, 2),
        "high": round(hi, 2),
        "low": round(lo, 2),
        "close": round(cl, 2),
        "volume": int(v),
        "buy_vol": int(buy_vol),
        "sell_vol": int(sell_vol),
        "buy_pct": round(buy_ratio * 100, 1),
        "sell_pct": round(sell_ratio * 100, 1),
        "delta": int(delta),
        "delta_pct": delta_pct,
        "imbalance_type": imbalance_type,
        "imbalance_ratio": ratio,
        "imbalance_label": imbalance_label,
        "is_stacked": is_stacked,
    }


def _fetch_candles(symbol: str, exchange: str, tf_code: str, n_bars: int, api_key: str) -> list:
    """Recent candles as [ts, o, h, l, c, v], newest last."""
    try:
        end = datetime.now().strftime("%Y-%m-%d")
        # enough calendar days to cover n_bars at the timeframe's cadence
        per_day = {"1m": 375, "3m": 125, "5m": 75, "15m": 25, "30m": 13, "1h": 7}.get(tf_code, 75)
        days = max(2, min(6, (n_bars // per_day) + 2))
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        ok, resp, _ = get_history(
            symbol=symbol, exchange=exchange, interval=tf_code,
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
                if ts > 4102444800:
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
        return out[-n_bars:]
    except Exception as e:
        log.warning("Orderflow history fetch error for %s (%s): %s", symbol, tf_code, e)
        return []


def get_orderflow(symbol: str, timeframe: str = "5m", n_bars: int = 25) -> Dict[str, Any]:
    """Fetch orderflow data for symbol, auto-routing indices/MCX roots to their
    corresponding near-month future contract."""
    target_info = resolve_orderflow_target(symbol)
    active_sym = target_info["target_symbol"]
    fut_exch = target_info.get("fut_exchange") or "NSE"
    api_key = _api_key()

    # Validate and standardize timeframe
    tf_clean = (timeframe or "5m").strip().lower()
    if tf_clean in ("1", "1m"):
        tf_code = "1m"
    elif tf_clean in ("3", "3m"):
        tf_code = "3m"
    elif tf_clean in ("5", "5m"):
        tf_code = "5m"
    elif tf_clean in ("15", "15m"):
        tf_code = "15m"
    elif tf_clean in ("30", "30m"):
        tf_code = "30m"
    elif tf_clean in ("60", "60m", "1h"):
        tf_code = "1h"
    elif tf_clean in ("d", "1d", "day"):
        tf_code = "D"
    else:
        tf_code = "5m"

    bars_count = max(10, min(100, int(n_bars or 25)))

    raw_candles = _fetch_candles(active_sym, fut_exch, tf_code, bars_count, api_key)

    # If futures candles were empty, fall back to the input symbol on its own exchange
    if not raw_candles and active_sym != target_info["input_symbol"]:
        in_exch = target_info["input_symbol"].split(":")[0] if ":" in target_info["input_symbol"] else "NSE"
        in_sym = target_info["input_symbol"].split(":")[-1] if ":" in target_info["input_symbol"] else target_info["input_symbol"]
        raw_candles = _fetch_candles(in_sym, in_exch, tf_code, bars_count, api_key)
        if raw_candles:
            active_sym = target_info["input_symbol"]

    # Live quote for the latest price / tick
    q = {}
    try:
        q = _quote_live(active_sym, fut_exch, api_key)
    except Exception:
        pass
    if not q:
        try:
            in_exch = target_info["input_symbol"].split(":")[0] if ":" in target_info["input_symbol"] else "NSE"
            in_sym = target_info["input_symbol"].split(":")[-1] if ":" in target_info["input_symbol"] else target_info["input_symbol"]
            q = _quote_live(in_sym, in_exch, api_key)
        except Exception:
            pass

    cur_ltp = float(q.get("ltp") or (raw_candles[-1][4] if raw_candles else 0.0))
    cur_ch = float(q.get("ch") or 0.0)
    try:
        if not cur_ch and q.get("ltp") and q.get("prev_close"):
            cur_ch = float(q["ltp"]) - float(q["prev_close"])
    except (TypeError, ValueError):
        pass
    cur_chp = float(q.get("chp") or 0.0)
    try:
        if not cur_chp and q.get("ltp") and q.get("prev_close"):
            cur_chp = (float(q["ltp"]) / float(q["prev_close"]) - 1.0) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    # Process candles into timewise orderflow
    bars: List[Dict[str, Any]] = []
    cvd = 0.0
    tot_vol = 0.0
    tot_buy = 0.0
    tot_sell = 0.0
    vol_by_price: Dict[float, float] = {}

    bin_step = 5.0 if "NIFTY" in target_info["root"] else (10.0 if "BANKNIFTY" in target_info["root"] else 1.0)

    for c in raw_candles:
        if len(c) < 6:
            continue
        bar = compute_bar_orderflow(c)
        cvd += bar["delta"]
        bar["cvd"] = int(cvd)
        bars.append(bar)

        tot_vol += bar["volume"]
        tot_buy += bar["buy_vol"]
        tot_sell += bar["sell_vol"]

        b_p = round(bar["close"] / bin_step) * bin_step
        vol_by_price[b_p] = vol_by_price.get(b_p, 0.0) + bar["volume"]

    # POC (Point of Control) and Value Area
    poc = max(vol_by_price, key=vol_by_price.get) if vol_by_price else round(cur_ltp / bin_step) * bin_step
    sorted_levels = sorted(vol_by_price.items(), key=lambda x: x[0])
    if sorted_levels:
        vah = max(k for k, _ in sorted_levels[-max(1, len(sorted_levels) // 4):])
        val = min(k for k, _ in sorted_levels[:max(1, len(sorted_levels) // 4)])
    else:
        vah = round(cur_ltp * 1.005, 2)
        val = round(cur_ltp * 0.995, 2)

    total_delta = int(tot_buy - tot_sell)
    if total_delta > 500:
        delta_bias = "BULLISH 🟢"
        imbalance_summary = "Aggressive Buyers Dominating"
    elif total_delta < -500:
        delta_bias = "BEARISH 🔴"
        imbalance_summary = "Aggressive Sellers Dominating"
    else:
        delta_bias = "BALANCED ⚖️"
        imbalance_summary = "Rotational Flow / Neutral"

    # Track this instrument in the DB (orderflow table state)
    try:
        upsert_instrument_row(
            target_info["root"], target_info["name"],
            fut_exch if fut_exch in ("NFO", "BFO", "MCX") else "NSE",
        )
    except Exception:
        pass

    return {
        "symbol": target_info["input_symbol"],
        "target_symbol": active_sym,
        "future_contract": target_info["future_contract"],
        "is_index": target_info["is_index"],
        "root": target_info["root"],
        "name": target_info["name"],
        "timeframe": tf_code,
        "bars": bars,
        "summary": {
            "ltp": cur_ltp,
            "ch": cur_ch,
            "chp": round(cur_chp, 2),
            "total_volume": int(tot_vol),
            "total_buy_vol": int(tot_buy),
            "total_sell_vol": int(tot_sell),
            "session_delta": total_delta,
            "session_cvd": int(cvd),
            "delta_bias": delta_bias,
            "imbalance_summary": imbalance_summary,
            "poc": round(poc, 2),
            "vah": round(vah, 2),
            "val": round(val, 2),
            "bar_count": len(bars),
            "updated_at": time.time(),
        },
    }


def _quote_live(symbol: str, exchange: str, api_key: str) -> dict:
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
    except Exception:
        pass
    return {}
