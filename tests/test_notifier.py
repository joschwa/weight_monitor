from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from weight_monitor.config import get_settings
from weight_monitor.events import create_pending_event, run_after, run_before
from weight_monitor.notifier import scan_and_retry, send

from .conftest import make_sensor


def _completed_event(conn, config, before_g=500, after_g=380):
    settings = get_settings(conn)
    before_ts = datetime.now(timezone.utc)
    after_ts = before_ts + timedelta(minutes=1)
    event_id = create_pending_event(conn, "feed", "08:00", before_ts, after_ts, settings)
    run_before(conn, make_sensor([before_g] * 4), event_id)
    run_after(conn, make_sensor([after_g] * 4), event_id, config)
    return conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()


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
