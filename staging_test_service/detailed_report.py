#!/usr/bin/env python3
"""Render report_detailed.html from the detailed locust run artifacts.

Standalone post-processor (stdlib + plotly, no pandas). Reads the files the
detailed locust job writes into a results dir with csv prefix "run":
run_timeseries.csv is required; run_users.csv, run_meta.json,
run_deployments.json, run_failures.csv and run_stats.csv are optional and
degrade gracefully. Usage: python detailed_report.py <results_dir> [--out NAME].
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import plotly.graph_objects as go
import plotly.io as pio
from plotly.offline import get_plotlyjs
from plotly.subplots import make_subplots


# Design tokens (project dataviz method). Defined once here, injected into the
# page CSS custom properties and referenced by every figure.
PAGE_BG = "#f9f9f7"  # page plane
SURFACE = "#fcfcfb"  # chart surface
INK = "#0b0b0b"  # primary ink
INK_2 = "#52514e"  # secondary ink
MUTED = "#898781"
GRID = "#e1e0d9"  # hairline grid
BASELINE = "#c3c2b7"  # zero line
BORDER = "rgba(11,11,11,0.10)"  # hairline border
FONT_FAMILY = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# Percentile ramp, one hue light to dark. p99.9 and max are hover-only.
PCTL_LINES = (
    ("p50", "#86b6ef"),
    ("p90", "#3987e5"),
    ("p95", "#1c5cab"),
    ("p99", "#0d366b"),
)
RPS_COLOR = "#1baf7a"
USERS_COLOR = "#2a78d6"
FAIL_COLOR = "#d03b3b"  # reserved status color
# Fixed categorical order for the mux drill-down, never cycled past 8 models.
MUX_COLORS = (
    "#2a78d6", "#1baf7a", "#eda100", "#008300",
    "#4a3aa7", "#e34948", "#e87ba4", "#eb6834",
)
SPIKE_FILL = "rgba(237,161,0,0.10)"

# Drill-down keys in the timeseries; excluded from deployment sections/totals.
MUX_MODEL_PREFIX = "/mux/ ["


@dataclass(frozen=True)
class Bucket:
    elapsed_min: float
    interval_s: float
    requests: int
    failures: int
    rps: Optional[float]
    p50: Optional[float]
    p90: Optional[float]
    p95: Optional[float]
    p99: Optional[float]
    p999: Optional[float]
    max_ms: Optional[float]


def _opt_float(value: Optional[str]) -> Optional[float]:
    """Numeric CSV cell; empty means None (no data in the bucket)."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _as_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Optional[str]) -> int:
    """Integer CSV cell; empty, non-numeric, or non-finite (nan/inf) -> 0."""
    parsed = _opt_float(value)
    if parsed is None or not math.isfinite(parsed):
        return 0
    return int(parsed)


def _load_timeseries(path: Path) -> Dict[str, List[Bucket]]:
    series: Dict[str, List[Bucket]] = {}
    skipped = 0
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            key = (row.get("key") or "").strip()
            elapsed_s = _opt_float(row.get("elapsed_s"))
            if not key or elapsed_s is None:
                skipped += 1
                continue
            series.setdefault(key, []).append(
                Bucket(
                    elapsed_min=elapsed_s / 60.0,
                    interval_s=_opt_float(row.get("interval_s")) or 15.0,
                    requests=_opt_int(row.get("requests")),
                    failures=_opt_int(row.get("failures")),
                    rps=_opt_float(row.get("rps")),
                    p50=_opt_float(row.get("p50_ms")),
                    p90=_opt_float(row.get("p90_ms")),
                    p95=_opt_float(row.get("p95_ms")),
                    p99=_opt_float(row.get("p99_ms")),
                    p999=_opt_float(row.get("p999_ms")),
                    max_ms=_opt_float(row.get("max_ms")),
                )
            )
    if skipped:
        print(f"{path.name}: skipped {skipped} malformed rows", file=sys.stderr)
    for buckets in series.values():
        buckets.sort(key=lambda b: b.elapsed_min)
    return series


def _load_users(path: Path) -> List[Tuple[float, Optional[float], Optional[float]]]:
    if not path.is_file():
        return []
    rows: List[Tuple[float, Optional[float], Optional[float]]] = []
    skipped = 0
    try:
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                elapsed_s = _opt_float(row.get("elapsed_s"))
                if elapsed_s is None:
                    skipped += 1
                    continue
                rows.append(
                    (
                        elapsed_s / 60.0,
                        _opt_float(row.get("user_count")),
                        _opt_float(row.get("target_user_count")),
                    )
                )
    except Exception as exc:
        print(f"{path.name}: {exc}", file=sys.stderr)
    if skipped:
        print(f"{path.name}: skipped {skipped} malformed rows", file=sys.stderr)
    rows.sort(key=lambda r: r[0])
    return rows


def _load_json_dict(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text())
    except Exception as exc:
        print(f"{path.name}: {exc}", file=sys.stderr)
        return {}
    return raw if isinstance(raw, dict) else {}


def _load_failures(path: Path) -> Dict[str, List[Tuple[str, int]]]:
    """Group locust's failures CSV (Method,Name,Error,Occurrences) by
    deployment, merging identical error texts across request names."""
    if not path.is_file():
        return {}
    grouped: Dict[str, Dict[str, int]] = {}
    try:
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("Name") or "").strip()
                error = (row.get("Error") or "").strip()
                if not name or not error:
                    continue
                dep = grouped.setdefault(name.split(" [", 1)[0], {})
                dep[error] = dep.get(error, 0) + int(_opt_float(row.get("Occurrences")) or 0)
    except Exception as exc:
        print(f"{path.name}: {exc}", file=sys.stderr)
    return {
        key: sorted(errors.items(), key=lambda kv: (-kv[1], kv[0]))
        for key, errors in grouped.items()
    }


def _load_stats_totals(path: Path) -> Dict[str, Tuple[int, int]]:
    """Fallback per-deployment totals from locust's stats CSV. Counts merge
    exactly across request names (percentiles do not, so only counts)."""
    if not path.is_file():
        return {}
    totals: Dict[str, Tuple[int, int]] = {}
    try:
        with path.open(newline="") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("Name") or "").strip()
                if not name or name.lower() == "aggregated":
                    continue
                key = name.split(" [", 1)[0]
                requests, failures = totals.get(key, (0, 0))
                totals[key] = (
                    requests + int(_opt_float(row.get("Request Count")) or 0),
                    failures + int(_opt_float(row.get("Failure Count")) or 0),
                )
    except Exception as exc:
        print(f"{path.name}: {exc}", file=sys.stderr)
        return {}
    return totals


def _embed_native_report(results_dir: Path, out_path: Path) -> Optional[str]:
    """Embed Locust's self-contained native report.html as a collapsible
    section via an isolated iframe (srcdoc), so the combined output is a single
    file with no CSS/JS collision between Locust's charts and plotly. Returns
    None when the native report is absent or is the file we are about to write."""
    path = results_dir / "report.html"
    if not path.is_file() or path.resolve() == out_path.resolve():
        return None
    try:
        raw = path.read_text()
    except Exception as exc:
        print(f"{path.name}: {exc}", file=sys.stderr)
        return None
    # srcdoc holds an HTML document as an attribute value: only & and " must be
    # escaped. The native report is self-contained, so nothing is fetched.
    srcdoc = raw.replace("&", "&amp;").replace('"', "&quot;")
    return (
        '<section><details><summary>Locust native summary report</summary>'
        f'<iframe srcdoc="{srcdoc}" title="Locust native report" '
        'style="width:100%;height:900px;border:1px solid var(--border);'
        'border-radius:6px;margin-top:10px"></iframe>'
        "</details></section>"
    )


def _fmt_ms(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.0f} ms"


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _truncate(text: str, limit: int = 160) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _style_figure(fig: go.Figure, height: int) -> None:
    fig.update_layout(
        font=dict(family=FONT_FAMILY, size=12, color=INK),
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        hovermode="x unified",
        hoverlabel=dict(
            bgcolor="#ffffff",
            bordercolor=GRID,
            font=dict(family=FONT_FAMILY, size=11, color=INK),
        ),
        legend=dict(
            orientation="h", x=0, xanchor="left", y=1.02, yanchor="bottom",
            font=dict(size=11, color=INK_2),
        ),
        margin=dict(l=64, r=16, t=36, b=44),
        height=height,
    )
    fig.update_xaxes(
        gridcolor=GRID, gridwidth=1, zeroline=False, showline=False,
        tickfont=dict(size=11, color=INK_2), title_font=dict(size=11, color=INK_2),
        hoverformat=".1f",
    )
    fig.update_yaxes(
        gridcolor=GRID, gridwidth=1, zerolinecolor=BASELINE, zerolinewidth=1,
        rangemode="tozero", tickfont=dict(size=11, color=INK_2),
        title_font=dict(size=11, color=INK_2),
    )


def _decorate_time_axis(fig: go.Figure, meta: dict) -> None:
    """Spike-window bands on every row, plus thin phase boundary lines."""
    run_min = _as_float(meta.get("run_minutes"))
    if run_min:
        for phase in meta.get("phases") or []:
            try:
                start_h = float(phase[0])
            except (TypeError, ValueError, IndexError):
                continue
            if start_h <= 0:
                continue
            fig.add_vline(
                x=start_h / 24.0 * run_min, line_width=1, line_color=GRID,
                layer="below", row="all", col="all",
            )
    for window in meta.get("spike_windows_elapsed_s") or []:
        try:
            x0 = float(window["start"]) / 60.0
            x1 = float(window["end"]) / 60.0
        except (TypeError, ValueError, KeyError):
            continue
        fig.add_vrect(
            x0=x0, x1=x1, fillcolor=SPIKE_FILL, line_width=0,
            layer="below", row="all", col="all",
        )
        boost = _as_float(window.get("boost"))
        fig.add_annotation(
            x=x0, xref="x", xanchor="left", y=1, yref="y domain", yanchor="top",
            text=f"{boost:g}x spike" if boost else "spike",
            showarrow=False, font=dict(size=10, color=MUTED),
        )


def _line_mode(x: List[float]) -> str:
    # Markers only when the series is too short for a visible line.
    return "lines" if len(x) > 2 else "lines+markers"


_MARKER = dict(size=6, line=dict(width=2, color=SURFACE))


def _deployment_figure(buckets: List[Bucket], meta: dict) -> go.Figure:
    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True,
        row_heights=[0.55, 0.25, 0.20], vertical_spacing=0.05,
    )
    x = [b.elapsed_min for b in buckets]
    mode = _line_mode(x)
    hover_extra = [[_fmt_ms(b.p999), _fmt_ms(b.max_ms), f"{b.requests:,}"] for b in buckets]
    for label, color in PCTL_LINES:
        if label == "p99":
            extra = dict(
                customdata=hover_extra,
                hovertemplate=(
                    "p99 %{y:,.0f} ms<br>p99.9 %{customdata[0]}, "
                    "max %{customdata[1]}, requests %{customdata[2]}<extra></extra>"
                ),
            )
        else:
            extra = dict(hovertemplate=label + " %{y:,.0f} ms<extra></extra>")
        fig.add_trace(
            go.Scatter(
                x=x, y=[getattr(b, label) for b in buckets], name=label,
                mode=mode, marker=_MARKER, line=dict(width=2, color=color),
                connectgaps=False, **extra,
            ),
            row=1, col=1,
        )
    fig.add_trace(
        go.Scatter(
            x=x, y=[b.rps for b in buckets], name="rps", mode=mode,
            marker=_MARKER, line=dict(width=2, color=RPS_COLOR),
            connectgaps=False, hovertemplate="rps %{y:,.2f}<extra></extra>",
        ),
        row=2, col=1,
    )
    fig.add_trace(
        go.Bar(
            x=x, y=[b.failures for b in buckets], name="failures",
            marker_color=FAIL_COLOR, marker_line_width=0,
            width=[b.interval_s / 60.0 * 0.85 for b in buckets],
            hovertemplate="failures %{y:,}<extra></extra>",
        ),
        row=3, col=1,
    )
    fig.update_yaxes(title_text="latency (ms)", row=1, col=1)
    fig.update_yaxes(title_text="rps", row=2, col=1)
    fig.update_yaxes(title_text="failures", row=3, col=1)
    fig.update_xaxes(title_text="elapsed (min)", row=3, col=1)
    _style_figure(fig, height=560)
    _decorate_time_axis(fig, meta)
    return fig


def _bucket_totals(
    series: Dict[str, List[Bucket]],
) -> Tuple[List[float], List[float], List[int], List[float]]:
    """Per-bucket totals summed over deployment keys only (mux model keys
    excluded, they would double count)."""
    agg: Dict[float, List[float]] = {}
    for key, buckets in series.items():
        if key.startswith(MUX_MODEL_PREFIX):
            continue
        for b in buckets:
            g = agg.setdefault(round(b.elapsed_min, 6), [0.0, 0, b.interval_s])
            g[0] += b.rps or 0.0
            g[1] += b.failures
    xs = sorted(agg)
    return (
        xs,
        [agg[x][0] for x in xs],
        [int(agg[x][1]) for x in xs],
        [agg[x][2] / 60.0 * 0.85 for x in xs],
    )


def _overview_figure(
    users: List[Tuple[float, Optional[float], Optional[float]]],
    series: Dict[str, List[Bucket]],
    meta: dict,
) -> go.Figure:
    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True,
        row_heights=[0.45, 0.30, 0.25], vertical_spacing=0.05,
    )
    if users:
        ux = [r[0] for r in users]
        fig.add_trace(
            go.Scatter(
                x=ux, y=[r[1] for r in users], name="users",
                mode="lines", line=dict(width=2, color=USERS_COLOR),
                connectgaps=False, hovertemplate="users %{y:,.0f}<extra></extra>",
            ),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=ux, y=[r[2] for r in users], name="target users", mode="lines",
                line=dict(width=2, color=MUTED, dash="dash", shape="hv"),
                connectgaps=False, hovertemplate="target %{y:,.0f}<extra></extra>",
            ),
            row=1, col=1,
        )
    xs, rps_totals, fail_totals, widths = _bucket_totals(series)
    fig.add_trace(
        go.Scatter(
            x=xs, y=rps_totals, name="total rps", mode=_line_mode(xs),
            marker=_MARKER, line=dict(width=2, color=RPS_COLOR),
            connectgaps=False, hovertemplate="total rps %{y:,.2f}<extra></extra>",
        ),
        row=2, col=1,
    )
    fig.add_trace(
        go.Bar(
            x=xs, y=fail_totals, name="failures", marker_color=FAIL_COLOR,
            marker_line_width=0, width=widths,
            hovertemplate="failures %{y:,}<extra></extra>",
        ),
        row=3, col=1,
    )
    fig.update_yaxes(title_text="users", row=1, col=1)
    fig.update_yaxes(title_text="rps", row=2, col=1)
    fig.update_yaxes(title_text="failures", row=3, col=1)
    fig.update_xaxes(title_text="elapsed (min)", row=3, col=1)
    _style_figure(fig, height=520)
    _decorate_time_axis(fig, meta)
    return fig


def _mux_figure(
    model_series: Dict[str, List[Bucket]], meta: dict
) -> Tuple[go.Figure, int]:
    """p99 over time for the top models by requests. Returns (figure,
    number of omitted models)."""
    ranked = sorted(
        model_series.items(),
        key=lambda kv: (-sum(b.requests for b in kv[1]), kv[0]),
    )
    top = ranked[: len(MUX_COLORS)]
    fig = make_subplots(rows=1, cols=1)
    for (key, buckets), color in zip(top, MUX_COLORS):
        label = key.split("[", 1)[1].rstrip("]") if "[" in key else key
        x = [b.elapsed_min for b in buckets]
        fig.add_trace(
            go.Scatter(
                x=x, y=[b.p99 for b in buckets], name=label, mode=_line_mode(x),
                marker=_MARKER, line=dict(width=2, color=color), connectgaps=False,
                hovertemplate=label + " p99 %{y:,.0f} ms<extra></extra>",
            ),
            row=1, col=1,
        )
    fig.update_yaxes(title_text="p99 (ms)", row=1, col=1)
    fig.update_xaxes(title_text="elapsed (min)", row=1, col=1)
    _style_figure(fig, height=360)
    fig.update_layout(showlegend=len(top) > 1)
    _decorate_time_axis(fig, meta)
    return fig, max(0, len(ranked) - len(MUX_COLORS))


def _fig_html(fig: go.Figure) -> str:
    return pio.to_html(
        fig,
        full_html=False,
        include_plotlyjs=False,
        config={"displaylogo": False, "responsive": True},
        default_width="100%",
        default_height=f"{int(fig.layout.height)}px",
    )


@dataclass(frozen=True)
class _SectionInfo:
    key: str
    buckets: List[Bucket]
    requests: int
    failures: int
    badness: float


def _run_totals(
    key: str,
    buckets: List[Bucket],
    run_deps: dict,
    stats_totals: Dict[str, Tuple[int, int]],
) -> Tuple[int, int]:
    dep = run_deps.get(key)
    if isinstance(dep, dict) and dep.get("requests") is not None:
        try:
            return int(dep.get("requests") or 0), int(dep.get("failures") or 0)
        except (TypeError, ValueError):
            pass
    if key in stats_totals:
        return stats_totals[key]
    return sum(b.requests for b in buckets), sum(b.failures for b in buckets)


def _ordered_sections(
    series: Dict[str, List[Bucket]],
    run_deps: dict,
    stats_totals: Dict[str, Tuple[int, int]],
) -> List[_SectionInfo]:
    """Deployment keys ordered by tail badness (max bucket p99 relative to
    the run-level p50) descending; zero-request deployments last."""
    keys = [k for k in series if not k.startswith(MUX_MODEL_PREFIX)]
    keys.extend(
        k for k in run_deps
        if k not in series and not k.startswith(MUX_MODEL_PREFIX)
    )
    infos = []
    for key in keys:
        buckets = series.get(key, [])
        requests, failures = _run_totals(key, buckets, run_deps, stats_totals)
        max_p99 = max((b.p99 for b in buckets if b.p99 is not None), default=0.0)
        dep = run_deps.get(key)
        run_p50 = _as_float(dep.get("p50")) if isinstance(dep, dict) else None
        badness = max_p99 / max(run_p50 or 0.0, 1.0)
        infos.append(_SectionInfo(key, buckets, requests, failures, badness))
    return sorted(infos, key=lambda i: (1 if i.requests == 0 else 0, -i.badness, i.key))


def _percentile_table(dep: dict) -> str:
    labels = ("p50", "p90", "p95", "p99", "p99.9")
    head = "".join(f"<th>{label}</th>" for label in labels)
    cells = "".join(
        f'<td class="num">{_esc(_fmt_ms(_as_float(dep.get(label))))}</td>'
        for label in labels
    )
    return (
        '<div class="table-wrap"><table>'
        f"<thead><tr>{head}</tr></thead>"
        f"<tbody><tr>{cells}</tr></tbody></table></div>"
    )


def _failures_table(rows: List[Tuple[str, int]]) -> str:
    body = "".join(
        f'<tr><td class="err">{_esc(_truncate(error))}</td>'
        f'<td class="num">{count:,}</td></tr>'
        for error, count in rows[:5]
    )
    return (
        '<div class="table-wrap"><table>'
        "<thead><tr><th>Error</th><th>Occurrences</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def _deployment_section(
    info: _SectionInfo,
    meta: dict,
    run_deps: dict,
    failures: Dict[str, List[Tuple[str, int]]],
) -> str:
    parts = [
        f"<h2>{_esc(info.key)}</h2>",
        f'<p class="meta">{info.requests:,} requests, {info.failures:,} failures</p>',
    ]
    dep = run_deps.get(info.key)
    if isinstance(dep, dict):
        parts.append(_percentile_table(dep))
    notes = meta.get("deployment_notes")
    note = notes.get(info.key) if isinstance(notes, dict) else None
    if note:
        parts.append(f'<p class="muted caveat">{_esc(note)}</p>')
    if info.requests == 0:
        parts.append('<p class="muted">no traffic recorded for this deployment during the run</p>')
    elif not info.buckets:
        parts.append('<p class="muted">no per-bucket samples recorded for this deployment</p>')
    else:
        parts.append(f'<div class="fig">{_fig_html(_deployment_figure(info.buckets, meta))}</div>')
    rows = failures.get(info.key)
    if rows:
        parts.append(_failures_table(rows))
    return "<section>" + "".join(parts) + "</section>"


def _header_html(meta: dict) -> str:
    pairs: List[Tuple[str, str]] = []
    for label, key in (
        ("host", "host"),
        ("started", "started_utc"),
        ("finished", "finished_utc"),
        ("run minutes", "run_minutes"),
        ("peak users", "peak_users"),
        ("interval (s)", "interval_s"),
        ("locust", "locust_version"),
    ):
        value = meta.get(key)
        if value is None or value == "":
            continue
        pairs.append((label, str(value)))
    pairs.append(("generated", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
    items = "".join(
        f'<div><span class="k">{_esc(k)}</span><span class="v">{_esc(v)}</span></div>'
        for k, v in pairs
    )
    return (
        "<header><h1>Detailed locust load test report</h1>"
        f'<div class="run-meta">{items}</div></header>'
    )


def _page_css() -> str:
    return f"""
:root {{
  --page: {PAGE_BG};
  --surface: {SURFACE};
  --ink: {INK};
  --ink-2: {INK_2};
  --muted: {MUTED};
  --grid: {GRID};
  --baseline: {BASELINE};
  --border: {BORDER};
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 28px 20px 48px;
  background: var(--page);
  color: var(--ink);
  font-family: {FONT_FAMILY};
  font-size: 14px;
  line-height: 1.45;
}}
main {{ max-width: 1080px; margin: 0 auto; }}
h1 {{ font-size: 22px; font-weight: 600; margin: 0 0 6px; }}
h2 {{ font-size: 16px; font-weight: 600; margin: 0 0 4px; }}
section {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 16px 18px 12px;
  margin: 18px 0;
}}
.run-meta {{
  display: flex;
  flex-wrap: wrap;
  gap: 2px 26px;
  margin: 8px 0 0;
  font-size: 13px;
  color: var(--ink-2);
}}
.run-meta .k {{ color: var(--muted); margin-right: 5px; }}
.run-meta .v {{ font-variant-numeric: tabular-nums; }}
.meta {{ color: var(--ink-2); font-size: 13px; margin: 2px 0 6px; }}
.muted {{ color: var(--muted); }}
.caveat {{ font-size: 13px; margin: 4px 0 8px; }}
.table-wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; font-size: 13px; margin: 6px 0 10px; }}
th, td {{
  text-align: left;
  padding: 3px 18px 3px 0;
  border-bottom: 1px solid var(--grid);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}}
th {{ font-weight: 600; color: var(--ink-2); font-size: 12px; }}
td.num {{ text-align: right; }}
td.err {{ white-space: normal; overflow-wrap: anywhere; min-width: 320px; }}
.fig {{ margin: 8px 0 4px; }}
.fig .xtick text, .fig .ytick text {{ font-variant-numeric: tabular-nums; }}
details > summary {{ cursor: pointer; font-weight: 600; color: var(--ink-2); }}
footer {{ color: var(--muted); font-size: 12px; margin: 26px 0 0; }}
"""


def _render_html(
    series: Dict[str, List[Bucket]],
    users: List[Tuple[float, Optional[float], Optional[float]]],
    meta: dict,
    run_deps: dict,
    failures: Dict[str, List[Tuple[str, int]]],
    stats_totals: Dict[str, Tuple[int, int]],
    native_report_html: Optional[str] = None,
) -> str:
    body: List[str] = [_header_html(meta)]
    if native_report_html:
        body.append(native_report_html)

    has_deployment_rows = any(not k.startswith(MUX_MODEL_PREFIX) for k in series)
    if users or has_deployment_rows:
        overview = _fig_html(_overview_figure(users, series, meta))
        body.append(f'<section><h2>Overview</h2><div class="fig">{overview}</div></section>')
    else:
        body.append('<section><h2>Overview</h2><p class="muted">no samples recorded</p></section>')

    infos = _ordered_sections(series, run_deps, stats_totals)
    if infos:
        for info in infos:
            body.append(_deployment_section(info, meta, run_deps, failures))
    else:
        body.append('<p class="muted">no deployment rows in run_timeseries.csv</p>')

    model_series = {k: v for k, v in series.items() if k.startswith(MUX_MODEL_PREFIX)}
    if model_series:
        fig, omitted = _mux_figure(model_series, meta)
        section = [
            "<section><h2>Mux model drill-down</h2>",
            '<p class="meta">p99 per bucket for the top models by requests</p>',
            f'<div class="fig">{_fig_html(fig)}</div>',
        ]
        if omitted:
            section.append(f'<p class="muted">{omitted} more models omitted</p>')
        section.append("</section>")
        body.append("".join(section))

    body.append(
        "<footer>Percentiles are exact per bucket (histogram diff on the locust "
        "master) and inherit locust's response-time rounding.</footer>"
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>Detailed locust load test report</title>\n"
        f"<style>{_page_css()}</style>\n"
        f"<script>{get_plotlyjs()}</script>\n"
        "</head>\n<body>\n<main>\n" + "\n".join(body) + "\n</main>\n</body>\n</html>\n"
    )


def generate_report(results_dir: Path, out_name: str = "report_detailed.html") -> Path:
    """Render the detailed HTML report into results_dir and return its path.

    Raises FileNotFoundError when run_timeseries.csv is missing; every other
    input is optional.
    """
    results_dir = Path(results_dir)
    timeseries_path = results_dir / "run_timeseries.csv"
    if not timeseries_path.is_file():
        raise FileNotFoundError(f"{timeseries_path} not found")

    series = _load_timeseries(timeseries_path)
    users = _load_users(results_dir / "run_users.csv")
    meta = _load_json_dict(results_dir / "run_meta.json")
    run_deps = _load_json_dict(results_dir / "run_deployments.json")
    failures = _load_failures(results_dir / "run_failures.csv")
    stats_totals = _load_stats_totals(results_dir / "run_stats.csv")

    out_path = results_dir / out_name
    native = _embed_native_report(results_dir, out_path)
    out_path.write_text(
        _render_html(series, users, meta, run_deps, failures, stats_totals, native)
    )
    print(f"Wrote detailed report to {out_path}", flush=True)
    return out_path


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render report_detailed.html from a detailed locust results dir."
    )
    parser.add_argument("results_dir", type=Path, help="dir containing run_timeseries.csv")
    parser.add_argument("--out", default="report_detailed.html", metavar="NAME")
    args = parser.parse_args(argv)
    try:
        generate_report(args.results_dir, args.out)
    except FileNotFoundError as exc:
        print(f"Cannot generate detailed report: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
