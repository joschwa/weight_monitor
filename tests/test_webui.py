from datetime import datetime, timedelta, timezone

from weight_monitor.config import get_settings
from weight_monitor.events import create_pending_event, run_after, run_before
from weight_monitor.webui import create_app

from .conftest import make_sensor


def _seed_event(conn, config, label, event_type="feed", before_g=500, after_g=380, days_ago=0):
    settings = get_settings(conn)
    before_ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    after_ts = before_ts + timedelta(minutes=1)
    event_id = create_pending_event(conn, event_type, label, before_ts, after_ts, settings)
    run_before(conn, make_sensor([before_g] * 4), event_id)
    run_after(conn, make_sensor([after_g] * 4), event_id, config)
    return event_id


def _client(conn, config):
    app = create_app(config, conn)
    app.testing = True
    return app.test_client()


def test_index_returns_200_and_reflects_settings(conn, config):
    client = _client(conn, config)
    r = client.get("/")
    assert r.status_code == 200
    assert b"08:00" in r.data  # default feed_times


def test_index_shows_seeded_event_delta(conn, config):
    _seed_event(conn, config, "08:00")
    client = _client(conn, config)
    r = client.get("/")
    assert r.status_code == 200
    assert b"120" in r.data  # 500 - 380


def test_settings_post_valid_updates_table(conn, config):
    client = _client(conn, config)
    r = client.post("/settings", data={
        "feed_times": "07:00, 19:00",
        "control_time": "01:00",
        "baseline_minutes": "5",
        "delay_minutes": "15",
        "threshold_g": "150",
        # calibration_mode omitted -> unchecked -> False
    })
    assert r.status_code == 302
    settings = get_settings(conn)
    assert settings.feed_times == ["07:00", "19:00"]
    assert settings.control_time == "01:00"
    assert settings.baseline_minutes == 5
    assert settings.delay_minutes == 15
    assert settings.threshold_g == 150
    assert settings.calibration_mode is False


def test_settings_post_invalid_feed_times_leaves_table_unchanged(conn, config):
    before = get_settings(conn)
    client = _client(conn, config)
    r = client.post("/settings", data={
        "feed_times": "not-a-time",
        "control_time": "00:00",
        "baseline_minutes": "10",
        "delay_minutes": "20",
        "threshold_g": "100",
    })
    assert r.status_code == 400
    assert b"not a valid HH:MM" in r.data
    after = get_settings(conn)
    assert after == before


def test_settings_post_negative_threshold_rejected(conn, config):
    client = _client(conn, config)
    r = client.post("/settings", data={
        "feed_times": "08:00",
        "control_time": "00:00",
        "baseline_minutes": "10",
        "delay_minutes": "20",
        "threshold_g": "-5",
    })
    assert r.status_code == 400
    assert b"must be &gt; 0" in r.data or b"must be > 0" in r.data


def test_label_filter_excludes_other_labels(conn, config):
    _seed_event(conn, config, "08:00", before_g=500, after_g=380)  # delta 120
    _seed_event(conn, config, "18:00", before_g=600, after_g=350)  # delta 250
    client = _client(conn, config)

    r = client.get("/?filtered=1&label=08:00")
    assert b"120" in r.data
    assert b"250" not in r.data


def test_date_range_excludes_events_outside_window(conn, config):
    _seed_event(conn, config, "08:00", before_g=500, after_g=380, days_ago=0)  # delta 120, today
    _seed_event(conn, config, "08:00", before_g=600, after_g=200, days_ago=40)  # delta 400, 40 days ago
    client = _client(conn, config)

    # default range is last 28 days
    r = client.get("/")
    assert b"120" in r.data
    assert b"400" not in r.data
