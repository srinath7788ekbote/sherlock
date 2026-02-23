"""
Discovery engine for Sherlock.

Asks New Relic "what data exists for this service in this time window?"
before deciding what to investigate. The EVENT_REGISTRY is the single
source of truth for known event types — adding a new event type here
is the only code change needed to make Sherlock aware of it.
"""

import asyncio
import logging
import time
from typing import Any

from pydantic import BaseModel, Field

from core.credentials import Credentials

logger = logging.getLogger("sherlock.discovery")

# NerdGraph NRQL query template.
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


# ── Pydantic Models ─────────────────────────────────────────────────────


class EventTypeInfo(BaseModel):
    """Metadata for a single New Relic event type."""

    domain: str
    service_filters: list[str] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)
    description: str = ""


class AvailableEventType(BaseModel):
    """An event type confirmed to have data for the target service."""

    event_type: str
    domain: str
    event_count: int = 0
    matched_filter: str = ""
    matched_value: str = ""
    signals: list[str] = Field(default_factory=list)


class DiscoveryResult(BaseModel):
    """Result of the discovery phase — what data exists."""

    available: dict[str, AvailableEventType] = Field(default_factory=dict)
    unavailable: list[str] = Field(default_factory=list)
    domains_with_data: list[str] = Field(default_factory=list)
    service_filter_map: dict[str, str] = Field(default_factory=dict)
    discovery_duration_ms: int = 0
    total_event_types_checked: int = 0
    discovery_timeout: bool = False


# ── Discovery Constants ──────────────────────────────────────────────────

# Discovery only needs to know if data EXISTS — use a short window.
DISCOVERY_WINDOW_MINUTES = 30

# Timeout for each discovery tier.
DISCOVERY_TIMEOUT_S = 15.0

# Tier 1: Always checked — core event types.
TIER1_EVENT_TYPES = {
    "Transaction", "TransactionError", "Log",
    "K8sPodSample", "K8sDeploymentSample", "SyntheticCheck",
}

# Tier 2: Only checked if Tier 1 found K8s data.
TIER2_EVENT_TYPES = {
    "K8sContainerSample", "K8sHpaSample", "K8sReplicaSetSample",
    "InfrastructureEvent", "K8sNodeSample",
}


# ── Event Type Registry ─────────────────────────────────────────────────

EVENT_REGISTRY: dict[str, EventTypeInfo] = {
    # ── K8s ──────────────────────────────────────────────
    "K8sPodSample": EventTypeInfo(
        domain="k8s",
        service_filters=["deploymentName", "podName", "namespaceName", "label.app"],
        signals=["pod_status", "restarts"],
        description="Pod lifecycle, status, restart counts",
    ),
    "K8sDeploymentSample": EventTypeInfo(
        domain="k8s",
        service_filters=["deploymentName", "namespaceName"],
        signals=["replica_health"],
        description="Deployment desired vs ready replicas",
    ),
    "K8sReplicaSetSample": EventTypeInfo(
        domain="k8s",
        service_filters=["replicaSetName", "namespaceName"],
        signals=["replicaset_health"],
        description="ReplicaSet desired vs ready",
    ),
    "K8sHpaSample": EventTypeInfo(
        domain="k8s",
        service_filters=["horizontalPodAutoscalerName", "namespaceName"],
        signals=["hpa_scaling"],
        description="HPA min/max/current replicas, scaling events",
    ),
    "K8sContainerSample": EventTypeInfo(
        domain="k8s",
        service_filters=["deploymentName", "containerName", "namespaceName"],
        signals=["oom_kills", "resource_usage", "resource_limits", "throttling"],
        description="Container CPU/memory usage, OOMKills",
    ),
    "K8sNodeSample": EventTypeInfo(
        domain="k8s",
        service_filters=["nodeName"],
        signals=["node_pressure", "node_capacity"],
        description="Node CPU/memory pressure",
    ),
    "K8sVolumeSample": EventTypeInfo(
        domain="k8s",
        service_filters=["deploymentName", "namespaceName"],
        signals=["disk_pressure"],
        description="PersistentVolume usage",
    ),
    "K8sStatefulSetSample": EventTypeInfo(
        domain="k8s",
        service_filters=["statefulSetName", "namespaceName"],
        signals=["statefulset_health"],
        description="StatefulSet replica health",
    ),
    "K8sDaemonSetSample": EventTypeInfo(
        domain="k8s",
        service_filters=["daemonSetName", "namespaceName"],
        signals=["daemonset_health"],
        description="DaemonSet desired vs ready",
    ),
    "K8sJobSample": EventTypeInfo(
        domain="k8s",
        service_filters=["jobName", "namespaceName"],
        signals=["job_completion"],
        description="Job success/failure counts",
    ),
    "InfrastructureEvent": EventTypeInfo(
        domain="k8s",
        service_filters=["involvedObjectName", "involvedObjectNamespace"],
        signals=["k8s_events", "warnings", "errors"],
        description="Kubernetes events: OOMKilling, BackOff etc",
    ),
    # ── APM ──────────────────────────────────────────────
    "Transaction": EventTypeInfo(
        domain="apm",
        service_filters=["appName", "entity.name"],
        signals=["error_rate", "latency", "throughput", "apdex"],
        description="Request latency, throughput, errors",
    ),
    "TransactionError": EventTypeInfo(
        domain="apm",
        service_filters=["appName", "entity.name"],
        signals=["error_classes", "error_messages", "error_rate"],
        description="Error classes, messages, stack traces",
    ),
    "Span": EventTypeInfo(
        domain="apm",
        service_filters=["appName", "entity.name", "service.name"],
        signals=["distributed_traces", "external_calls", "slow_spans"],
        description="Distributed traces, external HTTP calls",
    ),
    "DatastoreSegment": EventTypeInfo(
        domain="apm",
        service_filters=["appName", "entity.name"],
        signals=["db_performance", "slow_queries"],
        description="Database query performance",
    ),
    "ErrorTrace": EventTypeInfo(
        domain="apm",
        service_filters=["appName", "entity.name"],
        signals=["stack_traces", "error_detail"],
        description="Full error stack traces",
    ),
    # ── Logs ─────────────────────────────────────────────
    "Log": EventTypeInfo(
        domain="logs",
        service_filters=["service.name", "entity.name", "app", "deployment", "container"],
        signals=["error_logs", "warn_logs", "log_patterns"],
        description="Application log lines",
    ),
    # ── Infrastructure ────────────────────────────────────
    "SystemSample": EventTypeInfo(
        domain="infra",
        service_filters=["entityKey", "hostname"],
        signals=["cpu_usage", "memory_usage", "disk_usage"],
        description="Host CPU, memory, disk metrics",
    ),
    "NetworkSample": EventTypeInfo(
        domain="infra",
        service_filters=["entityKey", "hostname"],
        signals=["network_errors", "bandwidth"],
        description="Network I/O and error rates",
    ),
    "ProcessSample": EventTypeInfo(
        domain="infra",
        service_filters=["processDisplayName", "hostname"],
        signals=["process_cpu", "process_memory"],
        description="Per-process resource usage",
    ),
    # ── Synthetics ───────────────────────────────────────
    "SyntheticCheck": EventTypeInfo(
        domain="synthetics",
        service_filters=["monitorName", "monitorId"],
        signals=["pass_rate", "failures", "location_status"],
        description="Synthetic monitor run results",
    ),
    "SyntheticRequest": EventTypeInfo(
        domain="synthetics",
        service_filters=["monitorName"],
        signals=["failed_requests", "response_codes"],
        description="HTTP requests made by synthetic monitors",
    ),
    # ── Browser ──────────────────────────────────────────
    "PageView": EventTypeInfo(
        domain="browser",
        service_filters=["appName", "entity.name"],
        signals=["page_load_time", "js_errors", "user_impact"],
        description="Browser page load times",
    ),
    "AjaxRequest": EventTypeInfo(
        domain="browser",
        service_filters=["appName", "entity.name"],
        signals=["ajax_errors", "api_call_failures"],
        description="Browser AJAX/API call performance",
    ),
    "JavaScriptError": EventTypeInfo(
        domain="browser",
        service_filters=["appName", "entity.name"],
        signals=["js_error_rate", "js_error_classes"],
        description="JavaScript errors in browser",
    ),
    # ── Custom / Queue ───────────────────────────────────
    "QueueSample": EventTypeInfo(
        domain="messaging",
        service_filters=["provider.queueName", "aws.sqs.QueueName"],
        signals=["queue_depth", "message_age", "dlq_count"],
        description="Queue depth, message age, DLQ",
    ),
    "MessageBrokerSample": EventTypeInfo(
        domain="messaging",
        service_filters=["provider.queueName", "entityName"],
        signals=["broker_health"],
        description="Message broker metrics",
    ),
}


# ── Discovery Engine ─────────────────────────────────────────────────────


async def _check_event_type(
    event_type: str,
    info: EventTypeInfo,
    service_candidates: list[str],
    since_minutes: int,
    until_clause: str,
    account_id: str,
    headers: dict[str, str],
    endpoint: str,
) -> AvailableEventType | None:
    """Check whether an event type has data for any service candidate.

    Tries each service filter attribute in order until one returns count > 0.
    Returns None if no data found for any candidate/filter combination.
    """
    import httpx

    for candidate in service_candidates:
        for filter_attr in info.service_filters:
            # Sanitize candidate for NRQL embedding.
            safe_candidate = candidate.replace("'", "").replace('"', "")
            nrql = (
                f"SELECT count(*) as event_count "
                f"FROM {event_type} "
                f"WHERE `{filter_attr}` LIKE '%{safe_candidate}%' "
                f"SINCE {since_minutes} minutes ago "
                f"{until_clause}"
            )
            escaped_nrql = nrql.replace('"', '\\"')
            gql = GQL_NRQL_QUERY % (account_id, escaped_nrql)

            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.post(
                        endpoint,
                        json={"query": gql},
                        headers=headers,
                    )
                    resp.raise_for_status()
                    body = resp.json()

                results = (
                    body.get("data", {})
                    .get("actor", {})
                    .get("account", {})
                    .get("nrql", {})
                    .get("results", [])
                )

                if results:
                    count = results[0].get("event_count", 0)
                    if count and count > 0:
                        return AvailableEventType(
                            event_type=event_type,
                            domain=info.domain,
                            event_count=int(count),
                            matched_filter=filter_attr,
                            matched_value=safe_candidate,
                            signals=info.signals,
                        )
            except Exception as exc:
                logger.debug(
                    "Discovery check failed for %s/%s: %s",
                    event_type, filter_attr, exc,
                )
                continue

    return None


async def discover_available_data(
    service_candidates: list[str],
    anchor: Any,
    credentials: Credentials,
    intelligence: Any = None,
) -> DiscoveryResult:
    """Discover which event types have data for the given service candidates.

    Uses tiered discovery with a capped time window and timeout:
      Tier 1 (always): Transaction, TransactionError, Log, K8sPodSample,
                        K8sDeploymentSample, SyntheticCheck
      Tier 2 (if K8s found): K8sContainerSample, K8sHpaSample,
                              K8sReplicaSetSample, InfrastructureEvent, K8sNodeSample
      Tier 3 (conditional): PageView, SystemSample, QueueSample, etc.

    Discovery COUNT queries use min(since_minutes, 30) for speed.
    Each tier has a 15-second timeout; on timeout returns sensible defaults.

    Args:
        service_candidates: List of candidate service names to check.
        anchor: InvestigationAnchor with since_minutes and until_clause.
        credentials: Active account credentials.
        intelligence: Optional AccountIntelligence for tier 3 decisions.

    Returns:
        DiscoveryResult listing which event types have data.
    """
    start_ms = int(time.time() * 1000)

    if not service_candidates:
        return DiscoveryResult(
            unavailable=list(EVENT_REGISTRY.keys()),
            total_event_types_checked=len(EVENT_REGISTRY),
            discovery_duration_ms=int(time.time() * 1000) - start_ms,
        )

    headers = {
        "API-Key": credentials.api_key,
        "Content-Type": "application/json",
    }
    endpoint = credentials.endpoint
    account_id = credentials.account_id

    # Cap discovery window for efficiency — discovery only checks existence.
    discovery_since = min(anchor.since_minutes, DISCOVERY_WINDOW_MINUTES)
    until_clause = anchor.until_clause

    available: dict[str, AvailableEventType] = {}
    unavailable: list[str] = []
    service_filter_map: dict[str, str] = {}
    domains_seen: set[str] = set()
    total_checked = 0
    timed_out = False

    def _build_tasks(event_type_names: set[str]):
        """Build check tasks for a set of event type names."""
        tasks = []
        names = []
        for event_type, info in EVENT_REGISTRY.items():
            if event_type in event_type_names:
                names.append(event_type)
                tasks.append(
                    _check_event_type(
                        event_type=event_type,
                        info=info,
                        service_candidates=service_candidates,
                        since_minutes=discovery_since,
                        until_clause=until_clause,
                        account_id=account_id,
                        headers=headers,
                        endpoint=endpoint,
                    )
                )
        return names, tasks

    def _process_results(names, results_list):
        """Process gather results into available/unavailable."""
        for name, result in zip(names, results_list):
            if isinstance(result, Exception) or result is None:
                unavailable.append(name)
            else:
                available[name] = result
                service_filter_map[name] = result.matched_filter
                domains_seen.add(result.domain)

    # ── Tier 1: Core event types (always checked) ──
    tier1_names, tier1_tasks = _build_tasks(TIER1_EVENT_TYPES)
    total_checked += len(tier1_names)

    try:
        tier1_results = await asyncio.wait_for(
            asyncio.gather(*tier1_tasks, return_exceptions=True),
            timeout=DISCOVERY_TIMEOUT_S,
        )
        _process_results(tier1_names, tier1_results)
    except asyncio.TimeoutError:
        logger.warning(
            "Discovery Tier 1 timed out after %.0fs — using defaults",
            DISCOVERY_TIMEOUT_S,
        )
        timed_out = True
        # Sensible defaults: assume APM + logs available.
        if service_candidates:
            available["Transaction"] = AvailableEventType(
                event_type="Transaction", domain="apm",
                matched_filter="appName", matched_value=service_candidates[0],
                signals=["error_rate", "latency", "throughput", "apdex"],
            )
            available["Log"] = AvailableEventType(
                event_type="Log", domain="logs",
                matched_filter="service.name", matched_value=service_candidates[0],
                signals=["error_logs", "warn_logs", "log_patterns"],
            )
            service_filter_map["Transaction"] = "appName"
            service_filter_map["Log"] = "service.name"
            domains_seen.update({"apm", "logs"})

    # ── Tier 2: Extended K8s (only if Tier 1 found K8s data) ──
    if not timed_out and "k8s" in domains_seen:
        tier2_names, tier2_tasks = _build_tasks(TIER2_EVENT_TYPES)
        total_checked += len(tier2_names)

        if tier2_tasks:
            try:
                tier2_results = await asyncio.wait_for(
                    asyncio.gather(*tier2_tasks, return_exceptions=True),
                    timeout=DISCOVERY_TIMEOUT_S,
                )
                _process_results(tier2_names, tier2_results)
            except asyncio.TimeoutError:
                logger.warning("Discovery Tier 2 timed out")
                unavailable.extend(tier2_names)

    # ── Tier 3: Conditional event types ──
    if not timed_out:
        tier3_types: set[str] = set()
        if intelligence:
            if getattr(getattr(intelligence, "browser", None), "enabled", False):
                tier3_types.add("PageView")
            if getattr(getattr(intelligence, "infra", None), "host_count", 0) > 0:
                tier3_types.add("SystemSample")
        # Add messaging if candidates suggest queue/message services.
        for svc in service_candidates:
            if any(kw in svc.lower() for kw in ("queue", "message", "kafka", "rabbit", "sqs")):
                tier3_types.add("QueueSample")
                tier3_types.add("MessageBrokerSample")

        if tier3_types:
            tier3_names, tier3_tasks = _build_tasks(tier3_types)
            total_checked += len(tier3_names)

            if tier3_tasks:
                try:
                    tier3_results = await asyncio.wait_for(
                        asyncio.gather(*tier3_tasks, return_exceptions=True),
                        timeout=DISCOVERY_TIMEOUT_S,
                    )
                    _process_results(tier3_names, tier3_results)
                except asyncio.TimeoutError:
                    logger.warning("Discovery Tier 3 timed out")
                    unavailable.extend(tier3_names)

    # Mark remaining unchecked event types as unavailable.
    checked_set = set(n for n in available) | set(unavailable)
    for et in EVENT_REGISTRY:
        if et not in checked_set:
            unavailable.append(et)

    duration_ms = int(time.time() * 1000) - start_ms

    return DiscoveryResult(
        available=available,
        unavailable=unavailable,
        domains_with_data=sorted(domains_seen),
        service_filter_map=service_filter_map,
        discovery_duration_ms=duration_ms,
        total_event_types_checked=total_checked,
        discovery_timeout=timed_out,
    )
