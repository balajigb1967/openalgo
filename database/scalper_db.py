# database/scalper_db.py
"""
Persistence for the Scalper Advisor plugin (ported from fno-trader-pro).

The advisor generates option-buying advisories from the live chain; every
generated signal is recorded here as an intraday alert with its entry levels
frozen at creation, so refreshes and reconnects keep the day's history.

Mirrors database/tv_watchlist_db.py: SQLite via the canonical engine factory
(NullPool), scoped_session registered on import.
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
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


class ScalperAlert(Base):
    """One generated option-buying signal, with its live-monitor state."""

    __tablename__ = "scalper_alerts"

    id = Column(Integer, primary_key=True)
    alert_id = Column(String(20), unique=True, nullable=False, index=True)
    key = Column(String(20), nullable=False, index=True)          # instrument key (NIFTY, CRUDEOIL...)
    name = Column(String(40))
    market = Column(String(8))                                     # NSE / BSE / MCX
    side = Column(String(4))                                       # CE / PE
    strike = Column(Float)
    option_symbol = Column(String(60), index=True)
    opp_strike = Column(Float)
    opp_symbol = Column(String(60))
    opp_premium = Column(Float)
    entry_premium = Column(Float)
    current_premium = Column(Float)
    spot_at_entry = Column(Float)
    spot_now = Column(Float)
    target_premium = Column(Float)
    sl_premium = Column(Float)
    target_spot = Column(Float)
    sl_spot = Column(Float)
    confidence = Column(Integer)
    rr = Column(Float)
    pnl_pct = Column(Float)
    basis = Column(Text)                                           # JSON list of basis lines
    note = Column(Text)
    status = Column(String(10), default="ACTIVE", index=True)      # ACTIVE / CLOSED
    created_ts = Column(Float, index=True)                         # epoch, for sorting
    created_at = Column(String(20))                                # "17 Sep 10:05:00" IST wall clock
    closed_at = Column(String(20))
    close_reason = Column(Text)
    close_pnl_pct = Column(Float)
    close_outcome = Column(String(6))                              # WIN / LOSS
    armed = Column(Integer, default=0)                             # monitor armed flag
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_db():
    """Create the scalper tables if they do not exist (idempotent)."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Scalper Advisor DB initialized")
        return True
    except Exception as e:
        logger.exception(f"Error initializing Scalper Advisor DB: {e}")
        return False


def add_alert_row(data: dict) -> None:
    """Insert or update one alert row from the service's alert dict."""
    try:
        row = db_session.query(ScalperAlert).filter_by(alert_id=data.get("alert_id")).first()
        if not row:
            row = ScalperAlert(alert_id=data.get("alert_id"))
            db_session.add(row)
        for field in (
            "key", "name", "market", "side", "strike", "option_symbol",
            "opp_strike", "opp_symbol", "opp_premium", "entry_premium",
            "current_premium", "spot_at_entry", "spot_now", "target_premium",
            "sl_premium", "target_spot", "sl_spot", "confidence", "rr",
            "pnl_pct", "note", "status", "created_ts", "created_at",
            "closed_at", "close_reason", "close_pnl_pct", "close_outcome",
        ):
            if data.get(field) is not None:
                setattr(row, field, data[field])
        if data.get("basis") is not None:
            import json
            row.basis = json.dumps(data["basis"], ensure_ascii=False)
        row.armed = 1 if data.get("armed") else 0
        db_session.commit()
    except Exception as e:
        db_session.rollback()
        logger.debug(f"scalper add_alert_row failed: {e}")


def load_today_alerts(day_prefix: str) -> list[dict]:
    """Load this day's alerts as plain dicts (alert_id starts with YYYYMMDD)."""
    try:
        rows = (
            db_session.query(ScalperAlert)
            .filter(ScalperAlert.alert_id.like(f"{day_prefix}%"))
            .order_by(ScalperAlert.created_ts.desc())
            .limit(120)
            .all()
        )
        import json
        out = []
        for r in rows:
            d = {
                c.name: getattr(r, c.name)
                for c in r.__table__.columns
                if c.name not in ("id", "basis", "updated_at")
            }
            try:
                d["basis"] = json.loads(r.basis) if r.basis else []
            except Exception:
                d["basis"] = []
            d["armed"] = bool(r.armed)
            out.append(d)
        return out
    except Exception as e:
        logger.debug(f"scalper load_today_alerts failed: {e}")
        return []


def health_check() -> bool:
    """Lightweight liveness probe for the diagnostics page."""
    try:
        db_session.query(func.count(ScalperAlert.id)).scalar()
        return True
    except Exception:
        return False
