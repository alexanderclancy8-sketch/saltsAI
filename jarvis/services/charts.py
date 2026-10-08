"""Chart specs for the display: validated on the server, drawn by the console (web/charts.js) from the validated JSON only.

A chart is DATA, never code or markup. Jarvis (``show_chart``, or ``fsm_analyse`` with a chart requested) produces a spec; this
module checks it against a closed vocabulary and hard caps and hands back a normalised copy that is the only thing ever published
to the display. The console then builds inline SVG with DOM APIs from it (``textContent`` for every label, no ``innerHTML``).

Spec (all of it is plain JSON)::

    {"type": "bar" | "line" | "pie" | "donut" | "stacked_bar",
     "title": "Jobs per engineer, 2026",            # required, <= 120 chars
     "x_label": "Engineer", "y_label": "Jobs",      # optional, <= 60 chars
     "unit": "number" | "gbp" | "percent",           # how values are formatted (default number)
     "series": [{"name": "Jobs", "points": [{"label": "Dan", "value": 41}, ...]}, ...]}

Caps: bar 1 series x 24 bars; line up to 6 series x 60 points; pie/donut 1 series x 12 slices; stacked_bar up to 8 series x 24
categories; 240 points in all; text is stripped of control characters and HTML tags; every value is a finite number no bigger than
1e15; pie/donut/stacked values can't be negative. Nothing here reads the clock or the network.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Any

from ..integrations.fsm_data import clean_text

CHART_TYPES = ("bar", "line", "pie", "donut", "stacked_bar")
UNITS = ("number", "gbp", "percent")
MAX_TITLE = 120
MAX_AXIS_LABEL = 60
MAX_LABEL = 60
MAX_SERIES_NAME = 40
MAX_VALUE = 1e15
MAX_TOTAL_POINTS = 240
# type -> (max series, max points per series / categories)
LIMITS: dict[str, tuple[int, int]] = {"bar": (1, 24), "line": (6, 60), "pie": (1, 12), "donut": (1, 12), "stacked_bar": (8, 24)}
NOUN = {"bar": "bars", "line": "points", "pie": "slices", "donut": "slices", "stacked_bar": "categories"}


class ChartError(ValueError):
    """The chart spec is not acceptable. The message says what to change and is what the model is told."""


def _text(value: Any, limit: int) -> str:
    return clean_text(value, limit) if value is not None else ""


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool):
        raise ChartError(f"{where}: a value must be a number, not true/false.")
    if isinstance(value, (int, float, Decimal)):
        try:
            num = float(value)
        except OverflowError:
            raise ChartError(f"{where}: a value is too large to chart (over 1e15).") from None
    elif isinstance(value, str):
        try:
            num = float(Decimal(value.strip().replace(",", "").lstrip("£")))
        except (InvalidOperation, ValueError):
            raise ChartError(f"{where}: '{clean_text(value, 20)}' isn't a number.") from None
    else:
        raise ChartError(f"{where}: a value must be a number.")
    if not math.isfinite(num):
        raise ChartError(f"{where}: a value is not a finite number (NaN or infinity).")
    if abs(num) > MAX_VALUE:
        raise ChartError(f"{where}: a value is too large to chart (over 1e15).")
    return num


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def validate_chart(spec: Any) -> dict[str, Any]:
    """The normalised copy of ``spec`` (see the module docstring), or ChartError saying what is wrong. Accepts a dict or a pydantic model."""
    if hasattr(spec, "model_dump"):
        spec = spec.model_dump()
    if not isinstance(spec, dict):
        raise ChartError("A chart spec must be an object with type, title and series.")
    ctype = str(spec.get("type") or "").strip().lower().replace(" ", "_").replace("-", "_")
    if ctype not in CHART_TYPES:
        raise ChartError(f"Chart type must be one of: {', '.join(CHART_TYPES)}.")
    title = _text(spec.get("title"), MAX_TITLE)
    if not title:
        raise ChartError("A chart needs a title saying what it shows (and the period, if it has one).")
    unit = str(spec.get("unit") or "number").strip().lower()
    if unit not in UNITS:
        raise ChartError(f"unit must be one of: {', '.join(UNITS)}.")
    raw_series = spec.get("series")
    if not isinstance(raw_series, (list, tuple)) or not raw_series:
        raise ChartError("A chart needs at least one series: [{name, points: [{label, value}, ...]}].")
    max_series, max_points = LIMITS[ctype]
    if len(raw_series) > max_series:
        raise ChartError(f"A {ctype.replace('_', ' ')} chart takes at most {max_series} series (got {len(raw_series)})." +
                         (" Use a stacked_bar or line chart for several series." if max_series == 1 else ""))
    series: list[dict[str, Any]] = []
    categories: list[str] = []
    total = 0
    names: set[str] = set()
    for i, s in enumerate(raw_series, 1):
        if isinstance(s, (str, bytes, int, float, list, tuple)) or s is None:
            raise ChartError(f"Series {i} must be an object with name and points.")
        points = _get(s, "points")
        if not isinstance(points, (list, tuple)) or not points:
            raise ChartError(f"Series {i} needs at least one point: {{label, value}}.")
        if len(points) > max_points:
            raise ChartError(f"A {ctype.replace('_', ' ')} chart shows at most {max_points} {NOUN[ctype]} (series {i} has "
                             f"{len(points)}). Keep the biggest and group the rest as 'Other'.")
        total += len(points)
        if total > MAX_TOTAL_POINTS:
            raise ChartError(f"Too many points in all ({MAX_TOTAL_POINTS} at most).")
        name = _text(_get(s, "name"), MAX_SERIES_NAME) or (title[:MAX_SERIES_NAME] if len(raw_series) == 1 else f"Series {i}")
        base, n = name, 2
        while name in names:
            name, n = f"{base} ({n})", n + 1
        names.add(name)
        seen: set[str] = set()
        pts: list[dict[str, Any]] = []
        for k, p in enumerate(points, 1):
            where = f"Series {i}, point {k}"
            if isinstance(p, (str, bytes, int, float, list, tuple)) or p is None:
                raise ChartError(f"{where}: each point must be an object with label and value.")
            label = _text(_get(p, "label"), MAX_LABEL)
            if not label:
                raise ChartError(f"{where}: every point needs a label.")
            if label in seen:
                raise ChartError(f"{where}: the label '{label}' appears twice in one series - labels must be unique.")
            seen.add(label)
            value = _number(_get(p, "value"), where)
            if value < 0 and ctype in ("pie", "donut", "stacked_bar"):
                raise ChartError(f"{where}: {ctype.replace('_', ' ')} charts can't show negative values - use a bar chart.")
            pts.append({"label": label, "value": round(value, 2) if unit == "gbp" else round(value, 4)})
            if label not in categories:
                categories.append(label)
        series.append({"name": name, "points": pts})
    if len(categories) > max_points:
        raise ChartError(f"The series together use {len(categories)} different labels; at most {max_points} fit.")
    if ctype in ("pie", "donut") and sum(p["value"] for p in series[0]["points"]) <= 0:
        raise ChartError("A pie or donut chart needs values that add up to more than zero.")
    return {"type": ctype, "title": title, "x_label": _text(spec.get("x_label"), MAX_AXIS_LABEL),
            "y_label": _text(spec.get("y_label"), MAX_AXIS_LABEL), "unit": unit, "series": series}


def spec_text(spec: dict[str, Any]) -> str:
    """Every string a validated spec will put on screen (title, axis labels, series names, point labels) plus every value as typed:
    what the sensitive-figure guard compares against."""
    parts = [spec.get("title", ""), spec.get("x_label", ""), spec.get("y_label", "")]
    for s in spec.get("series", []):
        parts.append(s.get("name", ""))
        for p in s.get("points", []):
            parts += [p.get("label", ""), f"{p.get('value')}", f"{p.get('value'):,.2f}"]
    return " ".join(str(x) for x in parts)


def categories(spec: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for s in spec["series"]:
        for p in s["points"]:
            if p["label"] not in out:
                out.append(p["label"])
    return out
