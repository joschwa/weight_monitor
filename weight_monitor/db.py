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


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
    conn.commit()
