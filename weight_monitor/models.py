from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

EVENT_TYPES = ("feed", "control")

STATUSES = ("pending", "before_recorded", "complete", "delayed", "missed", "error")

ANOMALY_FLAGS = ("negative_delta", "implausible_spike", "sensor_error")

REFILL_ALERT_TYPES = ("equal", "below")


@dataclass
class Event:
    event_type: str
    scheduled_label: str
    scheduled_before_ts: datetime
    scheduled_after_ts: datetime
    calibration_mode_at_time: bool
    threshold_g_at_time: float | None = None

    id: int | None = None
    before_weight_g: float | None = None
    before_ts: datetime | None = None
    after_weight_g: float | None = None
    after_ts: datetime | None = None
    delta_g: float | None = None
    delay_minutes_used: float | None = None
    status: str = "pending"
    anomaly_flag: str | None = None
    notification_sent: bool = False
    notification_sent_ts: datetime | None = None
    error_message: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    feeds_left_at_time: int | None = None
    refill_alert_type: str | None = None
    refill_alert_sent: bool = False


@dataclass
class Settings:
    feed_times: list[str]
    control_time: str
    baseline_minutes: int
    delay_minutes: int
    threshold_g: float
    calibration_mode: bool
    refill_countdown_enabled: bool
    feeder_empty_weight_g: float | None
    feeder_empty_weight_set_at: str | None
    feeds_left_equal_notify: int
    feeds_left_below_notify: int
    feeds_left_equal_alerted: bool
    feeds_left_below_alerted: bool
