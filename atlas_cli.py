"""
Atlas CLI client for org-level cluster overview.

Trimmed version -- only the methods needed for the weekly scorecard:
clusters, processes, metrics, alerts, and suggested indexes.
"""

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class AtlasClient:
    """Atlas CLI client for org-level scanning."""

    def __init__(self, project_id: str = ""):
        self.project_id = project_id

        try:
            result = self._run_raw(["atlas", "--version"])
            logger.info(f"Atlas CLI: {result.strip()}")
        except FileNotFoundError:
            raise RuntimeError("atlas CLI not found")

    def set_project_id(self, project_id: str):
        self.project_id = project_id

    def _run_raw(self, cmd: list, timeout: int = 120) -> str:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(f"Atlas CLI failed: {stderr[:300]}")
        return result.stdout

    def _run_json(self, cmd: list, timeout: int = 120):
        output = self._run_raw(cmd, timeout=timeout)
        if not output.strip():
            return {}
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return {}

    def _base_args(self) -> list:
        return ["--projectId", self.project_id, "--output", "json"]

    # ── Projects ─────────────────────────────────────────────────

    def list_projects(self, org_id: str) -> list:
        """List all projects in an organization."""
        result = self._run_json(
            [
                "atlas",
                "projects",
                "list",
                "--orgId",
                org_id,
                "--output",
                "json",
                "--limit",
                "500",
            ]
        )
        if isinstance(result, dict):
            return result.get("results", [])
        return result if isinstance(result, list) else []

    # ── Clusters ─────────────────────────────────────────────────

    def list_clusters(self) -> list:
        result = self._run_json(["atlas", "clusters", "list"] + self._base_args())
        if isinstance(result, dict):
            return result.get("results", [])
        return result if isinstance(result, list) else []

    def get_cluster(self, cluster_name: str) -> dict:
        result = self._run_json(
            ["atlas", "clusters", "describe", cluster_name] + self._base_args()
        )
        return result if isinstance(result, dict) else {}

    # ── Processes ────────────────────────────────────────────────

    def list_processes(self) -> list:
        result = self._run_json(["atlas", "processes", "list"] + self._base_args())
        if isinstance(result, dict):
            return result.get("results", [])
        return result if isinstance(result, list) else []

    def get_primary_process_id(self, cluster_name: str, all_processes: list) -> str:
        """Find primary process ID for a cluster from the process list."""
        cluster_lower = cluster_name.lower()
        matched = [
            p
            for p in all_processes
            if cluster_lower in (p.get("userAlias") or "").lower()
            or cluster_lower in (p.get("hostname") or "").lower()
        ]

        for p in matched:
            if p.get("typeName") in ("REPLICA_PRIMARY", "SHARD_PRIMARY"):
                pid = p.get("id", "")
                if pid and ":" not in pid:
                    pid = f"{pid}:{p.get('port', 27017)}"
                return pid

        if matched:
            p = matched[0]
            pid = p.get("id", "")
            if pid and ":" not in pid:
                pid = f"{pid}:{p.get('port', 27017)}"
            return pid
        return ""

    def get_shard_primaries(self, cluster_name: str, all_processes: list) -> list:
        """For sharded clusters, get the primary of each shard."""
        cluster_lower = cluster_name.lower()
        matched = [
            p
            for p in all_processes
            if cluster_lower in (p.get("userAlias") or "").lower()
            or cluster_lower in (p.get("hostname") or "").lower()
        ]

        primaries = []
        seen_rs = set()
        for p in matched:
            if p.get("typeName") in ("REPLICA_PRIMARY", "SHARD_PRIMARY"):
                rs = p.get("replicaSetName", "")
                if rs not in seen_rs:
                    seen_rs.add(rs)
                    pid = p.get("id", "")
                    if pid and ":" not in pid:
                        pid = f"{pid}:{p.get('port', 27017)}"
                    primaries.append(
                        {
                            "id": pid,
                            "replicaSetName": rs,
                            "typeName": p.get("typeName"),
                        }
                    )
        return primaries

    # ── Metrics ──────────────────────────────────────────────────

    def get_process_metrics(
        self,
        process_id: str,
        metrics: list,
        period: str = "P7D",
        granularity: str = "PT1H",
    ) -> dict:
        """Get metrics for a process. Batches at 10 max per call."""
        BATCH_SIZE = 10
        all_measurements = []

        for i in range(0, len(metrics), BATCH_SIZE):
            batch = metrics[i : i + BATCH_SIZE]
            cmd = [
                "atlas",
                "metrics",
                "processes",
                process_id,
                "--projectId",
                self.project_id,
                "--granularity",
                granularity,
                "--period",
                period,
                "--output",
                "json",
            ]
            for m in batch:
                cmd.extend(["--type", m])

            try:
                result = self._run_json(cmd)
                if isinstance(result, dict):
                    all_measurements.extend(result.get("measurements", []))
            except RuntimeError as e:
                detail = str(e)
                bad = re.search(r"Params: \[(\w+)\]", detail)
                if bad:
                    bad_metric = bad.group(1)
                    logger.info(f"Metric '{bad_metric}' not available, skipping")
                    remaining = [m for m in batch if m != bad_metric]
                    for m in remaining:
                        try:
                            r = self._run_json(
                                [
                                    "atlas",
                                    "metrics",
                                    "processes",
                                    process_id,
                                    "--projectId",
                                    self.project_id,
                                    "--granularity",
                                    granularity,
                                    "--period",
                                    period,
                                    "--output",
                                    "json",
                                    "--type",
                                    m,
                                ]
                            )
                            if isinstance(r, dict):
                                all_measurements.extend(r.get("measurements", []))
                        except RuntimeError:
                            pass
                else:
                    if len(batch) == 1:
                        logger.info(
                            f"Metric '{batch[0]}' not available for {process_id}"
                        )
                    else:
                        logger.warning(
                            f"Metrics batch failed for {process_id}: {detail[:500]}"
                        )

        return {"measurements": all_measurements}

    # ── Disk Measurements ──────────────────────────────────────

    def list_disks(self, process_id: str) -> list:
        try:
            result = self._run_json(
                [
                    "atlas",
                    "metrics",
                    "disks",
                    "list",
                    process_id,
                ]
                + self._base_args()
            )
            if isinstance(result, dict):
                return result.get("results", [])
            return result if isinstance(result, list) else []
        except RuntimeError as e:
            logger.warning(f"Failed to list disks for {process_id}: {e}")
            return []

    def get_disk_measurements(
        self,
        process_id: str,
        partition_name: str,
        metrics: list,
        granularity: str = "PT1H",
        period: str = "P1D",
    ) -> dict:
        cmd = [
            "atlas",
            "metrics",
            "disks",
            "describe",
            process_id,
            partition_name,
            "--projectId",
            self.project_id,
            "--granularity",
            granularity,
            "--period",
            period,
            "--output",
            "json",
        ]
        for m in metrics:
            cmd.extend(["--type", m])
        try:
            result = self._run_json(cmd)
            return result if isinstance(result, dict) else {"measurements": []}
        except RuntimeError as e:
            logger.warning(f"Disk metrics failed for {process_id}: {e}")
            return {"measurements": []}

    # ── Alerts ───────────────────────────────────────────────────

    def get_all_alerts(self, max_pages: int = 10) -> list:
        """Fetch alerts with pagination (newest pages first)."""
        cmd = [
            "atlas",
            "alerts",
            "list",
            "--projectId",
            self.project_id,
            "--output",
            "json",
            "--limit",
            "1",
        ]
        result = self._run_json(cmd)
        total = result.get("totalCount", 0) if isinstance(result, dict) else 0
        if total == 0:
            return []

        per_page = 500
        total_pages = (total + per_page - 1) // per_page
        start_page = max(1, total_pages - max_pages + 1)
        all_results = []

        for page in range(start_page, total_pages + 1):
            cmd = [
                "atlas",
                "alerts",
                "list",
                "--projectId",
                self.project_id,
                "--output",
                "json",
                "--limit",
                str(per_page),
                "--page",
                str(page),
            ]
            result = self._run_json(cmd)
            if isinstance(result, dict):
                all_results.extend(result.get("results", []))

        return all_results

    # ── Events / Activity Feed ──────────────────────────────────

    def get_project_events(
        self, min_date: str, max_date: str = "", max_pages: int = 5
    ) -> list:
        """Fetch project events within a date range.

        Args:
            min_date: ISO 8601 start date (e.g. 2026-09-21T00:00:00-0000)
            max_date: ISO 8601 end date (optional)
            max_pages: Max pages to fetch (500 events/page)

        Returns:
            List of event dicts
        """
        # First call to get total count
        cmd = [
            "atlas", "events", "projects", "list",
            "--projectId", self.project_id,
            "--output", "json",
            "--limit", "1",
            "--minDate", min_date,
        ]
        if max_date:
            cmd.extend(["--maxDate", max_date])

        try:
            result = self._run_json(cmd)
        except RuntimeError as e:
            logger.warning(f"Failed to fetch events for {self.project_id}: {e}")
            return []

        total = result.get("totalCount", 0) if isinstance(result, dict) else 0
        if total == 0:
            return []

        per_page = 500
        total_pages = min((total + per_page - 1) // per_page, max_pages)
        all_results = []

        for page in range(1, total_pages + 1):
            cmd = [
                "atlas", "events", "projects", "list",
                "--projectId", self.project_id,
                "--output", "json",
                "--limit", str(per_page),
                "--page", str(page),
                "--minDate", min_date,
            ]
            if max_date:
                cmd.extend(["--maxDate", max_date])

            try:
                result = self._run_json(cmd)
                if isinstance(result, dict):
                    all_results.extend(result.get("results", []))
            except RuntimeError as e:
                logger.warning(f"Events page {page} failed: {e}")
                break

        logger.info(
            f"  Fetched {len(all_results)} events (of {total}) for project {self.project_id}"
        )
        return all_results

    # ── Performance Advisor ──────────────────────────────────────

    def get_suggested_indexes(self, process_id: str) -> list:
        try:
            cmd = [
                "atlas",
                "performanceAdvisor",
                "suggestedIndexes",
                "list",
                "--processName",
                process_id,
            ] + self._base_args()
            result = self._run_json(cmd)
            if isinstance(result, dict):
                return result.get("suggestedIndexes", [])
            return result if isinstance(result, list) else []
        except RuntimeError as e:
            logger.warning(f"Failed to get suggested indexes: {e}")
            return []
