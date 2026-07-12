from apscheduler.schedulers.background import BackgroundScheduler

from weight_monitor.config import set_setting
from weight_monitor.scheduler import _build_jobs, _labels
from weight_monitor.config import get_settings

from .conftest import make_sensor


def _field(trigger, name):
    return str(next(f for f in trigger.fields if f.name == name))


def test_build_jobs_creates_before_and_after_pair_per_label(conn, config):
    scheduler = BackgroundScheduler()
    sensor = make_sensor([500])
    _build_jobs(scheduler, conn, sensor, config)

    settings = get_settings(conn)
    expected_labels = {label for _, label in _labels(settings)}
    job_ids = {job.id for job in scheduler.get_jobs()}

    for label in expected_labels:
        assert f"before:{label}" in job_ids
        assert f"after:{label}" in job_ids


def test_after_job_cron_reflects_delay_minutes(conn, config):
    scheduler = BackgroundScheduler()
    sensor = make_sensor([500])
    set_setting(conn, "feed_times", ["08:00"])
    set_setting(conn, "control_time", "23:50")
    set_setting(conn, "delay_minutes", 25)

    _build_jobs(scheduler, conn, sensor, config)

    after_feed = scheduler.get_job("after:08:00")
    assert _field(after_feed.trigger, "hour") == "8"
    assert _field(after_feed.trigger, "minute") == "25"

    # crosses midnight: 23:50 + 25min -> 00:15 the next day
    after_control = scheduler.get_job("after:23:50")
    assert _field(after_control.trigger, "hour") == "0"
    assert _field(after_control.trigger, "minute") == "15"


def test_before_job_cron_reflects_baseline_minutes(conn, config):
    scheduler = BackgroundScheduler()
    sensor = make_sensor([500])
    set_setting(conn, "feed_times", ["08:00"])
    set_setting(conn, "control_time", "00:05")
    set_setting(conn, "baseline_minutes", 10)

    _build_jobs(scheduler, conn, sensor, config)

    before_feed = scheduler.get_job("before:08:00")
    assert _field(before_feed.trigger, "hour") == "7"
    assert _field(before_feed.trigger, "minute") == "50"

    # crosses midnight backwards: 00:05 - 10min -> 23:55 the prior day
    before_control = scheduler.get_job("before:00:05")
    assert _field(before_control.trigger, "hour") == "23"
    assert _field(before_control.trigger, "minute") == "55"


def test_rebuild_replaces_stale_jobs_when_settings_change(conn, config):
    scheduler = BackgroundScheduler()
    sensor = make_sensor([500])
    set_setting(conn, "feed_times", ["08:00", "18:00"])
    _build_jobs(scheduler, conn, sensor, config)
    assert scheduler.get_job("before:08:00") is not None

    set_setting(conn, "feed_times", ["09:00"])
    _build_jobs(scheduler, conn, sensor, config)

    assert scheduler.get_job("before:08:00") is None
    assert scheduler.get_job("before:09:00") is not None
