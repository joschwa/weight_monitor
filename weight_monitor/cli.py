from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from weight_monitor import db
from weight_monitor.calibration import Calibration
from weight_monitor.config import StaticConfig, get_settings, set_setting
from weight_monitor.events import create_pending_event, run_after, run_before
from weight_monitor.notifier import scan_and_retry, send
from weight_monitor.sensor import HX711RawReader, Sensor


def _build_sensor(config: StaticConfig) -> Sensor:
    calibration = Calibration.load(config.calibration_path)
    reader = HX711RawReader(config.gpio_dout_pin, config.gpio_pd_sck_pin)
    return Sensor(
        reader,
        calibration,
        samples_per_reading=config.samples_per_reading,
        trim_outliers=config.trim_outliers,
        sample_interval_seconds=config.sample_interval_seconds,
        read_retries=config.read_retries,
        read_retry_backoff_seconds=config.read_retry_backoff_seconds,
    )


def _trigger(event_type: str, args) -> None:
    config = StaticConfig.load()
    conn = db.connect(config.database_path)
    db.init_db(conn)
    sensor = _build_sensor(config)
    settings = get_settings(conn)

    delay_seconds = args.delay_seconds if args.delay_seconds is not None else settings.delay_minutes * 60
    before_ts = datetime.now(timezone.utc)
    after_ts = before_ts + timedelta(seconds=delay_seconds)
    label = settings.control_time if event_type == "control" else before_ts.astimezone().strftime("%H:%M")

    event_id = create_pending_event(conn, event_type, label, before_ts, after_ts, settings)
    run_before(conn, sensor, event_id)
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    print(f"before reading: {row['before_weight_g']}g (status={row['status']})")

    if row["status"] == "error":
        return

    print(f"waiting {delay_seconds}s before the after-reading...")
    time.sleep(delay_seconds)

    run_after(conn, sensor, event_id, config)
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    print(f"after reading: {row['after_weight_g']}g, delta={row['delta_g']}g, status={row['status']}, anomaly={row['anomaly_flag']}")

    if send(config, conn, row):
        print("notification sent")
    else:
        print("notification failed/queued for retry")


def _cmd_trigger_feed(args) -> None:
    _trigger("feed", args)


def _cmd_trigger_control(args) -> None:
    _trigger("control", args)


def _cmd_set_setting(args) -> None:
    config = StaticConfig.load()
    conn = db.connect(config.database_path)
    db.init_db(conn)
    try:
        value = json.loads(args.value)
    except json.JSONDecodeError:
        value = args.value
    set_setting(conn, args.key, value)
    print(f"{args.key} = {value!r}")


def _cmd_status(args) -> None:
    config = StaticConfig.load()
    conn = db.connect(config.database_path)
    db.init_db(conn)
    settings = get_settings(conn)
    print("Settings:")
    for field in ("feed_times", "control_time", "baseline_minutes", "delay_minutes", "threshold_g", "calibration_mode"):
        print(f"  {field}: {getattr(settings, field)}")

    print(f"\nLast {args.limit} events:")
    rows = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (args.limit,)
    ).fetchall()
    for row in rows:
        print(
            f"  #{row['id']} {row['event_type']:7s} {row['scheduled_label']:5s} "
            f"status={row['status']:16s} delta={row['delta_g']} anomaly={row['anomaly_flag']} "
            f"notified={bool(row['notification_sent'])}"
        )


def _cmd_notify_retry(args) -> None:
    config = StaticConfig.load()
    conn = db.connect(config.database_path)
    db.init_db(conn)
    sent = scan_and_retry(config, conn)
    print(f"sent {sent} pending notification(s)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wm-cli")
    sub = parser.add_subparsers(dest="command", required=True)

    p_feed = sub.add_parser("trigger-feed-now", help="run a feed before/after check right now")
    p_feed.add_argument("--delay-seconds", type=int, default=None, help="override the before->after delay for testing")
    p_feed.set_defaults(func=_cmd_trigger_feed)

    p_control = sub.add_parser("trigger-control-now", help="run a control before/after check right now")
    p_control.add_argument("--delay-seconds", type=int, default=None)
    p_control.set_defaults(func=_cmd_trigger_control)

    p_set = sub.add_parser("set-setting", help="set a settings-table key (JSON value, e.g. true, 100, [\"08:00\",\"18:00\"])")
    p_set.add_argument("key")
    p_set.add_argument("value")
    p_set.set_defaults(func=_cmd_set_setting)

    p_status = sub.add_parser("status", help="show current settings and recent events")
    p_status.add_argument("--limit", type=int, default=10)
    p_status.set_defaults(func=_cmd_status)

    p_retry = sub.add_parser("notify-retry", help="scan for and send any pending notifications now")
    p_retry.set_defaults(func=_cmd_notify_retry)

    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
