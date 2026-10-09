"""Self-contained visual evidence reports: single-file HTML with inline SVG.

The report renderer is read-only and offline: it turns already-recorded facts
(schema-v1 waveforms, Maestro specification verdicts, Reviewer findings and
manifest evidence hashes) into one portable HTML document.  It never invents
data — a missing input simply omits its section — and never references
external resources, so the output file can be archived as evidence.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import os
import re
from pathlib import Path

from .artifacts import timestamp
from .contract import no_duplicate_keys
from .errors import ValidationError


_LOG_ANALYSES = {"ac", "noise", "stb", "pac", "pnoise"}
_PALETTE = ("#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#17becf", "#8c564b", "#e377c2")
_MAX_TRACES = len(_PALETTE)
_MAX_POINTS = 2000
_OPERATION_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_WAVEFORM_FILE_BYTES = 256 * 1024 ** 2
_MAX_JSON_BYTES = 64 * 1024 ** 2
_MAX_OPERATION_WAVEFORMS = 32
_FONT = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

_STYLE = """
body{margin:0;background:#f6f7f9;color:#1c1e21;font-family:-apple-system,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;line-height:1.55}
header{background:#1f3350;color:#fff;padding:1rem 1.5rem}
header h1{margin:0;font-size:1.25rem}
main{max-width:1080px;margin:0 auto;padding:1rem 1.5rem}
section{background:#fff;border:1px solid #dfe2e7;border-radius:6px;padding:1rem 1.25rem;margin:1rem 0}
h2{margin-top:0;font-size:1.1rem;border-bottom:2px solid #1f3350;padding-bottom:.3rem}
h3{font-size:.95rem;margin:.8rem 0 .4rem;color:#33415c}
table{border-collapse:collapse;width:100%;font-size:.85rem}
th,td{border:1px solid #d5d9e0;padding:.35rem .55rem;text-align:left;vertical-align:top}
thead th{background:#eef1f5}
table.kv th{width:14rem;background:#f4f6f9;font-weight:600}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.sha{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;word-break:break-all}
figure{margin:.8rem 0}
figcaption{font-size:.8rem;color:#55607a;margin-bottom:.3rem}
figure svg{max-width:100%;height:auto;border:1px solid #e2e5ea;background:#fff}
.pass{color:#0a7a2f;font-weight:600}
.fail{color:#c02323;font-weight:600}
.near{color:#b26a00;font-weight:600}
.suspicion{color:#b26a00;font-weight:600}
.unconfirmed{color:#6b7280}
.violation{color:#c02323;font-weight:600}
.warning{color:#b26a00;font-weight:600}
footer{max-width:1080px;margin:0 auto;padding:0 1.5rem 1.5rem;color:#6b7280;font-size:.75rem}
"""


def _esc(value):
    return html.escape(str(value), quote=True)


def _engineering(value):
    """Format a finite number with an SI suffix (1000 -> ``1k``, 0.001 -> ``1m``)."""
    if value == 0:
        return "0"
    suffixes = {-12: "p", -9: "n", -6: "u", -3: "m", 0: "", 3: "k", 6: "M", 9: "G", 12: "T"}
    tier = max(-12, min(12, 3 * math.floor(math.log10(abs(value)) / 3)))
    scaled = value / 10.0 ** tier
    if abs(scaled) >= 999.95 and tier < 12:
        tier += 3
        scaled = value / 10.0 ** tier
    return f"{scaled:.4g}{suffixes[tier]}"


def _invalid_waveform(message):
    raise ValidationError(message, stage="report", code="invalid_waveform")


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _invalid_waveform("Waveform values must be finite numbers")
    return float(value)


def _checked_waveform(result):
    """Validate a schema-v1 waveform dict and return plot-ready content."""
    if not isinstance(result, dict):
        _invalid_waveform("Waveform result must be an object")
    axis = result.get("axis")
    if not isinstance(axis, dict):
        _invalid_waveform("Waveform axis is missing")
    raw_axis = axis.get("values")
    if not isinstance(raw_axis, list) or not raw_axis:
        _invalid_waveform("Waveform axis is empty")
    xs = [_finite(value) for value in raw_axis]
    row_count = result.get("row_count")
    if row_count is not None and (type(row_count) is not int or row_count != len(xs)):
        _invalid_waveform("Waveform row count is inconsistent")
    axis_name, axis_unit = axis.get("name", ""), axis.get("unit", "")
    if not isinstance(axis_name, str) or not isinstance(axis_unit, str):
        _invalid_waveform("Waveform axis labels must be strings")
    raw_signals = result.get("signals")
    if not isinstance(raw_signals, list) or not raw_signals:
        _invalid_waveform("Waveform signals are missing")
    signals = []
    for item in raw_signals:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            _invalid_waveform("Waveform signal names are missing")
        unit = item.get("unit", "")
        if not isinstance(unit, str):
            _invalid_waveform("Waveform signal units must be strings")
        values = item.get("values")
        if not isinstance(values, list) or len(values) != len(xs):
            _invalid_waveform("Waveform signal length differs from the axis")
        signals.append({"name": item["name"], "unit": unit, "values": [_finite(value) for value in values]})
    analysis = result.get("analysis", "")
    if not isinstance(analysis, str):
        _invalid_waveform("Waveform analysis must be a string")
    return analysis, {"name": axis_name, "unit": axis_unit, "values": xs}, signals


def _linear_ticks(lo, hi):
    """Choose 5-8 rounded tick positions inside [lo, hi] (requires lo < hi)."""
    span = hi - lo
    ticks = []
    for target in (6, 5, 7, 4, 8, 3, 2):
        step0 = span / target
        decade = 10.0 ** math.floor(math.log10(step0))
        step = next(factor * decade for factor in (1, 2, 2.5, 5, 10) if factor * decade >= step0)
        first = math.ceil(lo / step - 1e-9)
        last = math.floor(hi / step + 1e-9)
        ticks = [index * step for index in range(first, last + 1)]
        if 5 <= len(ticks) <= 8:
            break
    return ticks


def _log_ticks(lo, hi):
    """Choose gridline positions for a log10 axis inside [lo, hi] (both > 0)."""
    lower = math.floor(math.log10(lo))
    upper = math.ceil(math.log10(hi))

    def in_range(value):
        return lo * (1 - 1e-12) <= value <= hi * (1 + 1e-12)

    majors = [10.0 ** k for k in range(lower, upper + 1) if in_range(10.0 ** k)]
    if len(majors) > 8:
        return majors[:: math.ceil(len(majors) / 7)]
    if len(majors) >= 5:
        return majors
    ticks = majors
    for mantissas in ((1, 2, 5), tuple(range(1, 10))):
        ticks = sorted(value for k in range(lower, upper + 1) for value in (m * 10.0 ** k for m in mantissas) if in_range(value))
        if len(ticks) >= 5:
            break
    if len(ticks) > 8:
        return ticks[:: math.ceil(len(ticks) / 7)]
    return ticks


def waveform_svg(result, *, width=760, height=300, log_x=None, title=None):
    """Render a schema-v1 waveform dict as a standalone inline ``<svg>`` string.

    AC-family analyses (ac/noise/stb/pac/pnoise) with a strictly positive axis
    default to a log10 x axis; an explicit ``log_x`` always wins.  The y axis
    is linear with a 5% margin.  At most eight signals are drawn, cycling a
    built-in palette, and waveforms longer than about 2000 points are
    stride-decimated with the ratio recorded in an SVG comment.  All text is
    XML-escaped and no external resource is referenced.
    """
    if type(width) is not int or type(height) is not int or not 100 <= width <= 10000 or not 80 <= height <= 4000:
        _invalid_waveform("SVG size is outside the supported range")
    if log_x is not None and type(log_x) is not bool:
        _invalid_waveform("log_x must be a boolean or None")
    if title is not None and not isinstance(title, str):
        _invalid_waveform("SVG title must be a string")
    analysis, axis, signals = _checked_waveform(result)
    count = len(axis["values"])
    stride = max(1, math.ceil(count / _MAX_POINTS))
    indices = list(range(0, count, stride))
    if indices[-1] != count - 1:
        indices.append(count - 1)
    xs = [axis["values"][index] for index in indices]
    traces = signals[:_MAX_TRACES]
    use_log = log_x if log_x is not None else analysis.casefold() in _LOG_ANALYSES and all(value > 0 for value in xs)
    if use_log and any(value <= 0 for value in xs):
        _invalid_waveform("A logarithmic x axis requires strictly positive axis values")
    left, right = 64.0, 16.0
    top = 30.0 if title else 14.0
    bottom = 42.0
    plot_w, plot_h = width - left - right, height - top - bottom
    x_lo, x_hi = min(xs), max(xs)
    if use_log:
        base_lo, base_hi = math.log10(x_lo), math.log10(x_hi)
        if base_hi <= base_lo:
            base_lo, base_hi = base_lo - 0.5, base_hi + 0.5

        def x_map(value):
            return left + (math.log10(value) - base_lo) / (base_hi - base_lo) * plot_w

        x_ticks = _log_ticks(x_lo, x_hi)
    else:
        if x_hi <= x_lo:
            x_lo, x_hi = x_lo - 0.5, x_hi + 0.5

        def x_map(value):
            return left + (value - x_lo) / (x_hi - x_lo) * plot_w

        x_ticks = _linear_ticks(x_lo, x_hi)
    ys = [trace["values"][index] for trace in traces for index in indices]
    y_lo, y_hi = min(ys), max(ys)
    margin = (y_hi - y_lo) * 0.05
    if margin <= 0:
        margin = abs(y_hi) * 0.05 or 1.0
    y_lo, y_hi = y_lo - margin, y_hi + margin

    def y_map(value):
        return top + (y_hi - value) / (y_hi - y_lo) * plot_h

    y_ticks = _linear_ticks(y_lo, y_hi)

    def num(value):
        return f"{value:.2f}"

    scale = "log" if use_log else "linear"
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" data-x-scale="{scale}" font-family="{_FONT}" font-size="10">']
    if stride > 1:
        parts.append(f"<!-- aetherpilot.report: decimated stride={stride}, plotting {len(indices)} of {count} points per trace -->")
    if len(signals) > _MAX_TRACES:
        parts.append(f"<!-- aetherpilot.report: plotting first {_MAX_TRACES} of {len(signals)} signals -->")
    if title:
        parts.append(f'<text x="{num(width / 2)}" y="18" text-anchor="middle" font-size="13" font-weight="bold" fill="#1c1e21">{_esc(title)}</text>')
    parts.append(f'<rect x="{num(left)}" y="{num(top)}" width="{num(plot_w)}" height="{num(plot_h)}" fill="#ffffff" stroke="#9aa3b2" stroke-width="1"/>')
    for value in x_ticks:
        x = x_map(value)
        parts.append(f'<line x1="{num(x)}" y1="{num(top)}" x2="{num(x)}" y2="{num(top + plot_h)}" stroke="#e3e6ec" stroke-width="1"/>')
        parts.append(f'<text x="{num(x)}" y="{num(top + plot_h + 14)}" text-anchor="middle" fill="#4a5160">{_esc(_engineering(value))}</text>')
    for value in y_ticks:
        y = y_map(value)
        parts.append(f'<line x1="{num(left)}" y1="{num(y)}" x2="{num(left + plot_w)}" y2="{num(y)}" stroke="#e3e6ec" stroke-width="1"/>')
        parts.append(f'<text x="{num(left - 6)}" y="{num(y + 3)}" text-anchor="end" fill="#4a5160">{_esc(_engineering(value))}</text>')
    axis_title = f"{axis['name']}[{axis['unit']}]" if axis["unit"] else axis["name"]
    if axis_title:
        parts.append(f'<text x="{num(left + plot_w / 2)}" y="{num(height - 8)}" text-anchor="middle" fill="#1c1e21">{_esc(axis_title)}</text>')
    units = {trace["unit"] for trace in traces}
    if len(units) == 1 and next(iter(units)):
        cx, cy = 12.0, top + plot_h / 2
        parts.append(f'<text x="{num(cx)}" y="{num(cy)}" text-anchor="middle" fill="#1c1e21" transform="rotate(-90 {num(cx)} {num(cy)})">[{_esc(next(iter(units)))}]</text>')
    for index, trace in enumerate(traces):
        values = trace["values"]
        points = " ".join(f"{num(x_map(x))},{num(y_map(values[i]))}" for x, i in zip(xs, indices))
        parts.append(f'<polyline fill="none" stroke="{_PALETTE[index % len(_PALETTE)]}" stroke-width="1.5" points="{points}"/>')
    label_width = max(len(trace["name"]) for trace in traces)
    legend_w, legend_h = label_width * 6.4 + 30, len(traces) * 14 + 8
    legend_x, legend_y = left + plot_w - legend_w - 6, top + 6
    parts.append(f'<rect x="{num(legend_x)}" y="{num(legend_y)}" width="{num(legend_w)}" height="{num(legend_h)}" fill="#ffffff" fill-opacity="0.85" stroke="#c3c9d4" stroke-width="1"/>')
    for index, trace in enumerate(traces):
        entry_y = legend_y + 12 + index * 14
        parts.append(f'<line x1="{num(legend_x + 6)}" y1="{num(entry_y - 3)}" x2="{num(legend_x + 22)}" y2="{num(entry_y - 3)}" stroke="{_PALETTE[index % len(_PALETTE)]}" stroke-width="2"/>')
        parts.append(f'<text x="{num(legend_x + 26)}" y="{num(entry_y)}" fill="#1c1e21">{_esc(trace["name"])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _compact(value):
    """Render an arbitrary JSON-ish value as short report text."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sketch_num(value):
    return f"{value:.2f}"


def schematic_sketch_svg(readback, *, width=760, height=480, title=None):
    """Render a wire-level schematic sketch (instances as labeled boxes) to SVG.

    The sketch is drawn from readback geometry only (instance origins,
    pin positions, wire polylines); it illustrates connectivity and is not a
    rendering of the native symbol graphics.
    """
    num = _sketch_num
    if not isinstance(readback, dict):
        raise ValidationError("Schematic sketch requires a readback object", stage="report", code="invalid_schematic")
    instances = [item for item in readback.get("instances", []) if isinstance(item, dict)]
    wires = [item for item in readback.get("wires", []) if isinstance(item, dict)]
    pins = [item for item in readback.get("pins", []) if isinstance(item, dict)]
    points = []
    for wire in wires:
        for point in wire.get("points", []):
            if isinstance(point, list) and len(point) == 2 and all(isinstance(v, (int, float)) and math.isfinite(v) for v in point):
                points.append((float(point[0]), float(point[1])))
    for pin in pins:
        position = pin.get("position")
        if isinstance(position, list) and len(position) == 2:
            points.append((float(position[0]), float(position[1])))
    for instance in instances:
        origin = instance.get("origin")
        if isinstance(origin, list) and len(origin) == 2:
            points.append((float(origin[0]), float(origin[1])))
            points.append((float(origin[0]) + 1.5, float(origin[1]) + 0.75))
            points.append((float(origin[0]) - 0.25, float(origin[1]) - 0.75))
    if not points:
        raise ValidationError("Schematic sketch has no geometry to draw", stage="report", code="invalid_schematic")
    if title is not None and not isinstance(title, str):
        raise ValidationError("Sketch title must be a string", stage="report", code="invalid_schematic")
    min_x, max_x = min(p[0] for p in points), max(p[0] for p in points)
    min_y, max_y = min(p[1] for p in points), max(p[1] for p in points)
    left, right = 90.0, 24.0
    top = 28.0 if title else 14.0
    bottom = 14.0
    span_x, span_y = max(max_x - min_x, 0.0625), max(max_y - min_y, 0.0625)
    scale = min((width - left - right) / span_x, (height - top - bottom) / span_y)

    def sx(value):
        return left + (value - min_x) * scale

    def sy(value):
        return top + (max_y - value) * scale

    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{num(width)}" height="{num(height)}" '
             f'viewBox="0 0 {num(width)} {num(height)}" role="img" data-kind="schematic-sketch">',
             f'<rect x="0" y="0" width="{num(width)}" height="{num(height)}" fill="#ffffff" stroke="#d0d7de"/>']
    if title:
        parts.append(f'<text x="{num(width / 2)}" y="18" text-anchor="middle" font-size="13" font-weight="bold" fill="#1c1e21">{_esc(title)}</text>')
    for wire in wires:
        pts = [p for p in wire.get("points", []) if isinstance(p, list) and len(p) == 2]
        if len(pts) < 2:
            continue
        path = " ".join(f"{num(sx(p[0]))},{num(sy(p[1]))}" for p in pts)
        parts.append(f'<polyline points="{path}" fill="none" stroke="#4aa3ff" stroke-width="1.4"/>')
    for instance in instances:
        origin = instance.get("origin")
        if not (isinstance(origin, list) and len(origin) == 2):
            continue
        cx, cy = sx(float(origin[0]) + 0.625), sy(float(origin[1]))
        w, h = 1.5 * scale, 1.5 * scale
        master = instance.get("master", {})
        cell = master.get("cell", "") if isinstance(master, dict) else ""
        parts.append(f'<rect x="{num(cx - w / 2)}" y="{num(cy - h / 2)}" width="{num(w)}" height="{num(h)}" '
                     f'fill="#fff8e6" stroke="#b07d2b" stroke-width="1.2"/>')
        parts.append(f'<text x="{num(cx)}" y="{num(cy - 2)}" text-anchor="middle" font-size="11" font-weight="bold" fill="#1c1e21">{_esc(_compact(instance.get("name")))}</text>')
        parts.append(f'<text x="{num(cx)}" y="{num(cy + 11)}" text-anchor="middle" font-size="9" fill="#57606a">{_esc(_compact(cell))}</text>')
    for pin in pins:
        position = pin.get("position")
        if not (isinstance(position, list) and len(position) == 2):
            continue
        px, py = sx(float(position[0])), sy(float(position[1]))
        parts.append(f'<circle cx="{num(px)}" cy="{num(py)}" r="3" fill="#d1242f"/>')
        parts.append(f'<text x="{num(px + 5)}" y="{num(py - 4)}" font-size="9" fill="#d1242f">{_esc(_compact(pin.get("name")))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _images_block(images):
    figures = []
    for item in images:
        figures.append(f'<figure><figcaption>{_esc(item["name"])}（原生画布截图，证据文件经 SHA-256 登记）</figcaption>'
                       f'<img src="{item["data_uri"]}" alt="{_esc(item["name"])}" style="max-width:100%;border:1px solid #d5d9e0"/></figure>')
    return "".join(figures)


def _kv_block(title, mapping):
    rows = "".join(f"<tr><th>{_esc(key)}</th><td>{_esc(_compact(value))}</td></tr>" for key, value in mapping.items())
    return f"<h3>{_esc(title)}</h3><table class=\"kv\"><tbody>{rows}</tbody></table>"


def _spec_block(spec_results):
    counts = {"pass": 0, "near": 0, "fail": 0}
    rows = []
    for item in spec_results:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if status in counts:
            counts[status] += 1
        status_text = {"pass": "通过", "near": "临界", "fail": "失败"}.get(status, _compact(status))
        value = item.get("value")
        value_text = _engineering(value) if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) else _compact(value)
        rows.append("<tr>"
                    f"<td>{_esc(_compact(item.get('output')))}</td>"
                    f"<td>{_esc(_compact(item.get('test')))}</td>"
                    f"<td>{_esc(_compact(item.get('corner')))}</td>"
                    f"<td>{_esc(_compact(item.get('analysis')))}</td>"
                    f"<td>{_esc(_compact(item.get('expression')))}</td>"
                    f"<td>{_esc(_compact(item.get('spec')))}</td>"
                    f"<td class=\"num\">{_esc(value_text)}</td>"
                    f"<td class=\"{_esc(status if status in counts else '')}\">{_esc(status_text)}</td>"
                    "</tr>")
    summary = f"共 {len(rows)} 项判定：通过 {counts['pass']}，临界 {counts['near']}，失败 {counts['fail']}。"
    header = "<thead><tr><th>输出</th><th>测试</th><th>角点</th><th>分析</th><th>表达式</th><th>规格</th><th>数值</th><th>判定</th></tr></thead>"
    return f"<p>{_esc(summary)}</p><table>{header}<tbody>{''.join(rows)}</tbody></table>"


def _findings_block(findings):
    rows = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        classification = item.get("classification")
        objects = item.get("object_path")
        objects_text = ", ".join(str(value) for value in objects) if isinstance(objects, list) else _compact(objects)
        rows.append("<tr>"
                    f"<td>{_esc(_compact(item.get('rule_id')))}</td>"
                    f"<td class=\"{_esc(classification if classification in {'suspicion', 'unconfirmed'} else '')}\">{_esc(_compact(classification))}</td>"
                    f"<td>{_esc(objects_text)}</td>"
                    f"<td>{_esc(_compact(item.get('explanation')))}</td>"
                    f"<td>{_esc(_compact(item.get('recommendation')))}</td>"
                    f"<td>{_esc(_compact(item.get('evidence')))}</td>"
                    "</tr>")
    header = "<thead><tr><th>规则</th><th>分类</th><th>对象</th><th>说明</th><th>建议</th><th>证据</th></tr></thead>"
    return f"<p>共 {len(rows)} 项记录。</p><table>{header}<tbody>{''.join(rows)}</tbody></table>"


def _lint_block(lint):
    summary = lint.get("summary", {}) if isinstance(lint.get("summary"), dict) else {}
    verdict = "通过" if lint.get("ok") else "未通过"
    rows = []
    for item in lint.get("findings", []):
        if not isinstance(item, dict):
            continue
        severity = item.get("severity")
        rows.append("<tr>"
                    f"<td>{_esc(_compact(item.get('rule_id')))}</td>"
                    f"<td class=\"{_esc(severity if severity in {'violation', 'warning'} else '')}\">{_esc(_compact(severity))}</td>"
                    f"<td>{_esc(_compact(item.get('object')))}</td>"
                    f"<td>{_esc(_compact(item.get('message')))}</td>"
                    f"<td>{_esc(_compact(item.get('evidence')))}</td>"
                    "</tr>")
    head = (f"<p>风格 { _esc(_compact(lint.get('style'))) }：{verdict}——规则 {summary.get('rules', 0)} 条，"
            f"违例 {summary.get('violations', 0)}，警告 {summary.get('warnings', 0)}；"
            f"检查对象：实例 {summary.get('instances', 0)}、引脚 {summary.get('pins', 0)}、线段 {summary.get('wires', 0)}。</p>")
    if not rows:
        return head + "<p>无违例或警告。</p>"
    header = "<thead><tr><th>规则</th><th>级别</th><th>对象</th><th>说明</th><th>证据</th></tr></thead>"
    return head + f"<table>{header}<tbody>{''.join(rows)}</tbody></table>"


def _evidence_block(evidence):
    rows = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        size = item.get("size", item.get("bytes"))
        rows.append(f"<tr><td>{_esc(_compact(item.get('path')))}</td>"
                    f"<td class=\"num\">{_esc(_compact(size))}</td>"
                    f"<td class=\"sha\">{_esc(_compact(item.get('sha256')))}</td></tr>")
    header = "<thead><tr><th>文件</th><th>大小 (字节)</th><th>SHA-256</th></tr></thead>"
    return f"<table>{header}<tbody>{''.join(rows)}</tbody></table>"


def _waveform_figure(result):
    analysis = result.get("analysis", "") if isinstance(result, dict) else ""
    svg = waveform_svg(result, title=f"{analysis} 波形" if isinstance(analysis, str) and analysis else None)
    details = []
    if isinstance(result.get("row_count"), int):
        details.append(f"{result['row_count']} 点")
    source = result.get("source")
    if isinstance(source, dict):
        for key in ("test", "corner"):
            if source.get(key):
                details.append(f"{key}={source[key]}")
    fingerprint = result.get("content_sha256")
    if isinstance(fingerprint, str) and fingerprint:
        details.append(f"SHA-256 {fingerprint[:16]}…")
    names = ", ".join(item["name"] for item in result.get("signals", [])[:_MAX_TRACES] if isinstance(item, dict) and isinstance(item.get("name"), str))
    caption = " · ".join([f"分析 {analysis}" if analysis else "波形", *details])
    if names:
        caption += f" · 信号 {names}"
    return f"<figure><figcaption>{_esc(caption)}</figcaption>{svg}</figure>"


def _render_document(*, title, summary, waveforms, spec_results, findings, evidence, provenance, schematic=None, lint=None, images=None):
    sections = []
    parts = ["<!DOCTYPE html>", '<html lang="zh-CN">', "<head>", '<meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             f"<title>{_esc(title)}</title>", f"<style>{_STYLE}</style>", "</head>", "<body>",
             f"<header><h1>{_esc(title)}</h1></header>", "<main>"]
    if provenance or summary:
        blocks = (_kv_block("来源", provenance) if provenance else "") + (_kv_block("摘要", summary) if summary else "")
        parts.append(f'<section id="overview"><h2>概览</h2>{blocks}</section>')
        sections.append("overview")
    if schematic is not None:
        sketch = schematic_sketch_svg(schematic)
        parts.append(f'<section id="schematic"><h2>电路结构</h2><figure><figcaption>由回读几何离线渲染的线级草图，仅示意连接关系与器件身份，不代表原生符号图形。</figcaption>{sketch}</figure></section>')
        sections.append("schematic")
    if images:
        parts.append(f'<section id="images"><h2>设计图</h2>{_images_block(images)}</section>')
        sections.append("images")
    if spec_results:
        parts.append(f'<section id="specs"><h2>规格判定</h2>{_spec_block(spec_results)}</section>')
        sections.append("specs")
    if waveforms:
        figures = "".join(_waveform_figure(item) for item in waveforms)
        parts.append(f'<section id="waveforms"><h2>波形</h2>{figures}</section>')
        sections.append("waveforms")
    if findings:
        parts.append(f'<section id="findings"><h2>审查发现</h2>{_findings_block(findings)}</section>')
        sections.append("findings")
    if lint:
        parts.append(f'<section id="lint"><h2>可读性检查</h2>{_lint_block(lint)}</section>')
        sections.append("lint")
    if evidence:
        parts.append(f'<section id="evidence"><h2>证据清单</h2>{_evidence_block(evidence)}</section>')
        sections.append("evidence")
    parts.append("</main>")
    parts.append("<footer><p>本报告由 AetherPilot 离线生成，仅呈现已记录的证据（波形、规格判定、审查发现与文件摘要），不构成电路签核结论。</p></footer>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts) + "\n", sections


def render_report_html(*, title, summary=None, waveforms=None, spec_results=None, findings=None, evidence=None, provenance=None, schematic=None, lint=None, images=None):
    """Render a complete self-contained HTML evidence report (Chinese UI).

    Sections appear in a fixed order — 概览, 电路结构(可选), 规格判定, 波形, 审查发现,
    证据清单 — and any input that is absent or empty simply omits its section.  Every
    interpolated value is HTML-escaped; invalid waveforms fail closed with
    ``ValidationError(code="invalid_waveform")``.
    """
    if not isinstance(title, str) or not title:
        raise ValidationError("Report title must be a nonempty string", stage="report", code="invalid_report_input")
    for name, value, kind in (("summary", summary, dict), ("provenance", provenance, dict),
                              ("waveforms", waveforms, list), ("spec_results", spec_results, list),
                              ("findings", findings, list), ("evidence", evidence, list), ("lint", lint, dict),
                              ("images", images, list)):
        if value is not None and not isinstance(value, kind):
            raise ValidationError(f"Report field {name} has an invalid type", stage="report", code="invalid_report_input")
    document, _ = _render_document(title=title, summary=summary, waveforms=waveforms, spec_results=spec_results,
                                   findings=findings, evidence=evidence, provenance=provenance, schematic=schematic, lint=lint,
                                   images=images)
    return document


def _read_json(path, *, max_bytes=_MAX_JSON_BYTES):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise ValueError("unsafe or missing JSON input")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicate_keys)


def _load_waveform_entry(entry, directory, warnings):
    """Resolve one readback waveforms[] reference to its schema-v1 dict, or skip."""
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
        warnings.append("a waveforms entry without a usable path was skipped")
        return None
    path = Path(entry["path"])
    if not path.is_absolute():
        path = directory / path
    try:
        bundle = _read_json(path, max_bytes=_MAX_WAVEFORM_FILE_BYTES)
    except (OSError, ValueError):
        warnings.append(f"waveform evidence is unavailable and was skipped: {entry['path']}")
        return None
    waveform = bundle.get("waveform") if isinstance(bundle, dict) else None
    if waveform is None and isinstance(bundle, dict) and isinstance(bundle.get("axis"), dict) and isinstance(bundle.get("signals"), list):
        waveform = bundle
    if not isinstance(waveform, dict):
        warnings.append(f"waveform evidence has no waveform object and was skipped: {entry['path']}")
        return None
    try:
        _checked_waveform(waveform)
    except ValidationError:
        warnings.append(f"stored waveform failed validation and was skipped: {entry['path']}")
        return None
    return waveform


def render_operation_report(artifact_root, operation_id, output_path, *, extra_waveforms=None):
    """Render the evidence of one sealed operation directory into an HTML file.

    Reads ``<artifact_root>/<operation_id>/result.json`` (which must exist and
    carry ``ok=True``), collects stored Maestro waveforms referenced from
    ``result["readback"]["waveforms"]``, the optional sibling ``review.json``
    findings and ``manifest.json`` evidence list, and writes a single HTML
    report to ``output_path``.  The output parent directory must already exist
    and the output file is never overwritten.
    """
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise ValidationError("Operation ID must be a 32-character lowercase hex digest", stage="report", code="invalid_report_input")
    root = Path(artifact_root).expanduser().absolute()
    directory = root / operation_id
    try:
        result = _read_json(directory / "result.json")
    except (OSError, ValueError) as exc:
        raise ValidationError("Operation result.json is missing or unreadable", stage="report", code="invalid_report_input") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ValidationError("Only a successfully completed operation can be reported", stage="report", code="invalid_report_input")
    if result.get("operation_id") not in (None, operation_id):
        raise ValidationError("Operation result identity differs from the request", stage="report", code="invalid_report_input")
    output = Path(output_path).expanduser()
    if not output.parent.is_dir():
        raise ValidationError("Report output directory does not exist", stage="report", code="invalid_report_input")
    if output.exists():
        raise ValidationError("Report output already exists", stage="report", code="target_exists")
    warnings = []
    readback = result.get("readback") if isinstance(result.get("readback"), dict) else {}
    waveforms = []
    entries = readback.get("waveforms")
    if isinstance(entries, list):
        for entry in entries[:_MAX_OPERATION_WAVEFORMS]:
            waveform = _load_waveform_entry(entry, directory, warnings)
            if waveform is not None:
                waveforms.append(waveform)
        if len(entries) > _MAX_OPERATION_WAVEFORMS:
            warnings.append(f"only the first {_MAX_OPERATION_WAVEFORMS} of {len(entries)} waveform references were rendered")
    if extra_waveforms is not None:
        if not isinstance(extra_waveforms, list):
            raise ValidationError("extra_waveforms must be a list of schema-v1 waveforms", stage="report", code="invalid_report_input")
        waveforms.extend(extra_waveforms)
    findings = None
    try:
        review = _read_json(directory / "review.json")
    except (OSError, ValueError):
        review = None
        if (directory / "review.json").exists():
            warnings.append("review.json could not be parsed and was skipped")
    if isinstance(review, dict):
        if isinstance(review.get("findings"), list):
            findings = review["findings"]
        else:
            warnings.append("review.json contains no findings list and was skipped")
    evidence = None
    try:
        manifest = _read_json(directory / "manifest.json")
    except (OSError, ValueError):
        manifest = None
        if (directory / "manifest.json").exists():
            warnings.append("manifest.json could not be parsed and was skipped")
    files = manifest.get("files") if isinstance(manifest, dict) else manifest
    if isinstance(files, list):
        evidence = files
    elif manifest is not None:
        warnings.append("manifest.json contains no file list and was skipped")
    spec_results = readback.get("spec_results") if isinstance(readback.get("spec_results"), list) else None
    lint = None
    if isinstance(readback.get("findings"), list) and isinstance(readback.get("rules"), list):
        lint = readback  # schematic.lint operations carry their report in the readback
    else:
        try:
            lint_sidecar = _read_json(directory / "lint.json")
        except (OSError, ValueError):
            lint_sidecar = None
        if isinstance(lint_sidecar, dict) and isinstance(lint_sidecar.get("findings"), list):
            lint = lint_sidecar
    schematic = None
    for name in ("simulation-readback.json", "readback.json"):
        try:
            candidate = _read_json(directory / name)
        except (OSError, ValueError):
            continue
        if isinstance(candidate, dict) and candidate.get("instances"):
            schematic = candidate
            break
    if schematic is not None:
        try:
            schematic_sketch_svg(schematic)
        except ValidationError:
            warnings.append("stored readback could not be sketched and was skipped")
            schematic = None
    images = None
    screenshots_dir = directory / "screenshots"
    if screenshots_dir.is_dir():
        images = []
        for shot in sorted(screenshots_dir.glob("*.png"))[:4]:
            if shot.is_symlink() or not shot.is_file():
                continue
            size = shot.stat().st_size
            if size > 2 * 1024 * 1024:
                warnings.append(f"screenshot {shot.name} exceeds the inline size budget and was skipped")
                continue
            images.append({"name": shot.name, "data_uri": "data:image/png;base64," + base64.b64encode(shot.read_bytes()).decode("ascii")})
        if not images:
            images = None
    summary = {"operation": result.get("operation", ""), "operation_id": operation_id, "stage": result.get("stage", "")}
    for key in ("action", "history", "overall_spec"):
        if readback.get(key) not in (None, ""):
            summary[key] = readback[key]
    if isinstance(readback.get("tests"), list) and readback["tests"]:
        summary["tests"] = ", ".join(str(item) for item in readback["tests"])
    provenance = {"artifact_root": str(root), "operation_id": operation_id, "generated_at": timestamp(),
                  "result_sha256": hashlib.sha256((directory / "result.json").read_bytes()).hexdigest()}
    if warnings:
        provenance["warnings"] = "; ".join(warnings)
    title = f"AetherPilot 证据报告 · {result.get('operation', 'operation')} · {operation_id[:12]}"
    document, sections = _render_document(title=title, summary=summary, provenance=provenance,
                                          waveforms=waveforms or None, spec_results=spec_results,
                                          findings=findings or None, evidence=evidence or None, schematic=schematic, lint=lint,
                                          images=images)
    try:
        with output.open("x", encoding="utf-8", newline="") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ValidationError("Report output already exists", stage="report", code="target_exists") from exc
    except OSError as exc:
        raise ValidationError("Report output cannot be written", stage="report", code="invalid_report_input") from exc
    return {"path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "sections": sections}


__all__ = ["render_operation_report", "render_report_html", "schematic_sketch_svg", "waveform_svg"]
