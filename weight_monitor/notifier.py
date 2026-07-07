from __future__ import annotations

import logging
import smtplib
import sqlite3
from datetime import datetime, timezone
from email.mime.text import MIMEText

from weight_monitor.config import StaticConfig

logger = logging.getLogger(__name__)


def _fmt_local(ts_iso: str | None) -> str:
    if not ts_iso:
        return "n/a"
    return datetime.fromisoformat(ts_iso).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def build_message(row: sqlite3.Row) -> tuple[str, str]:
    label = row["scheduled_label"] or row["event_type"]

    if row["status"] == "error":
        subject = f"[WeightMonitor] SENSOR ERROR: {row['event_type']} {label}"
    elif row["status"] == "missed":
        subject = f"[WeightMonitor] MISSED: {row['event_type']} {label} (monitor was offline)"
    elif row["anomaly_flag"] == "negative_delta":
        subject = f"[WeightMonitor] ANOMALY: {row['event_type']} {label} — negative delta {row['delta_g']:.0f}g"
    elif row["anomaly_flag"] == "implausible_spike":
        subject = f"[WeightMonitor] ANOMALY: {row['event_type']} {label} — implausible delta {row['delta_g']:.0f}g"
    elif row["event_type"] == "feed" and not row["calibration_mode_at_time"] and row["delta_g"] < row["threshold_g_at_time"]:
        subject = (
            f"[WeightMonitor] ALERT: {row['event_type']} {label} — "
            f"only {row['delta_g']:.0f}g consumed (threshold {row['threshold_g_at_time']:.0f}g)"
        )
    else:
        subject = f"[WeightMonitor] {row['event_type']} {label} — consumed {row['delta_g']:.0f}g" if row["delta_g"] is not None else f"[WeightMonitor] {row['event_type']} {label}"

    lines = [
        f"Event type: {row['event_type']} ({label})",
        f"Scheduled before: {_fmt_local(row['scheduled_before_ts'])}",
        f"Actual before:    {_fmt_local(row['before_ts'])}",
        f"Scheduled after:  {_fmt_local(row['scheduled_after_ts'])}",
        f"Actual after:     {_fmt_local(row['after_ts'])}",
        f"Before weight:    {row['before_weight_g']}",
        f"After weight:     {row['after_weight_g']}",
        f"Delta:            {row['delta_g']}",
        f"Threshold:        {row['threshold_g_at_time']}",
        f"Calibration mode: {bool(row['calibration_mode_at_time'])}",
        f"Delay minutes used: {row['delay_minutes_used']}",
        f"Status:           {row['status']}",
        f"Anomaly:          {row['anomaly_flag']}",
    ]
    if row["error_message"]:
        lines.append(f"Error:            {row['error_message']}")
    body = "\n".join(lines)
    return subject, body


def send(config: StaticConfig, conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    subject, body = build_message(row)
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config.smtp_from_address
    msg["To"] = config.smtp_to_address

    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(config.smtp_from_address, config.smtp_password)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as exc:
        logger.warning("failed to send notification for event %s: %s", row["id"], exc)
        return False

    conn.execute(
        "UPDATE events SET notification_sent=1, notification_sent_ts=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), row["id"]),
    )
    conn.commit()
    return True


def scan_and_retry(config: StaticConfig, conn: sqlite3.Connection) -> int:
    """Send notifications for any event that needs one but hasn't gotten one yet.

    Import is local to avoid a circular import (events -> config -> ... -> notifier).
    """
    from weight_monitor.events import should_notify

    rows = conn.execute(
        "SELECT * FROM events WHERE notification_sent=0 AND status IN ('complete','delayed','error','missed')"
    ).fetchall()
    sent = 0
    for row in rows:
        if should_notify(row) and send(config, conn, row):
            sent += 1
    return sent
