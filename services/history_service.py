import importlib
import os
import threading
import time
from typing import Any

import httpx
import pandas as pd

from database.auth_db import get_auth_token_broker
from database.token_db import get_token
from utils.constants import VALID_EXCHANGES
from utils.logging import get_logger

# Initialize logger
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Peer-instance failover for history
#
# When the local broker's history API is unavailable (dead token, outage),
# transparently retry against a sibling OpenAlgo instance.  Reuses the same
# PEER_OPENALGO_URL / PEER_OPENALGO_API_KEY env vars as quotes_service.
# ---------------------------------------------------------------------------

def _peer_config() -> tuple[str, str, float, float]:
    """Read peer settings lazily so .env load order never matters."""
    return (
        os.getenv("PEER_OPENALGO_URL", "").strip(),
        os.getenv("PEER_OPENALGO_API_KEY", "").strip(),
        float(os.getenv("PEER_OPENALGO_TIMEOUT", "10")),   # history can be slow
        float(os.getenv("PEER_OPENALGO_COOLDOWN", "300")),
    )


_peer_lock = threading.Lock()
_peer_down_until = 0.0
_peer_inflight = threading.local()


def _peer_available() -> bool:
    url, key, _, _ = _peer_config()
    return bool(url and key) and time.monotonic() >= _peer_down_until


def _mark_peer_down(seconds: float | None = None) -> None:
    global _peer_down_until
    _, _, _, cooldown = _peer_config()
    with _peer_lock:
        _peer_down_until = time.monotonic() + (seconds if seconds is not None else cooldown)


def _try_peer_history(
    symbol: str, exchange: str, interval: str,
    start_date: str, end_date: str, local_broker: str,
) -> tuple[bool, dict[str, Any], int] | None:
    """Fetch history from the peer instance; None if unavailable."""
    if not _peer_available():
        return None
    if getattr(_peer_inflight, "active", False):
        return None  # never recurse A -> B -> A
    url_base, key, timeout, _ = _peer_config()
    if not (url_base and key):
        return None
    _peer_inflight.active = True
    try:
        url = url_base.rstrip("/") + "/api/v1/history"
        payload = {
            "apikey": key,
            "symbol": symbol,
            "exchange": exchange,
            "interval": interval,
            "start_date": start_date,
            "end_date": end_date,
        }
        try:
            resp = httpx.post(url, json=payload, timeout=timeout)
            if resp.status_code == 429:
                _mark_peer_down(30.0)
                return None
            if resp.status_code == 200:
                body = resp.json()
                if body.get("status") == "success" and body.get("data"):
                    logger.info(
                        f"History {exchange}:{symbol} served by peer instance "
                        f"(local broker '{local_broker}' unusable)"
                    )
                    return True, body, 200
                return None
            logger.debug(f"Peer history {url} -> HTTP {resp.status_code}")
            if resp.status_code >= 500 or resp.status_code in (401, 403):
                _mark_peer_down()
            return None
        except Exception as exc:
            logger.debug(f"Peer history {url} failed: {exc}")
            _mark_peer_down()
            return None
    finally:
        _peer_inflight.active = False


# Rate limiter: max 3 broker history API requests per second
# Uses minimum interval between calls to prevent burst requests
_last_history_call: float = 0.0
_MIN_HISTORY_INTERVAL = 0.35  # 350ms between calls (~3 req/sec, evenly spaced)


def _enforce_rate_limit():
    """Block until enough time has passed since the last request (~3 per second)."""
    global _last_history_call
    now = time.monotonic()
    elapsed = now - _last_history_call
    if elapsed < _MIN_HISTORY_INTERVAL:
        time.sleep(_MIN_HISTORY_INTERVAL - elapsed)
    _last_history_call = time.monotonic()


def validate_symbol_exchange(symbol: str, exchange: str) -> tuple[bool, str | None]:
    """
    Validate that a symbol exists for the given exchange.

    Args:
        symbol: Trading symbol
        exchange: Exchange (e.g., NSE, NFO)

    Returns:
        Tuple of (is_valid, error_message)
    """
    # Validate exchange
    exchange_upper = exchange.upper()
    if exchange_upper not in VALID_EXCHANGES:
        return False, f"Invalid exchange '{exchange}'. Must be one of: {', '.join(VALID_EXCHANGES)}"

    # Validate symbol exists in master contract
    token = get_token(symbol, exchange_upper)
    if token is None:
        return (
            False,
            f"Symbol '{symbol}' not found for exchange '{exchange}'. Please verify the symbol name and ensure master contracts are downloaded.",
        )

    return True, None


def import_broker_module(broker_name: str) -> Any | None:
    """
    Dynamically import the broker-specific data module.

    Args:
        broker_name: Name of the broker

    Returns:
        The imported module or None if import fails
    """
    try:
        module_path = f"broker.{broker_name}.api.data"
        broker_module = importlib.import_module(module_path)
        return broker_module
    except ImportError as error:
        logger.error(f"Error importing broker module '{module_path}': {error}")
        return None


def get_history_with_auth(
    auth_token: str,
    feed_token: str | None,
    broker: str,
    symbol: str,
    exchange: str,
    interval: str,
    start_date: str,
    end_date: str,
) -> tuple[bool, dict[str, Any], int]:
    """
    Get historical data for a symbol using provided auth tokens.

    Args:
        auth_token: Authentication token for the broker API
        feed_token: Feed token for market data (if required by broker)
        broker: Name of the broker
        symbol: Trading symbol
        exchange: Exchange (e.g., NSE, BSE)
        interval: Time interval (e.g., 1m, 5m, 15m, 1h, 1d)
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format

    Returns:
        Tuple containing:
        - Success status (bool)
        - Response data (dict)
        - HTTP status code (int)
    """
    # Validate symbol and exchange before making broker API call
    is_valid, error_msg = validate_symbol_exchange(symbol, exchange)
    if not is_valid:
        return False, {"status": "error", "message": error_msg}, 400

    broker_module = import_broker_module(broker)
    if broker_module is None:
        return False, {"status": "error", "message": "Broker-specific module not found"}, 404

    try:
        # Initialize broker's data handler based on broker's requirements
        if hasattr(broker_module.BrokerData.__init__, "__code__"):
            # Check number of parameters the broker's __init__ accepts
            param_count = broker_module.BrokerData.__init__.__code__.co_argcount
            if param_count > 2:  # More than self and auth_token
                data_handler = broker_module.BrokerData(auth_token, feed_token)
            else:
                data_handler = broker_module.BrokerData(auth_token)
        else:
            # Fallback to just auth token if we can't inspect
            data_handler = broker_module.BrokerData(auth_token)

        # Call the broker's get_history method
        df = data_handler.get_history(symbol, exchange, interval, start_date, end_date)

        if not isinstance(df, pd.DataFrame):
            raise ValueError("Invalid data format returned from broker")

        # Ensure all responses include 'oi' field, set to 0 if not present
        if "oi" not in df.columns:
            df["oi"] = 0

        if df.empty:
            # Distinguish "contract exists but never trades intraday" from
            # "no data at all" so charts show an actionable error instead of
            # a silently blank canvas. Illiquid / far-month MCX contracts
            # (e.g. COTTON, KAPAS, index dexes) carry daily marks at zero
            # volume, and brokers serve no intraday candles for them.
            try:
                probe = data_handler.get_history(symbol, exchange, "D", start_date, end_date)
            except Exception:
                probe = pd.DataFrame()
            if not probe.empty:
                return (
                    False,
                    {
                        "status": "error",
                        "message": (
                            f"No intraday candles for {exchange}:{symbol} between {start_date} and {end_date}. "
                            "The contract has zero trading volume (illiquid or far-month), so the broker serves no intraday data. "
                            "Daily ('D') candles are available - switch the interval to 'D' or chart a liquid near-month contract."
                        ),
                    },
                    404,
                )
            return (
                False,
                {
                    "status": "error",
                    "message": (
                        f"No historical data for {exchange}:{symbol} between {start_date} and {end_date}. "
                        "Verify the symbol via symbol search, or select a traded contract within this date range."
                    ),
                },
                404,
            )

        return True, {"status": "success", "data": df.to_dict(orient="records")}, 200
    except Exception as e:
        logger.exception(f"Error in broker_module.get_history: {e}")
        return False, {"status": "error", "message": str(e)}, 500


def get_history_from_db(
    symbol: str, exchange: str, interval: str, start_date: str, end_date: str
) -> tuple[bool, dict[str, Any], int]:
    """
    Get historical data from DuckDB/Historify database.

    Args:
        symbol: Trading symbol
        exchange: Exchange (e.g., NSE, BSE)
        interval: Time interval (e.g., 1m, 5m, 15m, 1h, D, W, M, Q, Y)
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format

    Returns:
        Tuple containing:
        - Success status (bool)
        - Response data (dict)
        - HTTP status code (int)
    """
    try:
        from datetime import date, datetime

        from database.historify_db import get_ohlcv

        # Convert dates to timestamps (handle both string and date objects)
        if isinstance(start_date, date):
            start_dt = datetime.combine(start_date, datetime.min.time())
        else:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")

        if isinstance(end_date, date):
            end_dt = datetime.combine(end_date, datetime.min.time())
        else:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")

        # Set end_date to end of day
        end_dt = end_dt.replace(hour=23, minute=59, second=59)

        start_timestamp = int(start_dt.timestamp())
        end_timestamp = int(end_dt.timestamp())

        # Get data from DuckDB
        df = get_ohlcv(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )

        if df.empty:
            return (
                False,
                {
                    "status": "error",
                    "message": f"No data found for {symbol}:{exchange} interval {interval} in local database. Download data first using Historify.",
                },
                404,
            )

        # Ensure 'oi' column exists
        if "oi" not in df.columns:
            df["oi"] = 0

        # Reorder columns to match API response format
        columns = ["timestamp", "open", "high", "low", "close", "volume", "oi"]
        df = df[columns]

        return True, {"status": "success", "data": df.to_dict(orient="records")}, 200

    except Exception as e:
        logger.exception(f"Error fetching history from DB: {e}")
        return False, {"status": "error", "message": str(e)}, 500


def get_history(
    symbol: str,
    exchange: str,
    interval: str,
    start_date: str,
    end_date: str,
    api_key: str | None = None,
    auth_token: str | None = None,
    feed_token: str | None = None,
    broker: str | None = None,
    source: str = "api",
) -> tuple[bool, dict[str, Any], int]:
    """
    Get historical data for a symbol.
    Supports both API-based authentication and direct internal calls.

    Args:
        symbol: Trading symbol
        exchange: Exchange (e.g., NSE, BSE)
        interval: Time interval (e.g., 1m, 5m, 15m, 1h, D, W, M, Q, Y)
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        api_key: OpenAlgo API key (for API-based calls)
        auth_token: Direct broker authentication token (for internal calls)
        feed_token: Direct broker feed token (for internal calls)
        broker: Direct broker name (for internal calls)
        source: Data source - 'api' (broker, default) or 'db' (DuckDB/Historify).
            Unsupported values return 400 before a provider is called.

    Returns:
        Tuple containing:
        - Success status (bool)
        - Response data (dict)
        - HTTP status code (int)

        Unsupported source values return a 400 error before either provider is called.
    """
    if not isinstance(source, str) or source not in {"api", "db"}:
        return (
            False,
            {"status": "error", "message": "Source must be either 'api' or 'db'."},
            400,
        )

    # Source: 'db' - Fetch from DuckDB/Historify database
    if source == "db":
        return get_history_from_db(
            symbol=symbol,
            exchange=exchange,
            interval=interval,
            start_date=start_date,
            end_date=end_date,
        )

    # Source: 'api' (default) - Fetch from broker API
    # Enforce 3 requests/second rate limit for broker history calls
    _enforce_rate_limit()

    # Case 1: API-based authentication
    if api_key and not (auth_token and broker):
        AUTH_TOKEN, FEED_TOKEN, broker_name = get_auth_token_broker(
            api_key, include_feed_token=True
        )
        if AUTH_TOKEN is None:
            return False, {"status": "error", "message": "Invalid openalgo apikey"}, 403
        result = get_history_with_auth(
            AUTH_TOKEN, FEED_TOKEN, broker_name, symbol, exchange, interval, start_date, end_date
        )
        # Peer-instance failover: local broker history unavailable -> sibling
        if not result[0] and result[2] >= 500:
            peer = _try_peer_history(
                symbol, exchange, interval, start_date, end_date, broker_name
            )
            if peer is not None:
                return peer
        return result

    # Case 2: Direct internal call with auth_token and broker
    elif auth_token and broker:
        return get_history_with_auth(
            auth_token, feed_token, broker, symbol, exchange, interval, start_date, end_date
        )

    # Case 3: Invalid parameters
    else:
        return (
            False,
            {
                "status": "error",
                "message": "Either api_key or both auth_token and broker must be provided",
            },
            400,
        )
