"""
Org Scorecard HTML Report Generator.

Produces a self-contained dark-themed HTML report with:
1. Org Overview Scorecard
2. Per-Cluster Tier Cards
3. Trend Arrows (vs previous report)
4. Alert Summary
5. Index Recommendations
"""

import json
from datetime import datetime


def _esc(text) -> str:
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _md_to_html(text: str) -> str:
    """Convert simple markdown to HTML for AI summaries.

    Handles: **bold**, numbered lists, bullet lists, paragraphs.
    Not a full markdown parser -- just enough for LLM output.
    """
    import re

    if not text:
        return ""

    # Escape HTML entities first
    text = _esc(text)

    # Convert **bold** to <strong>
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)

    # Split into lines for block-level processing
    lines = text.split("\n")
    html_parts = []
    in_list = None  # "ol" or "ul" or None
    list_items = []

    def _flush_list():
        nonlocal in_list, list_items
        if in_list and list_items:
            tag = in_list
            items = "".join(f"<li>{item}</li>" for item in list_items)
            html_parts.append(f"<{tag}>{items}</{tag}>")
        in_list = None
        list_items = []

    for line in lines:
        stripped = line.strip()

        # Numbered list: "1. text" or "1) text"
        num_match = re.match(r"^\d+[\.\)]\s+(.+)", stripped)
        if num_match:
            if in_list != "ol":
                _flush_list()
                in_list = "ol"
            list_items.append(num_match.group(1))
            continue

        # Bullet list: "- text" or "* text"
        bullet_match = re.match(r"^[\-\*]\s+(.+)", stripped)
        if bullet_match:
            if in_list != "ul":
                _flush_list()
                in_list = "ul"
            list_items.append(bullet_match.group(1))
            continue

        # Not a list item -- flush any open list
        _flush_list()

        # Empty line = paragraph break
        if not stripped:
            continue

        # Regular paragraph
        html_parts.append(f"<p>{stripped}</p>")

    _flush_list()
    return "\n".join(html_parts)


def generate_html(
    scored_clusters: list,
    comparison: dict,
    collected_at: str,
    ai_summary: str = "",
    activity_events: dict = None,
    red_analyses: dict = None,
) -> str:
    """Generate the full HTML scorecard report."""

    red = [c for c in scored_clusters if c["score"]["tier"] == "RED"]
    yellow = [c for c in scored_clusters if c["score"]["tier"] == "YELLOW"]
    green = [c for c in scored_clusters if c["score"]["tier"] == "GREEN"]
    total = len(scored_clusters)

    # Reliability trend totals
    rel_red = sum(1 for c in scored_clusters if c.get("reliability", {}).get("tier") == "RED")
    rel_yellow = sum(1 for c in scored_clusters if c.get("reliability", {}).get("tier") == "YELLOW")
    rel_green = sum(1 for c in scored_clusters if c.get("reliability", {}).get("tier") == "GREEN")

    has_prev = comparison.get("has_previous", False)
    prev_date = comparison.get("previous_date", "")

    # Org-level alert totals
    total_open_alerts = sum(
        c.get("alerts", {}).get("open_count", 0) for c in scored_clusters
    )
    total_7d_alerts = sum(
        c.get("alerts", {}).get("past_7d_count", 0) for c in scored_clusters
    )
    total_30d_alerts = sum(
        c.get("alerts", {}).get("past_30d_count", 0) for c in scored_clusters
    )
    total_indexes = sum(c.get("suggested_index_count", 0) for c in scored_clusters)

    # Comparison summary
    comp_summary = ""
    if has_prev:
        org = comparison.get("org_summary", {})
        parts = []
        if org.get("improved"):
            parts.append(f"{org['improved']} improved")
        if org.get("degraded"):
            parts.append(f"{org['degraded']} degraded")
        if org.get("stable"):
            parts.append(f"{org['stable']} stable")
        if org.get("new"):
            parts.append(f"{org['new']} new")
        comp_summary = " | ".join(parts)

    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Production Cluster Scorecard</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0a18;color:#e0e0f0;min-height:100vh}}
.container{{max-width:1100px;margin:0 auto;padding:24px 20px}}
h1{{font-size:1.5rem;color:#fff;margin-bottom:4px}}
h2{{font-size:1.15rem;color:#fff;margin-bottom:12px;padding-top:8px}}
.subtitle{{font-size:0.8rem;color:#6b7280;margin-bottom:20px}}
.divider{{border:none;border-top:1px solid #1e1e3a;margin:28px 0}}

/* Score tiles */
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:20px}}
.tile{{background:#1a1a2e;border:1px solid #2a2a4a;border-radius:10px;padding:14px;text-align:center}}
.tile-val{{font-size:1.8rem;font-weight:700}}
.tile-lbl{{font-size:0.65rem;color:#6b7280;text-transform:uppercase;letter-spacing:0.3px;margin-top:2px}}
.tile-sub{{font-size:0.7rem;color:#8888aa;margin-top:2px}}

/* Cluster cards */
.card{{background:#1a1a2e;border:1px solid #2a2a4a;border-radius:10px;padding:16px;margin-bottom:10px;position:relative}}
.card-red{{border-left:4px solid #ef4444}}
.card-yellow{{border-left:4px solid #f59e0b}}
.card-green{{border-left:4px solid #22c55e}}
.card-header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
.card-name{{font-weight:700;font-size:0.95rem;color:#fff}}
.card-meta{{font-size:0.75rem;color:#6b7280}}

/* Badges */
.badge{{display:inline-block;padding:3px 10px;border-radius:12px;font-size:0.7rem;font-weight:700;text-transform:uppercase}}
.badge-red{{background:#ef444422;color:#ef4444;border:1px solid #ef444444}}
.badge-yellow{{background:#f59e0b22;color:#f59e0b;border:1px solid #f59e0b44}}
.badge-green{{background:#22c55e22;color:#22c55e;border:1px solid #22c55e44}}
.badge-new{{background:#6c63ff22;color:#6c63ff;border:1px solid #6c63ff44}}

/* Arrows */
.arrow-improving{{color:#22c55e;font-weight:700}}
.arrow-degrading{{color:#ef4444;font-weight:700}}
.arrow-stable{{color:#6b7280;font-weight:600}}
.arrow-new{{color:#6c63ff;font-weight:600}}

/* Metrics row */
.metrics-row{{display:flex;gap:12px;flex-wrap:wrap;font-size:0.75rem;color:#a0a0c0;margin:8px 0}}
.metric-item{{background:#0f0f22;padding:4px 8px;border-radius:4px}}
.metric-item strong{{color:#fff}}
.metric-warn{{color:#f59e0b}}
.metric-crit{{color:#ef4444}}

/* Justification */
.justification{{font-size:0.8rem;color:#a0a0c0;line-height:1.5;margin-top:6px}}

/* Trend detail */
.trend-detail{{font-size:0.75rem;color:#8888aa;margin-top:4px;font-style:italic}}

/* Alert table */
table{{width:100%;border-collapse:collapse;font-size:0.8rem}}
th{{text-align:left;padding:8px;color:#6b7280;font-weight:600;border-bottom:1px solid #2a2a4a;font-size:0.7rem;text-transform:uppercase}}
td{{padding:8px;border-bottom:1px solid #1a1a2a;color:#a0a0c0}}

/* Index card */
.idx-card{{background:#0f0f22;border:1px solid #2a2a4a;border-radius:8px;padding:10px 12px;margin-bottom:6px}}
.idx-recurring{{border-left:3px solid #f59e0b}}
.idx-new{{border-left:3px solid #22c55e}}

/* AI Summary */
.ai-box{{background:#1a1a2e;border:1px solid #6c63ff44;border-radius:10px;padding:16px;margin-bottom:20px;font-size:0.85rem;line-height:1.7;color:#c0c0e0}}
.ai-box p{{margin:0 0 10px 0}}
.ai-box ol,.ai-box ul{{margin:6px 0 12px 0;padding-left:24px}}
.ai-box li{{margin-bottom:4px}}
.ai-box strong{{color:#e0e0f0}}

/* Index dropdown */
details.idx-toggle{{margin-top:4px}}
details.idx-toggle summary{{font-size:0.7rem;color:#6c63ff;cursor:pointer;list-style:none}}
details.idx-toggle summary::-webkit-details-marker{{display:none}}
details.idx-toggle summary::before{{content:"▶ ";font-size:0.6rem}}
details.idx-toggle[open] summary::before{{content:"▼ "}}
details.idx-toggle .idx-list{{padding:6px 0 2px 0}}
details.idx-toggle .idx-item{{background:#0f0f22;border:1px solid #2a2a4a;border-radius:6px;padding:6px 8px;margin-bottom:4px;font-size:0.7rem}}
details.idx-toggle .idx-item code{{color:#6c63ff;font-size:0.7rem}}
details.idx-toggle .idx-item .idx-meta{{color:#6b7280;font-size:0.65rem;margin-top:2px}}

/* Red cluster AI analysis */
.red-analysis{{background:#1a1a2e;border:1px solid #ef444444;border-left:4px solid #ef4444;border-radius:8px;padding:14px 16px;margin:4px 0 12px 0;font-size:0.8rem;line-height:1.7;color:#c0c0e0}}
.red-analysis .diagnosis{{margin-bottom:10px}}
.red-analysis .recommendations{{margin:0;padding-left:20px}}
.red-analysis .recommendations li{{margin-bottom:4px}}
.red-analysis-label{{font-size:0.65rem;text-transform:uppercase;letter-spacing:0.5px;color:#ef4444;font-weight:700;margin-bottom:6px;display:flex;align-items:center;gap:6px}}
.red-analysis-label::before{{content:"";display:inline-block;width:12px;height:12px;background:#ef444422;border:1px solid #ef4444;border-radius:50%}}

/* Activity Feed */
.severity-critical{{background:#ef444422;color:#ef4444;border:1px solid #ef444444;display:inline-block;padding:2px 8px;border-radius:10px;font-size:0.6rem;font-weight:700;text-transform:uppercase}}
.severity-warning{{background:#f59e0b22;color:#f59e0b;border:1px solid #f59e0b44;display:inline-block;padding:2px 8px;border-radius:10px;font-size:0.6rem;font-weight:700;text-transform:uppercase}}
.severity-info{{background:#6b728022;color:#6b7280;border:1px solid #6b728044;display:inline-block;padding:2px 8px;border-radius:10px;font-size:0.6rem;font-weight:700;text-transform:uppercase}}
.event-metric{{font-size:0.7rem;color:#8888aa}}

/* Collapsible cluster event cards */
details.cluster-events{{background:#1a1a2e;border-radius:8px;margin-bottom:10px;overflow:hidden}}
details.cluster-events summary{{padding:10px 14px;display:flex;align-items:center;gap:10px;cursor:pointer;list-style:none;user-select:none}}
details.cluster-events summary::-webkit-details-marker{{display:none}}
details.cluster-events summary .chevron{{transition:transform 0.15s ease;font-size:0.7rem;color:#6b7280}}
details.cluster-events[open] summary .chevron{{transform:rotate(90deg)}}

.footer{{text-align:center;font-size:0.7rem;color:#4a4a6a;padding:20px 0}}
</style>
</head><body>
<div class="container">
<h1>Production Cluster Scorecard</h1>
<div class="subtitle">
  Generated {_esc(collected_at[:19])} UTC | {total} production cluster(s)
  {f" | Compared to {prev_date}" if has_prev else " | First report (no comparison available)"}
</div>

<!-- Section 1: Org Overview Scorecard -->
<div class="tiles">
  <div class="tile"><div class="tile-val" style="color:#ef4444">{len(red)}</div><div class="tile-lbl">Red</div></div>
  <div class="tile"><div class="tile-val" style="color:#f59e0b">{len(yellow)}</div><div class="tile-lbl">Yellow</div></div>
  <div class="tile"><div class="tile-val" style="color:#22c55e">{len(green)}</div><div class="tile-lbl">Green</div></div>
  <div class="tile"><div class="tile-val" style="color:#fff">{total}</div><div class="tile-lbl">Total Clusters</div></div>
  <div class="tile"><div class="tile-val" style="color:{"#ef4444" if total_open_alerts else "#22c55e"}">{total_open_alerts}</div><div class="tile-lbl">Open Alerts</div></div>
  <div class="tile"><div class="tile-val" style="color:#6c63ff">{total_indexes}</div><div class="tile-lbl">Index Suggestions</div></div>
</div>

<h2 style="font-size:0.85rem;color:#a0a0c0;margin:4px 0 8px 0;">Reliability Trend (7-day event patterns)</h2>
<div class="tiles">
  <div class="tile"><div class="tile-val" style="color:#ef4444">{rel_red}</div><div class="tile-lbl">Reliability Red</div></div>
  <div class="tile"><div class="tile-val" style="color:#f59e0b">{rel_yellow}</div><div class="tile-lbl">Reliability Watch</div></div>
  <div class="tile"><div class="tile-val" style="color:#22c55e">{rel_green}</div><div class="tile-lbl">Reliability Green</div></div>
</div>

{f'<div style="font-size:0.8rem;color:#a0a0c0;margin-bottom:16px;">{_esc(comp_summary)}</div>' if comp_summary else ""}

{f'<div class="ai-box">{_md_to_html(ai_summary)}</div>' if ai_summary else ""}

<hr class="divider">

<!-- Section 2 & 3: Cluster Health by Tier -->
{_render_tier_table(red, "RED", "#ef4444", "#ef444422", comparison, red_analyses=red_analyses or {}, activity_events=activity_events or {})}
{_render_tier_table(yellow, "YELLOW", "#f59e0b", "#f59e0b22", comparison, activity_events=activity_events or {})}
{_render_tier_table(green, "GREEN", "#22c55e", "#22c55e22", comparison, activity_events=activity_events or {})}

<hr class="divider">

<!-- Section 4: Alert Summary -->
<h2>Alert Summary</h2>
<div class="tiles" style="margin-bottom:12px;">
  <div class="tile"><div class="tile-val" style="color:{"#ef4444" if total_open_alerts else "#22c55e"}">{total_open_alerts}</div><div class="tile-lbl">Currently Open</div></div>
  <div class="tile"><div class="tile-val">{total_7d_alerts}</div><div class="tile-lbl">Past 7 Days</div>
    {f'<div class="tile-sub">prev: {comparison.get("prev_total_alerts_7d", "?")}</div>' if has_prev else ""}
  </div>
  <div class="tile"><div class="tile-val">{total_30d_alerts}</div><div class="tile-lbl">Past 30 Days</div></div>
  <div class="tile">
    <div class="tile-val" style="color:{"#ef4444" if comparison.get("alert_velocity") == "increasing" else "#22c55e" if comparison.get("alert_velocity") == "decreasing" else "#6b7280"}">
      {"↑" if comparison.get("alert_velocity") == "increasing" else "↓" if comparison.get("alert_velocity") == "decreasing" else "→"}
    </div>
    <div class="tile-lbl">Alert Trend</div>
  </div>
</div>

{_render_alert_table(scored_clusters)}

<hr class="divider">

<!-- Section 5: Activity Feed -->
{_render_activity_feed(activity_events or {})}

<div class="footer">
  Production Cluster Scorecard | Generated {_esc(collected_at[:19])} UTC | All data read-only via Atlas CLI
</div>
</div></body></html>"""


def _render_red_analysis(analysis_text: str) -> str:
    """Parse and render an AI analysis into structured HTML.

    Expects format:
        DIAGNOSIS: <text>

        RECOMMENDATIONS:
        1. <text>
        2. <text>
        3. <text>
    """
    import re

    # Try to split into diagnosis and recommendations
    diagnosis = ""
    recommendations = []

    # Extract DIAGNOSIS section
    diag_match = re.search(
        r"DIAGNOSIS:\s*(.+?)(?=\n\s*RECOMMENDATIONS:|\n\s*\d+\.\s|\Z)",
        analysis_text,
        re.DOTALL | re.IGNORECASE,
    )
    if diag_match:
        diagnosis = diag_match.group(1).strip()

    # Extract numbered recommendations
    rec_matches = re.findall(
        r"\d+\.\s*(.+?)(?=\n\s*\d+\.|\Z)", analysis_text, re.DOTALL
    )
    if rec_matches:
        recommendations = [r.strip() for r in rec_matches if r.strip()]

    # If parsing failed, fall back to rendering the whole text
    if not diagnosis and not recommendations:
        return f'<div style="white-space:pre-wrap;">{_esc(analysis_text)}</div>'

    html_parts = []
    if diagnosis:
        html_parts.append(
            f'<div class="diagnosis"><strong style="color:#ef4444;">Diagnosis:</strong> {_esc(diagnosis)}</div>'
        )

    if recommendations:
        rec_items = "".join(f"<li>{_esc(r)}</li>" for r in recommendations[:3])
        html_parts.append(
            f'<div><strong style="color:#f59e0b;">Recommendations:</strong>'
            f'<ol class="recommendations">{rec_items}</ol></div>'
        )

    return "".join(html_parts)


def _render_tier_table(
    clusters: list,
    tier: str,
    color: str,
    bg_color: str,
    comparison: dict,
    red_analyses: dict = None,
    activity_events: dict = None,
) -> str:
    """Render a tier section with a header badge and a table of clusters."""
    if not clusters:
        return ""

    # Build per-project event lookup for matching events to clusters
    project_events = {}
    if activity_events and activity_events.get("events"):
        for e in activity_events["events"]:
            if e.get("severity") not in ("CRITICAL", "WARNING"):
                continue
            pid = e.get("project_id", "")
            if pid:
                project_events.setdefault(pid, []).append(e)

    trends = comparison.get("cluster_trends", {})

    def _fmt_val(val, fmt=".1f", suffix=""):
        if val is None:
            return '<span style="color:#4a4a6a;">--</span>'
        return f"{val:{fmt}}{suffix}"

    def _color_val(val, warn, crit, fmt=".1f", suffix=""):
        if val is None:
            return '<span style="color:#4a4a6a;">--</span>'
        if crit is not None and val > crit:
            return f'<span style="color:#ef4444;font-weight:700;">{val:{fmt}}{suffix}</span>'
        if warn is not None and val > warn:
            return f'<span style="color:#f59e0b;font-weight:700;">{val:{fmt}}{suffix}</span>'
        return f'<span style="color:#22c55e;">{val:{fmt}}{suffix}</span>'

    def _trend_arrow(name):
        t = trends.get(name, {})
        arrow = t.get("arrow", "")
        if arrow == "IMPROVING":
            return '<span class="arrow-improving">↑</span>'
        elif arrow == "DEGRADING":
            return '<span class="arrow-degrading">↓</span>'
        elif arrow == "STABLE":
            return '<span class="arrow-stable">→</span>'
        elif arrow == "NEW":
            return '<span class="arrow-new">★</span>'
        return ""

    rows = []
    for c in clusters:
        name = c.get("cluster_name", "?")
        ms = c.get("score", {}).get("metrics_summary", {})
        justification = c.get("score", {}).get("justification", "")
        info_tier = c.get("tier", "?")
        version = c.get("mongodb_version", "?")
        alerts_open = ms.get("open_alerts", 0)
        idx_count = ms.get("suggested_indexes", 0)

        trend = trends.get(name, {})
        trend_detail = trend.get("details", "")
        tier_change = trend.get("tier_change")
        tier_change_html = ""
        if tier_change:
            tier_change_html = f'<span style="font-size:0.65rem;color:#a0a0c0;margin-left:4px;">{tier_change["from"]}→{tier_change["to"]}</span>'

        alert_style = "color:#ef4444;font-weight:700;" if alerts_open > 0 else ""

        # Build index dropdown for this cluster
        indexes = c.get("suggested_indexes", [])
        # Sort: HIGH first, then MEDIUM, then LOW
        priority_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        indexes_sorted = sorted(
            indexes, key=lambda x: priority_order.get(x.get("priority", "LOW"), 9)
        )
        idx_dropdown = ""
        if indexes_sorted:
            high_count = sum(1 for i in indexes_sorted if i.get("priority") == "HIGH")
            med_count = sum(1 for i in indexes_sorted if i.get("priority") == "MEDIUM")
            low_count = sum(1 for i in indexes_sorted if i.get("priority") == "LOW")
            idx_items = []
            for idx in indexes_sorted:
                ns = _esc(idx.get("namespace", ""))
                weight = idx.get("weight", 0)
                priority = idx.get("priority", "LOW")
                exec_hr = idx.get("exec_per_hour", 0)
                avg_ms = idx.get("avg_ms", 0)
                index_keys = idx.get("index", [])
                if isinstance(index_keys, list):
                    key_parts = []
                    for item in index_keys:
                        if isinstance(item, dict):
                            for k, v in item.items():
                                key_parts.append(f'"{k}": {v}')
                    index_str = "{{ " + ", ".join(key_parts) + " }}"
                else:
                    index_str = str(index_keys)

                if priority == "HIGH":
                    pri_badge = '<span style="color:#ef4444;font-weight:700;font-size:0.6rem;">HIGH</span>'
                    border = "border-left:2px solid #ef4444;"
                elif priority == "MEDIUM":
                    pri_badge = '<span style="color:#f59e0b;font-weight:700;font-size:0.6rem;">MED</span>'
                    border = "border-left:2px solid #f59e0b;"
                else:
                    pri_badge = '<span style="color:#6b7280;font-weight:700;font-size:0.6rem;">LOW</span>'
                    border = "border-left:2px solid #4a4a6a;opacity:0.7;"

                idx_items.append(
                    f'<div class="idx-item" style="{border}">'
                    f"{pri_badge} <code>{_esc(index_str)}</code>"
                    f'<div class="idx-meta">{ns} | weight: {weight:.2f} | {exec_hr:.1f} exec/hr | avg {avg_ms:.0f}ms</div>'
                    f"</div>"
                )

            summary_parts = []
            if high_count:
                summary_parts.append(f"{high_count} high")
            if med_count:
                summary_parts.append(f"{med_count} med")
            if low_count:
                summary_parts.append(f"{low_count} low")
            summary_label = f"{len(indexes_sorted)} index suggestion(s) ({', '.join(summary_parts)})"

            idx_dropdown = (
                f'<details class="idx-toggle">'
                f"<summary>{summary_label}</summary>"
                f'<div class="idx-list">{"".join(idx_items)}</div>'
                f"</details>"
            )
            idx_dropdown = (
                f'<details class="idx-toggle">'
                f"<summary>{len(indexes)} index suggestion(s)</summary>"
                f'<div class="idx-list">{"".join(idx_items)}</div>'
                f"</details>"
            )

        # Build events dropdown for this cluster (matched by project_id)
        events_dropdown = ""
        cluster_proj = c.get("project_id", "")
        cluster_evts = project_events.get(cluster_proj, [])
        if cluster_evts:
            crit_count = sum(1 for e in cluster_evts if e.get("severity") == "CRITICAL")
            warn_count = sum(1 for e in cluster_evts if e.get("severity") == "WARNING")
            evt_parts = []
            if crit_count:
                evt_parts.append(f"{crit_count} critical")
            if warn_count:
                evt_parts.append(f"{warn_count} warning")
            evt_label = f"{len(cluster_evts)} event(s) ({', '.join(evt_parts)})"

            evt_items = []
            for ev in cluster_evts[:15]:
                sev = ev.get("severity", "INFO")
                sev_color = "#ef4444" if sev == "CRITICAL" else "#f59e0b"
                etype = _esc(ev.get("event_type", "?"))
                created = _esc(ev.get("created", "")[:16]).replace("T", " ")
                metric = ev.get("metric_name", "")
                mval = ev.get("metric_value")
                munits = ev.get("metric_units", "")
                nodes = ev.get("node_count", 1)
                metric_str = ""
                if metric and mval is not None:
                    metric_str = (
                        f' <span style="color:#6b7280;">'
                        f'{_esc(metric.replace("_"," ").title())}: {mval} {_esc(munits)}'
                        f'</span>'
                    )
                node_str = f' <span style="color:#6b7280;">({nodes} nodes)</span>' if nodes > 1 else ""
                evt_items.append(
                    f'<div class="idx-item" style="border-left:2px solid {sev_color};">'
                    f'<span style="color:{sev_color};font-weight:700;font-size:0.6rem;">{sev}</span> '
                    f'<strong style="font-size:0.7rem;">{etype}</strong>{node_str}{metric_str}'
                    f'<div class="idx-meta">{created} UTC</div>'
                    f'</div>'
                )
            if len(cluster_evts) > 15:
                evt_items.append(
                    f'<div class="idx-meta" style="padding:4px 0;font-style:italic;">'
                    f'... and {len(cluster_evts) - 15} more (see Excel export)</div>'
                )

            events_dropdown = (
                f'<details class="idx-toggle">'
                f'<summary>{evt_label}</summary>'
                f'<div class="idx-list">{"".join(evt_items)}</div>'
                f'</details>'
            )

        # AI analysis for RED clusters
        analysis_html = ""
        if red_analyses and name in red_analyses:
            analysis_text = red_analyses[name]
            # Parse DIAGNOSIS and RECOMMENDATIONS sections
            analysis_rendered = _render_red_analysis(analysis_text)
            analysis_html = f"""
        <tr>
           <td colspan="12" style="padding:0 8px 8px 8px;border-bottom:1px solid #2a2a4a;">
            <div class="red-analysis">
              <div class="red-analysis-label">AI Analysis & Recommendations</div>
              {analysis_rendered}
            </div>
          </td>
        </tr>"""

        # Build the expandable details row (indexes + events) if either exists
        details_row = ""
        if idx_dropdown or events_dropdown:
            details_parts = []
            if idx_dropdown:
                details_parts.append(idx_dropdown)
            if events_dropdown:
                details_parts.append(events_dropdown)
            details_row = f"""
        <tr>
          <td colspan="12" style="padding:2px 8px 8px 8px;border-bottom:1px solid #2a2a4a;">
            <div style="display:flex;gap:16px;flex-wrap:wrap;">{"".join(details_parts)}</div>
          </td>
        </tr>"""

        # Recovery posture indicator
        recovery = c.get("score", {}).get("recovery_posture", {})
        backup_ok = recovery.get("backup_enabled", False)
        pitr_ok = recovery.get("pit_enabled", False)
        recovery_html = ""
        if not backup_ok:
            recovery_html = '<div style="font-size:0.6rem;color:#ef4444;">No Backup</div>'
        elif not pitr_ok:
            recovery_html = '<div style="font-size:0.6rem;color:#f59e0b;">No PITR</div>'

        # Reliability badge and watch reason
        reliability = c.get("reliability", {})
        rel_tier = reliability.get("tier", "")
        watch_reasons = reliability.get("watch_reasons", [])
        rel_badge = f'<span class="badge badge-{rel_tier.lower()}">{rel_tier}</span>' if rel_tier else ""
        watch_text = _esc(watch_reasons[0][:80]) if watch_reasons else ""

        rows.append(f"""
        <tr{"" if details_row or analysis_html else ""}>
          <td>
            <strong>{_esc(name)}</strong>{tier_change_html}
            <div style="font-size:0.65rem;color:#6b7280;">{_esc(info_tier)} | MongoDB {_esc(version)}</div>
            {recovery_html}
          </td>
          <td style="text-align:center;">{_trend_arrow(name)}</td>
          <td style="text-align:center;">{rel_badge}</td>
          <td style="font-size:0.7rem;color:#a0a0c0;max-width:200px;">{watch_text}</td>
          <td style="text-align:right;">{_color_val(ms.get("cpu_avg"), 50, 80, ".1f", "%")}</td>
          <td style="text-align:right;">{_fmt_val(ms.get("cpu_max"), ".1f", "%")}</td>
          <td style="text-align:right;">{_fmt_val(ms.get("connections_max"), ".0f", "")}</td>
          <td style="text-align:right;">{_color_val(ms.get("memory_pct"), 70, 85, ".0f", "%")}</td>
          <td style="text-align:right;">{_color_val(ms.get("disk_pct"), 65, 75, ".0f", "%")}</td>
          <td style="text-align:right;{alert_style}">{alerts_open}</td>
          <td style="text-align:right;">{idx_count}</td>
          <td style="font-size:0.75rem;color:#a0a0c0;max-width:280px;">{_esc(justification[:120])}</td>
        </tr>{details_row}{analysis_html}""")

    return f"""
<div style="margin-bottom:24px;">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
    <span class="badge badge-{tier.lower()}" style="font-size:0.8rem;padding:5px 14px;">{tier}</span>
    <span style="font-size:0.85rem;color:#a0a0c0;">{len(clusters)} cluster(s)</span>
  </div>
  <div style="overflow-x:auto;">
    <table style="min-width:1000px;">
      <thead>
        <tr>
          <th style="min-width:160px;">Cluster</th>
          <th style="text-align:center;width:40px;">Trend</th>
          <th style="text-align:center;">Reliability</th>
          <th>Watch Reason</th>
          <th style="text-align:right;">CPU Avg</th>
          <th style="text-align:right;">CPU Max</th>
          <th style="text-align:right;">Conn Max</th>
          <th style="text-align:right;">Memory %</th>
          <th style="text-align:right;">Disk %</th>
          <th style="text-align:right;">Alerts</th>
          <th style="text-align:right;">Indexes</th>
          <th>Justification</th>
        </tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
  </div>
</div>"""


def _render_alert_table(clusters: list) -> str:
    """Render per-cluster alert breakdown table."""
    rows = []
    for c in clusters:
        name = c.get("cluster_name", "?")
        alerts = c.get("alerts", {})
        open_count = alerts.get("open_count", 0)
        count_7d = alerts.get("past_7d_count", 0)
        count_30d = alerts.get("past_30d_count", 0)

        open_style = "color:#ef4444;font-weight:700;" if open_count > 0 else ""
        open_details = ""
        if open_count > 0:
            details = [a.get("event_type", "?") for a in alerts.get("open", [])[:3]]
            open_details = f'<div style="font-size:0.7rem;color:#8888aa;">{", ".join(details)}</div>'

        rows.append(f"""
        <tr>
          <td><strong>{_esc(name)}</strong></td>
          <td style="{open_style}">{open_count}{open_details}</td>
          <td>{count_7d}</td>
          <td>{count_30d}</td>
        </tr>""")

    if not rows:
        return '<div style="font-size:0.8rem;color:#6b7280;">No alert data available.</div>'

    return f"""
    <div style="overflow-x:auto;">
      <table>
        <thead><tr><th>Cluster</th><th>Open</th><th>7 Days</th><th>30 Days</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    </div>"""


def _render_activity_feed(activity_events: dict) -> str:
    """Render the activity feed summary tiles (events are shown per-cluster in tier tables)."""
    if not activity_events or not activity_events.get("events"):
        return """
<h2>Activity Feed <span style="font-size:0.75rem;color:#6b7280;font-weight:400;">(Past 7 Days)</span></h2>
<div style="font-size:0.8rem;color:#6b7280;">No significant events in the past 7 days.</div>"""

    critical_count = activity_events.get("critical_count", 0)
    warning_count = activity_events.get("warning_count", 0)
    info_count = activity_events.get("info_count", 0)
    noise_filtered = activity_events.get("filtered_noise_count", 0)

    return f"""
<h2>Activity Feed <span style="font-size:0.75rem;color:#6b7280;font-weight:400;">(Past 7 Days)</span></h2>
<div class="tiles" style="margin-bottom:12px;">
  <div class="tile"><div class="tile-val" style="color:#ef4444">{critical_count}</div><div class="tile-lbl">Critical Events</div></div>
  <div class="tile"><div class="tile-val" style="color:#f59e0b">{warning_count}</div><div class="tile-lbl">Warning Events</div></div>
  <div class="tile"><div class="tile-val" style="color:#6b7280">{info_count}</div><div class="tile-lbl">Info Events</div></div>
  <div class="tile"><div class="tile-val" style="color:#4a4a6a">{noise_filtered}</div><div class="tile-lbl">Noise Filtered</div></div>
</div>
<div style="font-size:0.75rem;color:#6b7280;">Expand event details per cluster in the health tables above.</div>"""


def _render_index_section(clusters: list, comparison: dict) -> str:
    """Render high-impact index recommendations with recurring flags."""
    recurring = set(comparison.get("recurring_indexes", []))
    recurring_count = comparison.get("recurring_index_count", 0)
    has_prev = comparison.get("has_previous", False)

    all_indexes = []
    for c in clusters:
        for idx in c.get("suggested_indexes", []):
            idx["_cluster"] = c.get("cluster_name", "?")
            # Check if recurring
            ns = idx.get("namespace", "")
            keys = json.dumps(idx.get("index", []), sort_keys=True)
            idx["_recurring"] = f"{ns}:{keys}" in recurring
            all_indexes.append(idx)

    if not all_indexes:
        return '<div class="card" style="padding:16px;text-align:center;color:#6b7280;">No high-impact index suggestions across any cluster.</div>'

    summary = f"**{len(all_indexes)}** high-impact suggestion(s) across **{len(set(i['_cluster'] for i in all_indexes))}** cluster(s)"
    if has_prev and recurring_count:
        summary += f" | **{recurring_count}** recurring from previous report"

    cards = []
    for idx in all_indexes:
        ns = _esc(idx.get("namespace", ""))
        cluster = _esc(idx.get("_cluster", ""))
        weight = idx.get("weight", 0)
        is_recurring = idx.get("_recurring", False)

        index_keys = idx.get("index", [])
        if isinstance(index_keys, list):
            key_parts = []
            for item in index_keys:
                if isinstance(item, dict):
                    for k, v in item.items():
                        key_parts.append(f'"{k}": {v}')
            index_str = "{ " + ", ".join(key_parts) + " }"
        else:
            index_str = json.dumps(index_keys)

        card_cls = "idx-recurring" if is_recurring else "idx-new"
        flag = (
            ' <span class="badge badge-yellow" style="font-size:0.6rem;">RECURRING</span>'
            if is_recurring
            else ""
        )

        cards.append(f"""
        <div class="idx-card {card_cls}">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
              <strong>{cluster}</strong> &rarr; <code style="font-size:0.8rem;">{ns}</code>{flag}
            </div>
            <span style="font-size:0.75rem;color:#6b7280;">weight: {weight:.2f}</span>
          </div>
          <code style="font-size:0.75rem;color:#6c63ff;display:block;margin-top:4px;">{_esc(index_str)}</code>
        </div>""")

    return f"""
    <div style="font-size:0.85rem;color:#a0a0c0;margin-bottom:12px;">{summary}</div>
    {"".join(cards)}"""
