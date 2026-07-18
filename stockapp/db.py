from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .config import DATABASE_PATH, INITIAL_CAPITAL, ensure_directories


SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS securities (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'stock',
    is_hs300 INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    industry TEXT,
    industry_classification TEXT,
    industry_updated_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_bars (
    code TEXT NOT NULL REFERENCES securities(code) ON DELETE CASCADE,
    trade_date TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    amount REAL NOT NULL,
    turnover_rate REAL,
    pe_ttm REAL,
    pb_mrq REAL,
    ps_ttm REAL,
    pcf_ncf_ttm REAL,
    trade_status INTEGER NOT NULL DEFAULT 1,
    is_st INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_bars_date ON daily_bars(trade_date);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL REFERENCES securities(code),
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    price REAL NOT NULL CHECK(price > 0),
    fee REAL NOT NULL DEFAULT 0 CHECK(fee >= 0),
    stop_price REAL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(trade_date, id);

CREATE TABLE IF NOT EXISTS watchlist (
    code TEXT PRIMARY KEY REFERENCES securities(code) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS position_controls (
    code TEXT PRIMARY KEY REFERENCES securities(code) ON DELETE CASCADE,
    opened_date TEXT NOT NULL,
    initial_stop REAL,
    current_stop REAL,
    highest_close REAL,
    partial_taken INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recommendation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    status TEXT NOT NULL,
    market_regime TEXT,
    message TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES recommendation_runs(id) ON DELETE CASCADE,
    code TEXT NOT NULL REFERENCES securities(code),
    action TEXT NOT NULL CHECK(action IN ('BUY', 'WATCH', 'HOLD', 'REDUCE', 'SELL')),
    rank INTEGER,
    score REAL,
    planned_price REAL,
    price_low REAL,
    price_high REAL,
    quantity INTEGER,
    stop_price REAL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    metrics_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_recommendations_run ON recommendations(run_id, action, rank);

CREATE TABLE IF NOT EXISTS industry_history (
    code TEXT NOT NULL REFERENCES securities(code) ON DELETE CASCADE,
    effective_date TEXT NOT NULL,
    industry TEXT NOT NULL,
    classification TEXT,
    PRIMARY KEY (code, effective_date)
);

CREATE TABLE IF NOT EXISTS index_membership_snapshots (
    index_code TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    code TEXT NOT NULL REFERENCES securities(code) ON DELETE CASCADE,
    name TEXT NOT NULL,
    PRIMARY KEY (index_code, snapshot_date, code)
);
CREATE INDEX IF NOT EXISTS idx_index_membership_date
ON index_membership_snapshots(index_code, snapshot_date);

CREATE TABLE IF NOT EXISTS financial_snapshots (
    code TEXT NOT NULL REFERENCES securities(code) ON DELETE CASCADE,
    report_period TEXT NOT NULL,
    publish_date TEXT NOT NULL,
    data_type TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (code, report_period, data_type)
);
CREATE INDEX IF NOT EXISTS idx_financial_publish
ON financial_snapshots(code, publish_date, report_period);

CREATE TABLE IF NOT EXISTS factor_snapshots (
    run_id INTEGER NOT NULL REFERENCES recommendation_runs(id) ON DELETE CASCADE,
    code TEXT NOT NULL REFERENCES securities(code),
    opportunity_score REAL NOT NULL,
    risk_score REAL NOT NULL,
    confidence REAL NOT NULL,
    group_scores_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    PRIMARY KEY (run_id, code)
);

CREATE TABLE IF NOT EXISTS risk_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES recommendation_runs(id) ON DELETE CASCADE,
    code TEXT NOT NULL REFERENCES securities(code),
    rank INTEGER NOT NULL,
    risk_score REAL NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('CAUTION', 'HIGH_RISK')),
    reasons_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_alerts_run ON risk_alerts(run_id, rank);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_directories()
    connection = sqlite3.connect(str(path or DATABASE_PATH), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def init_db(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    migrate_schema(connection)
    connection.execute(
        "INSERT OR IGNORE INTO settings(key, value) VALUES ('initial_capital', ?)",
        (str(INITIAL_CAPITAL),),
    )
    connection.commit()


def migrate_schema(connection: sqlite3.Connection) -> None:
    additions = {
        "securities": {
            "industry": "TEXT",
            "industry_classification": "TEXT",
            "industry_updated_at": "TEXT",
        },
        "daily_bars": {
            "turnover_rate": "REAL",
            "pe_ttm": "REAL",
            "pb_mrq": "REAL",
            "ps_ttm": "REAL",
            "pcf_ncf_ttm": "REAL",
        },
    }
    for table, columns in additions.items():
        existing = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        for column, definition in columns.items():
            if column not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@contextmanager
def database(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    connection = connect(path)
    init_db(connection)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_setting(connection: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(connection: sqlite3.Connection, key: str, value: object) -> None:
    connection.execute(
        """
        INSERT INTO settings(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
        """,
        (key, str(value)),
    )


def start_job(connection: sqlite3.Connection, job_type: str) -> int:
    cursor = connection.execute(
        "INSERT INTO job_runs(job_type, status) VALUES (?, 'RUNNING')", (job_type,)
    )
    connection.commit()
    return int(cursor.lastrowid)


def finish_job(connection: sqlite3.Connection, job_id: int, status: str, message: object) -> None:
    if not isinstance(message, str):
        message = json.dumps(message, ensure_ascii=False)
    connection.execute(
        """
        UPDATE job_runs SET status=?, message=?, finished_at=? WHERE id=?
        """,
        (status, message, datetime.now().isoformat(timespec="seconds"), job_id),
    )
    connection.commit()
