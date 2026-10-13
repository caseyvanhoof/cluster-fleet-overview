"""
Org-level collector: scans all p-* clusters across projects,
collects metrics (primary per shard), alerts, suggested indexes,
and activity feed events.
"""

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from atlas_cli import AtlasClient

logger = logging.getLogger(__name__)

# Key metrics for the scorecard (primary node only)
# Note: DISK_PARTITION_* metrics are not available via the process
# measurements endpoint -- they require the disk endpoint.
# Process-level metrics for the scorecard (primary node only)
SCORECARD_METRICS = [
    "SYSTEM_NORMALIZED_CPU_USER",
    "SYSTEM_NORMALIZED_CPU_KERNEL",
    "CONNECTIONS",
    "MEMORY_RESIDENT",
    "OPERATIONS_SCAN_AND_ORDER",
]

# No optional metrics -- we only query primaries where oplog lag doesn't apply
OPTIONAL_METRICS = []

# Extended metrics for deeper insight (each tried individually, silently skipped if unavailable)
EXTENDED_METRICS = [
    "OPCOUNTER_QUERY",
    "OPCOUNTER_INSERT",
    "OPCOUNTER_UPDATE",
    "OPCOUNTER_DELETE",
    "CACHE_DIRTY_BYTES",
    "CACHE_USED_BYTES",
    "CACHE_BYTES_READ_INTO",
    "CACHE_BYTES_WRITTEN_FROM",
    "TICKETS_AVAILABLE_READS",
    "TICKETS_AVAILABLE_WRITES",
    "QUERY_TARGETING_SCANNED_OBJECTS_PER_RETURNED",
    "EXTRA_INFO_PAGE_FAULTS",
    "GLOBAL_LOCK_CURRENT_QUEUE_TOTAL",
]

# Disk-level latency/queue metrics (fetched via disk measurements endpoint).
# Each is tried individually -- an invalid name won't break the others.
# Valid names confirmed via atlas metrics disks describe:
#   DISK_PARTITION_LATENCY_READ, DISK_PARTITION_LATENCY_WRITE,
#   DISK_QUEUE_DEPTH, DISK_PARTITION_IOPS_READ, DISK_PARTITION_IOPS_WRITE
DISK_EXTENDED_METRICS = [
    "DISK_PARTITION_LATENCY_READ",
    "DISK_PARTITION_LATENCY_WRITE",
    "DISK_QUEUE_DEPTH",
    "DISK_PARTITION_IOPS_READ",
    "DISK_PARTITION_IOPS_WRITE",
]

# ── Event severity classification ────────────────────────────────
# Events are classified as CRITICAL, WARNING, or INFO.
# Noise events (routine automated actions) are filtered out entirely.

CRITICAL_EVENT_TYPES = {
    "OUTSIDE_METRIC_THRESHOLD",
    "HOST_DOWN",
    "HOST_ROLLBACK",
    "HOST_HAS_INDEX_SUGGESTIONS",
    "REPLICATION_OPLOG_WINDOW_RUNNING_OUT",
    "NO_PRIMARY",
    "TOO_MANY_ELECTIONS",
    "PRIMARY_ELECTED",
    "CLUSTER_MONGOS_IS_MISSING",
    "HOST_SSL_CERTIFICATE_CURRENT_AND_NEEDS_RENEWAL",
    "HOST_SSL_CERTIFICATE_STALE_AND_NEEDS_RENEWAL",
    "DISK_PARTITION_RUNNING_OUT_OF_SPACE",
}

WARNING_EVENT_TYPES = {
    "HOST_RESTARTED",
    "HOST_MONGOT_RESTARTED",
    "HOST_NOW_PRIMARY",
    "HOST_NOW_SECONDARY",
    "HOST_RECOVERING",
    "HOST_UP",
    "CLUSTER_UPDATE_STARTED",
    "CLUSTER_UPDATE_COMPLETED",
    "PLAN_STARTED",
    "PLAN_COMPLETED",
    "MONGODB_USER_ADDED",
    "MONGODB_USER_UPDATED",
    "MONGODB_USER_DELETED",
    "MONGODB_ROLE_ADDED",
    "MONGODB_ROLE_DELETED",
    "MONGODB_ROLE_UPDATED",
    "CLUSTER_SCALED",
    "NDS_PROXY_RESTARTED",
    "MAINTENANCE_STARTED",
    "MAINTENANCE_COMPLETE",
    "PROJECT_BYPASSED_MAINTENANCE",
}

# Events to filter out entirely -- routine noise
NOISE_EVENT_TYPES = {
    "MONGODB_LOGS_DOWNLOADED",
    "INSIDE_METRIC_THRESHOLD",
    "CPS_SNAPSHOT_STARTED",
    "CPS_SNAPSHOT_SUCCESSFUL",
    "CPS_SNAPSHOT_FAILED",
    "CPS_COPY_SNAPSHOT_STARTED",
    "CPS_COPY_SNAPSHOT_SUCCESSFUL",
    "CPS_COPY_SNAPSHOT_FAILED",
    "CPS_RESTORE_SUCCESSFUL",
    "CPS_RESTORE_STARTED",
    "CPS_EXPORT_SUCCESSFUL",
    "CPS_EXPORT_STARTED",
    "FTDC_SETTINGS_UPDATED",
    "AUTOMATION_CONFIG_PUBLISHED_AUDIT",
    "FEATURE_FLAG_MAINTENANCE",
    "DATA_EXPLORER_QUERY",
    "DATA_EXPLORER",
}


def _parse_ts(ts_str: str):
    """Parse an ISO timestamp string."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _metric_stats(measurements: list, metric_name: str) -> dict:
    """Extract avg/max/current for a metric from measurements."""
    for m in measurements:
        if m.get("name") == metric_name:
            values = [
                dp.get("value")
                for dp in m.get("dataPoints", [])
                if dp.get("value") is not None
            ]
            if not values:
                return {}
            return {
                "avg": round(sum(values) / len(values), 2),
                "max": round(max(values), 2),
                "current": round(values[-1], 2),
                "data_points": len(values),
            }
    return {}


def _build_cluster_event_summary(activity_events: dict, project_id: str, cluster_name: str) -> dict:
    """Build per-cluster event aggregation from the activity feed."""
    events = activity_events.get("events", [])

    # Match events to this cluster by project_id
    cluster_events = [e for e in events
                      if e.get("project_id") == project_id
                      and e.get("severity") in ("CRITICAL", "WARNING")]

    if not cluster_events:
        return {"critical_7d": 0, "warning_7d": 0, "total_7d": 0,
                "distinct_days_with_critical": 0, "top_event_types": {},
                "has_recurring_critical": False, "recent_changes_24h": 0}

    critical = [e for e in cluster_events if e["severity"] == "CRITICAL"]
    warnings = [e for e in cluster_events if e["severity"] == "WARNING"]

    # Count distinct days with critical events
    crit_days = set()
    for e in critical:
        day = e.get("created", "")[:10]
        if day:
            crit_days.add(day)

    # Top event types
    type_counts = Counter(e["event_type"] for e in cluster_events)
    top_types = dict(type_counts.most_common(5))

    # Recurring = same critical event type on 3+ distinct days
    crit_type_days = {}
    for e in critical:
        etype = e.get("event_type", "")
        day = e.get("created", "")[:10]
        if etype and day:
            crit_type_days.setdefault(etype, set()).add(day)
    has_recurring = any(len(days) >= 3 for days in crit_type_days.values())

    # Count config/deployment changes in recent 24h
    now = datetime.now(timezone.utc)
    change_types = {"CLUSTER_UPDATE_STARTED", "CLUSTER_UPDATE_COMPLETED",
                    "PLAN_STARTED", "PLAN_COMPLETED", "MONGODB_USER_UPDATED",
                    "MONGODB_USER_ADDED", "MONGODB_USER_DELETED",
                    "MONGODB_ROLE_ADDED", "MONGODB_ROLE_DELETED",
                    "MONGODB_ROLE_UPDATED", "CLUSTER_SCALED",
                    "PROJECT_BYPASSED_MAINTENANCE"}
    recent_changes = 0
    for e in cluster_events:
        created = e.get("created", "")
        if created:
            try:
                ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                if (now - ts).total_seconds() < 86400:
                    if e.get("event_type") in change_types:
                        recent_changes += 1
            except (ValueError, TypeError):
                pass

    return {
        "critical_7d": len(critical),
        "warning_7d": len(warnings),
        "total_7d": len(cluster_events),
        "distinct_days_with_critical": len(crit_days),
        "top_event_types": top_types,
        "has_recurring_critical": has_recurring,
        "recent_changes_24h": recent_changes,
    }


def collect_org_data(
    client: AtlasClient,
    project_ids: list,
    cluster_prefix: str = "p-",
) -> dict:
    """Collect data for all production clusters across projects.

    Args:
        client: AtlasClient instance
        project_ids: List of project IDs to scan
        cluster_prefix: Only include clusters starting with this prefix

    Returns:
        dict with collected_at, clusters (list of cluster data dicts)
    """
    now = datetime.now(timezone.utc)
    all_clusters = []

    for proj_id in project_ids:
        logger.info(f"Scanning project {proj_id}...")
        client.set_project_id(proj_id)

        # List clusters, filter to production
        try:
            clusters = client.list_clusters()
        except RuntimeError as e:
            logger.error(f"Failed to list clusters for {proj_id}: {e}")
            continue

        prod_clusters = [
            c
            for c in clusters
            if c.get("name", "").lower().startswith(cluster_prefix.lower())
        ]

        if not prod_clusters:
            logger.info(f"  No {cluster_prefix}* clusters in project {proj_id}")
            continue

        logger.info(f"  Found {len(prod_clusters)} production cluster(s)")

        # Get all processes once per project
        try:
            all_processes = client.list_processes()
        except RuntimeError:
            all_processes = []

        # Get all alerts once per project
        try:
            all_alerts = client.get_all_alerts(max_pages=10)
        except RuntimeError:
            all_alerts = []

        for cluster in prod_clusters:
            cluster_name = cluster.get("name", "?")
            logger.info(f"  Collecting data for {cluster_name}...")

            cluster_data = _collect_cluster_data(
                client, cluster, cluster_name, all_processes, all_alerts, now
            )
            cluster_data["project_id"] = proj_id
            all_clusters.append(cluster_data)

    # Collect activity feed events (past 7 days) across all projects
    logger.info("Collecting activity feed events (past 7 days)...")
    activity_events = collect_activity_events(client, project_ids)

    # Attach per-cluster event summary for reliability scoring
    for c in all_clusters:
        c["event_summary"] = _build_cluster_event_summary(
            activity_events, c["project_id"], c["cluster_name"]
        )

    return {
        "collected_at": now.isoformat(),
        "cluster_count": len(all_clusters),
        "project_count": len(project_ids),
        "clusters": all_clusters,
        "activity_events": activity_events,
    }


def _collect_cluster_data(
    client: AtlasClient,
    cluster: dict,
    cluster_name: str,
    all_processes: list,
    all_alerts: list,
    now: datetime,
) -> dict:
    """Collect all data for a single cluster."""

    # Basic cluster info
    state = cluster.get("stateName", "UNKNOWN")
    cluster_type = cluster.get("clusterType", "?")
    mongo_version = cluster.get("mongoDBVersion", "?")
    disk_gb = cluster.get("diskSizeGB")

    tier = "?"
    regions = []
    for rs in cluster.get("replicationSpecs", []):
        for rc in rs.get("regionConfigs", []):
            es = rc.get("electableSpecs", {})
            if es.get("instanceSize"):
                tier = es["instanceSize"]
            if disk_gb is None and es.get("diskSizeGB"):
                disk_gb = es["diskSizeGB"]
            region = rc.get("regionName", "?")
            node_count = es.get("nodeCount", 0)
            regions.append({"region": region, "nodes": node_count, "tier": tier})

    # Auto-scaling info
    # autoScaling lives inside replicationSpecs[].regionConfigs[], not at root.
    # Check all regionConfigs; if any has it enabled, treat it as enabled.
    compute_auto = False
    disk_auto = False
    for rs in cluster.get("replicationSpecs", []):
        for rc in rs.get("regionConfigs", []):
            rc_auto = rc.get("autoScaling", {})
            if rc_auto.get("compute", {}).get("enabled", False):
                compute_auto = True
            if rc_auto.get("diskGB", {}).get("enabled", False):
                disk_auto = True

    # Determine if sharded
    is_sharded = cluster_type == "SHARDED"

    # Get primary process(es) and collect metrics
    metrics_data = {}
    primary_id = None

    if is_sharded:
        shard_primaries = client.get_shard_primaries(cluster_name, all_processes)
        logger.info(f"    Sharded: {len(shard_primaries)} shard primary(ies)")
        all_measurements = []
        for sp in shard_primaries:
            if not primary_id:
                primary_id = sp["id"]
            try:
                data = client.get_process_metrics(
                    sp["id"], SCORECARD_METRICS, period="P7D", granularity="PT1H"
                )
                all_measurements.extend(data.get("measurements", []))
            except RuntimeError as e:
                logger.warning(
                    f"    Metrics failed for shard {sp['replicaSetName']}: {e}"
                )
            # Try optional metrics individually (may not exist on all clusters)
            for m in OPTIONAL_METRICS:
                try:
                    data = client.get_process_metrics(
                        sp["id"], [m], period="P7D", granularity="PT1H"
                    )
                    all_measurements.extend(data.get("measurements", []))
                except RuntimeError:
                    pass
            # Try extended metrics individually (silently skip if unavailable)
            for m in EXTENDED_METRICS:
                try:
                    data = client.get_process_metrics(
                        sp["id"], [m], period="P7D", granularity="PT1H"
                    )
                    all_measurements.extend(data.get("measurements", []))
                except Exception:
                    pass
        metrics_data = {"measurements": all_measurements}
    else:
        primary_id = client.get_primary_process_id(cluster_name, all_processes)
        logger.info(f"    Primary process: {primary_id}")
        if primary_id:
            try:
                metrics_data = client.get_process_metrics(
                    primary_id, SCORECARD_METRICS, period="P7D", granularity="PT1H"
                )
                logger.info(
                    f"    Got {len(metrics_data.get('measurements', []))} measurements"
                )
            except RuntimeError as e:
                logger.warning(f"    Metrics failed for {primary_id}: {e}")
            # Try optional metrics individually (silently skip if unavailable)
            for m in OPTIONAL_METRICS:
                try:
                    data = client.get_process_metrics(
                        primary_id, [m], period="P7D", granularity="PT1H"
                    )
                    new_m = data.get("measurements", [])
                    if new_m:
                        metrics_data.setdefault("measurements", []).extend(new_m)
                        logger.info(f"    Optional metric {m}: OK")
                except Exception:
                    logger.info(f"    Optional metric {m}: not available (skipped)")
            # Try extended metrics individually (silently skip if unavailable)
            for m in EXTENDED_METRICS:
                try:
                    data = client.get_process_metrics(
                        primary_id, [m], period="P7D", granularity="PT1H"
                    )
                    new_m = data.get("measurements", [])
                    if new_m:
                        metrics_data.setdefault("measurements", []).extend(new_m)
                except Exception:
                    pass

    # Extract metric stats
    measurements = metrics_data.get("measurements", [])
    metric_summary = {}
    for metric_name in SCORECARD_METRICS:
        stats = _metric_stats(measurements, metric_name)
        if stats:
            metric_summary[metric_name] = stats
    # Also extract extended metrics stats
    for metric_name in EXTENDED_METRICS:
        stats = _metric_stats(measurements, metric_name)
        if stats:
            metric_summary[metric_name] = stats

    # Filter alerts for this cluster
    cluster_alerts = [
        a
        for a in all_alerts
        if a.get("clusterName", "").lower() == cluster_name.lower()
    ]

    open_alerts = [a for a in cluster_alerts if a.get("status") == "OPEN"]
    past_7d_alerts = []
    past_30d_alerts = []
    for a in cluster_alerts:
        created = _parse_ts(a.get("created", ""))
        if created:
            age = now - created
            if age.days < 7:
                past_7d_alerts.append(a)
            if age.days < 30:
                past_30d_alerts.append(a)

    # Alert type breakdown
    alert_types_30d = dict(
        Counter(a.get("eventTypeName", "?") for a in past_30d_alerts).most_common(10)
    )

    # Suggested indexes with execution data for priority scoring
    suggested_indexes = []
    high_priority_count = 0
    primary_id = client.get_primary_process_id(cluster_name, all_processes)
    if primary_id:
        try:
            raw_result = client.get_suggested_indexes(primary_id)
            # raw_result could be a list or dict with suggestedIndexes + shapes
            if isinstance(raw_result, dict):
                raw_suggestions = raw_result.get("suggestedIndexes", raw_result)
                shapes = raw_result.get("shapes", [])
            elif isinstance(raw_result, list):
                raw_suggestions = raw_result
                shapes = []
            else:
                raw_suggestions = []
                shapes = []

            # Build shape lookup by namespace for execution stats
            shape_by_ns = {}
            for shape in shapes:
                ns = shape.get("namespace", "")
                if ns and ns not in shape_by_ns:
                    shape_by_ns[ns] = shape

            for s in raw_suggestions:
                weight = s.get("weight", 0)
                if weight < 0.5:
                    continue  # skip low-weight suggestions entirely

                ns = s.get("namespace", "")
                shape = shape_by_ns.get(ns, {})
                exec_count = shape.get("count", 0)
                avg_ms = shape.get("avgMs", 0)

                # Calculate executions per hour (7-day window = 168 hours)
                exec_per_hour = round(exec_count / 168, 3) if exec_count else 0

                # Determine priority
                if exec_per_hour >= 1.0 and avg_ms >= 500:
                    priority = "HIGH"
                    high_priority_count += 1
                elif exec_per_hour >= 1.0 or avg_ms >= 500:
                    priority = "MEDIUM"
                    high_priority_count += 1
                else:
                    priority = "LOW"

                suggested_indexes.append(
                    {
                        "namespace": ns,
                        "index": s.get("index", []),
                        "weight": weight,
                        "exec_per_hour": exec_per_hour,
                        "avg_ms": round(avg_ms, 1),
                        "exec_count_7d": exec_count,
                        "priority": priority,
                    }
                )
        except RuntimeError as e:
            logger.warning(f"    Index suggestions failed: {e}")

    # Disk usage -- get from cluster config (diskSizeGB is provisioned)
    # and try to estimate used from disk metrics if available
    disk_used_gb = None
    disk_total_gb = disk_gb
    disk_pct = None
    disk_latency_read = {}
    disk_latency_write = {}
    disk_queue_depth = {}

    if primary_id:
        try:
            disks = client.list_disks(primary_id)
            if disks:
                partition = disks[0].get("partitionName", "data")
                # Fetch essential disk space metric first (must not fail)
                disk_data = client.get_disk_measurements(
                    primary_id,
                    partition,
                    ["DISK_PARTITION_SPACE_PERCENT_USED"],
                    granularity="PT1H",
                    period="P1D",
                )
                for m in disk_data.get("measurements", []):
                    if m.get("name") == "DISK_PARTITION_SPACE_PERCENT_USED":
                        values = [
                            dp.get("value")
                            for dp in m.get("dataPoints", [])
                            if dp.get("value") is not None
                        ]
                        if values:
                            disk_pct = round(max(values), 1)
                            if disk_total_gb:
                                disk_used_gb = round(disk_total_gb * disk_pct / 100, 1)
                # Try extended disk metrics individually (skip on failure)
                for dm in DISK_EXTENDED_METRICS:
                    try:
                        ext_data = client.get_disk_measurements(
                            primary_id, partition, [dm],
                            granularity="PT1H", period="P7D",
                        )
                        stats = _metric_stats(ext_data.get("measurements", []), dm)
                        if stats:
                            if dm == "DISK_PARTITION_LATENCY_READ":
                                disk_latency_read = stats
                            elif dm == "DISK_PARTITION_LATENCY_WRITE":
                                disk_latency_write = stats
                            elif dm == "DISK_QUEUE_DEPTH":
                                disk_queue_depth = stats
                    except RuntimeError:
                        pass
        except RuntimeError as e:
            logger.info(f"    Disk metrics not available: {e}")

    # Backup and PITR info from cluster config
    backup_enabled = cluster.get("backupEnabled", False)
    pit_enabled = cluster.get("pitEnabled", False)
    # If pitEnabled isn't at root, check inside replicationSpecs or config
    if not pit_enabled:
        pit_enabled = cluster.get("config", {}).get("pitEnabled", False)

    return {
        "cluster_name": cluster_name,
        "state": state,
        "cluster_type": cluster_type,
        "mongodb_version": mongo_version,
        "tier": tier,
        "disk_gb": disk_gb,
        "disk_used_gb": disk_used_gb,
        "disk_pct": disk_pct,
        "disk_latency_read": disk_latency_read,
        "disk_latency_write": disk_latency_write,
        "disk_queue_depth": disk_queue_depth,
        "regions": regions,
        "compute_auto_scaling": compute_auto,
        "disk_auto_scaling": disk_auto,
        "is_sharded": is_sharded,
        "backup_enabled": backup_enabled,
        "pit_enabled": pit_enabled,
        "metrics": metric_summary,
        "alerts": {
            "open_count": len(open_alerts),
            "open": [
                {
                    "event_type": a.get("eventTypeName", "?"),
                    "metric": a.get("metricName", ""),
                    "value": a.get("currentValue", {}).get("number"),
                    "created": a.get("created", ""),
                }
                for a in open_alerts
            ],
            "past_7d_count": len(past_7d_alerts),
            "past_30d_count": len(past_30d_alerts),
            "types_30d": alert_types_30d,
        },
        "suggested_indexes": suggested_indexes,
        "suggested_index_count": len(suggested_indexes),
        "high_priority_index_count": high_priority_count,
    }


# ── Activity Feed Events ────────────────────────────────────────


def _classify_event_severity(event_type: str) -> str:
    """Classify an event type into CRITICAL, WARNING, or INFO.

    Returns empty string for noise events that should be filtered out.
    """
    if event_type in NOISE_EVENT_TYPES:
        return ""
    if event_type in CRITICAL_EVENT_TYPES:
        return "CRITICAL"
    if event_type in WARNING_EVENT_TYPES:
        return "WARNING"
    # Unknown event types default to INFO (still shown, not filtered)
    return "INFO"


def _deduplicate_events(events: list) -> list:
    """Deduplicate events that fire per-node into a single event per cluster.

    Atlas fires metric threshold events per node, resulting in many duplicate
    entries for the same event on the same cluster at the same time. This
    groups by (eventType, replicaSetName/clusterName, metricName, minute)
    and keeps just one representative event per group with a count.
    """
    groups = {}
    for e in events:
        event_type = e.get("event_type", "")
        cluster = e.get("cluster_name") or e.get("replica_set", "") or ""
        metric = e.get("metric_name", "")
        created = e.get("created", "")
        # Group by minute (strip seconds)
        time_bucket = created[:16] if created else ""
        key = (event_type, cluster, metric, time_bucket)

        if key not in groups:
            groups[key] = {**e, "node_count": 1}
        else:
            groups[key]["node_count"] = groups[key].get("node_count", 1) + 1
            # Keep the highest metric value
            if e.get("metric_value") is not None:
                existing = groups[key].get("metric_value")
                if existing is None or e["metric_value"] > existing:
                    groups[key]["metric_value"] = e["metric_value"]
                    groups[key]["metric_units"] = e.get("metric_units", "")

    return sorted(groups.values(), key=lambda x: x.get("created", ""), reverse=True)


def collect_activity_events(
    client: AtlasClient, project_ids: list
) -> dict:
    """Collect and classify activity feed events from the past 7 days.

    Returns a dict with:
        - events: list of classified event dicts (deduplicated, sorted newest first)
        - summary: counts by severity
        - critical_count: number of critical events
        - warning_count: number of warning events
        - top_event_types: most common event types
        - filtered_noise_count: how many noise events were filtered out
    """
    now = datetime.now(timezone.utc)
    min_date = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S-0000")

    all_events = []
    noise_count = 0

    for proj_id in project_ids:
        client.set_project_id(proj_id)
        try:
            raw_events = client.get_project_events(min_date=min_date, max_pages=5)
        except Exception as e:
            logger.warning(f"Failed to fetch events for project {proj_id}: {e}")
            continue

        for event in raw_events:
            event_type = event.get("eventTypeName", "UNKNOWN")
            severity = _classify_event_severity(event_type)

            if not severity:
                noise_count += 1
                continue

            # Extract cluster/replica info
            cluster_name = event.get("clusterName", "")
            replica_set = event.get("replicaSetName", "")
            hostname = event.get("hostname", "")
            port = event.get("port", "")

            # Extract metric value if present
            current_value = event.get("currentValue", {})
            metric_value = current_value.get("number") if current_value else None
            metric_units = current_value.get("units", "") if current_value else ""
            metric_name = event.get("metricName", "")

            all_events.append({
                "event_type": event_type,
                "severity": severity,
                "created": event.get("created", ""),
                "project_id": proj_id,
                "cluster_name": cluster_name,
                "replica_set": replica_set,
                "hostname": hostname,
                "port": port,
                "metric_name": metric_name,
                "metric_value": round(metric_value, 2) if metric_value is not None else None,
                "metric_units": metric_units,
                "user": event.get("username", event.get("publicKey", "")),
            })

    # Deduplicate per-node events
    deduped = _deduplicate_events(all_events)

    critical = [e for e in deduped if e["severity"] == "CRITICAL"]
    warnings = [e for e in deduped if e["severity"] == "WARNING"]
    info = [e for e in deduped if e["severity"] == "INFO"]

    # Top event types
    type_counts = Counter(e["event_type"] for e in deduped)
    top_types = dict(type_counts.most_common(10))

    logger.info(
        f"  Activity feed: {len(critical)} critical, {len(warnings)} warning, "
        f"{len(info)} info events ({noise_count} noise filtered)"
    )

    return {
        "events": deduped,
        "critical_count": len(critical),
        "warning_count": len(warnings),
        "info_count": len(info),
        "total_count": len(deduped),
        "filtered_noise_count": noise_count,
        "top_event_types": top_types,
    }
