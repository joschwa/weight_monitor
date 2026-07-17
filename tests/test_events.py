from datetime import datetime, timedelta, timezone

import pytest

from weight_monitor.config import get_settings, set_setting
from weight_monitor.events import (
    _check_and_update_alert_state,
    _remove_iqr_outliers,
    compute_avg_feed_delta,
    compute_feeds_left,
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


def _seed_feed_delta(conn, config, delta_g, days_ago=0, label="08:00"):
    settings = get_settings(conn)
    before_ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    after_ts = before_ts + timedelta(minutes=1)
    event_id = create_pending_event(conn, "feed", label, before_ts, after_ts, settings)
    before_g = 1000.0
    after_g = before_g - delta_g
    run_before(conn, make_sensor([before_g] * 4), event_id)
    run_after(conn, make_sensor([after_g] * 4), event_id, config)
    return event_id


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
                         calibration_mode=True, refill_countdown_enabled=False,
                         feeder_empty_weight_g=None, feeder_empty_weight_set_at=None,
                         feeds_left_equal_notify=10, feeds_left_below_notify=5,
                         feeds_left_equal_alerted=False, feeds_left_below_alerted=False)
    since = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    until = since + timedelta(days=1)
    occurrences = expected_occurrences(settings, since, until)
    labels = sorted(label for _, label, _, _ in occurrences)
    assert labels == ["00:00", "08:00", "18:00"]


def test_expected_occurrences_offsets_before_and_after_from_label():
    from weight_monitor.models import Settings

    settings = Settings(feed_times=["08:00"], control_time="23:55",
                         baseline_minutes=10, delay_minutes=25, threshold_g=100,
                         calibration_mode=True, refill_countdown_enabled=False,
                         feeder_empty_weight_g=None, feeder_empty_weight_set_at=None,
                         feeds_left_equal_notify=10, feeds_left_below_notify=5,
                         feeds_left_equal_alerted=False, feeds_left_below_alerted=False)
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


def test_remove_iqr_outliers_drops_planted_outlier():
    values = [98, 100, 102, 101, 99, 100, 500]
    filtered = _remove_iqr_outliers(values)
    assert 500 not in filtered
    assert set(filtered) == {98, 99, 100, 101, 102}


def test_remove_iqr_outliers_returns_unchanged_when_too_few_points():
    values = [10, 500]
    assert _remove_iqr_outliers(values) == values


def test_compute_avg_feed_delta_excludes_control_deltas(conn, config):
    _seed_feed_delta(conn, config, 120, days_ago=1)
    _seed_feed_delta(conn, config, 130, days_ago=0)

    settings = get_settings(conn)
    before_ts = datetime.now(timezone.utc)
    after_ts = before_ts + timedelta(minutes=1)
    control_id = create_pending_event(conn, "control", "00:00", before_ts, after_ts, settings)
    run_before(conn, make_sensor([1000, 1000, 1000, 1000]), control_id)
    run_after(conn, make_sensor([999, 999, 999, 999]), control_id, config)  # delta=1, noise

    since = datetime.now(timezone.utc) - timedelta(days=2)
    avg = compute_avg_feed_delta(conn, since, max_samples=150)
    assert avg == pytest.approx(125)  # mean of 120, 130 -- control's delta excluded


def test_compute_avg_feed_delta_respects_since_cutoff(conn, config):
    _seed_feed_delta(conn, config, 999, days_ago=10)  # before cutoff
    _seed_feed_delta(conn, config, 120, days_ago=0)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    avg = compute_avg_feed_delta(conn, since, max_samples=150)
    assert avg == pytest.approx(120)


def test_compute_avg_feed_delta_none_when_no_data(conn, config):
    since = datetime.now(timezone.utc) - timedelta(days=1)
    assert compute_avg_feed_delta(conn, since, max_samples=150) is None


def test_compute_feeds_left_none_when_tracking_disabled(conn, config):
    settings = get_settings(conn)
    assert compute_feeds_left(conn, settings, 900, 150) is None


def test_compute_feeds_left_none_when_no_feeder_weight(conn, config):
    set_setting(conn, "refill_countdown_enabled", True)
    settings = get_settings(conn)
    assert compute_feeds_left(conn, settings, 900, 150) is None


def test_compute_feeds_left_formula(conn, config):
    set_setting(conn, "refill_countdown_enabled", True)
    set_setting(conn, "feeder_empty_weight_g", 200)
    _seed_feed_delta(conn, config, 100, days_ago=1)
    _seed_feed_delta(conn, config, 100, days_ago=0)
    settings = get_settings(conn)
    # current 900g, empty=200g -> remaining=700g, avg delta=100g -> floor(700/100)=7
    assert compute_feeds_left(conn, settings, 900, 150) == 7


def test_compute_feeds_left_clamps_negative_remaining_to_zero(conn, config):
    set_setting(conn, "refill_countdown_enabled", True)
    set_setting(conn, "feeder_empty_weight_g", 1000)
    _seed_feed_delta(conn, config, 100, days_ago=0)
    settings = get_settings(conn)
    # current weight below the empty reference -> remaining clamped to 0
    assert compute_feeds_left(conn, settings, 900, 150) == 0


def test_check_and_update_alert_state_below_fires_once_then_resets_on_recovery(conn):
    set_setting(conn, "feeds_left_below_notify", 5)
    settings = get_settings(conn)

    assert _check_and_update_alert_state(conn, settings, 4) == "below"
    settings = get_settings(conn)
    assert settings.feeds_left_below_alerted is True

    # still below -> no repeat while the streak continues
    assert _check_and_update_alert_state(conn, settings, 3) is None

    # recovers back to the threshold -> flag resets, no alert fired for the recovery itself
    assert _check_and_update_alert_state(conn, settings, 5) is None
    settings = get_settings(conn)
    assert settings.feeds_left_below_alerted is False

    # drops below again -> fires a second time, no recalibration needed
    assert _check_and_update_alert_state(conn, settings, 4) == "below"


def test_check_and_update_alert_state_equal_resets_only_strictly_above(conn):
    set_setting(conn, "feeds_left_equal_notify", 10)
    settings = get_settings(conn)

    assert _check_and_update_alert_state(conn, settings, 10) == "equal"
    settings = get_settings(conn)
    assert settings.feeds_left_equal_alerted is True

    # still at/below 10 (not strictly above) -> stays alerted, no re-fire or reset
    assert _check_and_update_alert_state(conn, settings, 9) is None
    settings = get_settings(conn)
    assert settings.feeds_left_equal_alerted is True

    # recovers strictly above 10 -> resets
    assert _check_and_update_alert_state(conn, settings, 11) is None
    settings = get_settings(conn)
    assert settings.feeds_left_equal_alerted is False

    # counts back down through 10 again -> fires again
    assert _check_and_update_alert_state(conn, settings, 10) == "equal"


def test_run_after_populates_feeds_left_when_tracking_enabled(conn, config):
    set_setting(conn, "refill_countdown_enabled", True)
    set_setting(conn, "feeder_empty_weight_g", 200)
    _seed_feed_delta(conn, config, 100, days_ago=1)
    event_id = _seed_feed_delta(conn, config, 100, days_ago=0)
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    assert row["feeds_left_at_time"] is not None


def test_run_after_does_not_populate_feeds_left_when_tracking_disabled(conn, config):
    event_id = _seed_feed_delta(conn, config, 100, days_ago=0)
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    assert row["feeds_left_at_time"] is None
    assert row["refill_alert_type"] is None


def test_run_after_fires_equal_threshold_alert_once(conn, config):
    set_setting(conn, "refill_countdown_enabled", True)
    set_setting(conn, "feeder_empty_weight_g", 0)
    set_setting(conn, "feeds_left_equal_notify", 10)
    set_setting(conn, "feeds_left_below_notify", 5)
    settings = get_settings(conn)

    # Every event here (history and the two "real" checks below) has an
    # identical delta of 100g, so the running average stays exactly 100g/feed
    # regardless of how many of these deltas get folded into it -- keeps the
    # feeds_left arithmetic exact and reproducible.
    for i in range(3):
        _seed_feed_delta(conn, config, 100, days_ago=3 - i)

    def _check(after_weight):
        before_ts = datetime.now(timezone.utc)
        after_ts = before_ts + timedelta(minutes=1)
        event_id = create_pending_event(conn, "feed", "08:00", before_ts, after_ts, settings)
        run_before(conn, make_sensor([after_weight + 100] * 4), event_id)
        run_after(conn, make_sensor([after_weight] * 4), event_id, config)
        return conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()

    # remaining = 1000 - 0 = 1000, avg = 100 -> feeds_left = 10 (equals threshold)
    first = _check(1000)
    assert first["feeds_left_at_time"] == 10
    assert first["refill_alert_type"] == "equal"

    # same feeds_left again -> already alerted this cycle, no repeat
    second = _check(1000)
    assert second["feeds_left_at_time"] == 10
    assert second["refill_alert_type"] is None


def test_run_after_fires_below_threshold_alert(conn, config):
    set_setting(conn, "refill_countdown_enabled", True)
    set_setting(conn, "feeder_empty_weight_g", 0)
    set_setting(conn, "feeds_left_equal_notify", 10)
    set_setting(conn, "feeds_left_below_notify", 5)
    settings = get_settings(conn)

    for i in range(3):
        _seed_feed_delta(conn, config, 100, days_ago=3 - i)

    before_ts = datetime.now(timezone.utc)
    after_ts = before_ts + timedelta(minutes=1)
    event_id = create_pending_event(conn, "feed", "08:00", before_ts, after_ts, settings)
    run_before(conn, make_sensor([500] * 4), event_id)
    run_after(conn, make_sensor([400] * 4), event_id, config)
    # remaining = 400 - 0 = 400, avg = 100 -> feeds_left = 4 (< 5 threshold)
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    assert row["feeds_left_at_time"] == 4
    assert row["refill_alert_type"] == "below"


def test_run_after_below_alert_resets_after_real_refill_and_refires(conn, config):
    """A refill just raises the weight -- no recalibration required for the
    alert to re-arm, per the automatic hysteresis reset."""
    set_setting(conn, "refill_countdown_enabled", True)
    set_setting(conn, "feeder_empty_weight_g", 0)
    set_setting(conn, "feeds_left_equal_notify", 10)
    set_setting(conn, "feeds_left_below_notify", 5)
    settings = get_settings(conn)

    for i in range(3):
        _seed_feed_delta(conn, config, 100, days_ago=3 - i)

    def _check(after_weight):
        before_ts = datetime.now(timezone.utc)
        after_ts = before_ts + timedelta(minutes=1)
        event_id = create_pending_event(conn, "feed", "08:00", before_ts, after_ts, settings)
        run_before(conn, make_sensor([after_weight + 100] * 4), event_id)
        run_after(conn, make_sensor([after_weight] * 4), event_id, config)
        return conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()

    # feeds_left = 4 -> below fires
    first = _check(400)
    assert first["refill_alert_type"] == "below"

    # refill: weight jumps back up, feeds_left recovers well above threshold
    recovered = _check(2000)
    assert recovered["refill_alert_type"] is None
    assert get_settings(conn).feeds_left_below_alerted is False

    # depletes again -> fires a second time, no recalibration in between
    second = _check(400)
    assert second["refill_alert_type"] == "below"
