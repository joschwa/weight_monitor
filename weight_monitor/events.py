from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from weight_monitor.config import StaticConfig, get_settings
from weight_monitor.models import Settings
from weight_monitor.sensor import Sensor, SensorReadError

logger = logging.getLogger(__name__)


def _local_time_today(label: str, on: datetime) -> datetime:
    """Combine an "HH:MM" label with a date, in the local timezone of `on`.

    Returned as a UTC-aware datetime -- all persisted timestamps are UTC so
    that plain ISO8601 string comparisons in SQL stay valid regardless of
    which offset was in effect when a given row was written (DST, etc.).
    Local wall-clock time only matters for interpreting the "HH:MM" label.
    """
    hour, minute = (int(x) for x in label.split(":"))
    local_dt = on.astimezone().replace(hour=hour, minute=minute, second=0, microsecond=0)
    return local_dt.astimezone(timezone.utc)


def expected_occurrences(
    settings: Settings, since: datetime, until: datetime
) -> list[tuple[str, str, datetime, datetime]]:
    """Every (event_type, label, before_ts, after_ts) scheduled to occur in [since, until).

    `since`/`until` and the returned timestamps are all UTC-aware.
    """
    labels = [("feed", t) for t in settings.feed_times] + [("control", settings.control_time)]
    occurrences = []
    day = since.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= until:
        for event_type, label in labels:
            nominal_ts = _local_time_today(label, day)
            before_ts = nominal_ts - timedelta(minutes=settings.baseline_minutes)
            after_ts = nominal_ts + timedelta(minutes=settings.delay_minutes)
            if since <= before_ts < until:
                occurrences.append((event_type, label, before_ts, after_ts))
        day += timedelta(days=1)
    return occurrences


def create_pending_event(
    conn: sqlite3.Connection,
    event_type: str,
    label: str,
    scheduled_before_ts: datetime,
    scheduled_after_ts: datetime,
    settings: Settings,
) -> int:
    cur = conn.execute(
        """INSERT INTO events (
            event_type, scheduled_label, scheduled_before_ts, scheduled_after_ts,
            threshold_g_at_time, calibration_mode_at_time, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (
            event_type,
            label,
            scheduled_before_ts.isoformat(),
            scheduled_after_ts.isoformat(),
            None if event_type == "control" else settings.threshold_g,
            int(settings.calibration_mode),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return cur.lastrowid


def run_before(conn: sqlite3.Connection, sensor: Sensor, event_id: int) -> None:
    now = datetime.now(timezone.utc)
    try:
        weight = sensor.read_grams()
    except SensorReadError as exc:
        logger.error("before-read failed for event %s: %s", event_id, exc)
        conn.execute(
            "UPDATE events SET status='error', anomaly_flag='sensor_error', error_message=? WHERE id=?",
            (str(exc), event_id),
        )
        conn.commit()
        return

    conn.execute(
        "UPDATE events SET before_weight_g=?, before_ts=?, status='before_recorded' WHERE id=?",
        (weight, now.isoformat(), event_id),
    )
    conn.commit()


def run_after(conn: sqlite3.Connection, sensor: Sensor, event_id: int, config: StaticConfig) -> None:
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    now = datetime.now(timezone.utc)

    if row["status"] == "error":
        return  # already failed at the before-read; nothing to complete

    try:
        after_weight = sensor.read_grams()
    except SensorReadError as exc:
        logger.error("after-read failed for event %s: %s", event_id, exc)
        conn.execute(
            "UPDATE events SET status='error', anomaly_flag='sensor_error', error_message=? WHERE id=?",
            (str(exc), event_id),
        )
        conn.commit()
        return

    before_ts = datetime.fromisoformat(row["before_ts"])
    before_weight = row["before_weight_g"]
    delta = before_weight - after_weight
    delay_minutes_used = (now - before_ts).total_seconds() / 60

    anomaly = None
    if delta < 0:
        anomaly = "negative_delta"
    elif delta > config.hopper_max_capacity_g:
        anomaly = "implausible_spike"

    scheduled_after = datetime.fromisoformat(row["scheduled_after_ts"])
    status = "delayed" if now > scheduled_after + timedelta(minutes=5) else "complete"

    conn.execute(
        """UPDATE events SET
            after_weight_g=?, after_ts=?, delta_g=?, delay_minutes_used=?,
            anomaly_flag=?, status=?
        WHERE id=?""",
        (after_weight, now.isoformat(), delta, delay_minutes_used, anomaly, status, event_id),
    )
    conn.commit()


def should_notify(row: sqlite3.Row) -> bool:
    """Decide whether a completed/errored event warrants an email.

    Control events always run and get logged (useful for the future web UI's
    history/plot), but only email in calibration mode -- once a threshold is
    set, their job is done and they'd otherwise just be noise every night.

    `status='missed'` rows are deliberately not handled here -- they're
    batched into a single grouped summary email by
    `notifier.summarize_and_send_missed()` instead of one email each.
    """
    if row["status"] == "error":
        return True
    if row["anomaly_flag"] is not None:
        return True
    if row["calibration_mode_at_time"]:
        return True
    if row["event_type"] == "control":
        return False
    # feed event, alert mode, no anomaly:
    return row["delta_g"] is not None and row["delta_g"] < row["threshold_g_at_time"]


def recover_missed_events(
    conn: sqlite3.Connection, sensor: Sensor, config: StaticConfig, lookback_hours: int = 48
) -> list[int]:
    """Called once at daemon startup. Returns ids of events that need a notification pass."""
    now = datetime.now(timezone.utc)
    settings = get_settings(conn)
    touched_ids: list[int] = []

    # 1. Before-recorded events whose after-check time has already passed: catch up now.
    stale = conn.execute(
        "SELECT id FROM events WHERE status='before_recorded' AND scheduled_after_ts < ?",
        (now.isoformat(),),
    ).fetchall()
    for r in stale:
        run_after(conn, sensor, r["id"], config)
        touched_ids.append(r["id"])

    # 2. Fully missed windows: expected occurrences in the lookback period with no event row at all.
    since = now - timedelta(hours=lookback_hours)
    existing = conn.execute(
        "SELECT scheduled_label, scheduled_before_ts FROM events WHERE scheduled_before_ts >= ?",
        (since.isoformat(),),
    ).fetchall()
    existing_keys = {(r["scheduled_label"], r["scheduled_before_ts"]) for r in existing}

    for event_type, label, before_ts, after_ts in expected_occurrences(settings, since, now):
        key = (label, before_ts.isoformat())
        if key in existing_keys:
            continue
        cur = conn.execute(
            """INSERT INTO events (
                event_type, scheduled_label, scheduled_before_ts, scheduled_after_ts,
                threshold_g_at_time, calibration_mode_at_time, status, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'missed', ?, ?)""",
            (
                event_type,
                label,
                before_ts.isoformat(),
                after_ts.isoformat(),
                None if event_type == "control" else settings.threshold_g,
                int(settings.calibration_mode),
                "monitor was offline during this scheduled window",
                now.isoformat(),
            ),
        )
        conn.commit()
        touched_ids.append(cur.lastrowid)

    return touched_ids
