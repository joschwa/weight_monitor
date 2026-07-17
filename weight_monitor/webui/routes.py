from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from weight_monitor.config import get_settings, set_setting
from weight_monitor.webui.charts import render_line_chart_svg

bp = Blueprint("webui", __name__)

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _conn():
    return current_app.config["WM_CONN"]


def _parse_hhmm(raw: str, field: str, errors: list[str]) -> str | None:
    raw = raw.strip()
    if not _HHMM_RE.match(raw):
        errors.append(f"{field}: {raw!r} is not a valid HH:MM time")
        return None
    return raw


def _parse_feed_times(raw: str, errors: list[str]) -> list[str] | None:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        errors.append("feed_times: must have at least one HH:MM time")
        return None
    result = []
    ok = True
    for p in parts:
        v = _parse_hhmm(p, "feed_times", errors)
        if v is None:
            ok = False
        else:
            result.append(v)
    return result if ok else None


def _parse_nonneg_int(raw: str, field: str, errors: list[str]) -> int | None:
    try:
        value = int(raw.strip())
    except ValueError:
        errors.append(f"{field}: {raw!r} is not an integer")
        return None
    if value < 0:
        errors.append(f"{field}: must be >= 0")
        return None
    return value


def _parse_positive_number(raw: str, field: str, errors: list[str]) -> float | None:
    try:
        value = float(raw.strip())
    except ValueError:
        errors.append(f"{field}: {raw!r} is not a number")
        return None
    if value <= 0:
        errors.append(f"{field}: must be > 0")
        return None
    return value


def _parse_optional_nonneg_number(raw: str, field: str, errors: list[str]) -> tuple[bool, float | None]:
    """Returns (was_provided, value). Blank input means 'leave unchanged'."""
    raw = raw.strip()
    if not raw:
        return False, None
    try:
        value = float(raw)
    except ValueError:
        errors.append(f"{field}: {raw!r} is not a number")
        return True, None
    if value < 0:
        errors.append(f"{field}: must be >= 0")
        return True, None
    return True, value


def _local_midnight(d: date) -> datetime:
    """Local midnight of `d`, tz-aware using the system's current UTC offset."""
    now_local = datetime.now().astimezone()
    return now_local.replace(year=d.year, month=d.month, day=d.day, hour=0, minute=0, second=0, microsecond=0)


def _date_range_from_args() -> tuple[date, date]:
    today = date.today()
    try:
        end = date.fromisoformat(request.args["end"]) if "end" in request.args else today
    except ValueError:
        end = today
    try:
        start = date.fromisoformat(request.args["start"]) if "start" in request.args else end - timedelta(days=27)
    except ValueError:
        start = end - timedelta(days=27)
    if start > end:
        start, end = end, start
    return start, end


def _available_labels(conn) -> list[str]:
    rows = conn.execute("SELECT DISTINCT scheduled_label FROM events ORDER BY scheduled_label").fetchall()
    return [r["scheduled_label"] for r in rows]


def _selected_labels(available: list[str]) -> list[str]:
    if "filtered" not in request.args:
        return available
    return request.args.getlist("label")


def _display_events(rows) -> list[dict]:
    display = []
    for row in rows:
        d = dict(row)
        d["local_time"] = datetime.fromisoformat(row["scheduled_before_ts"]).astimezone().strftime("%Y-%m-%d %H:%M")
        display.append(d)
    return display


@bp.route("/", methods=["GET"])
def index():
    conn = _conn()
    settings = get_settings(conn)

    start, end = _date_range_from_args()
    start_dt = _local_midnight(start)
    end_dt = _local_midnight(end) + timedelta(days=1)

    available_labels = _available_labels(conn)
    selected_labels = _selected_labels(available_labels)

    rows = []
    if selected_labels:
        placeholders = ",".join("?" for _ in selected_labels)
        rows = conn.execute(
            f"""SELECT * FROM events
                WHERE delta_g IS NOT NULL
                  AND scheduled_before_ts >= ? AND scheduled_before_ts < ?
                  AND scheduled_label IN ({placeholders})
                ORDER BY scheduled_before_ts DESC""",
            (start_dt.astimezone(timezone.utc).isoformat(), end_dt.astimezone(timezone.utc).isoformat(), *selected_labels),
        ).fetchall()

    series: dict[str, list[tuple[datetime, float]]] = {label: [] for label in selected_labels}
    for row in rows:
        ts = datetime.fromisoformat(row["scheduled_before_ts"]).astimezone()
        series[row["scheduled_label"]].append((ts, row["delta_g"]))

    chart_svg = render_line_chart_svg(series, threshold_g=settings.threshold_g)

    return render_template(
        "index.html",
        settings=settings,
        errors=[],
        available_labels=available_labels,
        selected_labels=selected_labels,
        filtered="filtered" in request.args,
        start=start.isoformat(),
        end=end.isoformat(),
        chart_svg=chart_svg,
        events=_display_events(rows),
    )


@bp.route("/settings", methods=["POST"])
def update_settings():
    conn = _conn()
    form = request.form
    errors: list[str] = []

    feed_times = _parse_feed_times(form.get("feed_times", ""), errors)
    control_time = _parse_hhmm(form.get("control_time", ""), "control_time", errors)
    baseline_minutes = _parse_nonneg_int(form.get("baseline_minutes", ""), "baseline_minutes", errors)
    delay_minutes = _parse_nonneg_int(form.get("delay_minutes", ""), "delay_minutes", errors)
    threshold_g = _parse_positive_number(form.get("threshold_g", ""), "threshold_g", errors)
    calibration_mode = form.get("calibration_mode") == "on"

    refill_countdown_enabled = form.get("refill_countdown_enabled") == "on"
    feeder_weight_provided, feeder_empty_weight_g = _parse_optional_nonneg_number(
        form.get("feeder_empty_weight_g", ""), "feeder_empty_weight_g", errors
    )
    feeds_left_equal_notify = _parse_nonneg_int(form.get("feeds_left_equal_notify", ""), "feeds_left_equal_notify", errors)
    feeds_left_below_notify = _parse_nonneg_int(form.get("feeds_left_below_notify", ""), "feeds_left_below_notify", errors)

    if errors:
        settings = get_settings(conn)
        available_labels = _available_labels(conn)
        return render_template(
            "index.html",
            settings=settings,
            errors=errors,
            available_labels=available_labels,
            selected_labels=available_labels,
            filtered=False,
            start=(date.today() - timedelta(days=27)).isoformat(),
            end=date.today().isoformat(),
            chart_svg=render_line_chart_svg({}, threshold_g=settings.threshold_g),
            events=[],
        ), 400

    set_setting(conn, "feed_times", feed_times)
    set_setting(conn, "control_time", control_time)
    set_setting(conn, "baseline_minutes", baseline_minutes)
    set_setting(conn, "delay_minutes", delay_minutes)
    set_setting(conn, "threshold_g", threshold_g)
    set_setting(conn, "calibration_mode", calibration_mode)
    set_setting(conn, "refill_countdown_enabled", refill_countdown_enabled)
    set_setting(conn, "feeds_left_equal_notify", feeds_left_equal_notify)
    set_setting(conn, "feeds_left_below_notify", feeds_left_below_notify)

    if feeder_weight_provided and feeder_empty_weight_g != get_settings(conn).feeder_empty_weight_g:
        set_setting(conn, "feeder_empty_weight_g", feeder_empty_weight_g)
        set_setting(conn, "feeder_empty_weight_set_at", datetime.now(timezone.utc).isoformat())
        # Manual reset option alongside the automatic recovery-based one in
        # events._check_and_update_alert_state.
        set_setting(conn, "feeds_left_equal_alerted", False)
        set_setting(conn, "feeds_left_below_alerted", False)

    return redirect(url_for("webui.index"))
