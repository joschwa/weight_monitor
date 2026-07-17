from __future__ import annotations

import logging
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

from weight_monitor.config import StaticConfig, get_settings, set_setting
from weight_monitor.models import Settings
from weight_monitor.sensor import Sensor, SensorReadError

logger = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


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

    if row["event_type"] == "feed":
        _update_feed_countdown(conn, config, event_id, after_weight)


def _remove_iqr_outliers(values: list[float], k: float = 1.5) -> list[float]:
    if len(values) < 4:
        return values
    q1, _, q3 = statistics.quantiles(values, n=4)
    iqr = q3 - q1
    lower, upper = q1 - k * iqr, q3 + k * iqr
    return [v for v in values if lower <= v <= upper]


def compute_avg_feed_delta(conn: sqlite3.Connection, since: datetime, max_samples: int) -> float | None:
    """Mean of up to `max_samples` most recent feed-event deltas since `since`,
    with IQR outlier removal. Control deltas are deliberately excluded --
    they're noise-floor measurements, not representative of consumption."""
    rows = conn.execute(
        """SELECT delta_g FROM events
           WHERE event_type='feed' AND delta_g IS NOT NULL AND scheduled_before_ts >= ?
           ORDER BY scheduled_before_ts DESC LIMIT ?""",
        (since.isoformat(), max_samples),
    ).fetchall()
    deltas = [r["delta_g"] for r in rows]
    if not deltas:
        return None
    filtered = _remove_iqr_outliers(deltas)
    if not filtered:
        return None
    return statistics.fmean(filtered)


def _countdown_since(settings: Settings) -> datetime:
    if settings.feeder_empty_weight_set_at is None:
        return _EPOCH
    return datetime.fromisoformat(settings.feeder_empty_weight_set_at)


def compute_feeds_left(
    conn: sqlite3.Connection, settings: Settings, current_weight_g: float, max_delta_samples: int
) -> int | None:
    if not settings.refill_countdown_enabled or settings.feeder_empty_weight_g is None:
        return None
    avg_delta = compute_avg_feed_delta(conn, _countdown_since(settings), max_delta_samples)
    if not avg_delta or avg_delta <= 0:
        return None
    remaining_food_g = max(0.0, current_weight_g - settings.feeder_empty_weight_g)
    return int(remaining_food_g // avg_delta)


def _check_and_update_alert_state(conn: sqlite3.Connection, settings: Settings, feeds_left: int) -> str | None:
    """Returns the alert type ('equal'|'below') to fire for this reading, if
    any, and persists the "already alerted" hysteresis state.

    Each threshold fires once per crossing, then re-arms automatically once
    feeds_left recovers back above it (e.g. a real refill) -- no dependency
    on re-running scripts/calibrate.py to reset anything.
    """
    alert_type = None

    if feeds_left < settings.feeds_left_below_notify:
        if not settings.feeds_left_below_alerted:
            alert_type = "below"
            set_setting(conn, "feeds_left_below_alerted", True)
    elif settings.feeds_left_below_alerted:
        set_setting(conn, "feeds_left_below_alerted", False)

    # below is more urgent, so it takes priority if both conditions somehow
    # match on the same reading.
    if alert_type is None and feeds_left == settings.feeds_left_equal_notify:
        if not settings.feeds_left_equal_alerted:
            alert_type = "equal"
            set_setting(conn, "feeds_left_equal_alerted", True)
    elif feeds_left > settings.feeds_left_equal_notify and settings.feeds_left_equal_alerted:
        set_setting(conn, "feeds_left_equal_alerted", False)

    return alert_type


def _update_feed_countdown(
    conn: sqlite3.Connection, config: StaticConfig, event_id: int, current_weight_g: float
) -> None:
    """Snapshot feeds-remaining onto this feed event, and flag a new
    threshold crossing (if any) for `notifier.send_refill_alert` to pick up.

    Runs unconditionally after every feed event's delta is computed,
    independent of `should_notify()` -- the refill alert must still fire
    even when the routine per-event email is suppressed (alert mode, delta
    above threshold).
    """
    settings = get_settings(conn)
    if not settings.refill_countdown_enabled:
        return
    feeds_left = compute_feeds_left(conn, settings, current_weight_g, config.countdown_max_delta_samples)
    if feeds_left is None:
        return

    refill_alert_type = _check_and_update_alert_state(conn, settings, feeds_left)

    conn.execute(
        "UPDATE events SET feeds_left_at_time=?, refill_alert_type=? WHERE id=?",
        (feeds_left, refill_alert_type, event_id),
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
