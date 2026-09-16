"""
Market calendar service — economic events + exchange holidays for OpenAlgo.

Two surfaces, both cached hard (calendar data is time-of-day sensitive, not
tick-sensitive):

  - economic_calendar(): the week's macro events (the same TradingView-week
    JSON the market brief uses), filtered/paginated for a sidebar panel.
  - holiday_calendar(): NSE/BSE/MCX holiday lists from NSE's public holiday
    API, with a static MCX fallback for the current year when the API blocks
    the request.

No broker dependency: everything is public data.
"""

import datetime
import threading
import time
from typing import Any, Dict, List

import requests

log = __import__("logging").getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_LOCK = threading.Lock()
_ECON_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_HOLIDAY_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_ECON_TTL = 600.0        # 10 min — events change a few times a day
_HOLIDAY_TTL = 21600.0   # 6 h — holiday lists move once a year

_IMPACT_RANK = {"High": 3, "Medium": 2, "Low": 1}


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


# ---------------------------------------------------------------------------
# Economic calendar (TradingView-week mirror)
# ---------------------------------------------------------------------------
def _fetch_economic_events() -> List[dict]:
    out: List[dict] = []
    try:
        r = requests.get(
            "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
            headers={"User-Agent": _UA},
            timeout=10,
        )
        if r.status_code == 200:
            for ev in (r.json() or []):
                d = str(ev.get("date") or "")
                impact = str(ev.get("impact") or "Medium")
                out.append({
                    "title": ev.get("title", ""),
                    "date": d,
                    "impact": impact,
                    "impact_rank": _IMPACT_RANK.get(impact, 0),
                    "actual": ev.get("actual", ""),
                    "forecast": ev.get("forecast", ""),
                    "previous": ev.get("previous", ""),
                    "currency": ev.get("country", ""),
                })
    except Exception as e:
        log.warning("economic calendar fetch failed: %s", e)
    return out


def economic_calendar(refresh: bool = False, limit: int = 40) -> Dict[str, Any]:
    """This week's macro events, next-up first. Cached 10 minutes."""
    with _LOCK:
        if not refresh and _ECON_CACHE["data"] and time.time() - _ECON_CACHE["ts"] < _ECON_TTL:
            return _ECON_CACHE["data"]

    events = _fetch_economic_events()
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    upcoming = [e for e in events if e["date"] and e["date"] >= now_iso]
    past = [e for e in events if e["date"] and e["date"] < now_iso]
    upcoming.sort(key=lambda e: (e["date"], -e["impact_rank"]))
    past.sort(key=lambda e: e["date"], reverse=True)

    data = {
        "status": "success",
        "upcoming": upcoming[:limit],
        "recent": past[: max(5, limit // 2)],
        "total": len(events),
        "ts": time.time(),
    }
    with _LOCK:
        _ECON_CACHE["ts"] = time.time()
        _ECON_CACHE["data"] = data
    return data


# ---------------------------------------------------------------------------
# Holiday calendar (NSE/BSE/MCX)
# ---------------------------------------------------------------------------
def _parse_nse_holidays(payload: dict) -> List[dict]:
    """NSE /api/holidays CM payload: rows have tradingDate (dd-MMM-yyyy),
    description, and sometimes week headers mixed in."""
    rows = payload.get("data") or []
    out: List[dict] = []
    for row in rows:
        try:
            if not isinstance(row, dict):
                continue
            tm = str(row.get("tradingDate") or "").strip()
            if not tm:
                continue
            day = datetime.datetime.strptime(tm, "%d-%b-%Y").date()
            out.append({
                "date": day.isoformat(),
                "date_display": tm,
                "day": day.strftime("%a"),
                "name": str(row.get("description") or "").title(),
            })
        except Exception:
            continue
    return out


# Static fallback: the fixed-date Indian market holidays (published by the
# exchanges each year). Used only when NSE's API is unreachable so the panel
# still shows a meaningful list; movable feasts may differ by a day.
_STATIC_HOLIDAYS = [
    ("01-26", "Republic Day"),
    ("03-14", "Holi"),
    ("03-31", "Id-Ul-Fitr (Eid)"),
    ("04-10", "Mahavir Jayanti"),
    ("04-14", "Dr. B. R. Ambedkar Jayanti"),
    ("04-18", "Good Friday"),
    ("05-01", "Maharashtra Day"),
    ("08-15", "Independence Day"),
    ("10-02", "Gandhi Jayanti"),
    ("10-21", "Diwali Laxmi Pujan"),
    ("10-22", "Diwali Balipratipada"),
    ("11-05", "Gurunanak Jayanti"),
    ("12-25", "Christmas"),
]

_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _fallback_holidays(year: int) -> List[dict]:
    out: List[dict] = []
    for mmdd, name in _STATIC_HOLIDAYS:
        try:
            day = datetime.date.fromisoformat(f"{year}-{mmdd}")
        except ValueError:
            continue
        out.append({
            "date": day.isoformat(),
            "date_display": day.strftime("%d-%b-%Y"),
            "day": _WEEKDAYS[day.weekday()],
            "name": name,
        })
    return sorted(out, key=lambda h: h["date"])


def _fetch_holiday_payloads() -> Dict[str, dict]:
    """NSE's public holiday endpoints (CM = equities, FO = derivatives).
    MCX's metals & energy contracts follow the FO holiday schedule, and BSE
    equity holidays match the CM list in practice."""
    out: Dict[str, dict] = {}
    try:
        s = _nse_session()
        for key, seg in (("CM", "CM"), ("FO", "FO")):
            try:
                r = s.get(
                    "https://www.nseindia.com/api/holidays",
                    params={"type": seg},
                    timeout=10,
                )
                if r.status_code == 200:
                    out[key] = r.json() or {}
            except Exception:
                continue
    except Exception as e:
        log.warning("holiday fetch failed: %s", e)
    return out


def holiday_calendar(refresh: bool = False) -> Dict[str, Any]:
    """NSE (CM), BSE (CM mirror) and MCX (FO mirror) holiday lists."""
    with _LOCK:
        if not refresh and _HOLIDAY_CACHE["data"] and time.time() - _HOLIDAY_CACHE["ts"] < _HOLIDAY_TTL:
            return _HOLIDAY_CACHE["data"]

    now = datetime.datetime.now()
    year = now.year
    payloads = _fetch_holiday_payloads()

    nse = _parse_nse_holidays(payloads.get("CM") or {})
    nse_fo = _parse_nse_holidays(payloads.get("FO") or {})
    if not nse:
        nse = _fallback_holidays(year)
    mcx = nse_fo or nse

    today = now.date().isoformat()
    data = {
        "status": "success",
        "year": year,
        "nse": nse,
        "bse": nse,
        "mcx": mcx,
        "next_nse_holiday": next((h for h in nse if h["date"] >= today), None),
        "today_status": {
            "today": today,
            "nse_trading_day": not any(h["date"] == today for h in nse),
            "mcx_trading_day": not any(h["date"] == today for h in mcx),
        },
        "source": "nse" if payloads else "fallback",
        "ts": time.time(),
    }
    with _LOCK:
        _HOLIDAY_CACHE["ts"] = time.time()
        _HOLIDAY_CACHE["data"] = data
    return data
