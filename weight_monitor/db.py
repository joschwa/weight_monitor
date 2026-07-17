from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type               TEXT NOT NULL CHECK(event_type IN ('feed','control')),
    scheduled_label          TEXT,
    scheduled_before_ts      TEXT NOT NULL,
    before_weight_g          REAL,
    before_ts                TEXT,
    scheduled_after_ts       TEXT NOT NULL,
    after_weight_g           REAL,
    after_ts                 TEXT,
    delta_g                  REAL,
    delay_minutes_used       REAL,
    threshold_g_at_time      REAL,
    calibration_mode_at_time INTEGER NOT NULL,
    status                   TEXT NOT NULL DEFAULT 'pending'
                             CHECK(status IN ('pending','before_recorded','complete','delayed','missed','error')),
    anomaly_flag             TEXT
                             CHECK(anomaly_flag IN ('negative_delta','implausible_spike','sensor_error') OR anomaly_flag IS NULL),
    notification_sent        INTEGER NOT NULL DEFAULT 0,
    notification_sent_ts     TEXT,
    error_message            TEXT,
    created_at                TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "feed_times": ["08:00", "18:00"],
    "control_time": "00:00",
    "baseline_minutes": 10,
    "delay_minutes": 20,
    "threshold_g": 100,
    "calibration_mode": True,
    "refill_countdown_enabled": False,
    "feeder_empty_weight_g": None,
    "feeder_empty_weight_set_at": None,
    "feeds_left_equal_notify": 10,
    "feeds_left_below_notify": 5,
    # Internal bookkeeping (not exposed in CLI/web forms): hysteresis state
    # for the two refill alerts -- reset automatically once feeds_left
    # recovers back above/to the threshold, so a real refill re-arms them
    # without needing to re-run scripts/calibrate.py.
    "feeds_left_equal_alerted": False,
    "feeds_left_below_alerted": False,
}

# Columns added to `events` after its initial release. Applied via
# ALTER TABLE on every startup so an already-populated Pi database (which
# CREATE TABLE IF NOT EXISTS won't touch) picks them up without losing
# existing history.
_EVENTS_MIGRATIONS = {
    "feeds_left_at_time": "feeds_left_at_time INTEGER",
    "refill_alert_type": "refill_alert_type TEXT CHECK(refill_alert_type IN ('equal','below') OR refill_alert_type IS NULL)",
    "refill_alert_sent": "refill_alert_sent INTEGER NOT NULL DEFAULT 0",
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the daemon shares this connection with
    # APScheduler's background worker thread (see scheduler.py, which pins
    # that executor to a single worker so access is serialized, not
    # concurrent).
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, declaration in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_columns(conn, "events", _EVENTS_MIGRATIONS)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
    conn.commit()
