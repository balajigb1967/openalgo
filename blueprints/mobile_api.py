# blueprints/mobile_api.py
"""
Mobile app backend — one compact REST surface for the OpenAlgo mobile SPA.

The mobile app (served from /m/) is a session-authenticated single page that
mirrors the desktop widgets: watchlist, chart page with Market Brief +
Orderflow + News, Scalper Advisor, Scalper Terminal (chain + depth + orders),
OpenAlgo tools (search/quotes/depth/close-cancel), Widgets Hub, Account and
Settings (tunnel port control).

This blueprint only adapts what already exists — plugin services, books
services, scalping terminal APIs, the ngrok tunnel manager — into phone-shaped
JSON payloads with CSRF exempted (session cookie only; JSON APIs under a
dedicated prefix, mirroring /api/v1's exemption) so the SPA can call them with
plain fetch + same-origin cookies.
"""

from flask import Blueprint, jsonify, request

from database.watchlist_db import get_watchlists
from services.positionbook_service import get_positionbook
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

mobile_bp = Blueprint("mobile_api_bp", __name__, url_prefix="/m/api")


def _user():
    from flask import session as flask_session

    return flask_session.get("user")


# ---------------------------------------------------------------------------
# Health / session bootstrap
# ---------------------------------------------------------------------------
@mobile_bp.route("/session", methods=["GET"])
def session_info():
    """Who am I + app mode + broker. Lets the SPA boot without guessing."""
    from database.settings_db import get_analyze_mode
    from flask import session as flask_session

    username = _user()
    if not username or not flask_session.get("logged_in"):
        return jsonify({"status": "error", "message": "Not authenticated"}), 401

    broker = flask_session.get("broker")
    try:
        analyzer = bool(get_analyze_mode())
    except Exception:  # noqa: BLE001
        analyzer = False
    return jsonify(
        {
            "status": "success",
            "user": username,
            "broker": broker,
            "mode": "analyzer" if analyzer else "live",
        }
    )


# ---------------------------------------------------------------------------
# Watchlist — the charting watchlist DB (same lists the terminal uses)
# ---------------------------------------------------------------------------
@mobile_bp.route("/watchlists", methods=["GET"])
@check_session_validity
def watchlists_route():
    """Every watchlist with its instruments."""
    user = _user()
    lists = get_watchlists(user)
    return jsonify({"status": "success", "data": lists})


@mobile_bp.route("/watchlist/quotes", methods=["POST"])
@check_session_validity
def watchlist_quotes_route():
    """Multi-quotes for a list of (symbol, exchange) pairs.

    Body: {"symbols": [{"symbol": "...", "exchange": "..."}]}
    Returns one row per symbol: ltp, change %, open/high/low — enough to paint
    the phone list. OI and volume ride along when the broker provides them.
    """
    payload = request.get_json(silent=True) or {}
    symbols = payload.get("symbols") or []
    if not isinstance(symbols, list) or not symbols:
        return jsonify({"status": "error", "message": "symbols required"}), 400

    from database.auth_db import get_auth_token
    from database.settings_db import get_analyze_mode
    from services.quotes_service import get_quotes

    user = _user()
    try:
        analyzer = bool(get_analyze_mode())
    except Exception:  # noqa: BLE001
        analyzer = False

    rows = []
    for item in symbols[:50]:  # hard cap: a phone list is not a screener
        symbol = (item.get("symbol") or "").strip()
        exchange = (item.get("exchange") or "").strip().upper()
        row = {"symbol": symbol, "exchange": exchange, "ltp": None, "chp": None,
               "open": None, "high": None, "low": None, "prev_close": None,
               "volume": None, "oi": None}
        if not symbol or not exchange:
            rows.append(row)
            continue
        try:
            auth_token = None if analyzer else get_auth_token(user)
            if auth_token:
                success, res, _code = get_quotes(
                    symbol=symbol, exchange=exchange, auth_token=auth_token,
                    broker=flask_session_broker(),
                )
            else:
                # No broker token (fresh login, token rollover) — the API key
                # path still serves market data, like the desktop panels.
                from database.auth_db import get_api_key_for_tradingview

                api_key = get_api_key_for_tradingview(user)
                success, res, _code = get_quotes(symbol=symbol, exchange=exchange, api_key=api_key)
            if success and isinstance(res, dict):
                data = res.get("data") or {}
                ltp = data.get("ltp")
                prev = data.get("prev_close")
                row.update(
                    ltp=ltp,
                    chp=round(((ltp - prev) / prev) * 100, 2) if (ltp and prev) else None,
                    open=data.get("open"),
                    high=data.get("high"),
                    low=data.get("low"),
                    prev_close=prev,
                    volume=data.get("volume"),
                    oi=data.get("oi"),
                )
        except Exception as e:  # noqa: BLE001 — one dead symbol must not kill the list
            logger.debug(f"mobile quote failed for {exchange}:{symbol}: {e}")
        rows.append(row)

    return jsonify({"status": "success", "data": rows})


def flask_session_broker():
    from flask import session as flask_session

    return flask_session.get("broker")


# ---------------------------------------------------------------------------
# Symbol search (OpenAlgo tools + watchlist add flows)
# ---------------------------------------------------------------------------
@mobile_bp.route("/search", methods=["GET"])
@check_session_validity
def search_route():
    """Instrument search. Query: q, exchange (optional)."""
    query = (request.args.get("q") or "").strip()
    exchange = (request.args.get("exchange") or "").strip().upper() or None
    if len(query) < 2:
        return jsonify({"status": "success", "data": []})
    from services.search_service import search_symbols

    _success, res, _code = search_symbols(query, exchange=exchange)
    results = res.get("data") if isinstance(res, dict) else []
    rows = []
    for r in (results or [])[:30]:
        rows.append(
            {
                "symbol": r.get("symbol"),
                "exchange": r.get("exchange"),
                "name": r.get("name"),
                "lotsize": r.get("lotsize"),
                "expiry": r.get("expiry"),
                "instrumenttype": r.get("instrumenttype") or r.get("instrument_type"),
                "strike": r.get("strike"),
                "optiontype": r.get("optiontype") or r.get("option_type"),
            }
        )
    return jsonify({"status": "success", "data": rows})


# ---------------------------------------------------------------------------
# Books (Account panel)
# ---------------------------------------------------------------------------
@mobile_bp.route("/account", methods=["GET"])
@check_session_validity
def account_route():
    """Funds + open positions + today's orders in one phone-shaped payload."""
    from database.auth_db import get_api_key_for_tradingview, get_auth_token
    from database.settings_db import get_analyze_mode
    from flask import session as flask_session
    from services.funds_service import get_funds
    from services.orderbook_service import get_orderbook

    user = _user()
    broker = flask_session.get("broker")
    try:
        analyzer = bool(get_analyze_mode())
    except Exception:  # noqa: BLE001
        analyzer = False

    funds, positions, orders = None, [], []
    try:
        api_key = get_api_key_for_tradingview(user)
        auth_token = None if analyzer else get_auth_token(user)
        if auth_token:
            _s, fres, _c = get_funds(auth_token=auth_token, broker=broker)
            _s1, ores, _c1 = get_orderbook(auth_token=auth_token, broker=broker)
        else:
            _s, fres, _c = get_funds(api_key=api_key)
            _s1, ores, _c1 = get_orderbook(api_key=api_key)
        if isinstance(fres, dict):
            funds = fres.get("data") or fres.get("funds")
        if isinstance(ores, dict):
            orders = (ores.get("data") or {}).get("orders") or []
    except Exception as e:  # noqa: BLE001
        logger.warning(f"mobile account funds/orders failed: {e}")

    try:
        payload = _positions_payload()
        positions = payload.get("positions") or []
        positions_error = payload.get("message")
    except Exception as e:  # noqa: BLE001
        positions, positions_error = [], str(e)

    return jsonify(
        {
            "status": "success",
            "mode": "analyzer" if analyzer else "live",
            "broker": broker,
            "funds": funds,
            "positions": positions,
            "orders": orders[:50],
            "positions_message": positions_error,
        }
    )


def _positions_payload():
    """Reuse the desktop /positions logic via its service path."""
    from database.auth_db import get_api_key_for_tradingview, get_auth_token
    from database.settings_db import get_analyze_mode
    from flask import session as flask_session
    from services.positionbook_service import get_positionbook

    user = _user()
    broker = flask_session.get("broker")
    try:
        analyzer = bool(get_analyze_mode())
    except Exception:  # noqa: BLE001
        analyzer = False
    if analyzer:
        api_key = get_api_key_for_tradingview(user)
        _s, res, _c = get_positionbook(api_key=api_key)
    else:
        auth_token = get_auth_token(user)
        if auth_token:
            _s, res, _c = get_positionbook(auth_token=auth_token, broker=broker)
        else:
            api_key = get_api_key_for_tradingview(user)
            _s, res, _c = get_positionbook(api_key=api_key)
    if isinstance(res, dict):
        return {"positions": res.get("data") or [], "message": res.get("message")}
    return {"positions": [], "message": "positionbook unavailable"}


# ---------------------------------------------------------------------------
# Scalper Advisor + Orderflow + Brief + News (thin adapters to the plugins)
# ---------------------------------------------------------------------------
@mobile_bp.route("/advisor", methods=["GET"])
@check_session_validity
def advisor_route():
    """Scalper advisor snapshot. Query: refresh=1, arm=KEY, disarm=KEY, auto_arm=1."""
    from services.scalper_advisor_service import scalper_advisor

    data = scalper_advisor(
        refresh=(request.args.get("refresh") in ("1", "true", "yes")),
        arm_key=request.args.get("arm") or None,
        disarm_key=request.args.get("disarm") or None,
        auto_arm=(request.args.get("auto_arm") in ("1", "true", "yes")),
    )
    return jsonify(data)


@mobile_bp.route("/scalper/close", methods=["POST"])
@check_session_validity
def scalper_close_route():
    from services.scalper_advisor_service import close_alert

    payload = request.get_json(silent=True) or {}
    alert_id = payload.get("alert_id")
    if not alert_id:
        return jsonify({"status": "error", "message": "alert_id required"}), 400
    data = close_alert(alert_id, payload.get("reason") or "Closed from mobile")
    return jsonify(data if isinstance(data, dict) else {"status": "success"})


@mobile_bp.route("/orderflow/table", methods=["GET"])
@check_session_validity
def orderflow_table_route():
    """Orderflow table rows (9 roots, cached). Query: tf, refresh=1."""
    from blueprints.scalper_orderflow import (
        _TABLE_CACHE,
        _TABLE_TTL,
        _orderflow_row,
        DEFAULT_ORDERFLOW_ROOTS,
    )

    tf = request.args.get("tf") or "5m"
    refresh = (request.args.get("refresh") in ("1", "true", "yes"))
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    now = _time.time()
    with _TABLE_CACHE["lock"]:
        cached = _TABLE_CACHE.get("data") if not refresh else None
        if cached and cached.get("timeframe") == tf and now - _TABLE_CACHE["ts"] < _TABLE_TTL:
            return jsonify(cached)
    with ThreadPoolExecutor(max_workers=len(DEFAULT_ORDERFLOW_ROOTS)) as ex:
        rows = list(ex.map(lambda inst: _orderflow_row(inst, tf), DEFAULT_ORDERFLOW_ROOTS))
    payload = {"status": "success", "timeframe": tf, "rows": rows}
    with _TABLE_CACHE["lock"]:
        _TABLE_CACHE["ts"] = _time.time()
        _TABLE_CACHE["data"] = payload
    return jsonify(payload)


@mobile_bp.route("/brief", methods=["GET"])
@check_session_validity
def brief_route():
    """Market brief snapshot (server-cached)."""
    from services.market_brief_service import market_brief

    refresh = (request.args.get("refresh") in ("1", "true", "yes"))
    return jsonify(market_brief(refresh=refresh))


@mobile_bp.route("/news", methods=["GET"])
@check_session_validity
def news_route():
    from services.market_news_service import fetch_news

    limit = min(100, max(10, int(request.args.get("limit") or 40)))
    refresh = (request.args.get("refresh") in ("1", "true", "yes"))
    return jsonify(fetch_news(limit=limit, refresh=refresh))


@mobile_bp.route("/news/symbol", methods=["GET"])
@check_session_validity
def news_symbol_route():
    from services.market_news_service import fetch_symbol_news

    symbol = request.args.get("symbol") or "NSE:NIFTY"
    limit = min(80, max(10, int(request.args.get("limit") or 20)))
    return jsonify(fetch_symbol_news(symbol, limit))


# ---------------------------------------------------------------------------
# Scalper terminal — chain, depth, orders (same contracts as /scalping)
# ---------------------------------------------------------------------------
@mobile_bp.route("/chain", methods=["GET"])
@check_session_validity
def chain_route():
    """Option chain for the terminal. Query: underlying, exchange, expiry, count."""
    from blueprints.scalping import _get_api_key, _normalize_expiry, VALID_LEG_EXCHANGES
    from services.option_chain_service import get_option_chain

    underlying = (request.args.get("underlying") or "").strip().upper()
    exchange = (request.args.get("exchange") or "").strip().upper()
    expiry_date = _normalize_expiry((request.args.get("expiry") or "").strip().upper())
    try:
        strike_count = max(1, min(20, int(request.args.get("count") or 8)))
    except (TypeError, ValueError):
        strike_count = 8

    if not underlying or not expiry_date or exchange not in VALID_LEG_EXCHANGES:
        return jsonify({"status": "error", "message": "underlying, exchange, expiry required"}), 400

    api_key = _get_api_key()
    if not api_key:
        return jsonify({"status": "error", "message": "API key not configured"}), 401

    success, response, status_code = get_option_chain(
        underlying=underlying, exchange=exchange, expiry_date=expiry_date,
        strike_count=strike_count, api_key=api_key, with_quotes=False,
    )
    if isinstance(response, dict):
        response["fo_exchange"] = exchange
    return jsonify(response), status_code


@mobile_bp.route("/expiries", methods=["GET"])
@check_session_validity
def expiries_route():
    """Expiry list for underlying+exchange. Query: underlying, exchange."""
    from blueprints.scalping import _get_api_key
    from services.expiry_service import get_expiry_dates

    underlying = (request.args.get("underlying") or "").strip().upper()
    exchange = (request.args.get("exchange") or "").strip().upper()
    api_key = _get_api_key()
    if not api_key:
        return jsonify({"status": "error", "message": "API key not configured"}), 401
    success, response, status_code = get_expiry_dates(
        symbol=underlying, exchange=exchange, instrumenttype="options", api_key=api_key
    )
    return jsonify(response), status_code


@mobile_bp.route("/depth", methods=["GET"])
@check_session_validity
def depth_route():
    """Top-of-book for one symbol. Query: symbol, exchange."""
    from database.auth_db import get_api_key_for_tradingview

    symbol = (request.args.get("symbol") or "").strip().upper()
    exchange = (request.args.get("exchange") or "").strip().upper()
    if not symbol or not exchange:
        return jsonify({"status": "error", "message": "symbol & exchange required"}), 400
    from services.depth_service import get_depth

    api_key = get_api_key_for_tradingview(_user())
    success, response, status_code = get_depth(symbol=symbol, exchange=exchange, api_key=api_key)
    return jsonify(response), status_code


@mobile_bp.route("/order", methods=["POST"])
@check_session_validity
def order_route():
    """Place a market order through the scalping terminal's own plumbing.

    Body: {symbol, exchange, action: BUY|SELL, quantity, product, lots?}
    Reuses blueprints.scalping's validation + quota logic by calling its view
    function's internals — same lot cap, same sandbox/live routing.
    """
    from blueprints.scalping import _resolve_session_auth, _validate_quantity
    from services.place_order_service import place_order

    payload = request.get_json(silent=True) or {}
    symbol = (payload.get("symbol") or "").strip().upper()
    exchange = (payload.get("exchange") or "").strip().upper()
    action = (payload.get("action") or "").strip().upper()
    product = (payload.get("product") or "MIS").strip().upper()
    try:
        quantity = int(payload.get("quantity") or 0)
    except (TypeError, ValueError):
        quantity = 0

    if not symbol or not exchange or action not in ("BUY", "SELL") or quantity <= 0:
        return jsonify({"status": "error", "message": "symbol, exchange, action, quantity required"}), 400
    if product not in ("MIS", "NRML", "CNC"):
        return jsonify({"status": "error", "message": "invalid product"}), 400

    qty_error = _validate_quantity(symbol, exchange, quantity)
    if qty_error:
        return jsonify({"status": "error", "message": qty_error}), 400

    _auth_token, _broker, api_key, err, code = _resolve_session_auth()
    if err:
        return jsonify(err), code

    success, response, status_code = place_order(
        order_data={
            "strategy": "MobileApp",
            "symbol": symbol,
            "exchange": exchange,
            "action": action,
            "pricetype": "MARKET",
            "product": product,
            "quantity": quantity,
        },
        api_key=api_key,
    )
    return jsonify(response), status_code


@mobile_bp.route("/positions/close", methods=["POST"])
@check_session_validity
def close_position_route():
    """Flatten one position. Body: {symbol, exchange, product}."""
    from flask import session as flask_session
    from services.close_position_service import close_position

    payload = request.get_json(silent=True) or {}
    symbol = (payload.get("symbol") or "").strip().upper()
    exchange = (payload.get("exchange") or "").strip().upper()
    product = (payload.get("product") or "").strip().upper()
    if not symbol or not exchange or not product:
        return jsonify({"status": "error", "message": "symbol, exchange, product required"}), 400

    from database.auth_db import get_api_key_for_tradingview, get_auth_token
    from database.settings_db import get_analyze_mode

    user = _user()
    broker = flask_session.get("broker")
    try:
        analyzer = bool(get_analyze_mode())
    except Exception:  # noqa: BLE001
        analyzer = False
    position_data = {"symbol": symbol, "exchange": exchange, "product": product}
    if analyzer:
        api_key = get_api_key_for_tradingview(user)
        _success, response, status_code = close_position(
            position_data=position_data, api_key=api_key
        )
    else:
        auth_token = get_auth_token(user)
        _success, response, status_code = close_position(
            position_data=position_data, auth_token=auth_token, broker=broker
        )
    return jsonify(response), status_code


# ---------------------------------------------------------------------------
# Settings — tunnel (ngrok) control
# ---------------------------------------------------------------------------
@mobile_bp.route("/tunnel", methods=["GET"])
@check_session_validity
def tunnel_status_route():
    """Tunnel status + the app's bind config, for the Settings screen."""
    from flask import session as flask_session
    from utils.ngrok_manager import get_ngrok_url, is_ngrok_enabled

    import os

    return jsonify(
        {
            "status": "success",
            "data": {
                "enabled": is_ngrok_enabled(),
                "url": get_ngrok_url(),
                "allow_env": os.getenv("NGROK_ALLOW", "FALSE"),
                "flask_port": os.getenv("FLASK_PORT", "5000"),
                "flask_host": os.getenv("FLASK_HOST_IP", "127.0.0.1"),
                "host_server": os.getenv("HOST_SERVER", ""),
                "user": flask_session.get("user"),
            },
        }
    )


@mobile_bp.route("/tunnel/port", methods=["POST"])
@check_session_validity
def tunnel_port_route():
    """Tunnel port control.

    Body: {"port": 5000} — restars the tunnel on a new local port.
    The Flask app itself is already bound (systemd); this manages the ngrok
    side so the phone can point a tunnel at any locally-listening port (the
    trading app on 5000, a dev copy, another service).
    """
    import os

    from utils.ngrok_manager import kill_existing_ngrok, start_ngrok_tunnel

    payload = request.get_json(silent=True) or {}
    try:
        port = int(payload.get("port") or int(os.getenv("FLASK_PORT", "5000")))
    except (TypeError, ValueError):
        port = 5000
    if not (1 <= port <= 65535):
        return jsonify({"status": "error", "message": "port must be 1-65535"}), 400

    if os.getenv("NGROK_ALLOW", "FALSE").upper() != "TRUE":
        return jsonify(
            {"status": "error",
             "message": "ngrok is disabled on the server (NGROK_ALLOW != TRUE). "
                        "Enable it in the desktop Admin UI, then restart this tunnel."}
        ), 400

    kill_existing_ngrok()
    url = start_ngrok_tunnel(port)
    if not url:
        return jsonify({"status": "error", "message": "Tunnel start failed — check server logs"}), 500
    return jsonify({"status": "success", "data": {"url": url, "port": port}})
