# blueprints/scalper_orderflow.py
"""
Scalper Advisor + Orderflow Table plugin — REST surface.

Both widgets are session-authenticated browser surfaces (the user is signed
into OpenAlgo), so every route runs behind check_session_validity, exactly like
the TradingView watchlist plugin. CSRF is enforced by the global Flask-WTF
hook in app.py; nothing here is a webhook, so nothing is exempted.

Endpoints (all under /plugins/):
  GET  /plugins/scalper/advisor      full advisory + monitor + alert history
  POST /plugins/scalper/close        close one alert
  GET  /plugins/scalper/chart        candles + meta for one alert's chart
  GET  /plugins/orderflow/table      orderflow rows for the default root set
  GET  /plugins/orderflow/detail     one symbol's timewise orderflow bars
  GET  /plugins/orderflow/health     liveness + DB probes for both plugins
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, jsonify, request
from flask import session as flask_session

from database.orderflow_db import health_check as of_health
from database.scalper_db import health_check as scalper_health
from services.market_brief_service import market_brief
from services.market_news_service import fetch_news, fetch_symbol_news
from services.orderflow_service import get_orderflow
from services.scalper_advisor_service import (
    alert_chart_data,
    scalper_advisor,
)
from utils.logging import get_logger
from utils.session import check_session_validity, is_session_valid
from flask import jsonify as _jsonify, request as _request
from functools import wraps as _wraps


def app_key_required(fn):
    """Dual auth for the mobile/Flutter app: accept a valid OpenAlgo API key
    (X-API-KEY header or ?apikey= query) OR a normal browser session.

    Plugin services self-authenticate to the broker via the instance's stored
    API key, so a valid app key is a sufficient and equivalent credential.
    """
    @_wraps(fn)
    def wrapper(*args, **kwargs):
        key = _request.headers.get("X-API-KEY") or _request.args.get("apikey")
        if key:
            try:
                from database.auth_db import get_auth_token_broker
                result = get_auth_token_broker(key)
                if result and result[0]:
                    return fn(*args, **kwargs)
            except Exception:
                pass
            return _jsonify({"status": "error", "message": "Invalid or revoked API key"}), 401
        if is_session_valid():
            return fn(*args, **kwargs)
        return _jsonify({"status": "error", "message": "Authentication required"}), 401
    return wrapper


logger = get_logger(__name__)

scalper_orderflow_bp = Blueprint(
    "scalper_orderflow_bp", __name__, url_prefix="/plugins"
)

DEFAULT_ORDERFLOW_ROOTS = [
    {"key": "NIFTY", "name": "NIFTY 50", "market": "NFO"},
    {"key": "BANKNIFTY", "name": "BANK NIFTY", "market": "NFO"},
    {"key": "SENSEX", "name": "BSE SENSEX", "market": "BFO"},
    {"key": "CRUDEOIL", "name": "CRUDE OIL", "market": "MCX"},
    {"key": "NATURALGAS", "name": "NATURAL GAS", "market": "MCX"},
    {"key": "GOLD", "name": "GOLD", "market": "MCX"},
    {"key": "SILVER", "name": "SILVER", "market": "MCX"},
    {"key": "GOLDM", "name": "GOLD MINI", "market": "MCX"},
    {"key": "SILVERM", "name": "SILVER MINI", "market": "MCX"},
]

_MAX_TF = {"1m", "3m", "5m", "15m", "30m", "1h"}

# ---- server-side caches: the table fans out to 9 broker history calls, so a
# 60s cache keeps the panel's 30s polling from rate-limit-tripping the broker
# (history service enforces 3 req/s). refresh=1 bypasses.
_TABLE_CACHE = {"ts": 0.0, "data": None, "lock": threading.Lock()}
_TABLE_TTL = 20.0  # realtime cadence — flow recomputes on every cache miss
_DETAIL_CACHE = {}   # (symbol, tf, bars) -> {"ts": float, "data": dict}
_DETAIL_TTL = 30.0
_DETAIL_LOCK = threading.Lock()


def _uid() -> str:
    return (flask_session.get("user_id") or "")


@scalper_orderflow_bp.route("/scalper/advisor", methods=["GET"])
@app_key_required
def scalper_advisor_route():
    """Full advisory payload. Query: refresh=1 forces rebuild."""
    try:
        refresh = (request.args.get("refresh") in ("1", "true", "yes"))
        arm_key = request.args.get("arm") or None
        disarm_key = request.args.get("disarm") or None
        arm_alert_id = request.args.get("arm_alert_id") or None
        close_alert_id = request.args.get("close_alert_id") or None
        close_reason = request.args.get("close_reason") or "Manual close"
        auto_arm = (request.args.get("auto_arm") in ("1", "true", "yes"))
        # Symbol sync: "NSE:NIFTY 50" / "MCX:GOLD" -> the advisor's instrument key.
        raw_focus = (request.args.get("focus") or "").upper()
        focus_key = raw_focus.split(":")[-1].strip() if raw_focus else None
        data = scalper_advisor(
            refresh=refresh,
            arm_key=arm_key,
            disarm_key=disarm_key,
            arm_alert_id=arm_alert_id,
            close_alert_id=close_alert_id,
            close_reason=close_reason,
            auto_arm=auto_arm,
            focus_key=focus_key,
        )
        return jsonify(data)
    except Exception as e:
        logger.exception(f"scalper advisor failed: {e}")
        return jsonify({"status": "error", "message": f"Advisor failed: {e}"}), 500


@scalper_orderflow_bp.route("/scalper/close", methods=["POST"])
@app_key_required
def scalper_close_route():
    """Close one alert. Body: {alert_id, reason?}"""
    data = request.get_json(silent=True) or {}
    alert_id = data.get("alert_id")
    if not alert_id:
        return jsonify({"status": "error", "message": "alert_id is required"}), 400
    try:
        from services.scalper_advisor_service import close_alert
        closed = close_alert(str(alert_id), str(data.get("reason") or "Manual close"))
        if not closed:
            return jsonify({"status": "error", "message": "Alert not found"}), 404
        return jsonify({"status": "success", "alert": closed})
    except Exception as e:
        logger.exception(f"scalper close failed: {e}")
        return jsonify({"status": "error", "message": f"Close failed: {e}"}), 500


@scalper_orderflow_bp.route("/scalper/chart", methods=["GET"])
@app_key_required
def scalper_chart_route():
    """Candles + meta for one alert's chart. Query: alert_id, symbol, tf."""
    alert_id = request.args.get("alert_id") or ""
    symbol = request.args.get("symbol") or ""
    tf = request.args.get("tf") or "5m"
    if tf not in _MAX_TF:
        tf = "5m"
    try:
        data = alert_chart_data(alert_id, symbol, tf, 120)
        return jsonify(data)
    except Exception as e:
        logger.exception(f"scalper chart failed: {e}")
        return jsonify({"status": "error", "message": f"Chart failed: {e}"}), 500


def _orderflow_row(inst: dict, tf: str) -> dict:
    """One table row (runs in a worker thread)."""
    try:
        data = get_orderflow(f"{inst['market']}:{inst['key']}", tf, 25)
        s = data.get("summary") or {}
        bars = data.get("bars") or []
        # Label of the most recent bar ("14:35") so clients can sort latest-on-top.
        last_bar = (bars[-1] or {}).get("time") if bars else None
        return {
            "key": inst["key"],
            "last_bar": last_bar,
            "name": inst["name"],
            "market": inst["market"],
            "ltp": s.get("ltp"),
            "chp": s.get("chp"),
            "delta_bias": s.get("delta_bias"),
            "session_delta": s.get("session_delta"),
            "session_cvd": s.get("session_cvd"),
            "total_volume": s.get("total_volume"),
            "poc": s.get("poc"),
            "vah": s.get("vah"),
            "val": s.get("val"),
            "bar_count": s.get("bar_count"),
            "target_symbol": data.get("target_symbol"),
            "updated_at": s.get("updated_at"),
        }
    except Exception as row_e:
        logger.warning("orderflow row %s failed: %s", inst["key"], row_e)
        return {"key": inst["key"], "name": inst["name"],
                "market": inst["market"], "error": str(row_e)}


@scalper_orderflow_bp.route("/orderflow/table", methods=["GET"])
@app_key_required
def orderflow_table_route():
    """Orderflow rows for the default root set. Query: tf (default 5m), refresh=1.
    Rows are fetched in parallel and cached for 60s."""
    try:
        tf = request.args.get("tf") or "5m"
        refresh = (request.args.get("refresh") in ("1", "true", "yes"))
        now = time.time()
        with _TABLE_CACHE["lock"]:
            cached = _TABLE_CACHE.get("data") if not refresh else None
            if cached and cached.get("timeframe") == tf and now - _TABLE_CACHE["ts"] < _TABLE_TTL:
                return jsonify(cached)
        with ThreadPoolExecutor(max_workers=len(DEFAULT_ORDERFLOW_ROOTS)) as ex:
            rows = list(ex.map(lambda inst: _orderflow_row(inst, tf), DEFAULT_ORDERFLOW_ROOTS))
        payload = {"status": "success", "timeframe": tf, "rows": rows}
        with _TABLE_CACHE["lock"]:
            _TABLE_CACHE["ts"] = time.time()
            _TABLE_CACHE["data"] = payload
        return jsonify(payload)
    except Exception as e:
        logger.exception(f"orderflow table failed: {e}")
        return jsonify({"status": "error", "message": f"Orderflow failed: {e}"}), 500


@scalper_orderflow_bp.route("/orderflow/detail", methods=["GET"])
@app_key_required
def orderflow_detail_route():
    """One symbol's timewise orderflow bars. Query: symbol (EXCH:SYM), tf, bars."""
    try:
        symbol = request.args.get("symbol") or "NSE:NIFTY 50"
        tf = request.args.get("tf") or "5m"
        if tf not in _MAX_TF | {"D"}:
            tf = "5m"
        bars_n = int(request.args.get("bars") or 25)
        refresh = (request.args.get("refresh") in ("1", "true", "yes"))
        ck = (symbol.upper(), tf, bars_n)
        now = time.time()
        with _DETAIL_LOCK:
            cached = _DETAIL_CACHE.get(ck) if not refresh else None
            if cached and now - cached["ts"] < _DETAIL_TTL:
                return jsonify(cached["data"])
        from services.orderflow_service import get_orderflow
        data = get_orderflow(symbol, tf, bars_n)
        with _DETAIL_LOCK:
            if len(_DETAIL_CACHE) > 64:
                _DETAIL_CACHE.clear()
            _DETAIL_CACHE[ck] = {"ts": time.time(), "data": data}
        return jsonify(data)
    except Exception as e:
        logger.exception(f"orderflow detail failed: {e}")
        return jsonify({"status": "error", "message": f"Orderflow failed: {e}"}), 500


@scalper_orderflow_bp.route("/orderflow/live", methods=["GET"])
@app_key_required
def orderflow_live_route():
    """Live LTP for one orderflow symbol (websocket-companion freshness).
    Query: symbol (EXCH:SYM). Lightweight: quotes only, no candles."""
    try:
        symbol = request.args.get("symbol") or "NSE:NIFTY 50"
        from services.orderflow_service import orderflow_live_quote
        return jsonify(orderflow_live_quote(symbol))
    except Exception as e:
        logger.exception(f"orderflow live failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


@scalper_orderflow_bp.route("/orderflow/health", methods=["GET"])
@app_key_required
def orderflow_health_route():
    """Liveness + DB probes for both plugins."""
    try:
        return jsonify({
            "status": "success",
            "scalper_db": bool(scalper_health()),
            "orderflow_db": bool(of_health()),
            "ts": __import__("time").time(),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# Market Brief
# ---------------------------------------------------------------------------
_BRIEF_RT_CACHE = {"ts": 0.0, "data": None}
_BRIEF_RT_LOCK = threading.Lock()


@scalper_orderflow_bp.route("/brief", methods=["GET"])
@app_key_required
def market_brief_route():
    """Full market brief snapshot (server-cached 30s, stale-while-revalidate).
    Query: refresh=1 forces a rebuild."""
    try:
        refresh = (request.args.get("refresh") in ("1", "true", "yes"))
        data = market_brief(refresh=refresh)
        return jsonify(data)
    except Exception as e:
        logger.exception(f"market brief failed: {e}")
        return jsonify({"status": "error", "message": f"Brief failed: {e}"}), 500


# ---------------------------------------------------------------------------
# News (RSS + TradingView headlines)
# ---------------------------------------------------------------------------
@scalper_orderflow_bp.route("/news", methods=["GET"])
@app_key_required
def news_route():
    """Merged RSS headlines (Indian + global). Query: limit, refresh=1."""
    try:
        limit = min(100, max(10, int(request.args.get("limit") or 60)))
        refresh = (request.args.get("refresh") in ("1", "true", "yes"))
        return jsonify(fetch_news(limit=limit, refresh=refresh))
    except Exception as e:
        logger.exception(f"news failed: {e}")
        return jsonify({"status": "error", "message": f"News failed: {e}"}), 500


@scalper_orderflow_bp.route("/news/symbol", methods=["GET"])
@app_key_required
def news_symbol_route():
    """TradingView headlines + RSS for one symbol. Query: symbol, limit."""
    try:
        symbol = request.args.get("symbol") or "NSE:NIFTY"
        limit = min(80, max(10, int(request.args.get("limit") or 40)))
        return jsonify(fetch_symbol_news(symbol, limit))
    except Exception as e:
        logger.exception(f"symbol news failed: {e}")
        return jsonify({"status": "error", "message": f"Symbol news failed: {e}"}), 500


# ---------------------------------------------------------------------------
# Market calendar (economic events + exchange holidays)
# ---------------------------------------------------------------------------
@scalper_orderflow_bp.route("/calendar/economic", methods=["GET"])
@app_key_required
def calendar_economic_route():
    """This week's macro events, next-up first. Query: refresh=1."""
    try:
        refresh = (request.args.get("refresh") in ("1", "true", "yes"))
        from services.plugin_calendar_service import economic_calendar
        return jsonify(economic_calendar(refresh=refresh))
    except Exception as e:
        logger.exception(f"economic calendar failed: {e}")
        return jsonify({"status": "error", "message": f"Calendar failed: {e}"}), 500


@scalper_orderflow_bp.route("/calendar/holidays", methods=["GET"])
@app_key_required
def calendar_holidays_route():
    """NSE/BSE/MCX holiday lists + today's trading-day status. Query: refresh=1."""
    try:
        refresh = (request.args.get("refresh") in ("1", "true", "yes"))
        from services.plugin_calendar_service import holiday_calendar
        return jsonify(holiday_calendar(refresh=refresh))
    except Exception as e:
        logger.exception(f"holiday calendar failed: {e}")
        return jsonify({"status": "error", "message": f"Calendar failed: {e}"}), 500


# ---- Mobile/Flutter option-tools endpoints (dual auth: API key or session) ----

@scalper_orderflow_bp.route("/options/expiries", methods=["GET"])
@app_key_required
def plugin_option_expiries():
    """Expiry list for an underlying - feeds the mobile Option Tools screen."""
    from services.expiry_service import get_expiry_dates
    symbol = (request.args.get("underlying") or "NIFTY").upper()
    exchange = (request.args.get("exchange") or "NFO").upper()
    _ok, resp, code = get_expiry_dates(symbol, exchange, "options")
    return jsonify(resp), code


@scalper_orderflow_bp.route("/options/chain", methods=["GET"])
@app_key_required
def plugin_option_chain():
    """Option chain with live quotes + Greeks for the mobile Option Tools screen."""
    from services.option_chain_service import get_option_chain
    from database.auth_db import get_first_available_api_key
    underlying = (request.args.get("underlying") or "NIFTY").upper()
    exchange = (request.args.get("exchange") or "NFO").upper()
    expiry_date = (request.args.get("expiry") or "").upper().replace("-", "")
    strike_count = request.args.get("strike_count", type=int) or 20
    if not expiry_date:
        return jsonify({"status": "error", "message": "expiry is required"}), 400
    _ok, resp, code = get_option_chain(
        underlying=underlying,
        exchange=exchange,
        expiry_date=expiry_date,
        strike_count=strike_count,
        api_key=get_first_available_api_key(),
        with_greeks=True,
    )
    return jsonify(resp), code


# ---- Global (dollar) quotes for the watchlists (desktop + Flutter) ----

@scalper_orderflow_bp.route("/globals/quotes", methods=["GET"])
@app_key_required
def plugin_global_quotes():
    """Dollar quotes for the global catalog: USOIL, BRENT, GOLD, SILVER,
    NATGAS, GIFTNIFTY. TradingView scanner with Yahoo fallback, 30s cache."""
    from services.global_quotes_service import get_global_quotes

    keys = request.args.get("keys")
    rows = get_global_quotes([k for k in keys.split(",") if k] if keys else None)
    return jsonify({"status": "success", "data": rows})


# ---- Global (dollar) history: makes GLOBAL rows chartable ----

@scalper_orderflow_bp.route("/globals/history", methods=["GET"])
@app_key_required
def plugin_global_history():
    """OHLCV candles for one global instrument (Yahoo-sourced). GIFT NIFTY
    returns a friendly error — it has no public OHLC endpoint, its watchlist
    row still streams the live price."""
    from services.global_quotes_service import get_global_history

    key = (request.args.get("symbol") or "").strip()
    interval = (request.args.get("interval") or "5m").strip()
    if not key:
        return jsonify({"status": "error", "message": "symbol is required"}), 400
    payload = get_global_history(key, interval)
    if payload.get("error"):
        return jsonify({"status": "error", "message": payload.get("message") or payload["error"]}), 404
    return jsonify({"status": "success", "data": payload})


# ---- Options analytics for the phone: the desktop /tools surface ----
# The desktop tool pages (OI Tracker, GEX, IV Chart...) are session-only
# browser views built on the same services/*_service functions. These routes
# re-serve those services behind app_key_required, so the Flutter app gets
# the identical numbers with its API key. Same per-view auth model as every
# other plugin route.

_ANALYTICS_CONFIG = {
    "oi": ("services.oi_tracker_service", "get_oi_data", ("underlying", "exchange", "expiry_date")),
    "maxpain": ("services.oi_tracker_service", "calculate_max_pain", ("underlying", "exchange", "expiry_date")),
    "gex": ("services.gex_service", "get_gex_data", ("underlying", "exchange", "expiry_date")),
    "gamma": ("services.gamma_density_service", "calculate_gamma_density", ("underlying", "exchange", "expiry_date")),
    "straddle": ("services.straddle_chart_service", "get_straddle_chart_data", ("underlying", "exchange", "expiry_date", "interval", "days")),
    "ivchart": ("services.iv_chart_service", "get_iv_chart_data", ("underlying", "exchange", "expiry_date", "interval", "days")),
    "ivsmile": ("services.iv_smile_service", "get_iv_smile_data", ("underlying", "exchange", "expiry_date")),
    "oiprofile": ("services.oi_profile_service", "get_oi_profile_data", ("underlying", "exchange", "expiry_date", "interval", "days")),
    "straddlepnl": ("services.custom_straddle_service", "get_custom_straddle_simulation", ("underlying", "exchange", "expiry_date", "interval", "days", "adjustment_points", "lot_size", "lots")),
    "volsurface": ("services.vol_surface_service", "get_vol_surface_data", ("underlying", "exchange", "expiry_dates", "strike_count")),
}

# Defaults the desktop pages use, so a phone request can omit them.
_ANALYTICS_DEFAULTS = {
    "interval": "5m", "days": 5, "adjustment_points": 50,
    "lot_size": 65, "lots": 1, "strike_count": 15,
}


_ANALYTICS_INT_FIELDS = {"days", "strike_count", "adjustment_points", "lot_size", "lots"}


def _run_analytics(tool: str, body: dict):
    module_name, fn_name, fields = _ANALYTICS_CONFIG[tool]
    mod = __import__(module_name, fromlist=[fn_name])
    fn = getattr(mod, fn_name)
    # The service layer self-authenticates to the broker with the instance's
    # stored key — the same path the desktop tool pages take internally.
    from database.auth_db import get_first_available_api_key

    kwargs = {}
    for f in fields:
        v = body.get(f, _ANALYTICS_DEFAULTS.get(f))
        if f in _ANALYTICS_INT_FIELDS and v is not None:
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = _ANALYTICS_DEFAULTS.get(f)
        kwargs[f] = v
    kwargs["api_key"] = get_first_available_api_key()
    return fn(**kwargs)


@scalper_orderflow_bp.route("/tools/<tool>", methods=["POST"])
@app_key_required
def plugin_tools(tool):  # noqa: C901 — dispatch by table
    """Run one of the options-analytics tools. Body mirrors the desktop page's
    request: {underlying, exchange, expiry_date[, interval, days, ...]}.
    Tools with a list field (expiry_dates) accept it as-is."""
    if tool not in _ANALYTICS_CONFIG:
        return jsonify({"status": "error", "message": f"unknown tool {tool}"}), 404
    body = request.get_json(silent=True) or {}
    # The phone sends selections in the query string (its plugin client is
    # query-first); the browser-shaped JSON body wins where both exist.
    for k, v in request.args.items():
        body.setdefault(k, v)
    underlying = (body.get("underlying") or "").strip().upper()
    exchange = (body.get("exchange") or "").strip().upper()
    if not underlying or not exchange:
        return jsonify({"status": "error", "message": "underlying and exchange are required"}), 400
    try:
        success, response, status_code = _run_analytics(tool, body)
        return jsonify(response), (status_code or 200)
    except Exception as e:  # noqa: BLE001 — report, never crash the worker
        logger.exception("plugin tools/%s failed: %s", tool, e)
        return jsonify({"status": "error", "message": "tool execution failed"}), 500


@scalper_orderflow_bp.route("/tools/underlyings", methods=["GET"])
@app_key_required
def plugin_tool_underlyings():
    """Optionable underlyings for the tools' pickers (per exchange)."""
    from database.symbol import get_distinct_underlyings

    exchange = (request.args.get("exchange") or "NFO").strip().upper()
    return jsonify({"status": "success", "data": get_distinct_underlyings(exchange)})


@scalper_orderflow_bp.route("/tools/expiries", methods=["GET"])
@app_key_required
def plugin_tool_expiries():
    """Option expiries for one underlying — the tools' expiry pickers."""
    from database.symbol import get_distinct_expiries

    exchange = (request.args.get("exchange") or "NFO").strip().upper()
    underlying = (request.args.get("underlying") or "").strip().upper()
    if not underlying:
        return jsonify({"status": "error", "message": "underlying is required"}), 400
    expiries = get_distinct_expiries(exchange=exchange, underlying=underlying, instrumenttype="options")
    return jsonify({"status": "success", "data": expiries})


@scalper_orderflow_bp.route("/tools/arbitrage", methods=["GET"])
@app_key_required
def plugin_tool_arbitrage():
    """Synthetic-future arbitrage universe (GET, like the desktop page)."""
    from services.arbitrage_service import DEFAULT_EXCHANGES, get_arbitrage_universe
    from database.auth_db import get_first_available_api_key

    raw = (request.args.get("exchanges") or "").strip()
    exchanges = [e.strip().upper() for e in raw.split(",") if e.strip()] or list(DEFAULT_EXCHANGES)
    success, response, status_code = get_arbitrage_universe(exchanges=exchanges, api_key=get_first_available_api_key())
    return jsonify(response), (status_code or 200)


# ---- Watchlist sync (the same DB lists the desktop terminal keeps) ----

def _sync_user():
    """Session user, or the OpenAlgo user an API key belongs to."""
    u = flask_session.get("user")
    if u:
        return u
    from database.auth_db import verify_api_key

    _key = request.headers.get("X-API-KEY") or request.args.get("apikey")
    return verify_api_key(_key) if _key else None


@scalper_orderflow_bp.route("/watchlist/sync", methods=["GET", "POST"])
@app_key_required
def plugin_watchlist_sync():
    """Read or replace the synced watchlist.

    GET  -> {lists: [{id, name, items: [{id, symbol, exchange, position}]}]}
    POST -> body {"lists": [{"name": "...", "items": [{"symbol", "exchange"}]}]}
            replaces the caller's lists wholesale (the phone is one device
            among several, so last-writer-wins is the whole contract).

    GLOBAL rows carry dollar prices from /plugins/globals/quotes, keyed by the
    symbol (USOIL, GOLD, SILVER, NATGAS, BRENT, GIFTNIFTY).
    """
    from database.watchlist_db import (
        add_item,
        clear_watchlist,
        create_watchlist,
        delete_watchlist,
        get_watchlists,
    )

    if request.method == "GET":
        lists = get_watchlists(_sync_user())
        return jsonify({"status": "success", "data": {"lists": lists}})

    payload = request.get_json(silent=True) or {}
    incoming = payload.get("lists")
    if not isinstance(incoming, list):
        return jsonify({"status": "error", "message": "lists must be a list"}), 400

    user = _sync_user()
    if not user:
        return jsonify({"status": "error", "message": "Authentication required"}), 401
    # Replace wholesale: delete every list the device does not carry, then
    # create/overwrite the ones it does. Watchlists are few, so this is cheap
    # and keeps desktop and phone trivially identical afterwards.
    existing = get_watchlists(user)
    existing_by_name = {l["name"]: l for l in existing}
    incoming_names = set()
    for entry in incoming[:20]:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()[:64]
        if not name:
            continue
        incoming_names.add(name)
        items = []
        for it in (entry.get("items") or [])[:250]:
            if not isinstance(it, dict):
                continue
            sym = (it.get("symbol") or "").strip().upper()
            exch = (it.get("exchange") or "").strip().upper()
            if sym and exch:
                items.append({"symbol": sym, "exchange": exch})
        target = existing_by_name.get(name)
        if target is None:
            created = create_watchlist(user, name, items or None)
            if created is None:
                logger.warning("watchlist sync: create failed for %r", name)
            continue
        # Exists: rebuild its items (clear, then re-add in order).
        clear_watchlist(user, target["id"])
        for it in items:
            add_item(user, target["id"], it["symbol"], it["exchange"])
    for lname, lst in existing_by_name.items():
        if lname not in incoming_names:
            delete_watchlist(user, lst["id"])

    lists = get_watchlists(user)
    return jsonify({"status": "success", "data": {"lists": lists}})
