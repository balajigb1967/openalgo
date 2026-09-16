# blueprints/tv_watchlist.py
"""
TradingView -> Watchlist plugin.

Two surfaces:

- POST /tvwatchlist/webhook/<token> -- the URL pasted into a TradingView
  alert. TradingView cannot set HTTP headers, so the URL token is the
  credential (same design as the strategy module webhook). The alert body
  needs a "ticker" or "symbols" field; {{ticker}} and {{exchange}} from
  TradingView's placeholder menu both work.
- /tvwatchlist/api/* -- session-authenticated endpoints the /tradingview page
  uses to read/update config, rotate the token, and paste-import symbols.

The webhook answers every caller in plain words and never leaks whether a
token exists beyond the message it already needs to send. An unknown token is
answered with 404 here rather than aborting, so it does not feed
Error404Tracker and count toward an IP ban -- a scanner walking the token
space must not be able to get the owner's own address banned.
"""

import os

from flask import Blueprint, jsonify, request
from flask import session as flask_session

from database.tv_watchlist_db import (
    add_log_entries,
    get_config,
    get_config_by_token,
    get_recent_log,
    rotate_token,
    update_config,
)
from database.watchlist_db import get_watchlists
from limiter import limiter
from services.tv_watchlist_service import sync_symbols
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

tv_watchlist_bp = Blueprint("tv_watchlist_bp", __name__, url_prefix="/tvwatchlist")

WEBHOOK_RATE_LIMIT = os.getenv("WEBHOOK_RATE_LIMIT", "100 per minute")

#: Cap on a pasted import or an alert's symbol list. The charting watchlist
#: caps itself at 250 per list; this bounds work per request before that.
MAX_SYMBOLS_PER_REQUEST = 200


@tv_watchlist_bp.route("/webhook/<token>", methods=["POST"])
@limiter.limit(WEBHOOK_RATE_LIMIT)
def webhook(token: str):
    """Receive one TradingView alert and sync its symbols into the watchlists.

    Accepted bodies (TradingView alert message box, placeholders allowed):
      {"ticker": "{{ticker}}", "exchange": "{{exchange}}"}
      {"symbols": "NSE:RELIANCE, NSE:TCS"}
      {"secret": "...", "ticker": "{{ticker}}"}   -- when a secret is set
    """
    config = get_config_by_token(token)
    if config is None:
        logger.warning("TradingView watchlist webhook hit with an unknown token")
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "This watchlist webhook link is not valid. Re-copy the URL from the TradingView page in OpenAlgo.",
                }
            ),
            404,
        )

    if not config.enabled:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "TradingView watchlist sync is switched off. Turn it on in OpenAlgo under Platforms > TradingView.",
                }
            ),
            409,
        )

    data = request.get_json(silent=True)
    if data is None:
        # TradingView can also be configured to send plain text.
        raw = request.get_data(as_text=True) or ""
    else:
        if isinstance(data, str):
            raw = data
        else:
            raw = data.get("symbols") or data.get("ticker") or data.get("symbol") or ""

    # Optional shared secret inside the payload.
    if isinstance(data, dict) and config.webhook_secret:
        supplied = str(data.get("secret") or "")
        if supplied != config.webhook_secret:
            logger.warning("TradingView watchlist webhook secret mismatch")
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "The alert's secret does not match the one configured in OpenAlgo.",
                    }
                ),
                401,
            )

    # Prefer the explicit exchange field when the alert carries one: the
    # placeholder resolves to TradingView's own code (NSE, BSE, MCX), which is
    # stronger evidence than the fallback chain.
    exchange_field = ""
    if isinstance(data, dict):
        exchange_field = str(data.get("exchange") or "").strip().upper()
    if exchange_field and ":" not in str(raw):
        raw = f"{exchange_field}:{raw}" if raw else raw

    if not raw or not str(raw).strip():
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "The alert had no symbols. Put {{ticker}} in the alert message, or a symbol list.",
                }
            ),
            400,
        )

    raw_text = str(raw)
    token_count = len([t for t in raw_text.replace(",", " ").split() if t.strip()])
    if token_count > MAX_SYMBOLS_PER_REQUEST:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": f"Too many symbols in one alert ({token_count}). The limit is {MAX_SYMBOLS_PER_REQUEST}.",
                }
            ),
            413,
        )

    result = sync_symbols(raw_text, source="webhook")
    status = 200 if result["status"] == "success" else 400
    return jsonify(result), status


# ---------------------------------------------------------------------------
# Session-authenticated API for the /tradingview page
# ---------------------------------------------------------------------------


def _user():
    return flask_session.get("user")


@tv_watchlist_bp.route("/api/config", methods=["GET"])
@check_session_validity
def read_config():
    """Config plus the watchlists the picker offers."""
    config = get_config()
    lists = [
        {"id": wl["id"], "name": wl["name"], "count": len(wl["items"])}
        for wl in get_watchlists(_user())
    ]
    return jsonify({"status": "success", "data": {"config": config, "watchlists": lists}})


@tv_watchlist_bp.route("/api/config", methods=["POST"])
@check_session_validity
def write_config():
    """Update the sync settings. Ownership of the target list is checked."""
    payload = request.get_json(silent=True) or {}
    user = _user()

    watchlist_id = payload.get("chart_watchlist_id")
    if watchlist_id is not None:
        if not isinstance(watchlist_id, int):
            return jsonify({"status": "error", "message": "chart_watchlist_id must be an id"}), 400
        owned = any(wl["id"] == watchlist_id for wl in get_watchlists(user))
        if not owned:
            return (
                jsonify({"status": "error", "message": "That watchlist does not exist"}),
                404,
            )

    updated = update_config(
        user_id=user,
        enabled=payload["enabled"] if isinstance(payload.get("enabled"), bool) else None,
        webhook_secret=(
            str(payload.get("webhook_secret"))
            if payload.get("webhook_secret") is not None
            else None
        ),
        chart_watchlist_id=watchlist_id,
        include_historify=(
            payload["include_historify"]
            if isinstance(payload.get("include_historify"), bool)
            else None
        ),
    )
    if updated is None:
        return jsonify({"status": "error", "message": "Could not save the settings"}), 500
    return jsonify({"status": "success", "data": updated})


@tv_watchlist_bp.route("/api/token/rotate", methods=["POST"])
@check_session_validity
def rotate():
    """Replace the webhook URL token. Old alert URLs stop working after this."""
    new_token = rotate_token()
    if not new_token:
        return jsonify({"status": "error", "message": "Could not rotate the webhook link"}), 500
    return jsonify({"status": "success", "data": {"webhook_token": new_token}})


@tv_watchlist_bp.route("/api/import", methods=["POST"])
@check_session_validity
def import_symbols():
    """Paste-import: same pipeline as the webhook, from the logged-in page.

    Accepts an optional watchlist_id override and an include_historify
    override so a one-off import can target a list without reconfiguring the
    webhook.
    """
    payload = request.get_json(silent=True) or {}
    raw = str(payload.get("symbols") or "")
    if not raw.strip():
        return (
            jsonify({"status": "error", "message": "Paste at least one symbol to import"}),
            400,
        )

    watchlist_id = payload.get("watchlist_id")
    if watchlist_id is not None:
        if not isinstance(watchlist_id, int):
            return jsonify({"status": "error", "message": "watchlist_id must be an id"}), 400
        owned = any(wl["id"] == watchlist_id for wl in get_watchlists(_user()))
        if not owned:
            return jsonify({"status": "error", "message": "That watchlist does not exist"}), 404

    include_historify = payload.get("include_historify")
    if not isinstance(include_historify, bool):
        include_historify = None

    token_count = len([t for t in raw.replace(",", " ").split() if t.strip()])
    if token_count > MAX_SYMBOLS_PER_REQUEST:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": f"Too many symbols ({token_count}). Import at most {MAX_SYMBOLS_PER_REQUEST} at a time.",
                }
            ),
            413,
        )

    result = sync_symbols(
        raw,
        source="import",
        user_id=_user(),
        chart_watchlist_id=watchlist_id,
        include_historify=include_historify,
    )
    return jsonify(result), (200 if result["status"] == "success" else 400)


@tv_watchlist_bp.route("/api/log", methods=["GET"])
@check_session_validity
def recent_log():
    """The newest webhook/import outcomes for the status card."""
    return jsonify({"status": "success", "data": get_recent_log()})
