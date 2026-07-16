from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from weight_monitor.config import get_settings
from weight_monitor.events import create_pending_event, run_after, run_before
from weight_monitor.notifier import scan_and_retry, send, summarize_and_send_missed

from .conftest import make_sensor


def _completed_event(conn, config, before_g=500, after_g=380):
    settings = get_settings(conn)
    before_ts = datetime.now(timezone.utc)
    after_ts = before_ts + timedelta(minutes=1)
    event_id = create_pending_event(conn, "feed", "08:00", before_ts, after_ts, settings)
    run_before(conn, make_sensor([before_g] * 4), event_id)
    run_after(conn, make_sensor([after_g] * 4), event_id, config)
    return conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()


def _seed_missed(conn, event_type, label, before_ts):
    after_ts = before_ts + timedelta(minutes=20)
    cur = conn.execute(
        """INSERT INTO events (
            event_type, scheduled_label, scheduled_before_ts, scheduled_after_ts,
            threshold_g_at_time, calibration_mode_at_time, status, error_message, created_at
        ) VALUES (?, ?, ?, ?, 100, 1, 'missed', 'monitor was offline', ?)""",
        (event_type, label, before_ts.isoformat(), after_ts.isoformat(), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cur.lastrowid


@patch("smtplib.SMTP")
def test_send_marks_notification_sent_on_success(mock_smtp_cls, conn, config):
    mock_smtp = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_smtp
    row = _completed_event(conn, config)

    assert send(config, conn, row) is True
    mock_smtp.starttls.assert_called_once()
    mock_smtp.login.assert_called_once()
    mock_smtp.send_message.assert_called_once()

    updated = conn.execute("SELECT * FROM events WHERE id=?", (row["id"],)).fetchone()
    assert updated["notification_sent"] == 1


@patch("smtplib.SMTP")
def test_send_leaves_unsent_on_smtp_failure(mock_smtp_cls, conn, config):
    mock_smtp_cls.side_effect = OSError("network unreachable")
    row = _completed_event(conn, config)

    assert send(config, conn, row) is False
    updated = conn.execute("SELECT * FROM events WHERE id=?", (row["id"],)).fetchone()
    assert updated["notification_sent"] == 0


@patch("smtplib.SMTP")
def test_scan_and_retry_sends_only_events_that_need_notifying(mock_smtp_cls, conn, config):
    mock_smtp = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

    # calibration mode on by default -> this feed event needs a notification
    _completed_event(conn, config, before_g=500, after_g=380)

    sent = scan_and_retry(config, conn)
    assert sent == 1
    assert mock_smtp.send_message.call_count == 1


@patch("smtplib.SMTP")
def test_summarize_and_send_missed_groups_by_type_and_label(mock_smtp_cls, conn, config):
    mock_smtp = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_smtp
    now = datetime.now(timezone.utc)

    _seed_missed(conn, "feed", "08:00", now - timedelta(days=2))
    _seed_missed(conn, "feed", "08:00", now - timedelta(days=1))
    _seed_missed(conn, "control", "00:00", now)

    assert summarize_and_send_missed(config, conn) is True
    assert mock_smtp.send_message.call_count == 1

    sent_msg = mock_smtp.send_message.call_args[0][0]
    body = sent_msg.get_payload()
    assert "feed 08:00: 2 missed" in body
    assert "control 00:00: 1 missed" in body
    assert "3 missed weigh-in" in sent_msg["Subject"]

    rows = conn.execute("SELECT notification_sent FROM events WHERE status='missed'").fetchall()
    assert all(r["notification_sent"] == 1 for r in rows)


def test_summarize_and_send_missed_noop_when_none_pending(conn, config):
    with patch("smtplib.SMTP") as mock_smtp_cls:
        assert summarize_and_send_missed(config, conn) is False
        mock_smtp_cls.assert_not_called()


@patch("smtplib.SMTP")
def test_summarize_and_send_missed_leaves_rows_unsent_on_smtp_failure(mock_smtp_cls, conn, config):
    mock_smtp_cls.side_effect = OSError("network unreachable")
    now = datetime.now(timezone.utc)
    _seed_missed(conn, "feed", "08:00", now)

    assert summarize_and_send_missed(config, conn) is False
    rows = conn.execute("SELECT notification_sent FROM events WHERE status='missed'").fetchall()
    assert all(r["notification_sent"] == 0 for r in rows)


@patch("smtplib.SMTP")
def test_scan_and_retry_sends_one_summary_plus_individual_events(mock_smtp_cls, conn, config):
    mock_smtp = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_smtp
    now = datetime.now(timezone.utc)

    _seed_missed(conn, "feed", "08:00", now - timedelta(days=1))
    _seed_missed(conn, "control", "00:00", now)
    _completed_event(conn, config, before_g=500, after_g=380)  # calibration mode -> needs notify

    sent = scan_and_retry(config, conn)
    assert sent == 2  # one summary email + one individual event email
    assert mock_smtp.send_message.call_count == 2
