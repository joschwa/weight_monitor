from datetime import datetime, timedelta

from weight_monitor.webui.charts import render_line_chart_svg


def test_empty_series_renders_no_data_message():
    svg = render_line_chart_svg({})
    assert "<svg" in svg
    assert "No data" in svg


def test_series_with_no_points_in_any_label_renders_no_data_message():
    svg = render_line_chart_svg({"08:00": [], "control": []})
    assert "No data" in svg


def test_one_polyline_per_nonempty_label():
    now = datetime.now()
    series = {
        "08:00": [(now - timedelta(days=1), 120.0), (now, 110.0)],
        "18:00": [(now - timedelta(days=1), 140.0), (now, 130.0)],
        "control": [],
    }
    svg = render_line_chart_svg(series)
    assert svg.count("<polyline") == 2
    assert "08:00" in svg
    assert "18:00" in svg
    assert "control" not in svg  # empty series shouldn't get a legend entry or line


def test_threshold_line_present_when_given_and_in_range():
    now = datetime.now()
    series = {"08:00": [(now - timedelta(days=1), 120.0), (now, 110.0)]}
    svg = render_line_chart_svg(series, threshold_g=100.0)
    assert "threshold 100g" in svg
    assert "stroke-dasharray" in svg


def test_threshold_line_absent_when_not_given():
    now = datetime.now()
    series = {"08:00": [(now - timedelta(days=1), 120.0), (now, 110.0)]}
    svg = render_line_chart_svg(series, threshold_g=None)
    assert "threshold" not in svg


def test_label_text_is_escaped():
    now = datetime.now()
    series = {"<script>": [(now, 1.0)]}
    svg = render_line_chart_svg(series)
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
