"""TradingView -> Watchlist symbol resolution.

The plugin's real logic lives in parse/resolve: everything after a resolved
(symbol, exchange) pair is a store call already covered by the store's own
tests. The exchange mapping and the index fallback are what keep "NSE:NIFTY"
from landing in the unresolved bucket, so those are pinned here with the
symbol-master lookup stubbed out.
"""

from contextlib import ExitStack
from unittest.mock import patch

from services import tv_watchlist_service as svc


class FakeRow:
    def __init__(self, symbol, exchange):
        self.symbol = symbol
        self.exchange = exchange


def _stub_lookup(rows_by_exchange):
    """Patch the master lookups with an exchange-keyed fake.

    The fake exact match only answers full-symbol equality; the fake fuzzy
    search answers substring hits like the real one (RELIANCE -> RELCHEMQ),
    which is the defect the exact-match-first rule exists to prevent.
    """

    def fake_exact(token, exchange):
        rows = rows_by_exchange.get(exchange, [])
        for symbol, exch in rows:
            if symbol == token:
                return (symbol, exch)
        return None

    def fake_search(token, exchange, limit=None):
        rows = rows_by_exchange.get(exchange, [])
        hits = [FakeRow(r[0], r[1]) for r in rows if token in r[0]]
        return hits[: limit or len(hits)]

    def _apply(stack: ExitStack):
        stack.enter_context(patch.object(svc, "_exact_match", side_effect=fake_exact))
        stack.enter_context(patch.object(svc, "enhanced_search_symbols", side_effect=fake_search))

    return _apply


def test_parse_splits_on_commas_newlines_and_whitespace():
    assert svc.parse_tv_symbols("NSE:RELIANCE, BSE:SENSEX\nMCX:CRUDEOIL TCS") == [
        "NSE:RELIANCE",
        "BSE:SENSEX",
        "MCX:CRUDEOIL",
        "TCS",
    ]


def test_parse_drops_duplicates_and_normalises_case():
    assert svc.parse_tv_symbols("NSE:RELIANCE, nse:reliance, , ,, abc:def") == [
        "NSE:RELIANCE",
        "ABC:DEF",
    ]


def test_parse_drops_tokens_longer_than_a_symbol():
    assert svc.parse_tv_symbols("NSE:RELIANCE " + "A" * 80) == ["NSE:RELIANCE"]


def test_resolve_prefers_the_pinned_exchange():
    with ExitStack() as stack:
        _stub_lookup(
            {
                "NSE": [("RELIANCE", "NSE")],
                "BSE": [("RELIANCE", "BSE")],
            }
        )(stack)
        assert svc.resolve_symbol("BSE:RELIANCE") == ("RELIANCE", "BSE")


def test_bare_symbol_falls_back_from_nse_to_bse():
    with ExitStack() as stack:
        _stub_lookup(
            {
                "BSE": [("SENSEX", "BSE")],
            }
        )(stack)
        assert svc.resolve_symbol("SENSEX") == ("SENSEX", "BSE")


def test_nse_index_falls_back_to_nse_index_exchange():
    with ExitStack() as stack:
        _stub_lookup(
            {
                "NSE_INDEX": [("NIFTY", "NSE_INDEX")],
            }
        )(stack)
        assert svc.resolve_symbol("NSE:NIFTY") == ("NIFTY", "NSE_INDEX")


def test_bse_index_falls_back_to_bse_index_exchange():
    with ExitStack() as stack:
        _stub_lookup(
            {
                "BSE_INDEX": [("SENSEX", "BSE_INDEX")],
            }
        )(stack)
        assert svc.resolve_symbol("BSE:SENSEX") == ("SENSEX", "BSE_INDEX")


def test_unknown_symbol_returns_none():
    with ExitStack() as stack:
        _stub_lookup({})(stack)
        assert svc.resolve_symbol("NSE:NOPE") is None


def test_exact_match_beats_a_fuzzy_partial_match():
    # The master holds RELCHEMQ, whose name fuzzily contains RELIANCE. An
    # NSE:RELIANCE alert must resolve to RELIANCE, never to a substring hit.
    with ExitStack() as stack:
        _stub_lookup({"NSE": [("RELCHEMQ", "NSE")]})(stack)
        assert svc.resolve_symbol("NSE:RELIANCE") is None

    with ExitStack() as stack:
        _stub_lookup({"NSE": [("RELCHEMQ", "NSE"), ("RELIANCE", "NSE")]})(stack)
        assert svc.resolve_symbol("NSE:RELIANCE") == ("RELIANCE", "NSE")


def test_unknown_exchange_prefix_is_not_searched_as_a_symbol():
    with ExitStack() as stack:
        _stub_lookup({})(stack)
        assert svc.resolve_symbol("ZZZ:RELIANCE") is None


def test_resolve_symbols_deduplicates_by_target_pair():
    with ExitStack() as stack:
        _stub_lookup({"NSE": [("RELIANCE", "NSE")]})(stack)
        resolved, unresolved = svc.resolve_symbols(["NSE:RELIANCE", "RELIANCE"])

    assert [r["symbol"] for r in resolved] == ["RELIANCE"]
    assert len(unresolved) == 1
    assert unresolved[0]["tv_symbol"] == "RELIANCE"


def test_resolve_symbols_reports_unresolved_without_guessing():
    with ExitStack() as stack:
        _stub_lookup({"NSE": [("TCS", "NSE")]})(stack)
        resolved, unresolved = svc.resolve_symbols(["NSE:TCS", "NSE:MISSING"])

    assert [r["symbol"] for r in resolved] == ["TCS"]
    assert unresolved[0]["tv_symbol"] == "NSE:MISSING"


def test_tradingview_futures_format_resolves_via_nfo():
    with ExitStack() as stack:
        _stub_lookup({"NFO": [("NIFTY28MAR24FUT", "NFO")]})(stack)
        assert svc.resolve_symbol("NFO:NIFTY28MAR24FUT") == ("NIFTY28MAR24FUT", "NFO")
