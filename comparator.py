"""
Report Comparator: loads the previous report and computes trend arrows,
tier changes, and metric diffs per cluster.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).parent / "reports"


def find_previous_report(current_date: str = None) -> dict:
    """Find the most recent previous report JSON file.

    Returns the parsed report data, or empty dict if none found.
    Only considers reports older than 7 days for comparison.
    """
    if not REPORTS_DIR.exists():
        return {}

    json_files = sorted(REPORTS_DIR.glob("org-*.json"), reverse=True)
    if not json_files:
        return {}

    now = datetime.now(timezone.utc)

    for f in json_files:
        try:
            data = json.loads(f.read_text())
            collected = data.get("collected_at", "")
            if collected:
                report_date = datetime.fromisoformat(collected.replace("Z", "+00:00"))
                age = now - report_date
                # Only compare against reports at least 1 day old
                # (skip the current run's file if it exists)
                if age.total_seconds() > 86400:
                    logger.info(
                        f"Previous report found: {f.name} ({age.days} days old)"
                    )
                    data["_file"] = str(f)
                    return data
        except (json.JSONDecodeError, ValueError):
            continue

    return {}


def compare_reports(current: dict, previous: dict) -> dict:
    """Compare current report against previous report.

    Returns:
        dict with per-cluster trends, org-level summary, and changes.
    """
    if not previous:
        return {
            "has_previous": False,
            "message": "No previous report found for comparison. This is the first run.",
            "cluster_trends": {},
            "org_summary": {},
        }

    prev_date = previous.get("collected_at", "?")[:10]
    prev_clusters = {c["cluster_name"]: c for c in previous.get("clusters", [])}
    curr_clusters = {c["cluster_name"]: c for c in current.get("clusters", [])}

    cluster_trends = {}
    tier_changes = []
    improved = 0
    degraded = 0
    stable = 0
    new_clusters = []
    removed_clusters = []

    # Clusters in both reports
    for name, curr in curr_clusters.items():
        if name not in prev_clusters:
            new_clusters.append(name)
            cluster_trends[name] = {
                "trend": "NEW",
                "arrow": "NEW",
                "details": "New cluster since last report",
                "tier_change": None,
            }
            continue

        prev = prev_clusters[name]
        trend = _compare_cluster(curr, prev)
        cluster_trends[name] = trend

        if trend["arrow"] == "IMPROVING":
            improved += 1
        elif trend["arrow"] == "DEGRADING":
            degraded += 1
        else:
            stable += 1

        if trend.get("tier_change"):
            tier_changes.append(trend["tier_change"])

    # Clusters removed since last report
    for name in prev_clusters:
        if name not in curr_clusters:
            removed_clusters.append(name)

    # Org-level alert comparison
    curr_total_alerts = sum(
        c.get("alerts", {}).get("past_7d_count", 0) for c in current.get("clusters", [])
    )
    prev_total_alerts = sum(
        c.get("alerts", {}).get("past_7d_count", 0)
        for c in previous.get("clusters", [])
    )
    alert_velocity = "stable"
    if curr_total_alerts > prev_total_alerts * 1.5:
        alert_velocity = "increasing"
    elif curr_total_alerts < prev_total_alerts * 0.5:
        alert_velocity = "decreasing"

    # Index comparison -- find recurring suggestions
    prev_indexes = _extract_index_set(previous)
    curr_indexes = _extract_index_set(current)
    recurring_indexes = prev_indexes & curr_indexes

    return {
        "has_previous": True,
        "previous_date": prev_date,
        "previous_file": previous.get("_file", ""),
        "cluster_trends": cluster_trends,
        "tier_changes": tier_changes,
        "improved": improved,
        "degraded": degraded,
        "stable": stable,
        "new_clusters": new_clusters,
        "removed_clusters": removed_clusters,
        "alert_velocity": alert_velocity,
        "curr_total_alerts_7d": curr_total_alerts,
        "prev_total_alerts_7d": prev_total_alerts,
        "recurring_index_count": len(recurring_indexes),
        "recurring_indexes": list(recurring_indexes),
        "org_summary": {
            "improved": improved,
            "degraded": degraded,
            "stable": stable,
            "new": len(new_clusters),
            "removed": len(removed_clusters),
        },
    }


def _compare_cluster(curr: dict, prev: dict) -> dict:
    """Compare a single cluster between current and previous reports."""
    curr_score = curr.get("score", {})
    prev_score = prev.get("score", {})

    curr_tier = curr_score.get("tier", "GREEN")
    prev_tier = prev_score.get("tier", "GREEN")

    curr_ms = curr_score.get("metrics_summary", {})
    prev_ms = prev_score.get("metrics_summary", {})

    # Metric changes
    changes = []
    metric_diffs = {}

    for key, label in [
        ("cpu_avg", "CPU avg"),
        ("connections_max", "Connections max"),
        ("disk_pct", "Disk usage"),
        ("open_alerts", "Open alerts"),
        ("suggested_indexes", "Index suggestions"),
    ]:
        curr_val = curr_ms.get(key)
        prev_val = prev_ms.get(key)

        if curr_val is not None and prev_val is not None:
            if isinstance(curr_val, (int, float)) and isinstance(
                prev_val, (int, float)
            ):
                diff = curr_val - prev_val
                pct = ((curr_val - prev_val) / prev_val * 100) if prev_val != 0 else 0

                metric_diffs[key] = {
                    "current": curr_val,
                    "previous": prev_val,
                    "diff": round(diff, 2),
                    "pct_change": round(pct, 1),
                }

                # Significant changes
                if key == "cpu_avg" and abs(diff) > 10:
                    direction = "increased" if diff > 0 else "decreased"
                    changes.append(
                        f"{label} {direction} from {prev_val:.1f}% to {curr_val:.1f}%"
                    )
                elif key == "open_alerts" and diff != 0:
                    direction = "increased" if diff > 0 else "decreased"
                    changes.append(f"{label} {direction} from {prev_val} to {curr_val}")

    # Determine trend direction
    tier_order = {"RED": 2, "YELLOW": 1, "GREEN": 0}
    curr_severity = tier_order.get(curr_tier, 0)
    prev_severity = tier_order.get(prev_tier, 0)

    if curr_severity > prev_severity:
        arrow = "DEGRADING"
    elif curr_severity < prev_severity:
        arrow = "IMPROVING"
    else:
        # Same tier -- check if metrics are getting worse
        worse_count = sum(
            1 for d in metric_diffs.values() if d.get("pct_change", 0) > 20
        )
        better_count = sum(
            1 for d in metric_diffs.values() if d.get("pct_change", 0) < -20
        )
        if worse_count > better_count:
            arrow = "DEGRADING"
        elif better_count > worse_count:
            arrow = "IMPROVING"
        else:
            arrow = "STABLE"

    tier_change = None
    if curr_tier != prev_tier:
        tier_change = {
            "cluster": curr.get("cluster_name"),
            "from": prev_tier,
            "to": curr_tier,
        }

    return {
        "trend": f"{prev_tier} → {curr_tier}" if curr_tier != prev_tier else curr_tier,
        "arrow": arrow,
        "details": "; ".join(changes) if changes else "No significant changes",
        "metric_diffs": metric_diffs,
        "tier_change": tier_change,
    }


def _extract_index_set(report: dict) -> set:
    """Extract a set of (namespace, index_keys_str) tuples for dedup."""
    indexes = set()
    for c in report.get("clusters", []):
        for idx in c.get("suggested_indexes", []):
            ns = idx.get("namespace", "")
            keys = json.dumps(idx.get("index", []), sort_keys=True)
            indexes.add(f"{ns}:{keys}")
    return indexes
