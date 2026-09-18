#!/usr/bin/env python3
"""Insert bare-root alias rows for MCX futures into symtoken.

The broker's master contract carries only dated futures (CRUDEOIL21SEP26FUT).
Watchlists, the scalper advisor and orderflow all reference the bare root
(CRUDEOIL). This adds a symtoken row per root pointing at the NEAR-month
future (smallest expiry >= today), so /quotes, /multiquotes and /history
resolve the bare root directly against the live contract.

Safe to re-run: existing alias rows are updated in place, and a root is
re-pointed to a new near month as contracts roll.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytz  # noqa: E402
from sqlalchemy import text  # noqa: E402

from database.engine_factory import create_db_engine  # noqa: E402
from utils.logging import get_logger  # noqa: E402

logger = get_logger(__name__)

# Roots the terminals watch. Extend freely — the matching is a LIKE on the
# dated symbol (<ROOT><DDMMMYY>FUT), so anything with dated contracts works.
ROOTS = [
    "CRUDEOIL", "CRUDEOILM", "GOLD", "GOLDM", "GOLDPETAL",
    "SILVER", "SILVERM", "SILVERMIC",
    "NATURALGAS", "NATURALGASM",
    "COPPER", "ZINC", "LEAD", "NICKEL", "ALUMINIUM", "ALUMINI",
    "COTTONCNDY", "MENTHAOIL", "COTTON",
]


def _exp_to_date(exp_str: str):
    if not exp_str:
        return None
    for fmt in ("%d-%b-%y", "%d-%b-%Y", "%d-%B-%y", "%d-%B-%Y"):
        try:
            return datetime.strptime(exp_str, fmt).date()
        except ValueError:
            continue
    return None


def run(engine) -> int:
    today = datetime.now(pytz.timezone("Asia/Kolkata")).date()
    written = 0
    with engine.begin() as conn:
        for root in ROOTS:
            rows = conn.execute(
                text(
                    "SELECT symbol, brsymbol, name, exchange, brexchange, token, "
                    "expiry, strike, lotsize, instrumenttype, tick_size, contract_value "
                    "FROM symtoken WHERE exchange='MCX' AND instrumenttype='FUT' "
                    "AND symbol LIKE :pat ORDER BY symbol"
                ),
                {"pat": root + "%FUT"},
            ).fetchall()
            # Keep only contracts whose parsed expiry is today or later, then
            # take the smallest — that is the near month.
            dated = []
            for r in rows:
                exp = _exp_to_date(r[6])
                if exp and exp >= today:
                    dated.append((exp, r))
            if not dated:
                continue
            dated.sort(key=lambda x: (x[0], x[1][0]))
            exp, near = dated[0]
            exists = conn.execute(
                text(
                    "SELECT id FROM symtoken WHERE exchange='MCX' AND symbol=:s"
                ),
                {"s": root},
            ).fetchone()
            if exists:
                conn.execute(
                    text(
                        "UPDATE symtoken SET brsymbol=:br, name=:nm, token=:tk, "
                        "expiry=:ex, strike=:st, lotsize=:ls, instrumenttype='FUT', "
                        "tick_size=:ts, contract_value=:cv WHERE exchange='MCX' AND symbol=:s"
                    ),
                    {
                        "br": near[1], "nm": root, "tk": near[5], "ex": near[6],
                        "st": near[7], "ls": near[8], "ts": near[10],
                        "cv": near[11], "s": root,
                    },
                )
            else:
                conn.execute(
                    text(
                        "INSERT INTO symtoken (symbol, brsymbol, name, exchange, "
                        "brexchange, token, expiry, strike, lotsize, instrumenttype, "
                        "tick_size, contract_value) VALUES "
                        "(:s, :br, :nm, 'MCX', 'MCX', :tk, :ex, :st, :ls, 'FUT', :ts, :cv)"
                    ),
                    {
                        "s": root, "br": near[1], "nm": root, "tk": near[5],
                        "ex": near[6], "st": near[7], "ls": near[8],
                        "ts": near[10], "cv": near[11],
                    },
                )
            written += 1
    return written


def main() -> None:
    from app import app  # noqa: F401 — app context for SQLAlchemy models
    with app.app_context():
        engine = create_db_engine()
        n = run(engine)
        logger.info(f"MCX root aliases ensured for {n} roots")


if __name__ == "__main__":
    main()
