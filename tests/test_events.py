from datetime import datetime, timedelta, timezone

from weight_monitor.config import get_settings, set_setting
from weight_monitor.events import (
    create_pending_event,
    expected_occurrences,
    recover_missed_events,
    run_after,
    run_before,
    should_notify,
)

from .conftest import make_sensor


def _new_event(conn, before_g=500, after_g=380):
    settings = get_settings(conn)
    before_ts = datetime.now(timezone.utc)
    after_ts = before_ts + timedelta(minutes=1)
    event_id = create_pending_event(conn, "feed", "08:00", before_ts, after_ts, settings)
    sensor = make_sensor([before_g, before_g, before_g, before_g])
    run_before(conn, sensor, event_id)
    return event_id, sensor


def test_run_before_then_after_computes_delta(conn, config):
    event_id, _ = _new_event(conn, before_g=500)
    sensor = make_sensor([380, 380, 380, 380])
    run_after(conn, sensor, event_id, config)
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    assert row["before_weight_g"] == 500
    assert row["after_weight_g"] == 380
    assert row["delta_g"] == 120
    assert row["status"] == "complete"
    assert row["anomaly_flag"] is None


def test_negative_delta_flagged_as_anomaly(conn, config):
    event_id, _ = _new_event(conn, before_g=300)
    sensor = make_sensor([350, 350, 350, 350])  # weight went up
    run_after(conn, sensor, event_id, config)
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    assert row["anomaly_flag"] == "negative_delta"


def test_implausible_spike_flagged(conn, config):
    event_id, _ = _new_event(conn, before_g=5000)
    sensor = make_sensor([0, 0, 0, 0])  # delta of 5000g, way over hopper_max_capacity_g=2000
    run_after(conn, sensor, event_id, config)
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    assert row["anomaly_flag"] == "implausible_spike"


def test_should_notify_calibration_mode_always_true(conn):
    set_setting(conn, "calibration_mode", True)
    row = {"status": "complete", "anomaly_flag": None, "event_type": "feed",
           "calibration_mode_at_time": 1, "delta_g": 150, "threshold_g_at_time": 100}
    assert should_notify(row)


def test_should_notify_alert_mode_only_below_threshold():
    below = {"status": "complete", "anomaly_flag": None, "event_type": "feed",
             "calibration_mode_at_time": 0, "delta_g": 50, "threshold_g_at_time": 100}
    above = {"status": "complete", "anomaly_flag": None, "event_type": "feed",
             "calibration_mode_at_time": 0, "delta_g": 150, "threshold_g_at_time": 100}
    assert should_notify(below) is True
    assert should_notify(above) is False


def test_should_notify_control_true_in_calibration_mode():
    row = {"status": "complete", "anomaly_flag": None, "event_type": "control",
           "calibration_mode_at_time": 1, "delta_g": 2, "threshold_g_at_time": None}
    assert should_notify(row) is True


def test_should_notify_control_false_in_alert_mode():
    row = {"status": "complete", "anomaly_flag": None, "event_type": "control",
           "calibration_mode_at_time": 0, "delta_g": 2, "threshold_g_at_time": None}
    assert should_notify(row) is False


def test_should_notify_control_anomaly_true_even_in_alert_mode():
    row = {"status": "complete", "anomaly_flag": "negative_delta", "event_type": "control",
           "calibration_mode_at_time": 0, "delta_g": -2, "threshold_g_at_time": None}
    assert should_notify(row) is True


def test_should_notify_sensor_error_always_true():
    row = {"status": "error", "anomaly_flag": "sensor_error", "event_type": "feed",
           "calibration_mode_at_time": 0, "delta_g": None, "threshold_g_at_time": 100}
    assert should_notify(row) is True


def test_expected_occurrences_covers_range():
    from weight_monitor.models import Settings

    settings = Settings(feed_times=["08:00", "18:00"], control_time="00:00",
                         baseline_minutes=10, delay_minutes=25, threshold_g=100,
                         calibration_mode=True)
    since = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    until = since + timedelta(days=1)
    occurrences = expected_occurrences(settings, since, until)
    labels = sorted(label for _, label, _, _ in occurrences)
    assert labels == ["00:00", "08:00", "18:00"]


def test_expected_occurrences_offsets_before_and_after_from_label():
    from weight_monitor.models import Settings

    settings = Settings(feed_times=["08:00"], control_time="23:55",
                         baseline_minutes=10, delay_minutes=25, threshold_g=100,
                         calibration_mode=True)
    since = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    until = since + timedelta(days=1)
    occurrences = expected_occurrences(settings, since, until)

    feed = next(o for o in occurrences if o[1] == "08:00")
    _, _, before_ts, after_ts = feed
    assert before_ts.astimezone().strftime("%H:%M") == "07:50"
    assert after_ts.astimezone().strftime("%H:%M") == "08:25"

    # control label crosses midnight when baseline is subtracted
    control = next(o for o in occurrences if o[1] == "23:55")
    _, _, before_ts, after_ts = control
    assert before_ts.astimezone().strftime("%H:%M") == "23:45"
    assert after_ts.astimezone().strftime("%H:%M") == "00:20"


def test_recover_missed_events_catches_up_stale_before_recorded(conn, config):
    settings = get_settings(conn)
    before_ts = datetime.now(timezone.utc) - timedelta(hours=1)
    after_ts = before_ts + timedelta(minutes=25)  # already in the past
    event_id = create_pending_event(conn, "feed", "08:00", before_ts, after_ts, settings)
    sensor_before = make_sensor([500, 500, 500, 500])
    run_before(conn, sensor_before, event_id)

    sensor_after = make_sensor([420, 420, 420, 420])
    touched = recover_missed_events(conn, sensor_after, config, lookback_hours=48)

    assert event_id in touched
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    assert row["status"] == "delayed"
    assert row["delta_g"] == 80
