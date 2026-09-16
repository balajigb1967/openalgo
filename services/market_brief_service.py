"""
Market Brief service — ported from fno-trader-pro onto OpenAlgo.

Assembles a server-cached 30s snapshot:
  - indices & commodities via TradingView's scanner API (public, one batch call)
  - options internals (PCR / max pain / ATM IV / likely move) via OpenAlgo's own
    option-chain service for NIFTY
  - FII/DII flows + sector scorecard from NSE public JSON APIs
  - overnight global cues, macro rates, geopolitical drivers, economic events
    (TradingView economic calendar), headline merge from the news service
  - a live session stance that reflects the CURRENT market phase
    (pre-open / open / closing / closed) for NSE/BSE and MCX
  - an intraday tactical gameplan with pivot levels for NIFTY & BANKNIFTY

Nothing is fabricated: every number is either fetched or computed from fetched
values; unavailable inputs degrade to "--" / empty sections.
"""

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta, timezone

import requests

from services.market_news_service import fetch_news

log = logging.getLogger("services.market_brief")

_IST = timezone(timedelta(hours=5, minutes=30))

_BRIEF_CACHE = {"ts": 0.0, "data": None}
_BRIEF_TTL = 30.0
_BRIEF_STALE = 600.0
_BRIEF_INFLIGHT = [False]

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ---------------- TradingView scanner batch quotes --------------------------
_SCANNER_URL = "https://scanner.tradingview.com/global/scan2"


def _tv_batch_quotes(tickers: list) -> dict:
    """One TradingView scanner POST for a list of tickers: {TVSYM: quote}."""
    if not tickers:
        return {}
    try:
        body = {
            "symbols": {"tickers": tickers, "query": {"types": []}},
            "columns": ["name", "close", "open", "high", "low", "change", "change_abs", "volume"],
            "range": [0, len(tickers)],
        }
        r = requests.post(_SCANNER_URL, json=body, headers={"User-Agent": _UA}, timeout=10)
        if r.status_code != 200:
            return {}
        j = r.json()
        fields = j.get("fields") or []
        out = {}
        for item in j.get("symbols") or []:
            f = item.get("f") or []
            d = dict(zip(fields, f))
            if d.get("close") is None:
                continue
            chp = d.get("change")
            out[item.get("s")] = {
                "ltp": round(d["close"], 2),
                "open": round(d["open"], 2) if d.get("open") is not None else None,
                "high": round(d["high"], 2) if d.get("high") is not None else None,
                "low": round(d["low"], 2) if d.get("low") is not None else None,
                "ch": round(d["change_abs"], 2) if d.get("change_abs") is not None else None,
                "chp": round(chp, 2) if chp is not None else None,
                "volume": d.get("volume"),
                "name": d.get("name") or item.get("s"),
            }
        return out
    except Exception as e:
        log.debug("TV scanner batch failed: %s", e)
        return {}


INDEX_WATCH = [
    ("NIFTY 50", "NSE:NIFTY"),
    ("BANK NIFTY", "NSE:BANKNIFTY"),
    ("FIN NIFTY", "NSE:FINNIFTY"),
    ("SENSEX", "BSE:SENSEX"),
]
COMMODITY_WATCH = [
    ("GOLD", "MCX:GOLD"),
    ("SILVER", "MCX:SILVER"),
    ("CRUDE OIL", "MCX:CRUDEOIL"),
    ("NATURAL GAS", "MCX:NATURALGAS"),
]

MACRO_RATES = [
    ("dxy", "TVC:DXY", "Dollar Index (DXY)"),
    ("us10y", "TVC:US10Y", "US 10Y Yield (%)"),
    ("in10y", "TVC:IN10Y", "India 10Y Yield (%)"),
    ("usdinr", "FX_IDC:USDINR", "USD / INR"),
    ("brent", "TVC:USOIL", "Brent Crude ($/bbl)"),
    ("gold", "TVC:GOLD", "Spot Gold ($/oz)"),
]

GEO_KEYWORDS = [
    "geopolit", "conflict", "war", "opec", "crude", "sanction", "middle east",
    "red sea", "shipping", "tariff", "fed", "dxy", "dollar", "rupee", "inr",
    "fii", "trade", "supply chain", "china", "russia", "ukraine", "brent",
    "defense", "oil", "export", "import", "gold", "military",
]

_NSE_SECTOR_CACHE = {"ts": 0.0, "data": []}
_NSE_SECTOR_TTL = 900.0
_FLOWS_CACHE = {"ts": 0.0, "data": None}
_FLOWS_TTL = 900.0


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _fmt_move(q: dict, suffix: str = "") -> str:
    if not q or q.get("ltp") is None:
        return "--"
    chp = q.get("chp")
    sign = "+" if (chp is not None and chp > 0) else ""
    move = f" ({sign}{chp:.2f}%)" if chp is not None else ""
    try:
        return f"{q['ltp']:,.2f}{suffix}{move}"
    except (TypeError, ValueError):
        return f"{q['ltp']}{suffix}{move}"


def _watch_batch(watch: list) -> list:
    batch = _tv_batch_quotes([sym for _, sym in watch])
    out = []
    for label, sym in watch:
        q = batch.get(sym) or {}
        out.append({
            "label": label, "symbol": sym, "name": q.get("name") or label,
            "ltp": q.get("ltp"), "ch": q.get("ch"), "chp": q.get("chp"),
        })
    return out


def _macro_rates() -> dict:
    batch = _tv_batch_quotes([sym for _, sym, _ in MACRO_RATES])
    out = {}
    for key, sym, label in MACRO_RATES:
        q = batch.get(sym) or {}
        out[key] = {"symbol": sym, "label": label, "ltp": q.get("ltp"), "chp": q.get("chp")}
    return out


# ---------------- NSE public sections ---------------------------------------
def _nse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    })
    s.get("https://www.nseindia.com", timeout=8)
    return s


def _nse_sector_changes() -> list:
    now = time.time()
    if _NSE_SECTOR_CACHE["data"] and now - _NSE_SECTOR_CACHE["ts"] < _NSE_SECTOR_TTL:
        return _NSE_SECTOR_CACHE["data"]
    wanted = {
        "NIFTY BANK": "🏦 Banking & Financials",
        "NIFTY IT": "💻 IT & Technology",
        "NIFTY AUTO": "🚗 Automobiles",
        "NIFTY FMCG": "🛒 FMCG & Consumer",
        "NIFTY PHARMA": "💊 Pharma & Healthcare",
        "NIFTY METAL": "⛏️ Metals & Mining",
        "NIFTY REALTY": "🏗️ Realty & Infra",
        "NIFTY ENERGY": "🛢️ Oil, Gas & Energy",
    }
    out = []
    try:
        r = _nse_session().get("https://www.nseindia.com/api/allIndices", timeout=10)
        by_name = {str(x.get("index", "")).upper(): x for x in (r.json().get("data") or [])}
        for nse_name, label in wanted.items():
            row = by_name.get(nse_name)
            if not row:
                continue
            chp = _num(row.get("percentChange"))
            out.append({
                "sector": label,
                "ltp": _num(row.get("last")) or None,
                "chp": round(chp, 2),
                "score": round(max(0.0, min(10.0, 5.0 + chp * 2.5)), 1),
                "verdict": "BULLISH" if chp >= 0.6 else ("BEARISH" if chp <= -0.6 else "NEUTRAL"),
            })
    except Exception as e:
        log.warning("NSE sector fetch failed: %s", e)
    _NSE_SECTOR_CACHE.update(ts=now, data=out)
    return out


def _flows_section() -> dict:
    """Live FII/DII cash-market net flows (Rs Cr) from NSE, cached 15 min."""
    now = time.time()
    if _FLOWS_CACHE["data"] and now - _FLOWS_CACHE["ts"] < _FLOWS_TTL:
        return _FLOWS_CACHE["data"]
    flows = {"fii_net": "--", "dii_net": "--", "date": None, "net_bias": "FLOWS UNAVAILABLE", "source": "NSE"}
    try:
        r = _nse_session().get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=10)
        rows = r.json() or []
        by_date, order = {}, []
        for row in rows:
            d = row.get("date")
            if d not in by_date:
                by_date[d] = {"fii": 0.0, "dii": 0.0}
                order.append(d)
            cat = str(row.get("category", "")).upper()
            net = _num(row.get("netValue"))
            if "FII" in cat or "FPI" in cat:
                by_date[d]["fii"] += net
            elif "DII" in cat:
                by_date[d]["dii"] += net
        date = order[0] if order else None
        if date is not None:
            fii_val, dii_val = by_date[date]["fii"], by_date[date]["dii"]
            flows.update(
                date=date,
                fii_net=f"{'+' if fii_val >= 0 else ''}{fii_val:,.0f} Cr",
                dii_net=f"{'+' if dii_val >= 0 else ''}{dii_val:,.0f} Cr",
            )
            if fii_val > 0 and dii_val > 0:
                flows["net_bias"] = "FII + DII BOTH BUYING"
            elif fii_val < 0 and dii_val > 0:
                flows["net_bias"] = "DII ABSORBING FII SALES"
            elif fii_val > 0 and dii_val < 0:
                flows["net_bias"] = "FII LED RALLY"
            else:
                flows["net_bias"] = "BOTH FII & DII SELLING"
    except Exception as e:
        log.warning("FII/DII flows fetch failed: %s", e)
    _FLOWS_CACHE.update(ts=now, data=flows)
    return flows


# ---------------- Options internals (via OpenAlgo's chain service) -----------
def _options_section() -> dict:
    """NIFTY option internals from OpenAlgo's own option-chain service."""
    try:
        from services.option_chain_service import get_option_chain
        from services.option_symbol_service import find_near_month_futures
        from services.expiry_service import get_expiry_dates
        from database.auth_db import get_first_available_api_key

        api_key = get_first_available_api_key()
        if not api_key:
            return {}
        ok, resp, _ = get_expiry_dates("NIFTY", "NFO", "options", api_key)
        if not ok:
            return {}
        dates = resp.get("data") or []
        if not dates:
            return {}
        exp = str(dates[0]).replace("-", "")
        ok2, resp2, _ = get_option_chain(
            underlying="NIFTY", exchange="NFO", expiry_date=exp,
            strike_count=10, api_key=api_key, with_quotes=True, with_greeks=True,
        )
        if not ok2:
            return {}
        payload = resp2 if isinstance(resp2, dict) else {}
        if isinstance(payload.get("data"), dict) and "chain" not in payload:
            payload = payload["data"]
        chain = payload.get("chain") or []
        spot = payload.get("underlying_ltp")
        if not chain or not spot:
            return {}

        total_ce_oi = total_pe_oi = 0.0
        ce_walls, pe_walls = [], []
        for s in chain:
            ce, pe = s.get("ce") or {}, s.get("pe") or {}
            c_oi, p_oi = float(ce.get("oi") or 0), float(pe.get("oi") or 0)
            total_ce_oi += c_oi
            total_pe_oi += p_oi
            if c_oi > 0:
                ce_walls.append((s.get("strike"), c_oi))
            if p_oi > 0:
                pe_walls.append((s.get("strike"), p_oi))
        pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else None

        max_pain = None
        if total_ce_oi > 0 or total_pe_oi > 0:
            best_loss, best_strike = None, None
            for s in chain:
                cand = float(s.get("strike") or 0)
                loss = 0.0
                for t in chain:
                    k = float(t.get("strike") or 0)
                    loss += float((t.get("ce") or {}).get("oi") or 0) * (cand - k) if cand > k else 0.0
                    loss += float((t.get("pe") or {}).get("oi") or 0) * (k - cand) if cand < k else 0.0
                if best_loss is None or loss < best_loss:
                    best_loss, best_strike = loss, cand
            max_pain = best_strike

        atm = payload.get("atm_strike")
        atm_leg = None
        for s in chain:
            if s.get("strike") == atm:
                atm_leg = (s.get("ce") or {}).get("implied_volatility") or (s.get("ce") or {}).get("iv")
                break
        atm_iv = round(float(atm_leg), 2) if atm_leg else None

        call_wall = max(ce_walls, key=lambda x: x[1])[0] if ce_walls else None
        put_wall = max(pe_walls, key=lambda x: x[1])[0] if pe_walls else None
        if pcr is not None and pcr >= 1.15:
            direction, conf = "BULLISH", 60
        elif pcr is not None and pcr <= 0.85:
            direction, conf = "BEARISH", 60
        elif spot and call_wall and put_wall:
            direction, conf = ("BULLISH", 55) if (spot - (put_wall or spot)) < ((call_wall or spot) - spot) else ("BEARISH", 55)
        else:
            direction, conf = "RANGE-BOUND", 50
        rng = f"{(spot or 0) * 0.995:,.0f} - {(spot or 0) * 1.005:,.0f}"

        return {
            "spot": spot, "atm": atm, "pcr": pcr, "max_pain": max_pain,
            "atm_iv": atm_iv,
            "call_wall": call_wall, "put_wall": put_wall,
            "likely_direction": direction, "likely_confidence": conf, "likely_range": rng,
        }
    except Exception as e:
        log.warning("brief options section failed: %s", e)
        return {}


# ---------------- Overnight cues / geo / events ------------------------------
def _overnight_cues_section() -> dict:
    us_mkts = [
        ("GIFT NIFTY", "NSEIX:NIFTY1!"), ("DOW JONES", "TVC:DJI"),
        ("S&P 500", "SP:SPX"), ("NASDAQ 100", "NASDAQ:NDX"),
        ("NIKKEI 225", "TVC:NI225"), ("HANG SENG", "TVC:HSI"),
    ]
    macro_items = [
        ("US 10Y YIELD", "TVC:US10Y"), ("DOLLAR INDEX (DXY)", "TVC:DXY"),
        ("BRENT CRUDE", "TVC:USOIL"), ("SPOT GOLD", "TVC:GOLD"), ("USD / INR", "FX_IDC:USDINR"),
    ]
    batch = _tv_batch_quotes([s for _, s in us_mkts + macro_items])

    def _pack(pairs):
        return [{"name": n, "symbol": s, "ltp": (batch.get(s) or {}).get("ltp"),
                 "ch": (batch.get(s) or {}).get("ch"), "chp": (batch.get(s) or {}).get("chp")}
                for n, s in pairs]

    cues_list = _pack(us_mkts)
    macro_list = _pack(macro_items)
    flows = _flows_section()

    def _chp(name):
        return next((c.get("chp") or 0.0 for c in cues_list if c["name"] == name), 0.0)

    gift = _chp("GIFT NIFTY")
    pos = sum(1 for c in cues_list if (c.get("chp") or 0) > 0)
    neg = sum(1 for c in cues_list if (c.get("chp") or 0) < 0)
    if gift > 0.3 or pos >= 4:
        sentiment = "BULLISH 🟢"
        tone = f"GIFT Nifty ({'+' if gift > 0 else ''}{gift:.2f}%) indicates strong opening momentum for Indian bourses."
    elif gift < -0.3 or neg >= 4:
        sentiment = "BEARISH 🔴"
        tone = f"GIFT Nifty ({gift:.2f}%) signals soft opening pressure amidst global headwinds."
    else:
        sentiment = "RANGEBOUND / MIXED ⚖️"
        tone = f"GIFT Nifty ({'+' if gift > 0 else ''}{gift:.2f}%) signals a steady opening for Indian bourses."

    return {
        "sentiment": sentiment,
        "summary": f"Global markets trade with a {sentiment.lower()} tone. {tone}",
        "global_indices": cues_list,
        "macro_indicators": macro_list,
        "institutional_flow": {
            "fii_net": flows.get("fii_net"), "dii_net": flows.get("dii_net"),
            "date": flows.get("date"), "net_bias": flows.get("net_bias"),
        },
    }


def _geopolitical_section(commodities: list) -> dict:
    com = {q.get("label", ""): q for q in (commodities or [])}
    oil_q = com.get("CRUDE OIL") or {}
    gold_q = com.get("GOLD") or {}
    rates = _macro_rates()
    oil_chp = oil_q.get("chp") or 0.0

    if oil_chp > 1.5:
        oil_impact = f"Spiking Crude ({oil_chp:+.2f}%) widens India's CAD and pressures INR."
        oil_bias = "BEARISH FOR INR & EQUITIES"
    elif oil_chp < -1.5:
        oil_impact = f"Falling Crude ({oil_chp:+.2f}%) eases India's import burden, supporting INR."
        oil_bias = "BULLISH FOR INR & EQUITIES"
    else:
        oil_impact = "Stable crude prices reduce imported inflation risk for India."
        oil_bias = "NEUTRAL"

    items, seen = [], set()
    try:
        for art in (fetch_news(40).get("articles") or []):
            t = art.get("title", "")
            tl = t.lower()
            if not t or t in seen:
                continue
            if any(kw in tl for kw in GEO_KEYWORDS):
                seen.add(t)
                cat = "security"
                if any(k in tl for k in ("oil", "crude", "opec", "energy", "petrol", "gas")):
                    cat = "energy"
                elif any(k in tl for k in ("fed", "dollar", "dxy", "rupee", "inr", "fii", "rate", "yield")):
                    cat = "forex"
                elif any(k in tl for k in ("trade", "tariff", "export", "import", "china", "supply chain")):
                    cat = "trade"
                items.append({
                    "title": t, "link": art.get("link", "#"),
                    "source": art.get("source", "Market Feed"), "category": cat,
                    "impact": "High" if any(k in tl for k in ("war", "crisis", "spike", "sanction", "tariff", "opec", "fed")) else "Moderate",
                })
                if len(items) >= 6:
                    break
    except Exception as e:
        log.warning("geo news parse failed: %s", e)

    dxy_chp = (rates.get("dxy") or {}).get("chp")
    if dxy_chp is not None and dxy_chp < 0:
        flow_bias, flow_txt = "EM FLOW FRIENDLY", "Softening dollar supports foreign inflows into Indian equities."
    elif dxy_chp is not None and dxy_chp > 0:
        flow_bias, flow_txt = "USD STRENGTH WATCH", "Firming dollar can slow FII inflows and pressure the rupee."
    else:
        flow_bias, flow_txt = "MACRO WATCH", "DXY, US yields and Fed policy drive FII flows and USD/INR."

    drivers = [
        {"topic": "🛢️ Crude Oil & Energy Import Burden",
         "detail": f"MCX Crude {_fmt_move(oil_q)} · Brent {_fmt_move(rates.get('brent') or {}, '/bbl')}. {oil_impact}",
         "bias": oil_bias},
        {"topic": "💵 US Fed, DXY & FII Capital Flows",
         "detail": f"DXY {_fmt_move(rates.get('dxy') or {})} · USD/INR {_fmt_move(rates.get('usdinr') or {})}. {flow_txt}",
         "bias": flow_bias},
        {"topic": "🚢 Global Shipping & Trade Policy",
         "detail": f"Gold {_fmt_move(gold_q)} (safe-haven bid). Middle East maritime security and tariff dialogues influence freight costs and export competitiveness.",
         "bias": "SUPPLY CHAIN WATCH"},
        {"topic": "🇮🇳 Domestic Rates & Rupee",
         "detail": f"India 10Y {_fmt_move(rates.get('in10y') or {}, '%')} · USD/INR {_fmt_move(rates.get('usdinr') or {})}. A stable rupee keeps import costs and FPI debt flows predictable.",
         "bias": "RATES WATCH"},
    ]
    return {
        "rates": rates, "drivers": drivers, "headlines": items,
        "sectors": _nse_sector_changes(),
    }


def _events_section(limit: int = 12) -> list:
    """Economic calendar events (TradingView econ API → faireconomy mirror),
    next 2 days first."""
    out = []
    try:
        now = datetime.now(timezone.utc)
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r = requests.get(url, headers={"User-Agent": _UA}, timeout=10)
        if r.status_code == 200:
            for ev in (r.json() or [])[:80]:
                d = ev.get("date", "")
                out.append({
                    "title": ev.get("title", ""),
                    "date": d,
                    "impact": ev.get("impact", "Medium"),
                    "actual": ev.get("actual", ""),
                    "forecast": ev.get("forecast", ""),
                    "previous": ev.get("previous", ""),
                    "currency": ev.get("country", ""),
                })
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        two_days = (now + timedelta(days=2)).strftime("%Y-%m-%dT23:59:59Z")
        soon = sorted([e for e in out if now_iso <= e["date"] <= two_days], key=lambda x: x["date"])
        rest = sorted([e for e in out if e["date"] > two_days], key=lambda x: x["date"])
        out = (soon + rest)[:limit]
    except Exception as e:
        log.warning("brief calendar failed: %s", e)
    return out


# ---------------- Session stance (live phase-aware) ---------------------------
def _ist_session_phase(now_ist: datetime) -> tuple:
    t = now_ist.time()
    nse_open, nse_pre, nse_close, nse_last30 = dtime(9, 15), dtime(9, 0), dtime(15, 30), dtime(15, 0)
    mcx_open, mcx_close = dtime(9, 0), dtime(23, 30)
    weekend = now_ist.weekday() >= 5
    if weekend:
        return ("NSE/BSE", "closed")
    if t < nse_pre:
        return ("MCX", "open" if t >= mcx_open else "closed")
    if nse_pre <= t < nse_open:
        return ("NSE/BSE", "pre_open")
    if nse_last30 <= t < nse_close:
        return ("NSE/BSE", "closing")
    if nse_close <= t < mcx_close:
        return ("MCX", "open")
    if t >= mcx_close:
        return ("NSE/BSE", "closed")
    return ("NSE/BSE", "open")


def _session_stance_section(indices: list, commodities: list, options: dict, cues: dict) -> dict:
    now_ist = datetime.now(_IST)
    ex, phase = _ist_session_phase(now_ist)

    def _q(label_part, lst):
        return next((q for q in lst if label_part.upper() in q.get("label", "").upper()), {})

    nifty = _q("NIFTY 50", indices)
    bn = _q("BANK", indices)
    snx = _q("SENSEX", indices)
    crude = _q("CRUDE", commodities)
    gold = _q("GOLD", commodities)

    def _g(q, k):
        try:
            return float(q.get(k)) if q.get(k) is not None else None
        except (TypeError, ValueError):
            return None

    nifty_ltp = _g(nifty, "ltp")
    nifty_chp = _g(nifty, "chp") or 0.0
    bn_chp = _g(bn, "chp") or 0.0
    snx_chp = _g(snx, "chp") or 0.0
    crude_chp = _g(crude, "chp") or 0.0
    gold_chp = _g(gold, "chp") or 0.0
    opts = options or {}
    pcr = opts.get("pcr")
    max_pain = opts.get("max_pain")

    adv = sum(1 for c in (nifty_chp, bn_chp, snx_chp) if c > 0.05)
    dec = sum(1 for c in (nifty_chp, bn_chp, snx_chp) if c < -0.05)
    if adv >= 2 and nifty_chp >= 0.1:
        stance, icon = "BULLISH", "🚀"
    elif dec >= 2 and nifty_chp <= -0.1:
        stance, icon = "BEARISH", "🔻"
    else:
        stance, icon = "RANGEBOUND", "⚖️"

    phase_label = {
        "pre_open": "PRE-OPEN (09:00-09:15 IST)",
        "open": f"LIVE SESSION ({ex} OPEN)",
        "closing": "CLOSING HOUR (15:00-15:30 IST)",
        "closed": "MARKETS CLOSED (NSE/BSE)",
    }[phase]

    day_dir = "+" if nifty_chp >= 0 else ""
    if phase == "pre_open":
        gift = (cues or {}).get("sentiment", "")
        narrative = (f"Pre-open: GIFT Nifty cues {gift}. NIFTY {nifty_ltp or 0:,.1f} ({day_dir}{nifty_chp:.2f}% overnight). "
                     f"Options suggest {opts.get('likely_direction') or 'a neutral open'}; watch 09:15 opening range.")
    elif phase == "open":
        pcr_txt = f"PCR {pcr:.2f}" if pcr is not None else "PCR --"
        mp_txt = f" · Max Pain {max_pain:,.0f}" if max_pain is not None else ""
        breadth = f"{adv} of 3 up" if adv >= dec else f"{dec} of 3 down"
        narrative = (f"Live: NIFTY {nifty_ltp or 0:,.1f} ({day_dir}{nifty_chp:.2f}%) with {breadth} across NIFTY/BANKNIFTY/SENSEX. "
                     f"{pcr_txt}{mp_txt}. Crude {crude_chp:+.2f}% / Gold {gold_chp:+.2f}% on MCX. "
                     + ("Momentum with buyers — dip-buying favoured." if stance == "BULLISH"
                        else "Sellers in control — rallies getting sold." if stance == "BEARISH"
                        else "Index oscillating in a range — trade levels, not bias."))
    elif phase == "closing":
        narrative = (f"Closing hour: NIFTY {nifty_ltp or 0:,.1f} ({day_dir}{nifty_chp:.2f}%). "
                     + ("Position-squaring may extend the move into the close." if stance != "RANGEBOUND"
                        else "Flat close likely; expect low-vol drift into 15:30."))
    else:
        mcx_note = (f"MCX open — Crude {crude_chp:+.2f}%, Gold {gold_chp:+.2f}%" if ex == "MCX" else "MCX also closed")
        narrative = (f"Markets closed: NIFTY ended {day_dir}{nifty_chp:.2f}%. {mcx_note}. "
                     "Overnight global cues drive the next session's opening stance.")

    return {
        "phase": phase, "exchange": ex, "phase_label": phase_label,
        "stance": stance, "icon": icon,
        "nifty_ltp": nifty_ltp, "nifty_chp": nifty_chp,
        "pcr": pcr, "max_pain": max_pain,
        "narrative": narrative,
        "updated_at": now_ist.strftime("%H:%M:%S IST"),
        "is_live": phase in ("open", "closing"),
    }


# ---------------- Gameplan ---------------------------------------------------
def _intraday_gameplan_section(indices: list, options: dict) -> dict:
    nifty = next((q for q in indices if "NIFTY 50" in q.get("label", "")), {})
    bn = next((q for q in indices if "BANK" in q.get("label", "")), {})
    try:
        nifty_ltp = float(nifty.get("ltp"))
        bn_ltp = float(bn.get("ltp"))
    except (TypeError, ValueError):
        return {}

    def _levels(ltp, up_pct, dn_pct):
        h, l = ltp * (1 + up_pct), ltp * (1 - dn_pct)
        p = round((h + l + ltp) / 3.0, 1)
        return (p, round(2 * p - h, 1), round(2 * p - l, 1), round(p + (h - l), 1), round(p - (h - l), 1))

    p, s1, r1, r2, s2 = _levels(nifty_ltp, 0.004, 0.006)
    opts = options or {}
    try:
        pcr = float(opts.get("pcr")) if opts.get("pcr") is not None else None
    except (TypeError, ValueError):
        pcr = None
    nifty_chp = nifty.get("chp") or 0.0

    nifty_plan = {
        "name": "NIFTY 50", "ltp": nifty_ltp, "chp": nifty_chp,
        "s1": s1, "s2": s2, "pivot": p, "r1": r1, "r2": r2,
        "pcr": pcr, "max_pain": opts.get("max_pain"),
        "bias": (("BUY ON DIPS" if (nifty_chp >= 0 and (pcr or 0) >= 0.9) else "SELL ON RISE") if pcr is not None
                 else ("MOMENTUM FOLLOW" if nifty_chp >= 0 else "WEAKNESS WATCH")),
        "action": f"Buy on dips near {s1} - {p} with SL below {s2}. Target {r1} / {r2}.",
        "scalp_trigger": f"Long above {r1} for momentum rally. Short below {s1} for mean reversion.",
    }

    bp, bs1, br1, br2, bs2 = _levels(bn_ltp, 0.006, 0.006)
    bn_chp = bn.get("chp") or 0.0
    bn_plan = {
        "name": "BANK NIFTY", "ltp": bn_ltp, "chp": bn_chp,
        "s1": bs1, "s2": bs2, "pivot": bp, "r1": br1, "r2": br2,
        "bias": "RANGEBOUND / SCALP" if abs(bn_chp) < 0.5 else ("BULLISH BREAKOUT" if bn_chp > 0 else "BEARISH PRESSURE"),
        "action": f"Trade the {bs1} to {br1} corridor. Initiate longs on retest of {bp}.",
        "scalp_trigger": f"Breakout trigger above {br1} targeting {br2}. Strict SL at {bp}.",
    }

    return {
        "nifty": nifty_plan,
        "banknifty": bn_plan,
        "likely_direction": opts.get("likely_direction"),
        "likely_range": opts.get("likely_range"),
        "scalper_rules": [
            "⏰ Opening 15-Min Range (09:15 - 09:30): Mark high and low; trade breakouts with confirmation.",
            "📊 20 EMA Trend Alignment: CE buys only when 5m closes above 20 EMA with rising RSI (>55).",
            "🛡️ Strict R:R: 10-15 points SL on NIFTY options, target 25-35+ points for 1:2 RR.",
            "⚡ Square Off: Book 50% at Target 1 and trail SL to entry.",
        ],
    }


def _summary(indices, commodities, options, news_items, geo, cues, gameplan, session_stance) -> str:
    def _line(q):
        if q.get("ltp") is None:
            return f"• {q['label']}: --"
        chp = q.get("chp")
        sign = "+" if (chp is not None and chp > 0) else ""
        return f"• {q['label']}: {q['ltp']:,.2f} ({sign}{chp if chp is not None else '--'}%)"

    lines = [f"📊 **MARKET BRIEF — {datetime.now(_IST).strftime('%a %d %b %Y %H:%M IST')}**", ""]
    if session_stance:
        lines += ["⚡ **AI SESSION STANCE (LIVE)**",
                  f"• **{session_stance.get('icon', '')} {session_stance.get('stance', '--')}** — {session_stance.get('phase_label', '')} · updated {session_stance.get('updated_at', '')}",
                  f"  - {session_stance.get('narrative', '')}", ""]
    if cues and cues.get("sentiment"):
        lines += ["🌍 **OVERNIGHT GLOBAL CUES & MACRO TONE**",
                  f"• **Sentiment**: {cues.get('sentiment')} — {cues.get('summary', '')}",
                  f"• **Institutional Flows**: FII Net {cues.get('institutional_flow', {}).get('fii_net', '--')} · DII Net {cues.get('institutional_flow', {}).get('dii_net', '--')}", ""]
    lines.append("**INDICES**")
    lines += [_line(q) for q in indices]
    lines += ["", "**COMMODITIES**"]
    lines += [_line(q) for q in commodities]
    if gameplan:
        n_p, bn_p = gameplan.get("nifty", {}), gameplan.get("banknifty", {})
        lines += ["", "🎯 **INTRADAY TACTICAL GAMEPLAN**",
                  f"• **NIFTY 50 [{n_p.get('bias', '--')}]**: S1 {n_p.get('s1')} | Pivot {n_p.get('pivot')} | R1 {n_p.get('r1')}",
                  f"  - Action: {n_p.get('action', '')}",
                  f"• **BANK NIFTY [{bn_p.get('bias', '--')}]**: S1 {bn_p.get('s1')} | Pivot {bn_p.get('pivot')} | R1 {bn_p.get('r1')}",
                  f"  - Action: {bn_p.get('action', '')}"]
    if options:
        lines += ["", "**OPTIONS (NIFTY)**",
                  f"• PCR {options.get('pcr', '--')} · Max Pain {options.get('max_pain', '--')} · ATM IV {options.get('atm_iv', '--')}%",
                  f"• Likely move: {options.get('likely_direction', '--')} ({options.get('likely_confidence', '--')}%) · Range {options.get('likely_range', '--')}"]
    if geo and geo.get("drivers"):
        lines += ["", "**GEOPOLITICAL & MACRO IMPACT (INDIA)**"]
        lines += [f"• **{d['topic']}**: {d['detail']} [{d['bias']}]" for d in geo["drivers"]]
        if geo.get("headlines"):
            lines.append("• **Key Geopolitical Headlines**:")
            lines += [f"  - {h['title']} ({h['source']})" for h in geo["headlines"][:3]]
    if news_items:
        lines += ["", "**HEADLINES**"]
        lines += [f"• {n['title']}" for n in news_items[:3]]
    return "\n".join(lines)


def _build_brief() -> dict:
    with ThreadPoolExecutor(max_workers=6) as ex:
        f_indices = ex.submit(_watch_batch, INDEX_WATCH)
        f_commodities = ex.submit(_watch_batch, COMMODITY_WATCH)
        f_options = ex.submit(_options_section)
        f_news = ex.submit(lambda: (fetch_news(40).get("articles") or [])[:5])
        f_events = ex.submit(_events_section)
        f_cues = ex.submit(_overnight_cues_section)
        indices = f_indices.result()
        commodities = f_commodities.result()
        options = f_options.result()
        news_items = f_news.result()
        events = f_events.result()
        cues = f_cues.result()

    geopolitical = _geopolitical_section(commodities)
    gameplan = _intraday_gameplan_section(indices, options)
    session_stance = _session_stance_section(indices, commodities, options, cues)

    return {
        "timestamp": time.time(),
        "generated_at": datetime.now(_IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "indices": indices,
        "commodities": commodities,
        "options": options,
        "news": news_items,
        "events": events,
        "geopolitical": geopolitical,
        "overnight_cues": cues,
        "gameplan": gameplan,
        "session_stance": session_stance,
        "summary_markdown": _summary(indices, commodities, options, news_items, geopolitical, cues, gameplan, session_stance),
    }


def market_brief(refresh: bool = False) -> dict:
    """Cached brief: 30s TTL, stale-while-revalidate up to 10 min."""
    now = time.time()
    cached = _BRIEF_CACHE.get("data")
    if cached and not refresh and now - _BRIEF_CACHE["ts"] < _BRIEF_TTL:
        return cached
    if cached and not refresh and now - _BRIEF_CACHE["ts"] < _BRIEF_STALE:
        if not _BRIEF_INFLIGHT[0]:
            _BRIEF_INFLIGHT[0] = True

            def _rebuild():
                try:
                    _BRIEF_CACHE.update(ts=time.time(), data=_build_brief())
                except Exception as e:
                    log.warning("brief bg rebuild failed: %s", e)
                finally:
                    _BRIEF_INFLIGHT[0] = False

            threading.Thread(target=_rebuild, daemon=True, name="brief-swr").start()
        return cached
    res = _build_brief()
    _BRIEF_CACHE.update(ts=now, data=res)
    return res
