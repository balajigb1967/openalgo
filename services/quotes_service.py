import importlib
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx

from database.auth_db import get_auth_token_broker
from database.token_db import get_token
from utils.constants import VALID_EXCHANGES
from utils.logging import get_logger

# Initialize logger
logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Peer-instance failover
#
# When the local broker's data plane is unavailable (dead/expired token, API
# outage), transparently retry quotes against a sibling OpenAlgo instance on
# the same host carrying a different broker (e.g. the Fyers instance failing
# over to the Flattrade instance). Opt-in via PEER_OPENALGO_URL and
# PEER_OPENALGO_API_KEY. A thread-local guard stops failover chains from
# recursing (A -> B -> A -> ...), and a cooldown keeps every quote poll from
# paying the peer latency penalty while the peer is known-down.
# ---------------------------------------------------------------------------

def _peer_config() -> tuple[str, str, float, float]:
    """Read peer settings lazily so .env load order never matters."""
    return (
        os.getenv("PEER_OPENALGO_URL", "").strip(),
        os.getenv("PEER_OPENALGO_API_KEY", "").strip(),
        float(os.getenv("PEER_OPENALGO_TIMEOUT", "6")),
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


def _peer_post(path: str, payload: dict) -> dict | None:
    """POST to the peer instance; None on any failure (cooldown armed)."""
    if getattr(_peer_inflight, "active", False):
        return None  # already inside a peer call on this thread - never recurse
    url_base, key, timeout, _ = _peer_config()
    if not (url_base and key):
        return None
    _peer_inflight.active = True
    try:
        url = url_base.rstrip("/") + path
        try:
            resp = httpx.post(url, json={**payload, "apikey": key}, timeout=timeout)
            if resp.status_code == 429:
                # Peer alive but rate-limited - retry again soon.
                _mark_peer_down(30.0)
                return None
            if resp.status_code == 200:
                return resp.json()
            logger.debug(f"Peer {url} -> HTTP {resp.status_code}")
            if resp.status_code >= 500 or resp.status_code in (401, 403):
                # Peer down or misconfigured - back off for the whole cooldown.
                _mark_peer_down()
            # 4xx (e.g. symbol absent on the peer's master contracts) is
            # request-specific: return None without arming the cooldown.
            return None
        except Exception as exc:
            logger.debug(f"Peer {url} failed: {exc}")
            _mark_peer_down()
            return None
    finally:
        _peer_inflight.active = False


def _response_has_data(response: Any) -> bool:
    """True when a quotes/multiquotes response carries at least one usable LTP."""
    if not isinstance(response, dict) or response.get("status") != "success":
        return False
    data = response.get("data")
    if isinstance(data, dict):
        return bool(data.get("ltp"))
    results = response.get("results")
    if isinstance(results, list):
        return any(
            isinstance(r, dict) and isinstance(r.get("data"), dict) and r["data"].get("ltp")
            for r in results
        )
    return False


def _try_peer_quotes(symbol: str, exchange: str, local_broker: str) -> dict | None:
    """Fetch a single quote from the peer instance; None if unavailable."""
    if not _peer_available():
        return None
    peer = _peer_post(
        "/api/v1/quotes",
        {"symbol": symbol, "exchange": exchange},
    )
    if peer is not None and _response_has_data(peer):
        logger.info(
            f"Quote {exchange}:{symbol} served by peer instance (local broker '{local_broker}' unusable)"
        )
        return peer
    return None


def _try_peer_multiquotes(symbols: list, local_broker: str) -> dict | None:
    """Fetch multiquotes from the peer instance; None if unavailable."""
    if not _peer_available():
        return None
    peer = _peer_post("/api/v1/multiquotes", {"symbols": symbols})
    if peer is not None and _response_has_data(peer):
        logger.info(
            f"Multiquotes ({len(symbols)} symbols) served by peer instance (local broker '{local_broker}' unusable)"
        )
        return peer
    return None


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


def validate_symbols_bulk(
    symbols: list[dict[str, str]],
) -> tuple[bool, list[dict[str, Any]], str | None]:
    """
    Validate multiple symbols and their exchanges.

    Args:
        symbols: List of dicts with 'symbol' and 'exchange' keys

    Returns:
        Tuple of (all_valid, validated_symbols_with_errors, first_error_message)
    """
    all_valid = True
    validated = []
    first_error = None

    for item in symbols:
        symbol = item.get("symbol", "")
        exchange = item.get("exchange", "")

        if not symbol or not exchange:
            error = "Missing symbol or exchange in request"
            validated.append({**item, "valid": False, "error": error})
            if all_valid:
                first_error = error
                all_valid = False
            continue

        is_valid, error = validate_symbol_exchange(symbol, exchange)
        validated.append({**item, "valid": is_valid, "error": error})

        if not is_valid and all_valid:
            first_error = error
            all_valid = False

    return all_valid, validated, first_error


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


def get_quotes_with_auth(
    auth_token: str, feed_token: str | None, broker: str, symbol: str, exchange: str
) -> tuple[bool, dict[str, Any], int]:
    """
    Get real-time quotes for a symbol using provided auth tokens.

    Args:
        auth_token: Authentication token for the broker API
        feed_token: Feed token for market data (if required by broker)
        broker: Name of the broker
        symbol: Trading symbol
        exchange: Exchange (e.g., NSE, BSE)

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

        quotes = data_handler.get_quotes(symbol, exchange)

        if quotes is None:
            return False, {"status": "error", "message": "Failed to fetch quotes"}, 500

        return True, {"status": "success", "data": quotes}, 200
    except Exception as e:
        # Check if this is a permission error
        error_msg = str(e)
        if "permission" in error_msg.lower() or "insufficient" in error_msg.lower():
            # Log at debug level for permission errors (common with personal APIs)
            logger.debug(f"Quote fetch permission denied: {error_msg}")
        else:
            # Log other errors normally
            logger.exception(f"Error in broker_module.get_quotes: {e}")

        return False, {"status": "error", "message": str(e)}, 500


def get_quotes(
    symbol: str,
    exchange: str,
    api_key: str | None = None,
    auth_token: str | None = None,
    feed_token: str | None = None,
    broker: str | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """
    Get real-time quotes for a symbol.
    Supports both API-based authentication and direct internal calls.

    Args:
        symbol: Trading symbol
        exchange: Exchange (e.g., NSE, BSE)
        api_key: OpenAlgo API key (for API-based calls)
        auth_token: Direct broker authentication token (for internal calls)
        feed_token: Direct broker feed token (for internal calls)
        broker: Direct broker name (for internal calls)

    Returns:
        Tuple containing:
        - Success status (bool)
        - Response data (dict)
        - HTTP status code (int)
    """
    # Case 1: API-based authentication
    if api_key and not (auth_token and broker):
        AUTH_TOKEN, FEED_TOKEN, broker_name = get_auth_token_broker(
            api_key, include_feed_token=True
        )
        if AUTH_TOKEN is None:
            return False, {"status": "error", "message": "Invalid openalgo apikey"}, 403
        result = get_quotes_with_auth(AUTH_TOKEN, FEED_TOKEN, broker_name, symbol, exchange)
        # Peer-instance failover: local broker data plane unusable -> sibling
        if not _response_has_data(result[1]):
            peer = _try_peer_quotes(symbol, exchange, broker_name)
            if peer is not None:
                return True, peer, 200
        return result

    # Case 2: Direct internal call with auth_token and broker
    elif auth_token and broker:
        return get_quotes_with_auth(auth_token, feed_token, broker, symbol, exchange)

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


def get_multiquotes_with_auth(
    auth_token: str, feed_token: str | None, broker: str, symbols: list
) -> tuple[bool, dict[str, Any], int]:
    """
    Get real-time quotes for multiple symbols using provided auth tokens.

    Args:
        auth_token: Authentication token for the broker API
        feed_token: Feed token for market data (if required by broker)
        broker: Name of the broker
        symbols: List of dicts with 'symbol' and 'exchange' keys

    Returns:
        Tuple containing:
        - Success status (bool)
        - Response data (dict)
        - HTTP status code (int)
    """
    # Validate all symbols before making broker API calls
    all_valid, validated_symbols, first_error = validate_symbols_bulk(symbols)

    # Separate valid and invalid symbols
    valid_symbols = [item for item in validated_symbols if item.get("valid", False)]
    invalid_symbols = [item for item in validated_symbols if not item.get("valid", False)]

    # If no valid symbols, return error
    if not valid_symbols:
        return (
            False,
            {
                "status": "error",
                "message": first_error or "No valid symbols provided",
                "invalid_symbols": [
                    {
                        "symbol": s.get("symbol"),
                        "exchange": s.get("exchange"),
                        "error": s.get("error"),
                    }
                    for s in invalid_symbols
                ],
            },
            400,
        )

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

        # Build results list starting with invalid symbols (marked as errors)
        results = []
        for item in invalid_symbols:
            results.append(
                {
                    "symbol": item.get("symbol"),
                    "exchange": item.get("exchange"),
                    "error": item.get("error"),
                }
            )

        # Check if broker supports multiquotes
        if not hasattr(data_handler, "get_multiquotes"):
            # Fallback: fetch quotes one by one for valid symbols only
            logger.debug(
                f"Broker {broker} doesn't support multiquotes, falling back to individual quotes"
            )
            for item in valid_symbols:
                try:
                    quote = data_handler.get_quotes(item["symbol"], item["exchange"])
                    results.append(
                        {"symbol": item["symbol"], "exchange": item["exchange"], "data": quote}
                    )
                except Exception as e:
                    logger.exception(
                        f"Error fetching quote for {item['exchange']}:{item['symbol']}: {e}"
                    )
                    results.append(
                        {"symbol": item["symbol"], "exchange": item["exchange"], "error": str(e)}
                    )

            return True, {"status": "success", "results": results}, 200

        # Use broker's native multiquotes method with only valid symbols
        # Strip validation metadata before passing to broker
        clean_symbols = [{"symbol": s["symbol"], "exchange": s["exchange"]} for s in valid_symbols]
        multiquotes = data_handler.get_multiquotes(clean_symbols)

        if multiquotes is None:
            return False, {"status": "error", "message": "Failed to fetch multiquotes"}, 500

        # Combine broker results with invalid symbol errors
        combined_results = results + (multiquotes if isinstance(multiquotes, list) else [])

        return True, {"status": "success", "results": combined_results}, 200
    except Exception as e:
        # Check if this is a permission error
        error_msg = str(e)
        if "permission" in error_msg.lower() or "insufficient" in error_msg.lower():
            # Log at debug level for permission errors (common with personal APIs)
            logger.debug(f"Multiquote fetch permission denied: {error_msg}")
        else:
            # Log other errors normally
            logger.exception(f"Error in broker_module.get_multiquotes: {e}")

        return False, {"status": "error", "message": str(e)}, 500


def get_multiquotes(
    symbols: list,
    api_key: str | None = None,
    auth_token: str | None = None,
    feed_token: str | None = None,
    broker: str | None = None,
) -> tuple[bool, dict[str, Any], int]:
    """
    Get real-time quotes for multiple symbols.
    Supports both API-based authentication and direct internal calls.

    Args:
        symbols: List of dicts with 'symbol' and 'exchange' keys
        api_key: OpenAlgo API key (for API-based calls)
        auth_token: Direct broker authentication token (for internal calls)
        feed_token: Direct broker feed token (for internal calls)
        broker: Direct broker name (for internal calls)

    Returns:
        Tuple containing:
        - Success status (bool)
        - Response data (dict)
        - HTTP status code (int)
    """
    # Case 1: API-based authentication
    if api_key and not (auth_token and broker):
        AUTH_TOKEN, FEED_TOKEN, broker_name = get_auth_token_broker(
            api_key, include_feed_token=True
        )
        if AUTH_TOKEN is None:
            return False, {"status": "error", "message": "Invalid openalgo apikey"}, 403
        result = get_multiquotes_with_auth(AUTH_TOKEN, FEED_TOKEN, broker_name, symbols)
        # Peer-instance failover: every result entry missing usable data -> sibling
        if not _response_has_data(result[1]):
            peer = _try_peer_multiquotes(symbols, broker_name)
            if peer is not None:
                return True, peer, 200
        return result

    # Case 2: Direct internal call with auth_token and broker
    elif auth_token and broker:
        return get_multiquotes_with_auth(auth_token, feed_token, broker, symbols)

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
