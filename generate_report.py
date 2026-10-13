#!/usr/bin/env python3
"""
Org-Level Production Cluster Scorecard Generator.

Scans all p-* clusters across specified projects, scores each as
RED/YELLOW/GREEN, compares against the previous report, and generates
an HTML scorecard.

Usage:
  python generate_report.py --project-ids 636ecde21295bc2f636d85f5,6928acd386339a2604813606
  python generate_report.py --project-ids 636ecde21295bc2f636d85f5 --prefix p-
  python generate_report.py --project-ids 636ecde21295bc2f636d85f5 --ai-provider openai
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from atlas_cli import AtlasClient
from collector import collect_org_data
from scorer import score_all_clusters
from comparator import find_previous_report, compare_reports
from report_html import generate_html
from export_xlsx import export_to_xlsx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("generate_report")

REPORTS_DIR = Path(__file__).parent / "reports"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate a production cluster scorecard for an organization.",
    )
    parser.add_argument(
        "--project-ids",
        default=None,
        help="Comma-separated list of Atlas project IDs. If not provided, reads from projects.txt",
    )
    parser.add_argument(
        "--prefix",
        default="p-",
        help="Cluster name prefix to filter (default: p-)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output HTML file path (default: reports/org-<date>.html)",
    )
    parser.add_argument(
        "--ai-provider",
        default=os.environ.get("AI_PROVIDER", "openai"),
        choices=["anthropic", "openai", "none"],
        help="AI provider for comparison summary (default: openai)",
    )
    parser.add_argument(
        "--ai-model",
        default=os.environ.get("AI_MODEL"),
        help="AI model override",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Skip AI comparison summary",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def _call_llm(
    provider: str,
    system_prompt: str,
    user_prompt: str,
    model: str = None,
    api_key: str = None,
    max_tokens: int = 1000,
) -> str:
    """Shared helper to call the configured LLM provider.

    Returns the response text, or empty string on failure.
    """
    try:
        if provider == "openai":
            from openai import OpenAI

            client_kwargs = {
                "api_key": api_key or os.environ.get("AI_API_KEY") or os.environ.get("OPENAI_API_KEY")
            }
            openai_base_url = os.environ.get("AI_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
            if openai_base_url:
                client_kwargs["base_url"] = openai_base_url.rstrip("/")

            client = OpenAI(**client_kwargs)
            model = model or "gpt-4o"

            request_params = {}
            if any(model.startswith(p) for p in ("gpt-4.1", "gpt-5", "o1", "o3")):
                request_params["max_completion_tokens"] = max_tokens
            else:
                request_params["max_tokens"] = max_tokens
                request_params["temperature"] = 0

            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                **request_params,
            )
            return resp.choices[0].message.content or ""

        elif provider == "anthropic":
            import anthropic

            anthropic_api_key = (
                api_key
                or os.environ.get("AI_API_KEY")
                or os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTRHOPIC_API_KEY")
            )
            anthropic_base_url = (
                os.environ.get("AI_BASE_URL")
                or os.environ.get("ANTHROPIC_BASE_URL")
                or os.environ.get("ANTRHOPIC_BASE_URL")
            )
            client_kwargs = {"api_key": anthropic_api_key}
            if anthropic_base_url:
                normalized_base_url = anthropic_base_url.rstrip("/")
                if normalized_base_url.endswith("/v1"):
                    normalized_base_url = normalized_base_url[:-3]
                client_kwargs["base_url"] = normalized_base_url

            client = anthropic.Anthropic(
                **client_kwargs
            )
            model = model or "claude-sonnet-4-20250514"
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return resp.content[0].text if resp.content else ""

    except Exception as e:
        logger.warning(f"LLM call failed ({provider}): {e}")
        return ""


def generate_ai_comparison(
    current: dict,
    previous: dict,
    comparison: dict,
    provider: str,
    model: str = None,
    api_key: str = None,
) -> str:
    """Use an LLM to produce a narrative comparison summary."""
    if not previous or provider == "none":
        return ""

    # Build a condensed prompt
    curr_summary = []
    for c in current.get("clusters", []):
        s = c.get("score", {})
        curr_summary.append(
            f"- {c['cluster_name']}: {s.get('tier')} -- {s.get('justification', '')[:150]}"
        )

    prev_summary = []
    for c in previous.get("clusters", []):
        s = c.get("score", {})
        prev_summary.append(
            f"- {c['cluster_name']}: {s.get('tier')} -- {s.get('justification', '')[:150]}"
        )

    trends = comparison.get("cluster_trends", {})
    trend_lines = []
    for name, t in trends.items():
        trend_lines.append(
            f"- {name}: {t.get('arrow')} -- {t.get('details', '')[:150]}"
        )

    # Build event pattern summary for the AI prompt
    event_pattern_lines = []
    activity = current.get("activity_events", {})
    if activity.get("events"):
        # Group events by type and count occurrences
        from collections import Counter
        type_counter = Counter()
        type_examples = {}
        for e in activity["events"]:
            if e.get("severity") not in ("CRITICAL", "WARNING"):
                continue
            key = (e.get("severity", ""), e.get("event_type", ""))
            type_counter[key] += 1
            if key not in type_examples and e.get("metric_name"):
                type_examples[key] = {
                    "metric": e["metric_name"],
                    "value": e.get("metric_value"),
                    "units": e.get("metric_units", ""),
                }

        for (sev, etype), count in type_counter.most_common(10):
            line = f"  - [{sev}] {etype}: {count} occurrence(s)"
            ex = type_examples.get((sev, etype))
            if ex and ex.get("value") is not None:
                line += f" (e.g. {ex['metric']}: {ex['value']} {ex['units']})"
            event_pattern_lines.append(line)

    event_patterns_block = chr(10).join(event_pattern_lines) if event_pattern_lines else "  (no significant events)"

    # Build reliability watch summary
    reliability_lines = []
    for c in current.get("clusters", []):
        rel = c.get("reliability", {})
        rel_tier = rel.get("tier", "GREEN")
        if rel_tier in ("RED", "YELLOW"):
            reasons = rel.get("watch_reasons", [])
            reasons_str = "; ".join(reasons[:2]) if reasons else "no specific reason"
            reliability_lines.append(
                f"- {c['cluster_name']}: Reliability {rel_tier} -- {reasons_str}"
            )
    reliability_block = chr(10).join(reliability_lines) if reliability_lines else "  (all clusters GREEN)"

    prompt = f"""Compare these two weekly MongoDB Atlas cluster health reports and provide a brief executive summary.

Include:
1. What changed, improved, or degraded (2-3 sentences)
2. If there are recurring event patterns (same event type firing many times), call these out specifically and suggest 1-2 steps to resolve the root cause
3. Any clusters that need immediate attention
4. Highlight any clusters with poor reliability trends (YELLOW/RED) even if current health is GREEN

CURRENT REPORT:
{chr(10).join(curr_summary)}

PREVIOUS REPORT (from {comparison.get("previous_date", "?")}):
{chr(10).join(prev_summary)}

TRENDS:
{chr(10).join(trend_lines)}

Alert velocity: {comparison.get("alert_velocity", "?")} (current 7d: {comparison.get("curr_total_alerts_7d", "?")}, previous 7d: {comparison.get("prev_total_alerts_7d", "?")})
Recurring index suggestions: {comparison.get("recurring_index_count", 0)}

RELIABILITY TREND (clusters with YELLOW/RED reliability):
{reliability_block}

ACTIVITY FEED EVENT PATTERNS (past 7 days):
  Total: {activity.get("critical_count", 0)} critical, {activity.get("warning_count", 0)} warning events
{event_patterns_block}

If an event type appears many times (e.g. OUTSIDE_METRIC_THRESHOLD firing 40+ times for the same metric), this indicates a persistent issue -- call it out and recommend specific resolution steps. Be concise and specific, cite cluster names and metric values."""

    return _call_llm(
        provider=provider,
        system_prompt="You are a MongoDB operations expert writing a brief executive comparison of weekly cluster health reports.",
        user_prompt=prompt,
        model=model,
        api_key=api_key,
        max_tokens=1000,
    )


def generate_red_cluster_analyses(
    scored_clusters: list,
    activity_events: dict,
    comparison: dict,
    provider: str,
    model: str = None,
    api_key: str = None,
) -> dict:
    """Generate per-cluster AI analysis for each RED cluster.

    Builds a rich prompt per cluster including:
    - All scoring reasons (red + yellow)
    - Full metrics summary
    - Open alerts with details
    - Relevant activity feed events (CRITICAL + WARNING for that cluster)
    - Suggested indexes
    - Previous report comparison / trend

    Returns:
        dict mapping cluster_name -> AI analysis string
    """
    if provider == "none":
        return {}

    # Include clusters with RED current health OR RED reliability
    red_clusters = [
        c for c in scored_clusters
        if c.get("score", {}).get("tier") == "RED"
        or c.get("reliability", {}).get("tier") == "RED"
    ]
    if not red_clusters:
        return {}

    # Build a lookup of activity events by cluster name / replica set
    cluster_events = {}
    if activity_events:
        for e in activity_events.get("events", []):
            if e.get("severity") not in ("CRITICAL", "WARNING"):
                continue
            # Match on cluster_name or replica_set
            name = e.get("cluster_name", "") or ""
            rs = e.get("replica_set", "") or ""
            for key in (name, rs):
                if key:
                    cluster_events.setdefault(key, []).append(e)

    # Build previous report lookup
    prev_lookup = {}
    if comparison.get("has_previous"):
        trends = comparison.get("cluster_trends", {})
        prev_lookup = trends

    analyses = {}

    for cluster in red_clusters:
        cname = cluster.get("cluster_name", "?")
        logger.info(f"  Generating AI analysis for RED cluster: {cname}...")

        score = cluster.get("score", {})
        ms = score.get("metrics_summary", {})
        alerts = cluster.get("alerts", {})
        indexes = cluster.get("suggested_indexes", [])

        # Collect all scoring reasons
        all_reasons = []
        for r in score.get("reasons_red", []):
            all_reasons.append(f"  [RED] {r}")
        for r in score.get("reasons_yellow", []):
            all_reasons.append(f"  [YELLOW] {r}")
        reasons_block = chr(10).join(all_reasons) if all_reasons else "  (none)"

        # Metrics detail
        metrics_lines = []
        if ms.get("cpu_avg") is not None:
            metrics_lines.append(
                f"  CPU: avg {ms['cpu_avg']:.1f}%, max {ms.get('cpu_max', 0):.1f}%"
            )
        if ms.get("memory_pct") is not None:
            metrics_lines.append(
                f"  Memory: {ms['memory_pct']:.0f}% utilized "
                f"({ms.get('memory_avg_mb', 0):.0f}MB / {ms.get('tier_ram_mb', 0)}MB)"
            )
        if ms.get("disk_pct") is not None:
            metrics_lines.append(
                f"  Disk: {ms['disk_pct']:.0f}% used "
                f"({ms.get('disk_used_gb', 0):.0f}GB / {ms.get('disk_total_gb', 0)}GB)"
            )
        if ms.get("connections_max") is not None:
            metrics_lines.append(f"  Connections max: {ms['connections_max']:.0f}")
        metrics_block = chr(10).join(metrics_lines) if metrics_lines else "  (no metrics)"

        # Open alerts
        alert_lines = []
        for a in alerts.get("open", [])[:10]:
            val = a.get("value")
            val_str = f" = {val:.2f}" if val is not None else ""
            alert_lines.append(
                f"  - {a.get('event_type', '?')} | {a.get('metric', '')}{val_str} | {a.get('created', '')[:19]}"
            )
        alert_summary = f"  Open: {alerts.get('open_count', 0)}, Past 7d: {alerts.get('past_7d_count', 0)}, Past 30d: {alerts.get('past_30d_count', 0)}"
        alerts_block = alert_summary
        if alert_lines:
            alerts_block += chr(10) + chr(10).join(alert_lines)

        # Activity feed events for this cluster (match on name or partial match)
        relevant_events = []
        cname_lower = cname.lower()
        for key, evts in cluster_events.items():
            if cname_lower in key.lower() or key.lower() in cname_lower:
                relevant_events.extend(evts)
        # Deduplicate by event id/created
        seen = set()
        unique_events = []
        for e in relevant_events:
            eid = (e.get("event_type", ""), e.get("created", ""))
            if eid not in seen:
                seen.add(eid)
                unique_events.append(e)
        unique_events.sort(key=lambda x: x.get("created", ""), reverse=True)

        event_lines = []
        for e in unique_events[:15]:
            sev = e.get("severity", "")
            etype = e.get("event_type", "")
            metric = e.get("metric_name", "")
            val = e.get("metric_value")
            units = e.get("metric_units", "")
            nodes = e.get("node_count", 1)
            val_str = f" = {val} {units}" if val is not None else ""
            node_str = f" ({nodes} nodes)" if nodes > 1 else ""
            event_lines.append(
                f"  - [{sev}] {etype}{val_str}{node_str} | {e.get('created', '')[:19]}"
            )
        events_block = chr(10).join(event_lines) if event_lines else "  (no significant events)"

        # Index suggestions
        idx_lines = []
        high_idx = [i for i in indexes if i.get("priority") == "HIGH"]
        med_idx = [i for i in indexes if i.get("priority") == "MEDIUM"]
        for i in (high_idx + med_idx)[:5]:
            idx_lines.append(
                f"  - [{i['priority']}] {i.get('namespace', '')} | "
                f"{i.get('exec_per_hour', 0):.1f} exec/hr | avg {i.get('avg_ms', 0):.0f}ms | "
                f"weight {i.get('weight', 0):.2f}"
            )
        idx_block = chr(10).join(idx_lines) if idx_lines else "  (none)"

        # Trend from previous report
        trend_info = ""
        t = prev_lookup.get(cname, {})
        if t:
            trend_info = f"Trend: {t.get('arrow', '?')} -- {t.get('details', '')[:200]}"
            tc = t.get("tier_change")
            if tc:
                trend_info += f" (tier changed: {tc['from']} -> {tc['to']})"

        # Reliability data for the prompt
        rel = cluster.get("reliability", {})
        rel_tier = rel.get("tier", "GREEN")
        rel_reasons = rel.get("watch_reasons", [])
        health_tier = score.get("tier", "?")
        rel_block = f"Reliability tier: {rel_tier}"
        if rel_reasons:
            rel_block += chr(10) + chr(10).join(f"  - {r}" for r in rel_reasons)

        analysis_label = "RED (critical)" if health_tier == "RED" else f"health={health_tier}, reliability=RED"

        prompt = f"""You are analyzing a MongoDB Atlas cluster that is currently rated {analysis_label} in our health scorecard. Provide:

1. A clear 2-3 sentence diagnosis of what is wrong with this cluster, referencing specific metric values and event patterns.
2. Exactly 1-3 actionable recommendations to resolve the issues, ordered by impact. Each recommendation should be specific and practical (e.g., "Scale up to M50" not "consider scaling").

CLUSTER: {cname}
Instance tier: {cluster.get('tier', '?')} | Type: {cluster.get('cluster_type', '?')} | MongoDB {cluster.get('mongodb_version', '?')}
Auto-scaling: compute={'Yes' if cluster.get('compute_auto_scaling') else 'No'}, disk={'Yes' if cluster.get('disk_auto_scaling') else 'No'}
Backup: {'Yes' if cluster.get('backup_enabled') else 'No'} | PITR: {'Yes' if cluster.get('pit_enabled') else 'No'}

SCORING REASONS:
{reasons_block}

RELIABILITY TREND:
{rel_block}

CURRENT METRICS:
{metrics_block}

ALERTS:
{alerts_block}

ACTIVITY FEED EVENTS (past 7 days, CRITICAL + WARNING only):
{events_block}

INDEX SUGGESTIONS (high/medium priority):
{idx_block}

{trend_info}

Format your response as:
DIAGNOSIS: <2-3 sentences>

RECOMMENDATIONS:
1. <specific action>
2. <specific action>
3. <specific action if needed>

Be direct and specific. Reference actual values from the data above. Do not repeat the cluster name in every sentence."""

        analysis = _call_llm(
            provider=provider,
            system_prompt="You are a senior MongoDB DBA and operations expert. Provide concise, actionable cluster health analysis.",
            user_prompt=prompt,
            model=model,
            api_key=api_key,
            max_tokens=800,
        )

        if analysis:
            analyses[cname] = analysis
            logger.info(f"  AI analysis for {cname}: {len(analysis)} chars")
        else:
            logger.warning(f"  AI analysis failed for {cname}")

    return analyses


def main():
    args = parse_args()
    logging.getLogger().setLevel(getattr(logging, args.log_level))

    # Load project IDs from CLI arg or projects.txt
    if args.project_ids:
        project_ids = [p.strip() for p in args.project_ids.split(",") if p.strip()]
    else:
        projects_file = Path(__file__).parent / "projects.txt"
        if not projects_file.exists():
            logger.error("No --project-ids provided and projects.txt not found")
            sys.exit(1)
        project_ids = [
            line.strip()
            for line in projects_file.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not project_ids:
            logger.error("projects.txt is empty (no uncommented project IDs)")
            sys.exit(1)
        logger.info(f"Loaded {len(project_ids)} project ID(s) from projects.txt")

    print(f"\n{'=' * 60}")
    print(f"Production Cluster Scorecard Generator")
    print(f"{'=' * 60}")
    print(f"  Projects: {len(project_ids)}")
    print(f"  Prefix filter: {args.prefix}*")
    print(f"  AI: {args.ai_provider if not args.no_ai else 'disabled'}")
    print(f"{'=' * 60}\n")

    start = time.time()

    # Initialize client
    client = AtlasClient(project_id=project_ids[0])

    # Phase 1: Collect data
    logger.info("Phase 1: Collecting data from all production clusters...")
    raw_data = collect_org_data(client, project_ids, cluster_prefix=args.prefix)

    if not raw_data.get("clusters"):
        logger.error(
            f"No {args.prefix}* clusters found across {len(project_ids)} project(s)"
        )
        sys.exit(1)

    collect_time = time.time() - start
    logger.info(
        f"Collection complete: {raw_data['cluster_count']} clusters in {collect_time:.1f}s"
    )

    # Phase 2: Score clusters
    logger.info("Phase 2: Scoring clusters...")
    scored = score_all_clusters(raw_data["clusters"])
    raw_data["clusters"] = scored

    # Log reliability counts
    rel_red = sum(1 for c in scored if c.get("reliability", {}).get("tier") == "RED")
    rel_yellow = sum(1 for c in scored if c.get("reliability", {}).get("tier") == "YELLOW")
    logger.info(f"Reliability: {rel_red} RED, {rel_yellow} YELLOW watch")

    # Phase 3: Load previous report and compare
    logger.info("Phase 3: Comparing with previous report...")
    previous = find_previous_report()
    comparison = compare_reports(raw_data, previous)

    if comparison.get("has_previous"):
        logger.info(
            f"Comparison: {comparison['improved']} improved, "
            f"{comparison['degraded']} degraded, {comparison['stable']} stable"
        )
    else:
        logger.info("No previous report found -- this is the first run")

    # Phase 4: AI comparison summary
    ai_summary = ""
    if not args.no_ai and comparison.get("has_previous"):
        logger.info("Phase 4: Generating AI comparison summary...")
        ai_summary = generate_ai_comparison(
            raw_data,
            previous,
            comparison,
            provider=args.ai_provider,
            model=args.ai_model,
        )
        if ai_summary:
            logger.info(f"AI summary generated ({len(ai_summary)} chars)")

    # Extract activity events from collected data
    activity_events = raw_data.get("activity_events", {})
    if activity_events:
        logger.info(
            f"Activity feed: {activity_events.get('critical_count', 0)} critical, "
            f"{activity_events.get('warning_count', 0)} warning events "
            f"({activity_events.get('filtered_noise_count', 0)} noise filtered)"
        )

    # Phase 4b: Generate per-RED-cluster AI analysis (includes RED reliability)
    red_analyses = {}
    red_count = sum(
        1 for c in scored
        if c.get("score", {}).get("tier") == "RED"
        or c.get("reliability", {}).get("tier") == "RED"
    )
    if not args.no_ai and red_count > 0:
        logger.info(
            f"Phase 4b: Generating AI analysis for {red_count} RED cluster(s)..."
        )
        red_analyses = generate_red_cluster_analyses(
            scored_clusters=scored,
            activity_events=activity_events,
            comparison=comparison,
            provider=args.ai_provider,
            model=args.ai_model,
        )
        if red_analyses:
            logger.info(f"AI analyses generated for {len(red_analyses)} RED cluster(s)")
            # Store in raw_data for JSON persistence
            raw_data["red_cluster_analyses"] = red_analyses

    # Phase 5: Generate HTML report
    logger.info("Phase 5: Generating HTML scorecard...")
    html = generate_html(
        scored,
        comparison,
        collected_at=raw_data["collected_at"],
        ai_summary=ai_summary,
        activity_events=activity_events,
        red_analyses=red_analyses,
    )

    # Save outputs
    REPORTS_DIR.mkdir(exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")

    # Save JSON (for future comparison)
    json_path = REPORTS_DIR / f"org-{date_str}.json"
    json_path.write_text(json.dumps(raw_data, indent=2, default=str))
    logger.info(f"JSON saved: {json_path}")

    # Save HTML
    if args.output:
        html_path = Path(args.output)
    else:
        html_path = REPORTS_DIR / f"org-{date_str}.html"

    html_path.write_text(html)
    logger.info(f"HTML saved: {html_path}")

    # Save Excel
    xlsx_path = REPORTS_DIR / f"org-{date_str}.xlsx"
    try:
        export_to_xlsx(
            scored,
            raw_data["collected_at"],
            str(xlsx_path),
            activity_events=activity_events,
            red_analyses=red_analyses,
            ai_summary=ai_summary,
        )
        logger.info(f"Excel saved: {xlsx_path}")
    except Exception as e:
        logger.warning(f"Excel export failed: {e}")
        xlsx_path = None

    total_time = time.time() - start

    # Summary
    red = sum(1 for c in scored if c["score"]["tier"] == "RED")
    yellow = sum(1 for c in scored if c["score"]["tier"] == "YELLOW")
    green = sum(1 for c in scored if c["score"]["tier"] == "GREEN")

    print(f"\n{'=' * 60}")
    print(f"Scorecard Complete ({total_time:.1f}s)")
    print(f"{'=' * 60}")
    print(f"  Clusters: {len(scored)} ({red} RED, {yellow} YELLOW, {green} GREEN)")
    if comparison.get("has_previous"):
        print(
            f"  vs Previous: {comparison['improved']} improved, {comparison['degraded']} degraded"
        )
    print(f"  HTML:  {html_path}")
    if xlsx_path:
        print(f"  Excel: {xlsx_path}")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
