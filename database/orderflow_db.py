# database/orderflow_db.py
"""
Persistence for the Orderflow Table plugin (ported from fno-trader-pro).

Mirrors database/scalper_db.py: SQLite via the canonical engine factory,
scoped_session registered on import.
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    String,
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


class OrderflowInstrument(Base):
    """One tracked instrument (futures root) with its last computed state."""

    __tablename__ = "orderflow_instruments"

    id = Column(Integer, primary_key=True)
    key = Column(String(20), unique=True, nullable=False, index=True)   # NIFTY, CRUDEOIL...
    name = Column(String(40))
    market = Column(String(8))                                          # NSE / BSE / MCX
    enabled = Column(Integer, default=1)
    sort_order = Column(Integer, default=0)

    # last computation snapshot (refreshed by the engine)
    spot = Column(Float)
    fut_price = Column(Float)
    spot_change_pct = Column(Float)
    oi_change_pct = Column(Float)
    vol_oi_ratio = Column(Float)
    maxpain = Column(Float)
    pcr = Column(Float)
    bullish_oi = Column(Float)
    bearish_oi = Column(Float)
    prev_intr_day = Column(String(20))       # shows staleness of intraday numbers
    intraday_change_oi = Column(Float)
    intraday_bull_vol_oi = Column(Float)
    intraday_bear_vol_oi = Column(Float)
    intraday_direction = Column(String(10))
    intraday_bias = Column(Float)
    sentiment = Column(String(10))
    drift_pct = Column(Float)
    bias = Column(String(10))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_db():
    """Create the orderflow tables if they do not exist (idempotent)."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Orderflow DB initialized")
        return True
    except Exception as e:
        logger.exception(f"Error initializing Orderflow DB: {e}")
        return False


def upsert_instrument_row(key: str, name: str, market: str, sort_order: int = 0) -> None:
    """Ensure the instrument row exists (preserves enabled flag)."""
    try:
        row = db_session.query(OrderflowInstrument).filter_by(key=key).first()
        if not row:
            db_session.add(
                OrderflowInstrument(
                    key=key, name=name, market=market,
                    enabled=1, sort_order=sort_order,
                )
            )
            db_session.commit()
        else:
            if row.name != name or row.market != market:
                row.name, row.market = name, market
                db_session.commit()
    except Exception as e:
        db_session.rollback()
        logger.debug(f"orderflow upsert_instrument_row failed: {e}")


def set_instrument_enabled(key: str, enabled: bool) -> None:
    try:
        row = db_session.query(OrderflowInstrument).filter_by(key=key).first()
        if row:
            row.enabled = 1 if enabled else 0
            db_session.commit()
    except Exception as e:
        db_session.rollback()
        logger.debug(f"orderflow set_instrument_enabled failed: {e}")


def health_check() -> bool:
    try:
        db_session.query(OrderflowInstrument).first()
        return True
    except Exception:
        return False
