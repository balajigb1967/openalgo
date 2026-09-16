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

from flask import Blueprint, jsonify, request
from flask import session as flask_session

from database.orderflow_db import health_check as of_health
from database.scalper_db import health_check as scalper_health
from services.orderflow_service import get_orderflow
from services.scalper_advisor_service import (
    alert_chart_data,
    scalper_advisor,
)
from utils.logging import get_logger
from utils.session import check_session_validity

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


def _uid() -> str:
    return (flask_session.get("user_id") or "")


@scalper_orderflow_bp.route("/scalper/advisor", methods=["GET"])
@check_session_validity
def scalper_advisor_route():
    """Full advisory payload. Query: refresh=1 forces rebuild."""
    try:
        refresh = (request.args.get("refresh") in ("1", "true", "yes"))
        arm_key = request.args.get("arm") or None
        disarm_key = request.args.get("disarm") or None
        arm_alert_id = request.args.get("arm_alert_id") or None
        close_alert_id = request.args.get("close_alert_id") or None
        close_reason = request.args.get("close_reason") or "Manual close"
        data = scalper_advisor(
            refresh=refresh,
            arm_key=arm_key,
            disarm_key=disarm_key,
            arm_alert_id=arm_alert_id,
            close_alert_id=close_alert_id,
            close_reason=close_reason,
        )
        return jsonify(data)
    except Exception as e:
        logger.exception(f"scalper advisor failed: {e}")
        return jsonify({"status": "error", "message": f"Advisor failed: {e}"}), 500


@scalper_orderflow_bp.route("/scalper/close", methods=["POST"])
@check_session_validity
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
@check_session_validity
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


@scalper_orderflow_bp.route("/orderflow/table", methods=["GET"])
@check_session_validity
def orderflow_table_route():
    """Orderflow rows for the default root set. Query: tf (default 5m)."""
    try:
        tf = request.args.get("tf") or "5m"
        rows = []
        for inst in DEFAULT_ORDERFLOW_ROOTS:
            try:
                data = get_orderflow(f"{inst['market']}:{inst['key']}", tf, 25)
                s = data.get("summary") or {}
                rows.append({
                    "key": inst["key"],
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
                })
            except Exception as row_e:
                logger.warning("orderflow row %s failed: %s", inst["key"], row_e)
                rows.append({"key": inst["key"], "name": inst["name"],
                             "market": inst["market"], "error": str(row_e)})
        return jsonify({"status": "success", "timeframe": tf, "rows": rows})
    except Exception as e:
        logger.exception(f"orderflow table failed: {e}")
        return jsonify({"status": "error", "message": f"Orderflow failed: {e}"}), 500


@scalper_orderflow_bp.route("/orderflow/detail", methods=["GET"])
@check_session_validity
def orderflow_detail_route():
    """One symbol's timewise orderflow bars. Query: symbol (EXCH:SYM), tf, bars."""
    try:
        symbol = request.args.get("symbol") or "NSE:NIFTY 50"
        tf = request.args.get("tf") or "5m"
        if tf not in _MAX_TF | {"D"}:
            tf = "5m"
        bars_n = int(request.args.get("bars") or 25)
        data = get_orderflow(symbol, tf, bars_n)
        return jsonify(data)
    except Exception as e:
        logger.exception(f"orderflow detail failed: {e}")
        return jsonify({"status": "error", "message": f"Orderflow failed: {e}"}), 500


@scalper_orderflow_bp.route("/orderflow/health", methods=["GET"])
@check_session_validity
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
