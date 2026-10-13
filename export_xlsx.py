"""
Excel Export: writes the scorecard data to a multi-tab .xlsx workbook.

Tabs:
  1. Summary -- org-level KPIs, tier counts, alert totals
  2. Clusters -- one row per cluster with all metrics, tier, justification
  3. Alerts -- per-cluster alert breakdown
  4. Index Recommendations -- all suggestions with priority scoring
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from scorer import THRESHOLDS

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
except ImportError:
    raise ImportError("openpyxl required. Run: pip install openpyxl")

logger = logging.getLogger(__name__)

# Colors
RED_FILL = PatternFill(start_color="FFCCCC", end_color="FFCCCC", fill_type="solid")
YELLOW_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
GREEN_FILL = PatternFill(start_color="D5F5E3", end_color="D5F5E3", fill_type="solid")
HEADER_FILL = PatternFill(start_color="1a1a2e", end_color="1a1a2e", fill_type="solid")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
TIER_FILLS = {"RED": RED_FILL, "YELLOW": YELLOW_FILL, "GREEN": GREEN_FILL}
TIER_FONTS = {
    "RED": Font(bold=True, color="CC0000"),
    "YELLOW": Font(bold=True, color="CC8800"),
    "GREEN": Font(bold=True, color="008800"),
}
THIN_BORDER = Border(
    bottom=Side(style="thin", color="DDDDDD"),
)


def _style_header_row(ws, row, col_count):
    """Style a header row."""
    for col in range(1, col_count + 1):
        cell = ws.cell(row=row, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )


def _auto_width(ws, min_width=10, max_width=50):
    """Auto-fit column widths."""
    for col_cells in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col_cells[0].column)
        for cell in col_cells:
            try:
                val = str(cell.value or "")
                max_len = max(max_len, len(val))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(
            max(max_len + 2, min_width), max_width
        )


def export_to_xlsx(
    scored_clusters: list,
    collected_at: str,
    output_path: str,
    activity_events: dict = None,
    red_analyses: dict = None,
    ai_summary: str = "",
) -> str:
    """Export scorecard data to an Excel workbook.

    Args:
        scored_clusters: List of cluster dicts with scores
        collected_at: ISO timestamp of collection
        output_path: Path for the .xlsx file
        activity_events: Activity feed events dict (optional)
        red_analyses: Per-RED-cluster AI analysis dict (optional)
        ai_summary: Org-level AI comparison summary (optional)

    Returns:
        Path to the written file
    """
    wb = Workbook()

    # ── Tab 1: Summary ───────────────────────────────────────────
    ws_summary = wb.active
    ws_summary.title = "Summary"

    red = [c for c in scored_clusters if c.get("score", {}).get("tier") == "RED"]
    yellow = [c for c in scored_clusters if c.get("score", {}).get("tier") == "YELLOW"]
    green = [c for c in scored_clusters if c.get("score", {}).get("tier") == "GREEN"]
    total = len(scored_clusters)
    total_open_alerts = sum(
        c.get("alerts", {}).get("open_count", 0) for c in scored_clusters
    )
    total_7d = sum(c.get("alerts", {}).get("past_7d_count", 0) for c in scored_clusters)
    total_30d = sum(
        c.get("alerts", {}).get("past_30d_count", 0) for c in scored_clusters
    )
    total_indexes = sum(len(c.get("suggested_indexes", [])) for c in scored_clusters)
    total_high_idx = sum(c.get("high_priority_index_count", 0) for c in scored_clusters)

    ws_summary.append(["Production Cluster Scorecard"])
    ws_summary["A1"].font = Font(bold=True, size=14)
    ws_summary.append(["Generated", collected_at[:19] + " UTC"])
    ws_summary.append(["Total Clusters", total])
    ws_summary.append([])
    ws_summary.append(["Tier", "Count"])
    _style_header_row(ws_summary, 5, 2)
    ws_summary.append(["RED", len(red)])
    ws_summary.append(["YELLOW", len(yellow)])
    ws_summary.append(["GREEN", len(green)])
    for row in range(6, 9):
        tier = ws_summary.cell(row=row, column=1).value
        if tier in TIER_FILLS:
            ws_summary.cell(row=row, column=1).fill = TIER_FILLS[tier]
            ws_summary.cell(row=row, column=1).font = TIER_FONTS[tier]

    ws_summary.append([])
    ws_summary.append(["Alert Summary"])
    ws_summary.cell(row=10, column=1).font = Font(bold=True, size=12)
    ws_summary.append(["Open Alerts", total_open_alerts])
    ws_summary.append(["Past 7 Days", total_7d])
    ws_summary.append(["Past 30 Days", total_30d])
    ws_summary.append([])
    ws_summary.append(["Index Recommendations"])
    ws_summary.cell(row=15, column=1).font = Font(bold=True, size=12)
    ws_summary.append(["Total Suggestions", total_indexes])
    ws_summary.append(["High Priority", total_high_idx])

    if ai_summary:
        ws_summary.append([])
        ws_summary.append(["AI Summary"])
        header_row = ws_summary.max_row
        ws_summary.cell(row=header_row, column=1).font = Font(bold=True, size=12)
        # Write the summary text with word-wrap in a merged area
        ws_summary.append([ai_summary])
        text_row = ws_summary.max_row
        summary_cell = ws_summary.cell(row=text_row, column=1)
        summary_cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws_summary.merge_cells(
            start_row=text_row, start_column=1,
            end_row=text_row, end_column=4,
        )
        ws_summary.row_dimensions[text_row].height = max(
            60, 15 * ai_summary.count("\n") + 30
        )

    _auto_width(ws_summary)

    # ── Tab 2: Clusters ──────────────────────────────────────────
    ws_clusters = wb.create_sheet("Clusters")

    headers = [
        "Cluster",
        "Project ID",
        "Tier (Instance)",
        "Type",
        "MongoDB Version",
        "State",
        "Health Tier",
        "Justification",
        "CPU Avg %",
        "CPU Max %",
        "Memory Avg MB",
        "Memory %",
        "Connections Max",
        "Disk Total GB",
        "Disk Used GB",
        "Disk %",
        "Scan & Order Max /s",
        "Open Alerts",
        "Alerts 7d",
        "Alerts 30d",
        "Index Suggestions",
        "High Priority Indexes",
        "Auto-Scale Compute",
        "Auto-Scale Disk",
        "Reliability Tier",
        "Watch Reason",
        "Critical Events 7d",
        "Warning Events 7d",
        "Recurring Critical",
        "Changes 24h",
        "Backup Enabled",
        "PITR Enabled",
        "AI Analysis",
    ]
    ws_clusters.append(headers)
    _style_header_row(ws_clusters, 1, len(headers))

    for c in scored_clusters:
        score = c.get("score", {})
        ms = score.get("metrics_summary", {})
        alerts = c.get("alerts", {})
        tier = score.get("tier", "")

        # AI analysis for RED clusters
        cluster_name = c.get("cluster_name", "")
        ai_analysis = ""
        if red_analyses and cluster_name in red_analyses:
            ai_analysis = red_analyses[cluster_name]

        # Reliability data
        reliability = c.get("reliability", {})
        rel_tier = reliability.get("tier", "")
        watch_reasons = reliability.get("watch_reasons", [])
        event_summary = c.get("event_summary", {})

        row_data = [
            cluster_name,
            c.get("project_id", ""),
            c.get("tier", ""),
            c.get("cluster_type", ""),
            c.get("mongodb_version", ""),
            c.get("state", ""),
            tier,
            score.get("justification", ""),
            ms.get("cpu_avg"),
            ms.get("cpu_max"),
            ms.get("memory_avg_mb"),
            ms.get("memory_pct"),
            ms.get("connections_max"),
            ms.get("disk_total_gb"),
            ms.get("disk_used_gb"),
            ms.get("disk_pct"),
            c.get("metrics", {}).get("OPERATIONS_SCAN_AND_ORDER", {}).get("max"),
            alerts.get("open_count", 0),
            alerts.get("past_7d_count", 0),
            alerts.get("past_30d_count", 0),
            len(c.get("suggested_indexes", [])),
            c.get("high_priority_index_count", 0),
            "Yes" if c.get("compute_auto_scaling") else "No",
            "Yes" if c.get("disk_auto_scaling") else "No",
            rel_tier,
            watch_reasons[0] if watch_reasons else "",
            event_summary.get("critical_7d", 0),
            event_summary.get("warning_7d", 0),
            "Yes" if event_summary.get("has_recurring_critical") else "No",
            event_summary.get("recent_changes_24h", 0),
            "Yes" if c.get("backup_enabled") else "No",
            "Yes" if c.get("pit_enabled") else "No",
            ai_analysis,
        ]
        ws_clusters.append(row_data)

        # Color the health tier cell
        row_num = ws_clusters.max_row
        tier_cell = ws_clusters.cell(row=row_num, column=7)
        if tier in TIER_FILLS:
            tier_cell.fill = TIER_FILLS[tier]
            tier_cell.font = TIER_FONTS[tier]

        # Color the reliability tier cell (column 25)
        rel_tier_cell = ws_clusters.cell(row=row_num, column=25)
        if rel_tier in TIER_FILLS:
            rel_tier_cell.fill = TIER_FILLS[rel_tier]
            rel_tier_cell.font = TIER_FONTS[rel_tier]

        # Color disk % if high
        disk_pct = ms.get("disk_pct")
        if disk_pct is not None:
            disk_cell = ws_clusters.cell(row=row_num, column=16)
            if disk_pct > 75:
                disk_cell.fill = RED_FILL
            elif disk_pct > 65:
                disk_cell.fill = YELLOW_FILL

        # Color CPU avg if high
        cpu_avg = ms.get("cpu_avg")
        if cpu_avg is not None:
            cpu_cell = ws_clusters.cell(row=row_num, column=9)
            if cpu_avg > 80:
                cpu_cell.fill = RED_FILL
            elif cpu_avg > 50:
                cpu_cell.fill = YELLOW_FILL

        # Color memory % if high
        mem_pct = ms.get("memory_pct")
        if mem_pct is not None:
            mem_cell = ws_clusters.cell(row=row_num, column=12)
            if mem_pct > 85:
                mem_cell.fill = RED_FILL
            elif mem_pct > 70:
                mem_cell.fill = YELLOW_FILL

    # Freeze top row and auto-width
    ws_clusters.freeze_panes = "A2"
    ws_clusters.auto_filter.ref = ws_clusters.dimensions
    _auto_width(ws_clusters)

    # ── Tab 3: Alerts ────────────────────────────────────────────
    ws_alerts = wb.create_sheet("Alerts")

    alert_headers = ["Cluster", "Status", "Event Type", "Metric", "Value", "Created"]
    ws_alerts.append(alert_headers)
    _style_header_row(ws_alerts, 1, len(alert_headers))

    for c in scored_clusters:
        cluster_name = c.get("cluster_name", "")
        for a in c.get("alerts", {}).get("open", []):
            ws_alerts.append(
                [
                    cluster_name,
                    "OPEN",
                    a.get("event_type", ""),
                    a.get("metric", ""),
                    a.get("value"),
                    a.get("created", ""),
                ]
            )

    ws_alerts.freeze_panes = "A2"
    ws_alerts.auto_filter.ref = ws_alerts.dimensions
    _auto_width(ws_alerts)

    # ── Tab 4: Index Recommendations ─────────────────────────────
    ws_indexes = wb.create_sheet("Index Recommendations")

    idx_headers = [
        "Cluster",
        "Namespace",
        "Priority",
        "Index Definition",
        "Weight",
        "Exec/Hour",
        "Avg Ms",
        "Exec Count 7d",
    ]
    ws_indexes.append(idx_headers)
    _style_header_row(ws_indexes, 1, len(idx_headers))

    for c in scored_clusters:
        cluster_name = c.get("cluster_name", "")
        for idx in c.get("suggested_indexes", []):
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

            priority = idx.get("priority", "LOW")
            ws_indexes.append(
                [
                    cluster_name,
                    idx.get("namespace", ""),
                    priority,
                    index_str,
                    idx.get("weight", 0),
                    idx.get("exec_per_hour", 0),
                    idx.get("avg_ms", 0),
                    idx.get("exec_count_7d", 0),
                ]
            )

            # Color priority
            row_num = ws_indexes.max_row
            pri_cell = ws_indexes.cell(row=row_num, column=3)
            if priority == "HIGH":
                pri_cell.fill = RED_FILL
                pri_cell.font = Font(bold=True, color="CC0000")
            elif priority == "MEDIUM":
                pri_cell.fill = YELLOW_FILL
                pri_cell.font = Font(bold=True, color="CC8800")

    ws_indexes.freeze_panes = "A2"
    ws_indexes.auto_filter.ref = ws_indexes.dimensions
    _auto_width(ws_indexes)

    # ── Tab 5: Activity Feed ─────────────────────────────────────
    ws_events = wb.create_sheet("Activity Feed")

    event_headers = [
        "Severity",
        "Time (UTC)",
        "Event Type",
        "Cluster",
        "Metric",
        "Value",
        "Units",
        "Nodes",
        "User",
        "Project ID",
    ]
    ws_events.append(event_headers)
    _style_header_row(ws_events, 1, len(event_headers))

    CRITICAL_FILL = PatternFill(
        start_color="FFCCCC", end_color="FFCCCC", fill_type="solid"
    )
    WARNING_FILL = PatternFill(
        start_color="FFF2CC", end_color="FFF2CC", fill_type="solid"
    )
    CRITICAL_FONT = Font(bold=True, color="CC0000")
    WARNING_FONT = Font(bold=True, color="CC8800")

    if activity_events and activity_events.get("events"):
        # Show CRITICAL and WARNING events
        significant = [
            e
            for e in activity_events["events"]
            if e.get("severity") in ("CRITICAL", "WARNING")
        ]
        for e in significant:
            severity = e.get("severity", "INFO")
            ws_events.append(
                [
                    severity,
                    e.get("created", "")[:19].replace("T", " "),
                    e.get("event_type", ""),
                    e.get("cluster_name", "") or e.get("replica_set", ""),
                    e.get("metric_name", ""),
                    e.get("metric_value"),
                    e.get("metric_units", ""),
                    e.get("node_count", 1),
                    e.get("user", ""),
                    e.get("project_id", ""),
                ]
            )

            # Color severity cell
            row_num = ws_events.max_row
            sev_cell = ws_events.cell(row=row_num, column=1)
            if severity == "CRITICAL":
                sev_cell.fill = CRITICAL_FILL
                sev_cell.font = CRITICAL_FONT
            elif severity == "WARNING":
                sev_cell.fill = WARNING_FILL
                sev_cell.font = WARNING_FONT

    ws_events.freeze_panes = "A2"
    ws_events.auto_filter.ref = ws_events.dimensions
    _auto_width(ws_events)

    # ── Tab 6: Thresholds Reference ────────────────────────────────
    ws_thresh = wb.create_sheet("Thresholds")

    thresh_headers = ["Section", "Threshold", "Value", "Description"]
    ws_thresh.append(thresh_headers)
    _style_header_row(ws_thresh, 1, len(thresh_headers))

    # Threshold descriptions for documentation
    thresh_descriptions = {
        "current_health": {
            "cpu_avg_red": "CPU avg % above this triggers RED",
            "cpu_avg_yellow": "CPU avg % above this triggers YELLOW",
            "memory_pct_red": "Memory utilization % above this triggers RED",
            "memory_pct_yellow": "Memory utilization % above this triggers YELLOW",
            "disk_pct_red_no_auto": "Disk % triggers RED (no auto-scaling or at ceiling)",
            "disk_pct_yellow_no_auto": "Disk % triggers YELLOW (no auto-scaling)",
            "disk_pct_yellow_auto": "Disk % triggers YELLOW (with auto-scaling)",
            "connections_yellow": "Max connections above this triggers YELLOW",
            "scan_order_yellow": "Scan & order ops/s above this triggers YELLOW",
            "critical_alerts_red": "Active critical alerts at/above this triggers RED",
            "open_alerts_yellow": "Open alerts at/above this triggers YELLOW",
            "high_idx_red": "High-priority index suggestions at/above this triggers RED",
            "high_idx_yellow": "High-priority index suggestions at/above this triggers YELLOW",
        },
        "reliability": {
            "critical_7d_red": "Critical events in 7d at/above this triggers RED reliability",
            "critical_7d_yellow": "Critical events in 7d at/above this triggers YELLOW reliability",
            "warning_7d_yellow": "Warning events in 7d at/above this triggers YELLOW reliability",
            "recurring_days_red": "Distinct days with recurring critical to trigger RED reliability",
            "recurring_days_yellow": "Distinct days with recurring critical to detect pattern (3+ days)",
            "query_targeting_yellow": "Query targeting ratio max above this triggers YELLOW reliability",
        },
    }

    for section_name, section_thresholds in THRESHOLDS.items():
        section_label = section_name.replace("_", " ").title()
        descs = thresh_descriptions.get(section_name, {})
        for key, value in section_thresholds.items():
            ws_thresh.append([
                section_label,
                key,
                value,
                descs.get(key, ""),
            ])

    ws_thresh.freeze_panes = "A2"
    _auto_width(ws_thresh)

    # Save
    wb.save(output_path)
    logger.info(f"Excel exported: {output_path}")
    return output_path
