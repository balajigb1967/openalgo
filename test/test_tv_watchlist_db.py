"""TradingView watchlist plugin store behaviour.

Config is a single row with a create-on-first-read contract; the token is the
webhook's credential and rotation must actually change it; the log is a
trimmed tail, not an unbounded table.

Every test runs against a temporary database, never db/openalgo.db.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

import database.tv_watchlist_db as tvdb


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Rebind the module's engine and session to a throwaway database."""
    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'tv-watchlist.db'}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )
    monkeypatch.setattr(tvdb, "engine", test_engine)
    monkeypatch.setattr(tvdb, "db_session", test_session)
    tvdb.Base.query = test_session.query_property()
    yield
    test_session.remove()


def test_config_is_created_on_first_read_with_defaults():
    tvdb.init_db()
    config = tvdb.get_config()

    assert config["webhook_token"]
    assert isinstance(config["enabled"], bool)
    assert config["chart_watchlist_id"] is None
    assert config["include_historify"] is True


def test_update_changes_only_the_fields_given():
    tvdb.init_db()
    tvdb.update_config(enabled=False, webhook_secret="s3cret")
    config = tvdb.get_config()

    assert config["enabled"] is False
    assert config["webhook_secret"] == "s3cret"
    assert config["webhook_token"]  # untouched by update_config

    tvdb.update_config(enabled=True, webhook_secret="")
    assert tvdb.get_config()["enabled"] is True
    assert tvdb.get_config()["webhook_secret"] == ""


def test_rotate_token_changes_it_and_the_old_one_stops_authenticating():
    tvdb.init_db()
    old = tvdb.get_config()["webhook_token"]

    new = tvdb.rotate_token()

    assert new and new != old
    assert tvdb.get_config_by_token(old) is None
    assert tvdb.get_config_by_token(new) is not None


def test_log_is_returned_newest_first_and_trimmed():
    tvdb.init_db()
    entries = [
        {
            "source": "webhook",
            "outcome": "added",
            "symbol": f"S{i}",
            "exchange": "NSE",
            "tv_symbol": f"NSE:S{i}",
        }
        for i in range(tvdb.MAX_LOG_ROWS + 20)
    ]
    tvdb.add_log_entries(entries)

    log = tvdb.get_recent_log(limit=tvdb.MAX_LOG_ROWS)

    assert len(log) <= tvdb.MAX_LOG_ROWS
    assert log[0]["symbol"] == entries[-1]["symbol"]  # newest first
    assert all(entry["outcome"] == "added" for entry in log)


def test_get_config_by_token_returns_none_for_an_unknown_token():
    tvdb.init_db()
    assert tvdb.get_config_by_token("no-such-token") is None
