#!/usr/bin/env python3
"""
Migration: TradingView Watchlist Plugin

Adds the tables backing the TradingView -> Watchlist plugin:
- tv_watchlist_config: single-row sync settings and the webhook URL token
- tv_watchlist_log:    recent webhook/import outcomes for the status card

Both are new tables, so there is nothing to backfill and no existing value to
preserve. init_db() in database/tv_watchlist_db.py creates them too, but that
only helps an installation whose database is opened after this ships; this
script is what reaches the deployments that upgrade with `git pull`.

This migration is idempotent - safe to run multiple times.

Usage:
    cd upgrade
    uv run migrate_tv_watchlist.py           # Apply migration
    uv run migrate_tv_watchlist.py --status  # Check status without changing anything
"""

import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Add parent directory to path for imports
sys.path.insert(0, PROJECT_ROOT)
# Register the app's SQLite pragmas on this process's engines, so a migration
# waits the same 15s for a write lock the running app does instead of the
# sqlite3 default of 5s (GitHub issue #1726).
import _pragmas  # noqa: F401,E402
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

CONFIG_TABLE = "tv_watchlist_config"
LOG_TABLE = "tv_watchlist_log"

INDEXES = {
    "ix_tv_watchlist_log_created_at": (LOG_TABLE, "created_at"),
}


def resolve_sqlite_path(db_url):
    """Make a relative sqlite:/// path absolute against the project root."""
    if (
        db_url
        and db_url.startswith("sqlite:///")
        and not os.path.isabs(db_url.replace("sqlite:///", "", 1))
    ):
        db_path = db_url.replace("sqlite:///", "", 1)
        return f"sqlite:///{os.path.join(PROJECT_ROOT, db_path)}"
    return db_url


def get_database_url():
    """Read the main database URL the same way the app resolves it."""
    return resolve_sqlite_path(os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db"))


def table_exists(inspector, name):
    return name in inspector.get_table_names()


def migrate(db_url):
    engine = create_engine(db_url, poolclass=NullPool)
    try:
        with engine.begin() as conn:
            inspector = inspect(conn)

            if not table_exists(inspector, CONFIG_TABLE):
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE {CONFIG_TABLE} (
                            id INTEGER NOT NULL,
                            user_id VARCHAR(80),
                            enabled INTEGER NOT NULL DEFAULT 1,
                            webhook_token VARCHAR(64) NOT NULL,
                            webhook_secret VARCHAR(128),
                            chart_watchlist_id INTEGER,
                            include_historify INTEGER NOT NULL DEFAULT 1,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME,
                            PRIMARY KEY (id),
                            UNIQUE (webhook_token)
                        )
                        """
                    )
                )
                print(f"  Created table {CONFIG_TABLE}")
            else:
                print(f"  Table {CONFIG_TABLE} already exists")

            if not table_exists(inspector, LOG_TABLE):
                conn.execute(
                    text(
                        f"""
                        CREATE TABLE {LOG_TABLE} (
                            id INTEGER NOT NULL,
                            source VARCHAR(16) NOT NULL,
                            outcome VARCHAR(16) NOT NULL,
                            symbol VARCHAR(64) NOT NULL,
                            exchange VARCHAR(16) NOT NULL,
                            tv_symbol VARCHAR(64) NOT NULL,
                            message TEXT,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (id)
                        )
                        """
                    )
                )
                print(f"  Created table {LOG_TABLE}")
            else:
                print(f"  Table {LOG_TABLE} already exists")

            inspector = inspect(conn)
            existing_indexes = (
                {idx["name"] for idx in inspector.get_indexes(LOG_TABLE)}
                if table_exists(inspector, LOG_TABLE)
                else set()
            )
            for index_name, (table, column) in INDEXES.items():
                if index_name not in existing_indexes:
                    conn.execute(
                        text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({column})")
                    )
                    print(f"  Created index {index_name}")
                else:
                    print(f"  Index {index_name} already exists")

        print("Migration completed successfully")
        return 0
    except Exception as e:
        print(f"Migration failed: {e}")
        import traceback

        traceback.print_exc()
        return 1
    finally:
        engine.dispose()


def check_status(db_url):
    engine = create_engine(db_url, poolclass=NullPool)
    try:
        inspector = inspect(engine)
        config_ok = table_exists(inspector, CONFIG_TABLE)
        log_ok = table_exists(inspector, LOG_TABLE)
        print(f"  {CONFIG_TABLE}: {'present' if config_ok else 'missing'}")
        print(f"  {LOG_TABLE}: {'present' if log_ok else 'missing'}")
        if config_ok and log_ok:
            print("Status: UP TO DATE")
            return 0
        print("Status: MIGRATION NEEDED")
        return 1
    except Exception as e:
        print(f"Status check failed: {e}")
        return 1
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="TradingView Watchlist Plugin migration")
    parser.add_argument(
        "--status", action="store_true", help="Report what would change without changing it"
    )
    args = parser.parse_args()

    db_url = get_database_url()
    print(f"Database: {db_url}")

    if args.status:
        sys.exit(check_status(db_url))
    sys.exit(migrate(db_url))


if __name__ == "__main__":
    main()
