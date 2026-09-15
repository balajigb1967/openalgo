# services/tv_watchlist_service.py
"""
TradingView -> Watchlist sync.

Turns TradingView's symbol format ("NSE:RELIANCE", "BSE:SENSEX",
"MCX:CRUDEOILM", or a bare "RELIANCE") into OpenAlgo's (symbol, exchange)
pairs, validates them against the symbol master, and adds the matches to the
charting watchlist and/or the Historify watchlist.

The two watchlist stores have different rules -- the charting store caps list
sizes and deduplicates, Historify validates against the same symbol master but
reports per-symbol outcomes -- so this module normalizes them into one result
shape the blueprint can serve and the UI can display.

Pure decision-making plus the two store calls; no HTTP.
"""

from database.symbol import SymToken, enhanced_search_symbols
from database.tv_watchlist_db import get_config
from utils.logging import get_logger

logger = get_logger(__name__)

#: TradingView exchange -> OpenAlgo exchange. TradingView's India exchanges
#: carry both equities and derivatives; the symbol master lookup decides which
#: listing exists, preferring the exchange as written.
TV_EXCHANGE_MAP = {
    "NSE": "NSE",
    "BSE": "BSE",
    "NFO": "NFO",
    "BFO": "BFO",
    "MCX": "MCX",
    "NCDEX": "NCDEX",
    "CDS": "CDS",
    "BCD": "BCD",
    "NSE_INDEX": "NSE_INDEX",
    "BSE_INDEX": "BSE_INDEX",
    # TradingView writes NSE indices as NSE:NIFTY / NSE:BANKNIFTY, and its
    # index symbols (NIFTY, SENSEX, FINNIFTY, MIDCPNIFTY, BANKNIFTY) resolve
    # against the index exchanges below when NSE/BSE has no such row.
    "INDEX": "NSE_INDEX",
}

#: Exchanges whose symbols should fall back to the index exchanges when the
#: primary lookup misses. TradingView has no separate index exchange code for
#: India -- NIFTY publishes as NSE:NIFTY -- so the fallback is what makes
#: indices resolvable at all.
INDEX_FALLBACK = {
    "NSE": ["NSE_INDEX"],
    "BSE": ["BSE_INDEX"],
}

#: Fallback when the alert carries no exchange at all. Equities first: a bare
#: "RELIANCE" in an Indian deployment is almost always the NSE listing.
DEFAULT_EXCHANGES = ["NSE", "BSE"]


def parse_tv_symbols(raw: str) -> list[str]:
    """Split an alert payload or pasted block into TradingView symbol tokens.

    Accepts comma, newline, semicolon and whitespace separation. Duplicates
    are dropped preserving first-seen order. Anything that is not a plausible
    symbol (empty, over 64 chars) is dropped.
    """
    if not raw:
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
        for token in chunk.split():
            token = token.strip().upper()
            if not token or len(token) > 64 or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
    return tokens


def _exact_match(token: str, exchange: str) -> tuple[str, str] | None:
    """Exact (symbol, exchange) row from the master, or None.

    The fuzzy search would happily return RELCHEMQ for a RELIANCE alert
    (ILIKE '%RELIANCE%', arbitrary first row). A watchlist sync means the
    symbol the alert named, so exact match always wins where one exists.
    """
    try:
        row = SymToken.query.filter(SymToken.symbol == token, SymToken.exchange == exchange).first()
        return (row.symbol, row.exchange) if row else None
    except Exception:
        logger.exception("Exact symbol lookup failed for %s on %s", token, exchange)
        return None


def resolve_symbol(token: str) -> tuple[str, str] | None:
    """Map one TradingView token to a validated (symbol, exchange) pair.

    "NSE:RELIANCE" pins the exchange; a bare "RELIANCE" tries the default
    exchanges in order. An exchange pinned with no matching row falls back to
    the index exchange (NSE:NIFTY), then to the other default exchange only
    when the token carried no explicit exchange. Returns None when the symbol
    master has no match -- the caller reports it rather than guessing.
    """
    token = (token or "").strip().upper()
    if not token:
        return None

    exchange_hint: str | None = None
    if ":" in token:
        prefix, _, rest = token.partition(":")
        exchange_hint = TV_EXCHANGE_MAP.get(prefix.strip())
        token = rest.strip()
    elif "." in token and not token.split(".", 1)[1].isdigit():
        # "NSE.RELIANCE" -- some alert templates use a dot separator. A digit
        # suffix is a genuine symbol (e.g. "M&M.NS" style tickers), not an
        # exchange prefix, so only treat non-numeric suffixes as exchanges.
        prefix, _, rest = token.partition(".")
        exchange_hint = TV_EXCHANGE_MAP.get(prefix.strip())
        token = rest.strip()

    if not token:
        return None

    candidates: list[str] = []
    if exchange_hint:
        candidates.append(exchange_hint)
        candidates.extend(INDEX_FALLBACK.get(exchange_hint, []))
    else:
        candidates.extend(DEFAULT_EXCHANGES)
        candidates.extend(INDEX_FALLBACK.get("NSE", []))

    for exchange in candidates:
        exact = _exact_match(token, exchange)
        if exact:
            return exact
    # No exact row anywhere: fall back to the fuzzy search (which also
    # matches broker symbols, names and tokens) before giving up.
    for exchange in candidates:
        matches = enhanced_search_symbols(token, exchange, limit=1)
        if matches:
            return matches[0].symbol, matches[0].exchange
    return None


def resolve_symbols(tokens: list[str]) -> tuple[list[dict], list[dict]]:
    """Resolve many tokens. Returns (resolved, unresolved).

    resolved:  [{"symbol", "exchange", "tv_symbol"}]
    unresolved:[{"tv_symbol", "error"}]
    """
    resolved: list[dict] = []
    unresolved: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for token in tokens:
        pair = resolve_symbol(token)
        if pair is None:
            unresolved.append({"tv_symbol": token, "error": "Not found in the symbol master"})
            continue
        if pair in seen:
            unresolved.append({"tv_symbol": token, "error": f"Duplicate of {pair[0]} ({pair[1]})"})
            continue
        seen.add(pair)
        resolved.append({"symbol": pair[0], "exchange": pair[1], "tv_symbol": token})

    return resolved, unresolved


def add_to_chart_watchlist(user_id: str, watchlist_id: int, resolved: list[dict]) -> list[dict]:
    """Add resolved symbols to one charting watchlist. Per-symbol outcomes."""
    from database.watchlist_db import add_item

    outcomes: list[dict] = []
    for item in resolved:
        # add_item returns the existing row for a duplicate, which is the
        # outcome the user wants ("it is in the list") rather than an error.
        added = add_item(user_id, watchlist_id, item["symbol"], item["exchange"])
        if added is None:
            outcomes.append(
                {
                    **item,
                    "outcome": "failed",
                    "message": "Watchlist is full or was not found",
                }
            )
        else:
            outcomes.append({**item, "outcome": "added", "message": None})
    return outcomes


def add_to_historify(resolved: list[dict]) -> list[dict]:
    """Add resolved symbols to the Historify watchlist. Per-symbol outcomes."""
    from services.historify_service import bulk_add_to_watchlist

    if not resolved:
        return []
    try:
        _ok, data, _status = bulk_add_to_watchlist(
            [{"symbol": item["symbol"], "exchange": item["exchange"]} for item in resolved]
        )
    except Exception:
        logger.exception("Historify bulk add failed")
        return [
            {**item, "outcome": "failed", "message": "Historify add failed"} for item in resolved
        ]

    failed_by_symbol = {
        entry.get("symbol"): entry.get("error", "Historify rejected the symbol")
        for entry in (data.get("failed") or [])
        if isinstance(entry, dict)
    }
    added_count = int(data.get("added", 0) or 0)
    skipped_count = int(data.get("skipped", 0) or 0)

    # bulk_add reports counts, not per-row attribution, so outcomes are
    # attributed in order: accepted rows first, then the reported failures.
    outcomes: list[dict] = []
    for index, item in enumerate(resolved):
        if item["symbol"] in failed_by_symbol:
            outcomes.append(
                {**item, "outcome": "failed", "message": failed_by_symbol[item["symbol"]]}
            )
        elif index < added_count + skipped_count:
            outcomes.append({**item, "outcome": "added", "message": None})
        else:
            outcomes.append({**item, "outcome": "added", "message": None})
    return outcomes


def sync_symbols(
    raw_text: str,
    source: str = "webhook",
    user_id: str | None = None,
    chart_watchlist_id: int | None = None,
    include_historify: bool | None = None,
) -> dict:
    """Parse, resolve and add. One entry point for webhook and paste-import.

    Config (from database/tv_watchlist_db.py) decides the targets when the
    caller does not override them -- the webhook never overrides; the paste
    page may, so a user can import into a fresh list without reconfiguring
    the webhook.
    """
    config = get_config()
    effective_user = user_id or config.get("user_id") or ""
    effective_watchlist = (
        chart_watchlist_id if chart_watchlist_id is not None else config.get("chart_watchlist_id")
    )
    effective_historify = (
        include_historify
        if include_historify is not None
        else bool(config.get("include_historify"))
    )

    tokens = parse_tv_symbols(raw_text)
    if not tokens:
        return {
            "status": "error",
            "message": "No symbols found in the request",
            "resolved": [],
            "unresolved": [],
            "results": [],
            "added": 0,
            "failed": 0,
        }

    resolved, unresolved = resolve_symbols(tokens)

    results: list[dict] = []
    if resolved:
        if effective_watchlist is not None and effective_user:
            results.extend(add_to_chart_watchlist(effective_user, effective_watchlist, resolved))
        elif effective_watchlist is None:
            for item in resolved:
                results.append(
                    {**item, "outcome": "skipped", "message": "No charting watchlist selected"}
                )

        if effective_historify:
            results.extend(add_to_historify(resolved))

    from database.tv_watchlist_db import add_log_entries

    log_rows = [
        {
            "source": source,
            "outcome": item["outcome"],
            "symbol": item["symbol"],
            "exchange": item["exchange"],
            "tv_symbol": item["tv_symbol"],
            "message": item.get("message"),
        }
        for item in results
    ]
    log_rows.extend(
        {
            "source": source,
            "outcome": "failed",
            "symbol": "",
            "exchange": "",
            "tv_symbol": item["tv_symbol"],
            "message": item["error"],
        }
        for item in unresolved
    )
    add_log_entries(log_rows)

    added = sum(1 for item in results if item["outcome"] == "added")
    failed = sum(1 for item in results if item["outcome"] == "failed") + len(unresolved)

    return {
        "status": "success",
        "message": f"{added} added, {failed} not added",
        "resolved": resolved,
        "unresolved": unresolved,
        "results": results,
        "added": added,
        "failed": failed,
    }
