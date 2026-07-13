from __future__ import annotations

from datetime import datetime
from html import escape

_PALETTE = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]

_MARGIN_LEFT = 50
_MARGIN_RIGHT = 20
_MARGIN_TOP = 30
_MARGIN_BOTTOM = 40
_LEGEND_HEIGHT = 24


def render_line_chart_svg(
    series: dict[str, list[tuple[datetime, float]]],
    threshold_g: float | None = None,
    width: int = 800,
    height: int = 300,
) -> str:
    """Render a simple multi-line SVG chart of delta_g over time, one line per label.

    Pure function, no Flask/DB dependency, so it's trivial to unit test with
    synthetic data. `series` maps a label (e.g. "08:00", "00:00") to a list
    of (timestamp, delta_g) points; empty/missing labels are skipped.
    """
    points = [(ts, val) for pts in series.values() for ts, val in pts]
    if not points:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
            f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
            f'fill="#888" font-family="sans-serif" font-size="14">No data in this range</text>'
            f'</svg>'
        )

    chart_top = _MARGIN_TOP + _LEGEND_HEIGHT
    chart_bottom = height - _MARGIN_BOTTOM
    chart_left = _MARGIN_LEFT
    chart_right = width - _MARGIN_RIGHT

    x_values = [ts.timestamp() for ts, _ in points]
    y_values = [val for _, val in points]
    if threshold_g is not None:
        y_values.append(threshold_g)

    x_min, x_max = min(x_values), max(x_values)
    y_min, y_max = min(y_values), max(y_values)
    y_pad = (y_max - y_min) * 0.1 or 1
    y_min -= y_pad
    y_max += y_pad
    x_span = (x_max - x_min) or 1
    y_span = (y_max - y_min) or 1

    def scale_x(ts: datetime) -> float:
        return chart_left + (ts.timestamp() - x_min) / x_span * (chart_right - chart_left)

    def scale_y(val: float) -> float:
        return chart_bottom - (val - y_min) / y_span * (chart_bottom - chart_top)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="sans-serif" font-size="11">'
    ]

    for i in range(5):
        frac = i / 4
        y = chart_bottom - frac * (chart_bottom - chart_top)
        val = y_min + frac * y_span
        parts.append(
            f'<line x1="{chart_left}" y1="{y:.1f}" x2="{chart_right}" y2="{y:.1f}" '
            f'stroke="#e0e0e0" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{chart_left - 6}" y="{y + 3:.1f}" text-anchor="end" fill="#666">{val:.0f}g</text>'
        )

    tick_count = min(6, len(points))
    for i in range(tick_count):
        frac = i / max(tick_count - 1, 1)
        ts_val = x_min + frac * x_span
        x = chart_left + frac * (chart_right - chart_left)
        label = datetime.fromtimestamp(ts_val).strftime("%m/%d")
        parts.append(
            f'<text x="{x:.1f}" y="{chart_bottom + 16}" text-anchor="middle" fill="#666">{label}</text>'
        )

    if threshold_g is not None and y_min <= threshold_g <= y_max:
        y = scale_y(threshold_g)
        parts.append(
            f'<line x1="{chart_left}" y1="{y:.1f}" x2="{chart_right}" y2="{y:.1f}" '
            f'stroke="#e15759" stroke-width="1.5" stroke-dasharray="6,4"/>'
        )
        parts.append(
            f'<text x="{chart_right}" y="{y - 4:.1f}" text-anchor="end" fill="#e15759">'
            f'threshold {threshold_g:.0f}g</text>'
        )

    legend_x = chart_left
    legend_y = _MARGIN_TOP
    for i, (label, pts) in enumerate(sorted(series.items())):
        if not pts:
            continue
        color = _PALETTE[i % len(_PALETTE)]
        swatch_x = legend_x + i * 90
        parts.append(f'<rect x="{swatch_x}" y="{legend_y}" width="10" height="10" fill="{color}"/>')
        parts.append(
            f'<text x="{swatch_x + 14}" y="{legend_y + 9}" fill="#333">{escape(label)}</text>'
        )

        sorted_pts = sorted(pts, key=lambda p: p[0])
        coords = " ".join(f"{scale_x(ts):.1f},{scale_y(val):.1f}" for ts, val in sorted_pts)
        parts.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="2"/>')
        for ts, val in sorted_pts:
            parts.append(f'<circle cx="{scale_x(ts):.1f}" cy="{scale_y(val):.1f}" r="3" fill="{color}"/>')

    parts.append(
        f'<line x1="{chart_left}" y1="{chart_top}" x2="{chart_left}" y2="{chart_bottom}" stroke="#999"/>'
    )
    parts.append(
        f'<line x1="{chart_left}" y1="{chart_bottom}" x2="{chart_right}" y2="{chart_bottom}" stroke="#999"/>'
    )

    parts.append("</svg>")
    return "".join(parts)
