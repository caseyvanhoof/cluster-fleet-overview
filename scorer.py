"""
Cluster Scorer: assigns RED/YELLOW/GREEN tier to each cluster
with a specific justification citing actual metric values.

Scoring is split into two dimensions:
  - Current Health: based on open alerts + latest metrics (existing logic)
  - Reliability Trend: based on event patterns over the past 7 days
"""

import logging

logger = logging.getLogger(__name__)

# ── Scoring thresholds (single source of truth) ─────────────────
THRESHOLDS = {
    "current_health": {
        "cpu_avg_red": 80,
        "cpu_avg_yellow": 50,
        "memory_pct_red": 85,
        "memory_pct_yellow": 70,
        "disk_pct_red_no_auto": 75,
        "disk_pct_yellow_no_auto": 65,
        "disk_pct_yellow_auto": 85,
        "connections_yellow": 10000,
        "scan_order_yellow": 100,
        "critical_alerts_red": 3,
        "open_alerts_yellow": 1,
        "high_idx_red": 3,
        "high_idx_yellow": 1,
    },
    "reliability": {
        "critical_7d_red": 20,
        "critical_7d_yellow": 5,
        "warning_7d_yellow": 30,
        "recurring_days_red": 5,
        "recurring_days_yellow": 3,
        "query_targeting_yellow": 1000,
    },
}

# Atlas tier RAM mapping (approximate, in MB)
# Used to calculate memory utilization percentage
TIER_RAM_MB = {
    "M10": 2048,
    "M20": 4096,
    "M30": 8192,
    "M40": 16384,
    "M50": 32768,
    "M60": 65536,
    "M80": 131072,
    "M140": 196608,
    "M200": 262144,
    "M300": 393216,
    "M400": 524288,
    "M700": 786432,
    "R40": 16384,
    "R50": 32768,
    "R60": 65536,
    "R80": 131072,
}

# Atlas maximum disk size per tier (GB).
# Standard limits; extended storage can go higher on M40+.
# Used to determine whether auto-scaling still has room to grow.
TIER_MAX_DISK_GB = {
    "M10": 128,
    "M20": 256,
    "M30": 512,
    "M40": 4096,
    "M50": 8192,
    "M60": 8192,
    "M80": 14336,
    "M140": 14336,
    "M200": 14336,
    "M300": 14336,
    "M400": 14336,
    "M700": 14336,
    "R40": 4096,
    "R50": 8192,
    "R60": 8192,
    "R80": 14336,
}


def _get_tier_ram_mb(tier: str) -> int:
    """Get the total RAM in MB for an Atlas tier."""
    return TIER_RAM_MB.get(tier, 0)


def score_cluster(cluster_data: dict) -> dict:
    """Score a single cluster and return tier + justification.

    Tier criteria:
    - RED:    CPU avg > 80%, memory utilization > 85%, 3+ active CRITICAL alerts
    - YELLOW: CPU avg > 50%, memory utilization > 70%, targeting > 1000:1,
              1+ active alert, 3+ high-impact index suggestions
    - GREEN:  None of the above

    Notes:
    - Cluster state UPDATING (e.g., backups) does NOT trigger RED
    - CPU max spikes are not flagged -- only sustained avg matters
    - Cache fill ratio removed -- focusing on memory utilization instead

    Returns:
        dict with tier, justification, reasons (list), and metrics_summary
    """
    metrics = cluster_data.get("metrics", {})
    alerts = cluster_data.get("alerts", {})
    suggested_count = cluster_data.get("suggested_index_count", 0)
    state = cluster_data.get("state", "UNKNOWN")
    cluster_tier = cluster_data.get("tier", "")

    reasons_red = []
    reasons_yellow = []
    reasons_green = []

    # ── Cluster state ────────────────────────────────────────────
    # Only flag truly concerning states, not transient ones like UPDATING
    non_concerning_states = {"IDLE", "UPDATING", "REPAIRING"}
    if state not in non_concerning_states:
        reasons_red.append(f"Cluster state is {state}")

    th = THRESHOLDS["current_health"]

    # ── CPU (avg only -- spikes/max are expected and not flagged) ─
    cpu = metrics.get("SYSTEM_NORMALIZED_CPU_USER", {})
    if cpu:
        cpu_avg = cpu.get("avg", 0)
        cpu_max = cpu.get("max", 0)
        if cpu_avg > th["cpu_avg_red"]:
            reasons_red.append(f"CPU avg {cpu_avg:.1f}% -- sustained high CPU")
        elif cpu_avg > th["cpu_avg_yellow"]:
            reasons_yellow.append(f"CPU avg {cpu_avg:.1f}% -- elevated")
        else:
            reasons_green.append(f"CPU healthy: avg {cpu_avg:.1f}%")

    # ── Memory Utilization ───────────────────────────────────────
    # Compare resident memory against the tier's total RAM
    memory = metrics.get("MEMORY_RESIDENT", {})
    memory_avg_mb = memory.get("avg") if memory else None
    memory_max_mb = memory.get("max") if memory else None
    tier_ram_mb = _get_tier_ram_mb(cluster_tier)
    memory_pct = None

    if memory_avg_mb and tier_ram_mb > 0:
        memory_pct = round((memory_avg_mb / tier_ram_mb) * 100, 1)
        if memory_pct > th["memory_pct_red"]:
            reasons_red.append(
                f"Memory utilization avg {memory_pct:.0f}% "
                f"({memory_avg_mb:.0f}MB / {tier_ram_mb}MB) -- critically high"
            )
        elif memory_pct > th["memory_pct_yellow"]:
            reasons_yellow.append(
                f"Memory utilization avg {memory_pct:.0f}% "
                f"({memory_avg_mb:.0f}MB / {tier_ram_mb}MB)"
            )
        else:
            reasons_green.append(f"Memory healthy: {memory_pct:.0f}% utilized")

    # ── Connections ──────────────────────────────────────────────
    conn = metrics.get("CONNECTIONS", {})
    if conn:
        conn_max = conn.get("max", 0)
        if conn_max > th["connections_yellow"]:
            reasons_yellow.append(f"High connection count: max {conn_max:.0f}")

    # ── Disk Space ───────────────────────────────────────────────
    # Scoring accounts for disk auto-scaling:
    #   - Atlas auto-scales at ~90% and expands automatically.
    #   - If auto-scaling is enabled AND the cluster isn't at the Atlas
    #     max disk limit for its tier, high usage is not critical because
    #     Atlas will expand before it becomes a problem.
    #   - If auto-scaling is OFF, or the disk is already at/near the max
    #     limit (nowhere left to grow), then standard thresholds apply.
    disk_pct = cluster_data.get("disk_pct")
    disk_total = cluster_data.get("disk_gb")
    disk_used = cluster_data.get("disk_used_gb")
    disk_auto = cluster_data.get("disk_auto_scaling", False)
    max_disk_gb = TIER_MAX_DISK_GB.get(cluster_tier, 4096)

    # Check if there's still room for auto-scaling to grow
    # "at ceiling" means current allocation >= 90% of the tier's max
    disk_at_ceiling = disk_total is not None and disk_total >= (max_disk_gb * 0.9)

    if disk_pct is not None:
        disk_detail = f"{disk_pct:.0f}% ({disk_used:.0f}GB / {disk_total}GB)"

        if disk_auto and not disk_at_ceiling:
            # Auto-scaling is on and has room to grow -- Atlas will handle it
            if disk_pct > th["disk_pct_yellow_auto"]:
                # Getting close to the 90% auto-scale trigger, worth noting
                reasons_yellow.append(
                    f"Disk usage at {disk_detail} -- auto-scaling enabled, "
                    f"approaching scale trigger (90%)"
                )
            elif disk_pct > th["disk_pct_yellow_no_auto"]:
                reasons_green.append(
                    f"Disk at {disk_detail} -- auto-scaling enabled, "
                    f"room to grow (max {max_disk_gb}GB)"
                )
            else:
                reasons_green.append(f"Disk healthy: {disk_pct:.0f}% used (auto-scaling enabled)")
        elif disk_auto and disk_at_ceiling:
            # Auto-scaling is on but at the ceiling -- nowhere to grow
            if disk_pct > th["disk_pct_red_no_auto"]:
                reasons_red.append(
                    f"Disk usage at {disk_detail} -- at max disk for {cluster_tier} "
                    f"({max_disk_gb}GB), auto-scaling cannot expand further; "
                    f"consider sharding or archiving data"
                )
            elif disk_pct > th["disk_pct_yellow_no_auto"]:
                reasons_yellow.append(
                    f"Disk usage at {disk_detail} -- nearing max disk for {cluster_tier} "
                    f"({max_disk_gb}GB), auto-scaling has limited room"
                )
            else:
                reasons_green.append(f"Disk healthy: {disk_pct:.0f}% used")
        else:
            # No auto-scaling -- original thresholds apply
            if disk_pct > th["disk_pct_red_no_auto"]:
                reasons_red.append(
                    f"Disk usage at {disk_detail} -- critically high "
                    f"(disk auto-scaling is disabled)"
                )
            elif disk_pct > th["disk_pct_yellow_no_auto"]:
                reasons_yellow.append(
                    f"Disk usage at {disk_detail} (disk auto-scaling is disabled)"
                )
            else:
                reasons_green.append(f"Disk healthy: {disk_pct:.0f}% used")

    # ── Scan and Order ───────────────────────────────────────────
    scan_order = metrics.get("OPERATIONS_SCAN_AND_ORDER", {})
    if scan_order:
        so_max = scan_order.get("max", 0)
        if so_max > th["scan_order_yellow"]:
            reasons_yellow.append(f"In-memory sorts: max {so_max:.0f}/s")

    # ── Alerts ───────────────────────────────────────────────────
    open_count = alerts.get("open_count", 0)
    open_alerts = alerts.get("open", [])

    # Count critical alerts (metric threshold breaches)
    critical_alerts = [
        a for a in open_alerts if a.get("event_type", "").startswith("OUTSIDE_METRIC")
    ]

    if len(critical_alerts) >= th["critical_alerts_red"]:
        reasons_red.append(f"{len(critical_alerts)} active metric threshold alerts")
    elif open_count >= th["open_alerts_yellow"]:
        reasons_yellow.append(f"{open_count} active alert(s)")

    # ── Index Suggestions (priority-based) ─────────────────────
    high_priority_count = cluster_data.get("high_priority_index_count", 0)
    if high_priority_count >= th["high_idx_red"]:
        reasons_red.append(
            f"{high_priority_count} high-priority index suggestion(s) -- "
            f"frequent/slow queries without proper indexing"
        )
    elif high_priority_count >= th["high_idx_yellow"]:
        reasons_yellow.append(
            f"{high_priority_count} high-priority index suggestion(s) from Performance Advisor"
        )

    # ── Determine tier ───────────────────────────────────────────
    # Build a disk auto-scaling note if disk is elevated but covered
    disk_auto_note = ""
    if disk_auto and disk_pct is not None and disk_pct > 50 and not disk_at_ceiling:
        disk_auto_note = (
            f"Disk at {disk_pct:.0f}% but storage auto-scaling is enabled "
            f"(scales at 90%, max {max_disk_gb}GB)"
        )

    if reasons_red:
        tier = "RED"
        justification = "; ".join(reasons_red[:3])
    elif reasons_yellow:
        tier = "YELLOW"
        justification = "; ".join(reasons_yellow[:3])
        if disk_auto_note:
            justification += f"; {disk_auto_note}"
    else:
        tier = "GREEN"
        if reasons_green:
            justification = "All metrics within healthy ranges. " + reasons_green[0]
            if disk_auto_note:
                justification += f". {disk_auto_note}"
        else:
            justification = "No threshold violations or concerns detected."

    # ── Extended metrics summary ────────────────────────────────────
    cache_dirty = metrics.get("CACHE_DIRTY_BYTES", {})
    cache_used = metrics.get("CACHE_USED_BYTES", {})
    query_targeting = metrics.get("QUERY_TARGETING_SCANNED_OBJECTS_PER_RETURNED", {})
    disk_latency_read = cluster_data.get("disk_latency_read", {})
    disk_latency_write = cluster_data.get("disk_latency_write", {})
    disk_queue = cluster_data.get("disk_queue_depth", {})

    return {
        "tier": tier,
        "justification": justification,
        "reasons_red": reasons_red,
        "reasons_yellow": reasons_yellow,
        "reasons_green": reasons_green,
        "recovery_posture": {
            "backup_enabled": cluster_data.get("backup_enabled", False),
            "pit_enabled": cluster_data.get("pit_enabled", False),
        },
        "metrics_summary": {
            "cpu_avg": cpu.get("avg") if cpu else None,
            "cpu_max": cpu.get("max") if cpu else None,
            "connections_max": conn.get("max") if conn else None,
            "memory_avg_mb": memory_avg_mb,
            "memory_max_mb": memory_max_mb,
            "memory_pct": memory_pct,
            "tier_ram_mb": tier_ram_mb,
            "disk_pct": disk_pct,
            "disk_used_gb": disk_used,
            "disk_total_gb": disk_total,
            "open_alerts": open_count,
            "suggested_indexes": suggested_count,
            "high_priority_indexes": high_priority_count,
            # Extended metrics
            "cache_dirty_avg": cache_dirty.get("avg"),
            "cache_dirty_max": cache_dirty.get("max"),
            "cache_used_avg": cache_used.get("avg"),
            "cache_used_max": cache_used.get("max"),
            "query_targeting_avg": query_targeting.get("avg"),
            "query_targeting_max": query_targeting.get("max"),
            "disk_latency_read_avg": disk_latency_read.get("avg"),
            "disk_latency_read_max": disk_latency_read.get("max"),
            "disk_latency_write_avg": disk_latency_write.get("avg"),
            "disk_latency_write_max": disk_latency_write.get("max"),
            "disk_queue_depth_avg": disk_queue.get("avg"),
            "disk_queue_depth_max": disk_queue.get("max"),
        },
    }


def score_reliability(cluster_data: dict) -> dict:
    """Score the reliability trend for a cluster based on event patterns.

    Reliability scoring is independent of current health. A cluster can be
    GREEN on health but YELLOW on reliability if it has recurring events.

    Returns:
        dict with tier, justification, watch_reasons, and event_summary
    """
    event_summary = cluster_data.get("event_summary", {})
    metrics = cluster_data.get("metrics", {})
    th = THRESHOLDS["reliability"]

    critical_7d = event_summary.get("critical_7d", 0)
    warning_7d = event_summary.get("warning_7d", 0)
    has_recurring = event_summary.get("has_recurring_critical", False)
    distinct_crit_days = event_summary.get("distinct_days_with_critical", 0)
    recent_changes = event_summary.get("recent_changes_24h", 0)
    top_types = event_summary.get("top_event_types", {})

    query_targeting = metrics.get("QUERY_TARGETING_SCANNED_OBJECTS_PER_RETURNED", {})
    qt_max = query_targeting.get("max", 0) if query_targeting else 0

    watch_reasons = []

    # ── RED reliability checks ───────────────────────────────────
    is_red = False

    if has_recurring and distinct_crit_days >= th["recurring_days_red"]:
        is_red = True
        # Find which event type(s) are recurring
        recurring_types = [t for t, cnt in top_types.items() if cnt >= 3]
        type_str = ", ".join(recurring_types[:3]) if recurring_types else "multiple types"
        watch_reasons.append(
            f"Recurring critical events on {distinct_crit_days} of 7 days ({type_str})"
        )

    if critical_7d >= th["critical_7d_red"]:
        is_red = True
        watch_reasons.append(
            f"{critical_7d} critical events in 7 days"
        )

    # ── YELLOW reliability checks ────────────────────────────────
    is_yellow = False

    if has_recurring and not is_red:
        is_yellow = True
        recurring_types = [t for t, cnt in top_types.items() if cnt >= 3]
        type_str = ", ".join(recurring_types[:3]) if recurring_types else "multiple types"
        watch_reasons.append(
            f"Recurring critical events on {distinct_crit_days} of 7 days ({type_str})"
        )

    if critical_7d >= th["critical_7d_yellow"] and not is_red:
        is_yellow = True
        watch_reasons.append(
            f"{critical_7d} critical events in 7 days"
        )

    if warning_7d >= th["warning_7d_yellow"]:
        is_yellow = True
        watch_reasons.append(
            f"{warning_7d} warning events in 7 days"
        )

    if qt_max > th["query_targeting_yellow"]:
        is_yellow = True
        watch_reasons.append(
            f"Query targeting ratio max {qt_max:.0f}:1"
        )

    if recent_changes >= 5:
        watch_reasons.append(
            f"{recent_changes} config/deployment changes in past 24h"
        )

    # ── Determine tier ───────────────────────────────────────────
    if is_red:
        tier = "RED"
        justification = "; ".join(watch_reasons[:3])
    elif is_yellow:
        tier = "YELLOW"
        justification = "; ".join(watch_reasons[:3])
    else:
        tier = "GREEN"
        justification = "No recurring critical event patterns detected"

    return {
        "tier": tier,
        "justification": justification,
        "watch_reasons": watch_reasons,
        "event_summary": event_summary,
    }


def score_all_clusters(clusters: list) -> list:
    """Score all clusters and return enriched list."""
    scored = []
    for c in clusters:
        score = score_cluster(c)
        c["score"] = score
        reliability = score_reliability(c)
        c["reliability"] = reliability
        scored.append(c)

    # Sort: RED first, then YELLOW, then GREEN
    tier_order = {"RED": 0, "YELLOW": 1, "GREEN": 2}
    scored.sort(key=lambda c: tier_order.get(c["score"]["tier"], 9))

    return scored
