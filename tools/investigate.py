"""
Service investigation mega-tool for Sherlock.

Three-phase adaptive investigation engine:
  Phase 1 — ANCHOR & RESOLVE: Find the incident, anchor time window, resolve candidates.
  Phase 2 — DISCOVER: Ask New Relic what data exists for this service.
  Phase 3 — ADAPTIVE INVESTIGATE: Query only discovered data, generate findings.
"""

import asyncio
import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field

from core.context import AccountContext
from core.credentials import Credentials
from core.deeplinks import get_builder as _get_deeplink_builder
from core.discovery import discover_available_data
from core.query_builder import (
    build_investigation_queries,
    get_health_check,
    InvestigationQuery,
)
from core.sanitize import (
    fuzzy_resolve_service,
    fuzzy_resolve_service_candidates,
    parse_alert_target,
    sanitize_service_name,
)

logger = logging.getLogger("sherlock.tools.investigate")


def _safe_extract_results(body: dict) -> list[dict]:
    """Safely navigate ``data.actor.account.nrql.results`` even when
    intermediate values are *None* rather than missing."""
    d = body if isinstance(body, dict) else {}
    for key in ("data", "actor", "account", "nrql", "results"):
        d = d.get(key) if isinstance(d, dict) else None
        if d is None:
            return []
    return d if isinstance(d, list) else []


# Investigation timeout for the entire operation.
INVESTIGATION_TIMEOUT_S = 60

# Per-query timeout.
QUERY_TIMEOUT_S = 15

# NerdGraph NRQL template.
GQL_NRQL_QUERY = """
{
  actor {
    account(id: %s) {
      nrql(query: "%s") {
        results
      }
    }
  }
}
"""

# GraphQL to fetch incidents.
GQL_INCIDENTS = """
{
  actor {
    account(id: %s) {
      nrql(query: "SELECT * FROM NrAiIncident WHERE state = 'activated' SINCE 1 day ago LIMIT 100") {
        results
      }
    }
  }
}
"""

GQL_RECENT_INCIDENTS = """
{
  actor {
    account(id: %s) {
      nrql(query: "SELECT * FROM NrAiIncident WHERE title LIKE '%%%s%%' SINCE 7 days ago LIMIT 50") {
        results
      }
    }
  }
}
"""


# ── Pydantic Models ─────────────────────────────────────────────────────


class IncidentPattern(BaseModel):
    """Pattern analysis across recent incidents for the same service."""

    occurrence_count: int = 0
    first_occurrence: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_occurrence: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_recurring: bool = False
    recurrence_interval_hours: float | None = None
    consistent_cause: str | None = None
    pattern_summary: str = ""


class InvestigationAnchor(BaseModel):
    """Anchors an investigation to the correct time window and service."""

    primary_service: str = ""
    all_candidates: list[str] = Field(default_factory=list)
    active_incident: dict | None = None
    recent_incidents: list[dict] = Field(default_factory=list)
    window_start: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    window_end: datetime | None = None
    since_minutes: int = 60
    until_clause: str = ""
    window_source: str = "default"
    incident_pattern: IncidentPattern | None = None


# ── Phase 1: Anchor & Resolve ───────────────────────────────────────────


async def _fetch_active_incidents(
    credentials: Credentials,
) -> list[dict]:
    """Fetch all activated incidents for the account. Never raises."""
    try:
        escaped = GQL_INCIDENTS.replace("%s", credentials.account_id, 1)
        # We template the account_id directly into the GQL string.
        gql = """
{
  actor {
    account(id: %s) {
      nrql(query: "SELECT * FROM NrAiIncident WHERE state = 'activated' SINCE 1 day ago LIMIT 100") {
        results
      }
    }
  }
}
""" % credentials.account_id

        headers = {
            "API-Key": credentials.api_key,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                credentials.endpoint,
                json={"query": gql},
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()

        return _safe_extract_results(body)
    except Exception as exc:
        logger.debug("Failed to fetch incidents: %s", exc)
        return []


async def _fetch_recent_incidents(
    service_name: str,
    credentials: Credentials,
) -> list[dict]:
    """Fetch recent closed incidents for pattern analysis. Never raises."""
    try:
        safe_name = service_name.replace("'", "").replace('"', "")
        nrql = (
            f"SELECT * FROM NrAiIncident "
            f"WHERE title LIKE '%{safe_name}%' "
            f"SINCE 7 days ago LIMIT 50"
        )
        escaped_nrql = nrql.replace('"', '\\"')
        gql = GQL_NRQL_QUERY % (credentials.account_id, escaped_nrql)

        headers = {
            "API-Key": credentials.api_key,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                credentials.endpoint,
                json={"query": gql},
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()

        return _safe_extract_results(body)
    except Exception as exc:
        logger.debug("Failed to fetch recent incidents: %s", exc)
        return []


def _match_incident_to_candidates(
    incidents: list[dict],
    candidates: list[str],
) -> dict | None:
    """Find the best matching incident for the given candidates.

    Uses fuzzy substring matching on incident title and entityNames.
    """
    for incident in incidents:
        title = str(incident.get("title", "")).lower()
        entity = str(incident.get("entityName", "")).lower()
        combined = f"{title} {entity}"

        for candidate in candidates:
            if candidate.lower() in combined:
                return incident

    # Fallback: weaker match.
    from difflib import SequenceMatcher

    best_match = None
    best_score = 0.0
    for incident in incidents:
        title = str(incident.get("title", "")).lower()
        for candidate in candidates:
            score = SequenceMatcher(None, candidate.lower(), title).ratio()
            if score > best_score and score > 0.4:
                best_score = score
                best_match = incident

    return best_match


def _analyze_incident_pattern(recent_incidents: list[dict]) -> IncidentPattern | None:
    """Analyze recent incidents for recurring patterns."""
    if not recent_incidents:
        return None

    count = len(recent_incidents)

    # Extract timestamps.
    timestamps: list[datetime] = []
    titles: list[str] = []
    for inc in recent_incidents:
        ts = inc.get("createdAt", inc.get("timestamp", inc.get("openTime")))
        if ts:
            try:
                if isinstance(ts, (int, float)):
                    ts_dt = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
                else:
                    ts_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                timestamps.append(ts_dt)
            except Exception:
                pass
        titles.append(str(inc.get("title", "")))

    if not timestamps:
        return IncidentPattern(
            occurrence_count=count,
            is_recurring=count > 1,
            pattern_summary=f"{count} incident(s) in last 7 days",
        )

    timestamps.sort()
    first = timestamps[0]
    last = timestamps[-1]

    # Calculate recurrence interval.
    interval_hours: float | None = None
    if len(timestamps) > 1:
        total_hours = (last - first).total_seconds() / 3600
        interval_hours = round(total_hours / (len(timestamps) - 1), 1)

    # Check for consistent cause from titles.
    consistent_cause = None
    if titles:
        # Simple heuristic: if all titles are similar.
        from difflib import SequenceMatcher

        if len(titles) > 1:
            base = titles[0].lower()
            all_similar = all(
                SequenceMatcher(None, base, t.lower()).ratio() > 0.6
                for t in titles[1:]
            )
            if all_similar:
                consistent_cause = titles[0][:100]

    summary_parts = [f"{count} incident(s) in last 7 days"]
    if interval_hours:
        summary_parts.append(f"recurring ~every {interval_hours}h")
    if consistent_cause:
        summary_parts.append(f"consistent cause: {consistent_cause}")

    return IncidentPattern(
        occurrence_count=count,
        first_occurrence=first,
        last_occurrence=last,
        is_recurring=count > 1,
        recurrence_interval_hours=interval_hours,
        consistent_cause=consistent_cause,
        pattern_summary="; ".join(summary_parts),
    )


async def _anchor_investigation(
    service_candidates: list[str],
    since_minutes_requested: int,
    intelligence: Any,
    credentials: Credentials,
) -> InvestigationAnchor:
    """Find the relevant incident and establish the correct time window.

    Steps:
    1. Fetch activated incidents for the account.
    2. Match incidents to service candidates.
    3. If matched: anchor window to incident creation time.
    4. If not matched: use requested time window.
    5. Fetch recent incidents for pattern analysis.

    Never raises — degrades gracefully on any error.
    """
    try:
        primary = service_candidates[0] if service_candidates else ""
        now = datetime.now(timezone.utc)

        # Fetch active incidents.
        active_incidents = await _fetch_active_incidents(credentials)

        # Match to candidates.
        matched_incident = _match_incident_to_candidates(
            active_incidents, service_candidates
        )

        if matched_incident:
            # Anchor to incident.
            created_at = matched_incident.get(
                "createdAt",
                matched_incident.get("timestamp", matched_incident.get("openTime")),
            )
            closed_at = matched_incident.get("closedAt", matched_incident.get("closeTime"))

            window_start = now
            window_end: datetime | None = None

            if created_at:
                try:
                    if isinstance(created_at, (int, float)):
                        window_start = datetime.fromtimestamp(
                            created_at / 1000.0, tz=timezone.utc
                        )
                    else:
                        window_start = datetime.fromisoformat(
                            str(created_at).replace("Z", "+00:00")
                        )
                    # 30 min pre-incident baseline.
                    from datetime import timedelta

                    window_start = window_start - timedelta(minutes=30)
                except Exception:
                    window_start = now - __import__("datetime").timedelta(
                        minutes=since_minutes_requested
                    )

            if closed_at:
                try:
                    if isinstance(closed_at, (int, float)):
                        window_end = datetime.fromtimestamp(
                            closed_at / 1000.0, tz=timezone.utc
                        )
                    else:
                        window_end = datetime.fromisoformat(
                            str(closed_at).replace("Z", "+00:00")
                        )
                    from datetime import timedelta

                    window_end = window_end + timedelta(minutes=15)
                except Exception:
                    window_end = None

            since_minutes = max(
                1, math.ceil((now - window_start).total_seconds() / 60)
            )
            until_clause = (
                f"UNTIL '{window_end.isoformat()}'"
                if window_end
                else ""
            )

            # Fetch recent incidents for pattern.
            recent = await _fetch_recent_incidents(primary, credentials)
            pattern = _analyze_incident_pattern(recent)

            return InvestigationAnchor(
                primary_service=primary,
                all_candidates=service_candidates,
                active_incident=matched_incident,
                recent_incidents=recent,
                window_start=window_start,
                window_end=window_end,
                since_minutes=since_minutes,
                until_clause=until_clause,
                window_source="incident_anchored",
                incident_pattern=pattern,
            )
        else:
            # No matching incident — use requested window.
            from datetime import timedelta

            window_start = now - timedelta(minutes=since_minutes_requested)

            # Still fetch recent for pattern.
            recent = await _fetch_recent_incidents(primary, credentials)
            pattern = _analyze_incident_pattern(recent) if recent else None

            return InvestigationAnchor(
                primary_service=primary,
                all_candidates=service_candidates,
                active_incident=None,
                recent_incidents=recent,
                window_start=window_start,
                window_end=None,
                since_minutes=since_minutes_requested,
                until_clause="",
                window_source="requested",
                incident_pattern=pattern,
            )

    except Exception as exc:
        logger.warning("Anchor investigation failed: %s", exc)
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        primary = service_candidates[0] if service_candidates else ""
        return InvestigationAnchor(
            primary_service=primary,
            all_candidates=service_candidates,
            window_start=now - timedelta(minutes=since_minutes_requested),
            since_minutes=since_minutes_requested,
            window_source="default",
        )


# ── Phase 3: Adaptive Investigation Helpers ──────────────────────────────


async def _run_signal_query(
    query: InvestigationQuery,
    credentials: Credentials,
) -> list[dict]:
    """Execute a single signal query and return results. Never raises."""
    try:
        escaped_nrql = query.nrql.replace('"', '\\"')
        gql = GQL_NRQL_QUERY % (credentials.account_id, escaped_nrql)

        headers = {
            "API-Key": credentials.api_key,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=QUERY_TIMEOUT_S) as client:
            resp = await client.post(
                credentials.endpoint,
                json={"query": gql},
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()

        return _safe_extract_results(body)
    except Exception as exc:
        logger.debug("Signal query '%s' failed: %s", query.signal, exc)
        raise


def _strip_null_timeseries(results: list[dict]) -> list[dict]:
    """Remove timeseries buckets where all metric values are null.

    NRQL TIMESERIES queries return a bucket for every time window even when
    there is no data, filling metric columns with ``None``.  For sparse
    services this produces hundreds of useless rows.  Strip them so the
    output stays compact and readable.
    """
    if not results or not isinstance(results, list):
        return results

    # Detect timeseries rows by the presence of begin/endTimeSeconds.
    ts_keys = {"beginTimeSeconds", "endTimeSeconds"}
    filtered: list[dict] = []
    for row in results:
        if not isinstance(row, dict):
            filtered.append(row)
            continue
        if not ts_keys.issubset(row.keys()):
            # Not a timeseries row — keep as-is.
            filtered.append(row)
            continue
        # Check whether every non-timestamp value is None / null.
        metric_vals = [v for k, v in row.items() if k not in ts_keys]
        if any(v is not None for v in metric_vals):
            filtered.append(row)

    # If the filtered timeseries is still very long and all metric values are
    # identical (e.g. hundreds of buckets with error_rate=0.0), compact it
    # into a short summary to keep output readable.
    if len(filtered) > 20 and all(
        isinstance(r, dict) and ts_keys.issubset(r.keys()) for r in filtered
    ):
        metric_keys = [
            k for k in filtered[0] if k not in ts_keys
        ]
        # Collect unique metric value tuples.
        unique_vals = {
            tuple(r.get(k) for k in metric_keys) for r in filtered
        }
        if len(unique_vals) == 1:
            # Every single bucket has the same metrics — summarize.
            vals = next(iter(unique_vals))
            summary: dict = {
                "summary": "constant_value",
                "bucket_count": len(filtered),
                "first_bucket": filtered[0]["beginTimeSeconds"],
                "last_bucket": filtered[-1]["endTimeSeconds"],
            }
            for k, v in zip(metric_keys, vals):
                summary[k] = v
            return [summary]

    return filtered


def _severity_emoji(finding: str) -> str:
    """Extract severity from a finding string."""
    if "🔴" in finding:
        return "CRITICAL"
    elif "⚠️" in finding:
        return "WARNING"
    elif "ℹ️" in finding:
        return "INFO"
    return "INFO"


def _overall_status(findings: list[dict]) -> str:
    """Determine overall investigation status from findings."""
    severities = [f.get("severity", "INFO") for f in findings]
    if "CRITICAL" in severities:
        return "CRITICAL"
    if "WARNING" in severities:
        return "WARNING"
    return "HEALTHY"


def _generate_recommendations(
    findings: list[dict],
    anchor: InvestigationAnchor,
    discovery: Any,
    raw_data: dict,
) -> list[dict]:
    """Generate prioritized fix recommendations from findings."""
    recommendations: list[dict] = []
    finding_texts = " ".join(f.get("finding", "") for f in findings)
    finding_lower = finding_texts.lower()

    # OOMKill detection.
    if "oomkill" in finding_lower or "oom" in finding_lower:
        recommendations.append({
            "priority": "P1",
            "area": "k8s",
            "finding": "OOMKill events detected.",
            "action": "Increase container memory limits or fix memory leak. Check pod events, OOM kills, resource limits.",
            "urgency": "IMMEDIATE",
        })

    # Pod failures.
    if "failed" in finding_lower or "not ready" in finding_lower or "crashloop" in finding_lower:
        recommendations.append({
            "priority": "P1",
            "area": "k8s",
            "finding": "Kubernetes pods in bad state.",
            "action": "Check pod events, OOM kills, resource limits.",
            "urgency": "IMMEDIATE",
        })

    # Container restarts.
    if "restart" in finding_lower and "restart" in finding_lower:
        recommendations.append({
            "priority": "P2",
            "area": "k8s",
            "finding": "Container restart loops detected.",
            "action": "Check container logs, liveness/readiness probes.",
            "urgency": "SOON",
        })

    # High error rate.
    has_critical_errors = any(
        f.get("severity") == "CRITICAL" and "error rate" in f.get("finding", "").lower()
        for f in findings
    )
    if has_critical_errors:
        recommendations.append({
            "priority": "P1",
            "area": "errors",
            "finding": "Critically high error rate detected.",
            "action": "Check recent deployments, rollback if needed.",
            "urgency": "IMMEDIATE",
        })

    # Zero throughput.
    if "zero throughput" in finding_lower:
        recommendations.append({
            "priority": "P1",
            "area": "application",
            "finding": "Service has zero throughput — may be down.",
            "action": "Check service health, K8s pods, recent deployments.",
            "urgency": "IMMEDIATE",
        })

    # Synthetic failures.
    if "synthetic" in finding_lower and ("failing" in finding_lower or "down" in finding_lower):
        recommendations.append({
            "priority": "P1",
            "area": "external",
            "finding": "Synthetic monitor failures detected.",
            "action": "Check endpoint availability, auth flow, CDN, external dependencies.",
            "urgency": "IMMEDIATE",
        })

    # Application crashes detected in logs.
    if "application crashes" in finding_lower:
        recommendations.append({
            "priority": "P1",
            "area": "Application/Crash",
            "finding": "Application crash events detected in logs.",
            "action": (
                "Pods are crashing. Check previous pod logs: "
                "kubectl logs <pod> --previous -n <namespace>. "
                "Review stack traces in the error logs for root cause."
            ),
            "urgency": "IMMEDIATE",
        })

    # Dependency failures detected in logs.
    if "dependency failure" in finding_lower:
        dep_findings = [
            f.get("finding", "") for f in findings
            if "dependency failure" in f.get("finding", "").lower()
        ]
        for dep_f in dep_findings:
            dep_match = re.search(r"DEPENDENCY FAILURE: (.+?) unreachable", dep_f)
            dep_name = dep_match.group(1) if dep_match else "unknown"
            recommendations.append({
                "priority": "P1",
                "area": "External Dependency",
                "finding": f"Dependency {dep_name} is unreachable.",
                "action": (
                    f"Dependency {dep_name} is unreachable. Check: "
                    f"1. Is {dep_name} running? "
                    f"kubectl get pods -l app={dep_name.split('.')[0]} "
                    f"2. Is the service endpoint healthy? "
                    f"kubectl get svc {dep_name.split('.')[0]} "
                    f"3. Check {dep_name} logs for its own errors "
                    f"4. Check network policies between services"
                ),
                "urgency": "IMMEDIATE",
            })

    # Memory pressure in logs.
    if "memory pressure" in finding_lower:
        recommendations.append({
            "priority": "P2",
            "area": "Resources/Memory",
            "finding": "Memory pressure detected in logs.",
            "action": (
                "Check container memory limits and usage. "
                "Consider increasing memory limits or fixing memory leaks."
            ),
            "urgency": "SOON",
        })

    # Queue backlog.
    if "queue backlog" in finding_lower or "stale messages" in finding_lower:
        recommendations.append({
            "priority": "P2",
            "area": "messaging",
            "finding": "Queue backlog or stale messages detected.",
            "action": "Check consumer health, scale consumers, investigate dead letters.",
            "urgency": "SOON",
        })

    # HPA at max.
    if "hpa" in finding_lower and "max capacity" in finding_lower:
        recommendations.append({
            "priority": "P2",
            "area": "k8s",
            "finding": "HPA at maximum replica count.",
            "action": "Increase HPA maxReplicas or optimize application resource usage.",
            "urgency": "SOON",
        })

    # Slow database queries.
    if "slow db query" in finding_lower:
        recommendations.append({
            "priority": "P2",
            "area": "database",
            "finding": "Slow database queries detected.",
            "action": "Review query performance, add indexes, check connection pool.",
            "urgency": "SOON",
        })

    # Recurring incident pattern.
    if anchor.incident_pattern and anchor.incident_pattern.is_recurring:
        recommendations.append({
            "priority": "P1",
            "area": "reliability",
            "finding": f"Recurring incident: {anchor.incident_pattern.pattern_summary}",
            "action": (
                "This is a recurring incident. A permanent fix is needed, "
                "not just a restart. Review root cause from previous occurrences."
            ),
            "urgency": "IMMEDIATE",
        })

    # Sort by priority.
    recommendations.sort(key=lambda r: r.get("priority", "P9"))
    return recommendations


def _inject_finding_deep_links(
    findings: list[dict],
    anchor: InvestigationAnchor,
    entity_guid: str | None,
    effective_ns: str | None,
    intelligence: Any,
) -> None:
    """Add a ``deep_link`` URL to each finding that has a matching rule.

    Mutates *findings* in-place.  Never raises.
    """
    try:
        builder = _get_deeplink_builder()
        if builder is None:
            return

        service = anchor.primary_service
        since = anchor.since_minutes
        svc_attr = getattr(
            getattr(intelligence, "logs", None), "service_attribute", "service.name"
        ) or "service.name"

        # Compute bare service name for K8s links.
        bare_service = service
        nc = getattr(intelligence, "naming_convention", None)
        if nc and getattr(nc, "separator", None):
            sep = nc.separator
            if sep in service:
                if getattr(nc, "k8s_deployment_name_format", "full") == "bare":
                    if getattr(nc, "env_position", None) == "prefix":
                        bare_service = service.split(sep, 1)[1]
                    elif getattr(nc, "env_position", None) == "suffix":
                        bare_service = service.rsplit(sep, 1)[0]

        for finding in findings:
            try:
                source = finding.get("source", "").upper()
                signal = finding.get("signal", "")
                text = finding.get("finding", "")
                link: str | None = None

                if source == "APM":
                    if "error_rate" in signal:
                        nrql = (
                            f"SELECT percentage(count(*), WHERE error IS true) "
                            f"FROM Transaction WHERE appName='{service}' "
                            f"TIMESERIES 5 minutes SINCE {since} minutes ago"
                        )
                        link = builder.spike_chart(nrql, since)
                    elif "error_classes" in signal:
                        if entity_guid:
                            link = builder.apm_errors(entity_guid)
                        else:
                            nrql = (
                                f"SELECT count(*) FROM TransactionError "
                                f"WHERE appName='{service}' FACET errorClass "
                                f"SINCE {since} minutes ago LIMIT 10"
                            )
                            link = builder.nrql_chart(nrql, since)
                    elif "slow_queries" in signal:
                        nrql = (
                            f"SELECT average(duration), max(duration) "
                            f"FROM DatastoreSegment WHERE appName='{service}' "
                            f"FACET datastoreType, table "
                            f"SINCE {since} minutes ago"
                        )
                        link = builder.nrql_chart(nrql, since)
                    elif "external_calls" in signal:
                        if entity_guid:
                            link = builder.distributed_traces(entity_guid, since)

                elif source == "K8S":
                    ns = effective_ns or ""
                    if any(k in signal for k in ("pod_status", "replica_health", "hpa_scaling")):
                        if ns:
                            link = builder.k8s_workload(ns, bare_service)
                    elif any(k in signal for k in ("oom_kills", "resource_usage")):
                        if ns:
                            link = builder.k8s_workload(ns, bare_service)
                    elif "k8s_events" in signal:
                        link = builder.k8s_explorer(ns or None)

                elif source == "LOGS":
                    if signal == "error_logs":
                        if "DEPENDENCY FAILURE" in text or "APPLICATION CRASH" in text:
                            link = builder.log_search(
                                service, svc_attr, "ERROR", since
                            )

                elif source == "SYNTHETICS":
                    link = None  # synthetic links handled in synthetics.py

                elif source == "ALERTS":
                    pass  # handled in alerts.py

                if link:
                    finding["deep_link"] = link
            except Exception:
                continue
    except Exception:
        pass


def _inject_recommendation_links(
    recommendations: list[dict],
    anchor: InvestigationAnchor,
    entity_guid: str | None,
    effective_ns: str | None,
    intelligence: Any,
) -> None:
    """Add a ``links`` dict to each recommendation that has a matching rule.

    Mutates *recommendations* in-place.  Never raises.
    """
    try:
        builder = _get_deeplink_builder()
        if builder is None:
            return

        service = anchor.primary_service
        since = anchor.since_minutes
        svc_attr = getattr(
            getattr(intelligence, "logs", None), "service_attribute", "service.name"
        ) or "service.name"

        bare_service = service
        nc = getattr(intelligence, "naming_convention", None)
        if nc and getattr(nc, "separator", None):
            sep = nc.separator
            if sep in service:
                if getattr(nc, "k8s_deployment_name_format", "full") == "bare":
                    if getattr(nc, "env_position", None) == "prefix":
                        bare_service = service.split(sep, 1)[1]
                    elif getattr(nc, "env_position", None) == "suffix":
                        bare_service = service.rsplit(sep, 1)[0]

        for rec in recommendations:
            try:
                priority = rec.get("priority", "")
                area = rec.get("area", "").lower()
                links: dict[str, str | None] = {}

                if priority == "P1":
                    if any(k in area for k in ("apm", "errors", "error")):
                        err_nrql = (
                            f"SELECT percentage(count(*), WHERE error IS true) "
                            f"FROM Transaction WHERE appName='{service}' "
                            f"TIMESERIES 5 minutes SINCE {since} minutes ago"
                        )
                        links["error_profile"] = builder.apm_errors(entity_guid) if entity_guid else None
                        links["error_traces"] = builder.distributed_traces(entity_guid, since, error_only=True) if entity_guid else None
                        links["error_chart"] = builder.spike_chart(err_nrql, since)

                    if any(k in area for k in ("k8s", "memory", "oom")):
                        ns = effective_ns or ""
                        links["k8s_pods"] = builder.k8s_workload(ns, bare_service) if ns else None
                        links["k8s_explorer"] = builder.k8s_explorer(ns or None)
                        mem_nrql = (
                            f"SELECT average(memoryUsedBytes)/1e6 as avg_mem_mb, "
                            f"max(memoryUsedBytes)/1e6 as peak_mem_mb "
                            f"FROM K8sContainerSample "
                            f"WHERE deploymentName LIKE '%{bare_service}%' "
                            f"TIMESERIES 5 minutes SINCE {since} minutes ago"
                        )
                        links["memory_chart"] = builder.nrql_chart(mem_nrql, since)

                    if any(k in area for k in ("dependency", "external")):
                        links["error_logs"] = builder.log_search(service, svc_attr, "ERROR", since)
                        links["traces"] = builder.distributed_traces(entity_guid, since) if entity_guid else None

                    if "crash" in area:
                        ns = effective_ns or ""
                        links["crash_logs"] = builder.log_search(service, svc_attr, "ERROR", since)
                        links["pod_view"] = builder.k8s_workload(ns, bare_service) if ns else None

                elif priority == "P2":
                    if any(k in area for k in ("database", "latency")):
                        links["transactions"] = builder.apm_transactions(entity_guid) if entity_guid else None
                        sq_nrql = (
                            f"SELECT average(duration), max(duration) "
                            f"FROM DatastoreSegment WHERE appName='{service}' "
                            f"FACET datastoreType, table "
                            f"SINCE {since} minutes ago"
                        )
                        links["slow_queries"] = builder.nrql_chart(sq_nrql, since)

                # Only attach if any link was produced.
                if any(v is not None for v in links.values()):
                    rec["links"] = links
            except Exception:
                continue
    except Exception:
        pass


def _build_diagnosis_summary(
    anchor: InvestigationAnchor,
    findings: list[dict],
    recommendations: list[dict],
    domains_with_data: list[str],
) -> str:
    """Build a human-readable diagnosis summary."""
    parts: list[str] = []

    # Window info.
    if anchor.window_source == "incident_anchored":
        parts.append(
            f"Investigation anchored to incident at {anchor.window_start.isoformat()}"
        )
    else:
        parts.append(
            f"Investigation window: last {anchor.since_minutes} minutes"
        )

    # Domains.
    if domains_with_data:
        parts.append(f"Data found in: {', '.join(domains_with_data)}")

    # Critical findings count.
    critical = [f for f in findings if f.get("severity") == "CRITICAL"]
    warnings = [f for f in findings if f.get("severity") == "WARNING"]
    if critical:
        parts.append(f"{len(critical)} critical finding(s)")
    if warnings:
        parts.append(f"{len(warnings)} warning(s)")
    if not critical and not warnings:
        parts.append("No significant issues detected")

    # Top recommendation.
    if recommendations:
        top = recommendations[0]
        parts.append(f"Top action: {top.get('action', 'N/A')}")

    return "; ".join(parts)


# ── Main Investigation Function ──────────────────────────────────────────


async def investigate_service(
    service_name: str,
    namespace: str | None = None,
    since_minutes: int = 60,
) -> str:
    """Full three-phase adaptive investigation of a service.

    Phase 1: Anchor & Resolve — parse alert target, fuzzy resolve, anchor time.
    Phase 2: Discover — ask NR what data exists for this service.
    Phase 3: Adaptive Investigate — query discovered data, synthesize findings.

    Args:
        service_name: APM service name, alert target, or natural language.
        namespace: Optional K8s namespace.
        since_minutes: Time window in minutes (overridden by incident anchor).

    Returns:
        JSON string with comprehensive investigation report.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()

        # ── PHASE 1: ANCHOR & RESOLVE ──────────────────────────

        # Parse alert target to get candidates.
        candidates_raw = parse_alert_target(service_name)

        # Fuzzy resolve all candidates against known services.
        all_candidates: list[str] = []
        for candidate in candidates_raw:
            matches = fuzzy_resolve_service_candidates(
                candidate,
                intelligence.apm.service_names,
                threshold=0.45,
                max_candidates=3,
            )
            all_candidates.extend(name for name, score in matches)

        # Deduplicate preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for c in all_candidates:
            if c.lower() not in seen:
                seen.add(c.lower())
                deduped.append(c)
        all_candidates = deduped

        # Fallback: if nothing resolved, try the original name.
        if not all_candidates:
            safe_name = sanitize_service_name(service_name)
            try:
                resolved, was_fuzzy, conf = fuzzy_resolve_service(
                    safe_name, intelligence.apm.service_names
                )
                all_candidates = [resolved]
            except Exception:
                all_candidates = [safe_name]

        # Anchor investigation to incident time window.
        anchor = await _anchor_investigation(
            service_candidates=all_candidates,
            since_minutes_requested=since_minutes,
            intelligence=intelligence,
            credentials=credentials,
        )

        # ── PHASE 2: DISCOVER ──────────────────────────────────

        discovery = await discover_available_data(
            service_candidates=all_candidates,
            anchor=anchor,
            credentials=credentials,
            intelligence=intelligence,
        )

        # ── PHASE 3: ADAPTIVE INVESTIGATION ────────────────────

        # Determine namespace.
        effective_ns = namespace
        if not effective_ns and intelligence.k8s.namespaces:
            # Try to infer from anchor or intelligence.
            for ns in intelligence.k8s.namespaces:
                for cand in all_candidates:
                    if cand.lower() in ns.lower() or ns.lower() in cand.lower():
                        effective_ns = ns
                        break
                if effective_ns:
                    break

        # Build queries from discovery.
        severity_attr = intelligence.logs.severity_attribute or "level"
        queries = build_investigation_queries(
            discovery=discovery,
            anchor=anchor,
            namespace=effective_ns,
            severity_attr=severity_attr,
            naming_convention=intelligence.naming_convention,
        )

        # Run ALL queries in parallel.
        query_tasks = [
            asyncio.create_task(_run_signal_query(q, credentials))
            for q in queries
        ]

        results = await asyncio.gather(*query_tasks, return_exceptions=True)

        # Apply health checks to each result.
        findings: list[dict] = []
        raw_data: dict[str, Any] = {}

        for query, result in zip(queries, results):
            if isinstance(result, Exception):
                raw_data[query.signal] = {"error": str(result)}
                continue

            raw_data[query.signal] = _strip_null_timeseries(result)
            health_check = get_health_check(query.signal)
            signals = health_check(result)
            for signal in signals:
                findings.append({
                    "source": query.domain.upper(),
                    "signal": query.signal,
                    "severity": _severity_emoji(signal),
                    "finding": signal,
                })

        # ── DEEP LINKS ─────────────────────────────────────────

        entity_guid = intelligence.apm.service_guids.get(
            anchor.primary_service
        )

        _inject_finding_deep_links(
            findings, anchor, entity_guid, effective_ns, intelligence
        )

        # ── SYNTHESIS ──────────────────────────────────────────

        recommendations = _generate_recommendations(
            findings, anchor, discovery, raw_data
        )

        _inject_recommendation_links(
            recommendations, anchor, entity_guid, effective_ns, intelligence
        )

        diagnosis_summary = _build_diagnosis_summary(
            anchor, findings, recommendations, discovery.domains_with_data
        )

        # ── PATTERN ANALYSIS ───────────────────────────────────

        pattern_analysis: dict | None = None
        if anchor.incident_pattern and anchor.incident_pattern.is_recurring:
            pattern_analysis = {
                "occurrences": anchor.incident_pattern.occurrence_count,
                "period": "last 7 days",
                "first_seen": anchor.incident_pattern.first_occurrence.isoformat(),
                "last_seen": anchor.incident_pattern.last_occurrence.isoformat(),
                "recurrence_interval_hours": anchor.incident_pattern.recurrence_interval_hours,
                "consistent_cause": anchor.incident_pattern.consistent_cause,
                "summary": anchor.incident_pattern.pattern_summary,
                "recommendation": (
                    "This is a recurring incident. "
                    "A permanent fix is needed, not just a restart."
                ),
            }

        # ── FINAL REPORT ───────────────────────────────────────

        duration_ms = int((time.time() - start) * 1000)

        return json.dumps(
            {
                "investigation_report": {
                    "service": anchor.primary_service,
                    "service_overview": (
                        _get_deeplink_builder().entity_link(entity_guid)
                        if entity_guid and _get_deeplink_builder()
                        else None
                    ),
                    "input_received": service_name,
                    "all_services_investigated": all_candidates,
                    "window": {
                        "source": anchor.window_source,
                        "start": anchor.window_start.isoformat(),
                        "end": (
                            anchor.window_end.isoformat()
                            if anchor.window_end
                            else "now"
                        ),
                        "since_minutes": anchor.since_minutes,
                        "note": (
                            "Window anchored to incident start time"
                            if anchor.window_source == "incident_anchored"
                            else "Window based on request parameter"
                        ),
                    },
                    "overall_status": _overall_status(findings),
                    "diagnosis_summary": diagnosis_summary,
                    "domains_investigated": discovery.domains_with_data,
                    "domains_no_data": [
                        d
                        for d in [
                            "k8s",
                            "apm",
                            "logs",
                            "synthetics",
                            "infra",
                            "browser",
                            "messaging",
                        ]
                        if d not in discovery.domains_with_data
                    ],
                    "discovery_duration_ms": discovery.discovery_duration_ms,
                    "active_incident": anchor.active_incident,
                },
                "pattern_analysis": pattern_analysis,
                "findings": findings,
                "prioritized_recommendations": recommendations,
                "raw_data": raw_data,
                "duration_ms": duration_ms,
            },
            indent=2,
        )

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("investigate_service failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "investigate_service",
            "hint": "Check service name. Use get_apm_applications() to list services.",
            "data_available": False,
            "duration_ms": duration_ms,
        })


def _safe_parse(result: str | BaseException, source: str) -> dict:
    """Safely parse a JSON result from a parallel task.

    Args:
        result: Raw result string or exception from asyncio.gather.
        source: Source name for error logging.

    Returns:
        Parsed dict or error dict.
    """
    if isinstance(result, BaseException):
        logger.warning("Source '%s' failed: %s", source, result)
        return {"error": str(result), "source": source}
    try:
        return json.loads(result)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Source '%s' parse error: %s", source, exc)
        return {"error": f"Parse error: {exc}", "source": source}

