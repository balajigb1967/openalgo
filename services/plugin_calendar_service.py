"""
Market calendar plugin service — economic events + exchange holidays.

  - economic_calendar(): the week's macro events (the same TradingView-week
    JSON the market brief uses), filtered/paginated for a sidebar panel.
  - holiday_calendar(): exchange holidays sourced from OpenAlgo's own market
    calendar database (database/market_calendar_db.py), which records per
    holiday which exchanges are fully closed and which trade special sessions
    — e.g. MCX's evening-only 17:00–23:55 session on most NSE holidays, and
    MCX fully closed on Republic Day / Good Friday / Gandhi Jayanti /
    Christmas. A static fallback covers years the DB has not seeded.
"""

import datetime
import json
import os
import threading
import time
from typing import Any, Dict, List

import requests

log = __import__("logging").getLogger(__name__)

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), name="IST")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_LOCK = threading.Lock()
_ECON_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_HOLIDAY_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_ECON_TTL = 600.0        # 10 min — events change a few times a day
_ECON_FAIL_COOLDOWN = 300.0  # after a failed fetch, wait 5 min before retrying upstream
_HOLIDAY_TTL = 21600.0   # 6 h — holiday lists move once a year

# Disk mirror of the last good snapshot so a restart (or a long upstream
# outage) still has events to show. Lives in the app's db/ directory.
_CACHE_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "db", "calendar_cache.json")
)
_LAST_FAIL = {"ts": 0.0}


def _load_disk_cache() -> None:
    """Restore the last good snapshot from disk if memory is empty."""
    if _ECON_CACHE["data"]:
        return
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            blob = json.load(f)
        if isinstance(blob, dict) and (blob.get("upcoming") or blob.get("recent")):
            _ECON_CACHE["ts"] = float(blob.get("ts") or 0.0)
            _ECON_CACHE["data"] = blob
    except Exception:
        pass


def _save_disk_cache(data: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log.debug("calendar disk cache write failed: %s", e)


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
                    "impact_rank": {"High": 3, "Medium": 2, "Low": 1}.get(impact, 0),
                    "actual": ev.get("actual", ""),
                    "forecast": ev.get("forecast", ""),
                    "previous": ev.get("previous", ""),
                    "currency": ev.get("country", ""),
                })
    except Exception as e:
        log.warning("economic calendar fetch failed: %s", e)
    return out


def economic_calendar(refresh: bool = False, limit: int = 40) -> Dict[str, Any]:
    """This week's macro events, next-up first. Cached 10 minutes.

    The upstream (faireconomy/ForexFactory) rate-limits per IP after frequent
    polls and answers 429 with an HTML page for a while afterwards. Rules:
      - a failed fetch NEVER overwrites the last good snapshot (memory or disk);
      - after a failure we cool down 5 minutes before hitting upstream again,
        so panel auto-refresh cannot keep tripping the rate limit;
      - the last good snapshot is mirrored to db/calendar_cache.json so it
        survives restarts."""
    with _LOCK:
        _load_disk_cache()
        if not refresh and _ECON_CACHE["data"] and time.time() - _ECON_CACHE["ts"] < _ECON_TTL:
            return _ECON_CACHE["data"]
        cooling = time.time() - _LAST_FAIL["ts"] < _ECON_FAIL_COOLDOWN

    if cooling:
        return _stale_payload()

    events = _fetch_economic_events()
    if not events:
        _LAST_FAIL["ts"] = time.time()
        return _stale_payload()

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
        _save_disk_cache(data)
    return data


def _stale_payload() -> Dict[str, Any] | None:
    """Serve the last good snapshot (memory first, then disk), flagged stale."""
    with _LOCK:
        _load_disk_cache()
        if _ECON_CACHE["data"]:
            stale = dict(_ECON_CACHE["data"])
            stale["stale"] = True
            stale["stale_min"] = round(max(0.0, time.time() - _ECON_CACHE["ts"]) / 60)
            return stale
    return {
        "status": "success",
        "upcoming": [],
        "recent": [],
        "total": 0,
        "ts": time.time(),
        "stale": True,
    }


# ---------------------------------------------------------------------------
# Holiday calendar — from OpenAlgo's market calendar DB (session-aware)
# ---------------------------------------------------------------------------
# Static fallback for years the DB has not seeded. Fixed-date holidays only;
# movable feasts may differ. MCX is listed closed (conservative).
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


def _ms_to_ist_hhmm(ms: Any) -> str | None:
    try:
        return datetime.datetime.fromtimestamp(int(ms) / 1000, tz=_IST).strftime("%H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _display(date_iso: str) -> tuple[str, str]:
    d = datetime.date.fromisoformat(date_iso)
    return d.strftime("%d-%b-%Y"), _WEEKDAYS[d.weekday()]


def _within_session(session: str | None, now: datetime.datetime) -> bool:
    """'17:00 – 23:55' vs now (IST)."""
    if not session:
        return False
    try:
        parts = session.replace("–", "-").split("-")
        start = datetime.datetime.strptime(parts[0].strip(), "%H:%M").time()
        end = datetime.datetime.strptime(parts[1].strip(), "%H:%M").time()
        return start <= now.time() <= end
    except (ValueError, IndexError):
        return False


def _upstream_rows(year: int) -> List[dict]:
    """OpenAlgo's authoritative holiday rows for a year (closed + open exchanges)."""
    try:
        from database.market_calendar_db import get_holidays_by_year
        return get_holidays_by_year(year) or []
    except Exception as e:
        log.warning("upstream holiday DB unavailable: %s", e)
        return []


def _fallback_rows(year: int) -> List[dict]:
    rows = []
    for mmdd, name in _STATIC_HOLIDAYS:
        try:
            datetime.date.fromisoformat(f"{year}-{mmdd}")
        except ValueError:
            continue
        rows.append({
            "date": f"{year}-{mmdd}",
            "description": name,
            "holiday_type": "TRADING_HOLIDAY",
            "closed_exchanges": ["NSE", "BSE", "NFO", "BFO", "CDS", "BCD", "MCX"],
            "open_exchanges": [],
        })
    return rows


def _mcx_view(row: dict) -> dict:
    """Classify one holiday row from MCX's perspective.

    CLOSED    — MCX in closed_exchanges (full holiday)
    EVENING   — MCX trades a special session that day (e.g. 17:00–23:55)
    OPEN      — not listed / special day where MCX keeps normal hours
    """
    opens = {o.get("exchange"): o for o in (row.get("open_exchanges") or [])}
    mcx_open = opens.get("MCX")
    if "MCX" in (row.get("closed_exchanges") or []):
        return {"kind": "CLOSED", "session": None}
    if mcx_open:
        s = _ms_to_ist_hhmm(mcx_open.get("start_time"))
        e = _ms_to_ist_hhmm(mcx_open.get("end_time"))
        session = f"{s} – {e}" if s and e else None
        kind = "EVENING"
        if s and s < "12:00":
            kind = "SPECIAL"  # daytime special session (e.g. Muhurat-style)
        return {"kind": kind, "session": session}
    return {"kind": "OPEN", "session": None}


def holiday_calendar(refresh: bool = False) -> Dict[str, Any]:
    """Holiday lists with per-exchange session detail (MCX evening sessions included)."""
    with _LOCK:
        if not refresh and _HOLIDAY_CACHE["data"] and time.time() - _HOLIDAY_CACHE["ts"] < _HOLIDAY_TTL:
            return _HOLIDAY_CACHE["data"]

    now = datetime.datetime.now(_IST)
    today = now.date().isoformat()
    year = now.year

    rows = _upstream_rows(year)
    source = "openalgo-db"
    if not rows:
        source = "fallback"
        rows = _fallback_rows(year)

    holidays: List[dict] = []
    nse_list: List[dict] = []
    bse_list: List[dict] = []
    mcx_list: List[dict] = []       # MCX fully closed
    mcx_special: List[dict] = []    # MCX evening / special sessions

    for row in rows:
        date_iso = str(row.get("date") or "")
        if not date_iso:
            continue
        try:
            disp, wd = _display(date_iso)
        except ValueError:
            continue
        name = str(row.get("description") or "")
        closed = set(row.get("closed_exchanges") or [])
        mcx = _mcx_view(row)

        item = {"date": date_iso, "date_display": disp, "day": wd, "name": name}
        detail = {
            **item,
            "nse_closed": "NSE" in closed,
            "bse_closed": "BSE" in closed,
            "mcx_closed": mcx["kind"] == "CLOSED",
            "mcx_kind": mcx["kind"],
            "mcx_session": mcx["session"],
            "mcx_note": (
                "MCX closed" if mcx["kind"] == "CLOSED"
                else f"MCX evening session {mcx['session']}" if mcx["kind"] == "EVENING" and mcx["session"]
                else f"MCX special session {mcx['session']}" if mcx["kind"] == "SPECIAL" and mcx["session"]
                else "MCX open (normal hours)"
            ),
        }
        holidays.append(detail)
        if detail["nse_closed"]:
            nse_list.append(item)
        if detail["bse_closed"]:
            bse_list.append(item)
        if detail["mcx_closed"]:
            mcx_list.append(item)
        if mcx["kind"] in ("EVENING", "SPECIAL"):
            mcx_special.append({**item, "session": mcx["session"]})

    holidays.sort(key=lambda h: h["date"])

    def _today_status(exch: str) -> dict:
        row = next((h for h in holidays if h["date"] == today), None)
        if row:
            if exch == "MCX":
                if row["mcx_closed"]:
                    return {"trading": False, "note": "Holiday — MCX closed today"}
                if row["mcx_session"]:
                    open_now = _within_session(row["mcx_session"], now)
                    return {
                        "trading": open_now,
                        "note": f"Holiday: {row['mcx_session']} only" + (" — session OPEN now" if open_now else ""),
                    }
                return {"trading": True, "note": "Trading (special day, normal hours)"}
            if row["nse_closed"]:
                return {"trading": False, "note": "Holiday — closed today"}
            return {"trading": True, "note": "Trading (special day, normal hours)"}
        if now.weekday() >= 5:
            return {"trading": False, "note": "Weekend"}
        return {"trading": True, "note": "Trading today"}

    nse_today = _today_status("NSE")
    mcx_today = _today_status("MCX")

    next_nse = next((h for h in holidays if h["date"] >= today and h["nse_closed"]), None)
    next_mcx = next((h for h in holidays if h["date"] >= today and h["mcx_closed"]), None)

    data = {
        "status": "success",
        "year": year,
        "source": source,
        # Detailed per-day rows (drives the panel's MCX session badges)
        "holidays": holidays,
        # Compat lists per exchange (full-holiday days only)
        "nse": nse_list,
        "bse": bse_list,
        "mcx": mcx_list,
        # MCX evening/special session days — the "open in evening" detail
        "mcx_special": mcx_special,
        "today_status": {
            "today": today,
            "nse_trading_day": nse_today["trading"],
            "mcx_trading_day": mcx_today["trading"],
            "nse": nse_today,
            "mcx": mcx_today,
        },
        "next_nse_holiday": next_nse,
        "next_mcx_holiday": next_mcx,
        "ts": time.time(),
    }
    with _LOCK:
        _HOLIDAY_CACHE["ts"] = time.time()
        _HOLIDAY_CACHE["data"] = data
    return data
