from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from weight_monitor.config import StaticConfig, get_settings
from weight_monitor.events import create_pending_event, run_after, run_before
from weight_monitor.models import Settings
from weight_monitor.notifier import scan_and_retry
from weight_monitor.sensor import Sensor

logger = logging.getLogger(__name__)

BEFORE_JOB_PREFIX = "before:"
AFTER_JOB_PREFIX = "after:"
SETTINGS_POLL_JOB_ID = "settings-poll"
NOTIFICATION_RETRY_JOB_ID = "notification-retry"


def _labels(settings: Settings) -> list[tuple[str, str]]:
    return [("feed", t) for t in settings.feed_times] + [("control", settings.control_time)]


def _settings_hash(settings: Settings) -> tuple:
    return (
        tuple(settings.feed_times),
        settings.control_time,
        settings.delay_minutes,
        settings.threshold_g,
        settings.calibration_mode,
    )


def _run_before_job(label: str, event_type: str, conn: sqlite3.Connection, sensor: Sensor) -> None:
    settings = get_settings(conn)
    scheduled_before_ts = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    scheduled_after_ts = scheduled_before_ts + timedelta(minutes=settings.delay_minutes)
    event_id = create_pending_event(conn, event_type, label, scheduled_before_ts, scheduled_after_ts, settings)
    run_before(conn, sensor, event_id)


def _run_after_job(label: str, conn: sqlite3.Connection, sensor: Sensor, config: StaticConfig) -> None:
    row = conn.execute(
        """SELECT * FROM events WHERE scheduled_label=? AND status='before_recorded'
           ORDER BY scheduled_before_ts DESC LIMIT 1""",
        (label,),
    ).fetchone()
    if row is None:
        logger.warning("after-job for %s found no matching before_recorded event", label)
        return
    run_after(conn, sensor, row["id"], config)


def _build_jobs(
    scheduler: BackgroundScheduler, conn: sqlite3.Connection, sensor: Sensor, config: StaticConfig
) -> tuple:
    settings = get_settings(conn)
    for job in scheduler.get_jobs():
        if job.id.startswith(BEFORE_JOB_PREFIX) or job.id.startswith(AFTER_JOB_PREFIX):
            scheduler.remove_job(job.id)

    for event_type, label in _labels(settings):
        hour, minute = (int(x) for x in label.split(":"))
        scheduler.add_job(
            _run_before_job,
            trigger=CronTrigger(hour=hour, minute=minute),
            args=[label, event_type, conn, sensor],
            id=f"{BEFORE_JOB_PREFIX}{label}",
            replace_existing=True,
        )

        # Delay is added to a fixed reference time, then re-split into hour/minute so the
        # "after" check happens at a fixed clock time each day (may land on a different
        # hour, e.g. 23:50 + 25min -> 00:15, which is fine for a daily cron trigger).
        after_dt = datetime(2000, 1, 1, hour, minute) + timedelta(minutes=settings.delay_minutes)
        scheduler.add_job(
            _run_after_job,
            trigger=CronTrigger(hour=after_dt.hour, minute=after_dt.minute),
            args=[label, conn, sensor, config],
            id=f"{AFTER_JOB_PREFIX}{label}",
            replace_existing=True,
        )

    return _settings_hash(settings)


def start_scheduler(conn: sqlite3.Connection, sensor: Sensor, config: StaticConfig) -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    state = {"hash": _build_jobs(scheduler, conn, sensor, config)}

    def settings_poll() -> None:
        settings = get_settings(conn)
        new_hash = _settings_hash(settings)
        if new_hash != state["hash"]:
            logger.info("settings changed, rescheduling jobs")
            state["hash"] = _build_jobs(scheduler, conn, sensor, config)

    scheduler.add_job(settings_poll, trigger="interval", seconds=60, id=SETTINGS_POLL_JOB_ID)
    scheduler.add_job(
        lambda: scan_and_retry(config, conn),
        trigger="interval",
        minutes=config.smtp_retry_scan_interval_minutes,
        id=NOTIFICATION_RETRY_JOB_ID,
    )

    scheduler.start()
    return scheduler
