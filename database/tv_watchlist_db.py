# database/tv_watchlist_db.py
"""
Persistence for the TradingView -> Watchlist plugin.

TradingView alerts cannot set custom HTTP headers (the webhook is configured
in TradingView's UI), so the plugin authenticates the same way the strategy
module does: a random token in the URL path. The token is stored here, shown
once per page load on /tradingview, and rotatable by the user.

Config is a single row (id=1) -- there is one deployment, one user, one sync
target. Mirrors database/watchlist_db.py: SQLite via the canonical engine
factory (NullPool), scoped_session registered in utils/db_sessions.py.
"""

import secrets
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker

from database.engine_factory import create_db_engine
from utils.logging import get_logger

logger = get_logger(__name__)

engine = create_db_engine()

db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()

#: How many recent webhook results to keep for the status card. A log, not an
#: audit trail -- the platform log (log/errors.jsonl) carries the detail.
MAX_LOG_ROWS = 100


class TVWatchlistConfig(Base):
    """Single-row configuration for the TradingView watchlist sync."""

    __tablename__ = "tv_watchlist_config"

    id = Column(Integer, primary_key=True)
    #: The username captured from the session when config was last saved. The
    #: webhook has no session, so watchlists are created/updated under this
    #: identity.
    user_id = Column(String(80), nullable=True)
    #: Master switch; when False the webhook answers but changes nothing.
    enabled = Column(Integer, nullable=False, default=1)
    #: Secret URL token, the credential for /tvwatchlist/webhook/<token>.
    webhook_token = Column(String(64), nullable=False, unique=True)
    #: Optional extra check on the alert body: {"secret": "..."} must match
    #: when set. TradingView sends it in the JSON payload.
    webhook_secret = Column(String(128), nullable=True)
    #: Which charting watchlist receives the symbols (watchlists.id).
    chart_watchlist_id = Column(Integer, nullable=True)
    #: Add every synced symbol to the Historify watchlist too.
    include_historify = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TVWatchlistLog(Base):
    """One row per webhook ingestion, newest first, trimmed to MAX_LOG_ROWS."""

    __tablename__ = "tv_watchlist_log"

    id = Column(Integer, primary_key=True)
    #: 'webhook' (alert) or 'import' (paste page)
    source = Column(String(16), nullable=False)
    #: 'added' | 'skipped' (already present / disabled) | 'failed' (unknown symbol)
    outcome = Column(String(16), nullable=False)
    symbol = Column(String(64), nullable=False)
    exchange = Column(String(16), nullable=False)
    #: TradingView's raw "NSE:RELIANCE" form, kept for the status card.
    tv_symbol = Column(String(64), nullable=False)
    #: Short human-readable reason; details go to the platform log.
    message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())


def init_db():
    """Create the plugin tables."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "TradingView Watchlist DB", logger)


def _generate_token() -> str:
    return secrets.token_urlsafe(32)


def get_config() -> dict:
    """The config row as a dict, creating the row on first read."""
    try:
        row = db_session.query(TVWatchlistConfig).filter_by(id=1).first()
        if row is None:
            row = TVWatchlistConfig(id=1, webhook_token=_generate_token())
            db_session.add(row)
            db_session.commit()
        return {
            "user_id": row.user_id or "",
            "enabled": bool(row.enabled),
            "webhook_token": row.webhook_token,
            "webhook_secret": row.webhook_secret or "",
            "chart_watchlist_id": row.chart_watchlist_id,
            "include_historify": bool(row.include_historify),
        }
    except Exception:
        logger.exception("Could not read TradingView watchlist config")
        db_session.rollback()
        return {
            "user_id": "",
            "enabled": False,
            "webhook_token": "",
            "webhook_secret": "",
            "chart_watchlist_id": None,
            "include_historify": True,
        }


def update_config(
    user_id: str | None = None,
    enabled: bool | None = None,
    webhook_secret: str | None = None,
    chart_watchlist_id: int | None = None,
    include_historify: bool | None = None,
) -> dict | None:
    """Update the editable fields. None leaves a field unchanged.

    ``user_id`` is captured from the session on every save from the UI, so the
    webhook can act as that user later. A non-empty stored value is never
    overwritten with an empty one.
    """
    try:
        row = db_session.query(TVWatchlistConfig).filter_by(id=1).first()
        if row is None:
            row = TVWatchlistConfig(id=1, webhook_token=_generate_token())
            db_session.add(row)
            db_session.flush()

        if user_id and (not row.user_id or user_id != row.user_id):
            row.user_id = user_id
        if enabled is not None:
            row.enabled = 1 if enabled else 0
        if webhook_secret is not None:
            row.webhook_secret = (webhook_secret or "").strip() or None
        if chart_watchlist_id is not None:
            row.chart_watchlist_id = chart_watchlist_id
        if include_historify is not None:
            row.include_historify = 1 if include_historify else 0

        db_session.commit()
        return get_config()
    except Exception:
        logger.exception("Could not update TradingView watchlist config")
        db_session.rollback()
        return None


def rotate_token() -> str | None:
    """Replace the webhook token. Old TradingView alert URLs stop working."""
    try:
        row = db_session.query(TVWatchlistConfig).filter_by(id=1).first()
        if row is None:
            row = TVWatchlistConfig(id=1, webhook_token=_generate_token())
            db_session.add(row)
        else:
            row.webhook_token = _generate_token()
        db_session.commit()
        return row.webhook_token
    except Exception:
        logger.exception("Could not rotate the TradingView webhook token")
        db_session.rollback()
        return None


def get_config_by_token(token: str) -> TVWatchlistConfig | None:
    """The config row for a webhook token, or None. Webhook hot path."""
    try:
        return db_session.query(TVWatchlistConfig).filter_by(webhook_token=token).first()
    except Exception:
        logger.exception("Could not look up TradingView webhook token")
        db_session.rollback()
        return None


def add_log_entries(entries: list[dict]) -> None:
    """Record webhook/import outcomes and trim to the newest MAX_LOG_ROWS.

    Never raises: callers run inside request handling and a logging failure
    must not fail an alert that was otherwise processed.
    """
    if not entries:
        return
    try:
        # Keep the newest MAX_LOG_ROWS: a caller can hand in more than the
        # cap (one alert may carry up to 200 symbols), and the tail is the
        # part the status card exists to show.
        for entry in entries[-MAX_LOG_ROWS:]:
            db_session.add(
                TVWatchlistLog(
                    source=entry.get("source", "webhook"),
                    outcome=entry.get("outcome", "failed"),
                    symbol=(entry.get("symbol") or "")[:64],
                    exchange=(entry.get("exchange") or "")[:16],
                    tv_symbol=(entry.get("tv_symbol") or "")[:64],
                    message=entry.get("message"),
                )
            )
        db_session.commit()

        trim_ids = (
            db_session.query(TVWatchlistLog.id)
            .order_by(TVWatchlistLog.id.desc())
            .offset(MAX_LOG_ROWS)
            .limit(500)
            .all()
        )
        if trim_ids:
            db_session.query(TVWatchlistLog).filter(
                TVWatchlistLog.id.in_([row[0] for row in trim_ids])
            ).delete(synchronize_session=False)
            db_session.commit()
    except Exception:
        logger.exception("Could not write TradingView watchlist log")
        db_session.rollback()


def get_recent_log(limit: int = 25) -> list[dict]:
    """The newest ingestion results for the status card."""
    try:
        rows = (
            db_session.query(TVWatchlistLog)
            .order_by(TVWatchlistLog.id.desc())
            .limit(min(max(limit, 1), MAX_LOG_ROWS))
            .all()
        )
        return [
            {
                "id": row.id,
                "source": row.source,
                "outcome": row.outcome,
                "symbol": row.symbol,
                "exchange": row.exchange,
                "tv_symbol": row.tv_symbol,
                "message": row.message,
                "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
            }
            for row in rows
        ]
    except Exception:
        logger.exception("Could not read TradingView watchlist log")
        db_session.rollback()
        return []


def ensure_tv_watchlist_tables_exists():
    """Alias matching the app.py startup pattern."""
    init_db()
