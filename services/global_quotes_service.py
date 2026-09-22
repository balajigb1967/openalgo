"""
Global (dollar) market data for the watchlist.

A short catalog of international instruments the Indian terminals watch
alongside NSE/BSE/MCX names: US oil benchmarks, precious metals, natural gas
and the GIFT NIFTY future. Quotes are dollar-denominated by nature of the
products, and come from TradingView's public scanner endpoint — the same batch
call the market brief widget already uses — so no broker session is involved
and the list quotes even while every Indian exchange is closed.

Nothing here touches the broker: these symbols do not exist in any Indian
master contract, and the prices are the international benchmarks themselves
(CL, GC, SI, NG futures; the NSE IX GIFT NIFTY future).
"""

import threading
import time

import requests

from utils.logging import get_logger

logger = get_logger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_SCANNER_URL = "https://scanner.tradingview.com/global/scan2"

# The catalog. key: the stable watchlist symbol (exchange GLOBAL). tv: the
# TradingView ticker the scanner answers for. display: the name rows show.
# Yahoo fallbacks cover the commodities if the scanner is unreachable; GIFT
# NIFTY has no Yahoo listing, so it is scanner-only.
CATALOG = {
    "USOIL": {"tv": "NYMEX:CL1!", "name": "Crude Oil WTI (USD)", "yahoo": "CL=F", "decimals": 2},
    "BRENT": {"tv": "NYMEX:BZ1!", "name": "Brent Crude (USD)", "yahoo": "BZ=F", "decimals": 2},
    "GOLD": {"tv": "TVC:GOLD", "name": "Gold Spot (USD)", "yahoo": "GC=F", "decimals": 2},
    "SILVER": {"tv": "TVC:SILVER", "name": "Silver Spot (USD)", "yahoo": "SI=F", "decimals": 3},
    "NATGAS": {"tv": "NYMEX:NG1!", "name": "Natural Gas (USD)", "yahoo": "NG=F", "decimals": 3},
    "GIFTNIFTY": {"tv": "NSEIX:NIFTY1!", "name": "GIFT NIFTY (USD)", "yahoo": "^NSEI", "decimals": 1},
}

#: How long a fetched batch is reused. Globals move slowly relative to Indian
#: ticks, and the scanner is one HTTP call per batch regardless of size.
CACHE_TTL = 8.0

_cache_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "rows": {}}


def _tv_batch(tickers: list) -> dict:
    """One scanner POST: {TVSYM: {ltp, ch, chp, open, high, low, prev_close}}."""
    body = {
        "symbols": {"tickers": tickers, "query": {"types": []}},
        "columns": ["name", "close", "open", "high", "low", "change", "change_abs"],
        "range": [0, len(tickers)],
    }
    r = requests.post(_SCANNER_URL, json=body, headers={"User-Agent": _UA}, timeout=10)
    if r.status_code != 200:
        return {}
    j = r.json()
    fields = j.get("fields") or []
    out = {}
    for item in j.get("symbols") or []:
        d = dict(zip(fields, item.get("f") or []))
        if d.get("close") is None:
            continue
        out[item.get("s")] = {
            "ltp": d["close"],
            "open": d.get("open"),
            "high": d.get("high"),
            "low": d.get("low"),
            "ch": d.get("change_abs"),
            "chp": d.get("change"),
        }
    return out


def _yahoo_batch(yahoo_symbols: list) -> dict:
    """Yahoo chart fallback for the commodities. {'YAHOO:<sym>': {...}}."""
    out = {}
    import urllib.request

    for sym in yahoo_symbols:
        try:
            url = (
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                + urllib.request.quote(sym, safe="^=")
                + "?range=1d&interval=1d"
            )
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            import json as _json

            meta = _json.loads(urllib.request.urlopen(req, timeout=8).read())["chart"][
                "result"
            ][0]["meta"]
            ltp = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if ltp is None:
                continue
            out[f"YAHOO:{sym}"] = {
                "ltp": ltp,
                "open": meta.get("regularMarketOpen"),
                "high": meta.get("regularMarketDayHigh"),
                "low": meta.get("regularMarketDayLow"),
                "prev_close": prev,
                "ch": (ltp - prev) if prev else None,
                "chp": ((ltp - prev) / prev * 100) if (prev and prev != 0) else None,
            }
        except Exception as e:  # noqa: BLE001 — one dead fallback must not stall the rest
            logger.debug("yahoo fallback failed for %s: %s", sym, e)
    return out


def get_global_quotes(keys: list | None = None) -> dict:
    """
    Quotes for the requested catalog keys (all of them when keys is None).

    Returns {key: {name, ltp, ch, chp, open, high, low, currency}}. Rows keep
    the last good values when a source hiccups, so a transient failure blanks
    nothing the user was just reading. Results are cached process-wide for
    CACHE_TTL seconds — the desktop panel and the phone app share this.
    """
    keys = [k.upper() for k in keys] if keys else list(CATALOG)
    unknown = [k for k in keys if k not in CATALOG]
    if unknown:
        return {}

    now = time.time()
    with _cache_lock:
        if _cache["rows"] and now - _cache["ts"] < CACHE_TTL:
            return {k: v for k, v in _cache["rows"].items() if k in keys}
        stale = dict(_cache["rows"])

    tickers = [CATALOG[k]["tv"] for k in keys]
    by_tv: dict = {}
    try:
        by_tv = _tv_batch(tickers)
    except Exception as e:  # noqa: BLE001
        logger.debug("global TV batch failed: %s", e)

    # Yahoo only for what the scanner missed and Yahoo carries.
    missing = [k for k in keys if CATALOG[k]["tv"] not in by_tv]
    yahoo_needed = [CATALOG[k]["yahoo"] for k in missing if CATALOG[k].get("yahoo")]
    by_yahoo: dict = {}
    if yahoo_needed:
        try:
            by_yahoo = _yahoo_batch(yahoo_needed)
        except Exception as e:  # noqa: BLE001
            logger.debug("global yahoo fallback failed: %s", e)

    rows = dict(stale)  # start from last-good, overwrite what we fetched
    for k in keys:
        meta = CATALOG[k]
        q = by_tv.get(meta["tv"]) or (
            by_yahoo.get(f"YAHOO:{meta['yahoo']}") if meta.get("yahoo") else None
        )
        if not q:
            continue
        ltp = q.get("ltp")
        if ltp is None:
            continue
        dp = meta["decimals"]
        rows[k] = {
            "name": meta["name"],
            "ltp": round(ltp, dp),
            "ch": round(q["ch"], dp) if q.get("ch") is not None else None,
            "chp": round(q["chp"], 2) if q.get("chp") is not None else None,
            "open": round(q["open"], dp) if q.get("open") else None,
            "high": round(q["high"], dp) if q.get("high") else None,
            "low": round(q["low"], dp) if q.get("low") else None,
            "currency": "USD",
        }

    with _cache_lock:
        _cache["ts"] = now
        _cache["rows"] = rows
    return {k: v for k, v in rows.items() if k in keys}


# ---------------------------------------------------------------------------
# History (candles) for the global rows — this is what makes them chartable.
# Yahoo serves the OHLCV series for the commodities; GIFT NIFTY has no public
# OHLC endpoint, so its chart is quote-only and the caller shows the price
# header with a friendly note.
# ---------------------------------------------------------------------------

_YAHOO_RESOLUTIONS = {
    "1m": ("1m", "2d"),
    "3m": ("5m", "5d"),
    "5m": ("5m", "5d"),
    "10m": ("15m", "10d"),
    "15m": ("15m", "10d"),
    "30m": ("30m", "60d"),
    "1h": ("60m", "30d"),
    "D": ("1d", "2y"),
    "W": ("1wk", "5y"),
    "M": ("1mo", "10y"),
}

_hist_cache: dict = {}  # (key, interval) -> {"ts": float, "candles": [...]}
_hist_lock = threading.Lock()


def get_global_history(key: str, interval: str = "5m") -> dict:
    """OHLCV candles for one global instrument, from Yahoo.

    Returns {symbol, interval, currency: 'USD', candles: [{time, open, high,
    low, close, volume}]} with `time` in epoch seconds, or {"error": ...}.
    Cached ~30s per (key, interval) — a phone or desktop chart refreshes
    slower than that, and the backend call is light.
    """
    key = (key or "").upper()
    interval = interval if interval in _YAHOO_RESOLUTIONS else "5m"
    meta = CATALOG.get(key)
    yahoo = (meta or {}).get("yahoo")
    if not meta:
        return {"error": f"unknown global symbol {key}"}
    if not yahoo:
        return {
            "error": "history_not_available",
            "message": f"{key} has no public OHLC source; live price is shown in the watchlist",
        }

    now = time.time()
    ck = (key, interval)
    with _hist_lock:
        hit = _hist_cache.get(ck)
        if hit and now - hit["ts"] < 30:
            return hit["payload"]

    res, rng = _YAHOO_RESOLUTIONS[interval]
    payload: dict = {"symbol": key, "interval": interval, "currency": "USD", "candles": []}
    try:
        resp = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/" + yahoo,
            params={"interval": res, "range": rng, "includePrePost": "false"},
            headers={"User-Agent": _UA},
            timeout=12,
        )
        resp.raise_for_status()
        result = (resp.json().get("chart") or {}).get("result") or []
        if result:
            stamps = result[0].get("timestamp") or []
            quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            closes = quote.get("close") or []
            volumes = quote.get("volume") or []
            dp = meta["decimals"]
            candles = []
            for i, ts in enumerate(stamps):
                o = opens[i] if i < len(opens) else None
                h = highs[i] if i < len(highs) else None
                l = lows[i] if i < len(lows) else None
                c = closes[i] if i < len(closes) else None
                if o is None or h is None or l is None or c is None:
                    continue
                candles.append(
                    {
                        "time": int(ts),
                        "open": round(float(o), dp),
                        "high": round(float(h), dp),
                        "low": round(float(l), dp),
                        "close": round(float(c), dp),
                        "volume": float(volumes[i]) if i < len(volumes) and volumes[i] else 0.0,
                    }
                )
            payload["candles"] = candles[-1500:]
    except Exception as e:  # noqa: BLE001 — degraded chart beats a 500
        logger.debug("global history failed for %s %s: %s", key, interval, e)
        payload = {"symbol": key, "interval": interval, "currency": "USD", "candles": [], "error": "upstream_unavailable"}

    with _hist_lock:
        _hist_cache[ck] = {"ts": now, "payload": payload}
    return payload
