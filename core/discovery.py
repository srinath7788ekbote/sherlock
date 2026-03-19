"""
Discovery engine for Sherlock.

DEPRECATED: This module is part of the monolith investigation architecture.
The agent-team architecture uses direct NRQL queries in each domain tool
instead. Kept for backward compatibility with tools/investigate.py (also
deprecated). Do not add new code here.

Original purpose: Asks New Relic "what data exists for this service in this
time window?" before deciding what to investigate.
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

# Discovery needs to know if data EXISTS.  The window must be wide
# enough to cover the full investigation period so that data present
# e.g. 12 hours ago (but quiet recently) is not missed.
# Floor: always check at least DISCOVERY_WINDOW_MINUTES (120 min).
# Ceiling: never exceed DISCOVERY_MAX_WINDOW_MINUTES (1440 min / 24 h).
DISCOVERY_WINDOW_MINUTES = 120
DISCOVERY_MAX_WINDOW_MINUTES = 1440

# Single timeout for the full discovery pass (all event types in parallel).
DISCOVERY_TIMEOUT_S = 45.0

# Maximum concurrent NerdGraph requests during discovery.
DISCOVERY_CONCURRENCY = 10

# Tier constants kept for backward-compatibility with tests/imports.
# Discovery no longer gates any tier on prior results — all event types
# are probed unconditionally in a single parallel pass.
TIER1_EVENT_TYPES = {
    "Transaction", "TransactionError", "Log",
    "K8sPodSample", "K8sDeploymentSample", "SyntheticCheck",
}

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

# All domains represented in the EVENT_REGISTRY.
ALL_DOMAINS = sorted({info.domain for info in EVENT_REGISTRY.values()})


# ── Discovery Engine ─────────────────────────────────────────────────────


async def _safe_nrql_count(
    nrql: str,
    account_id: str,
    headers: dict[str, str],
    endpoint: str,
) -> int:
    """Execute a count NRQL query and return the count, or 0 on any error.

    Handles NerdGraph responses where intermediate keys are ``None``
    (e.g. ``"nrql": null`` on query syntax errors) without crashing.
    """
    import httpx

    escaped = nrql.replace('"', '\\"')
    gql = GQL_NRQL_QUERY % (account_id, escaped)

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                endpoint,
                json={"query": gql},
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()

        # Safe navigation — any ``None`` intermediate stops the chain.
        d = body
        for key in ("data", "actor", "account", "nrql", "results"):
            d = d.get(key) if isinstance(d, dict) else None
            if d is None:
                return 0

        if isinstance(d, list) and d:
            count = d[0].get("event_count", 0)
            return int(count) if count else 0
        return 0
    except Exception:
        return 0


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

    Per-candidate existence check: for each candidate, build ONE query
    with all filter attributes OR'd (short query).  Short-circuit on the
    first candidate that returns count > 0.  Then identify the specific
    matched filter attribute for downstream query building.

    Attribute names with dots (e.g. ``label.app``) are backtick-quoted;
    plain names are left unquoted for maximum NRQL compatibility.
    """
    safe_candidates = [c.replace("'", "").replace('"', "") for c in service_candidates]

    def _attr_ref(fattr: str) -> str:
        return f"`{fattr}`" if "." in fattr else fattr

    # ── Phase 1: Per-candidate existence check ──
    # Each candidate → ONE query with all filters OR'd (~4 OR parts).
    # Short-circuit on first match.
    matched_candidate: str | None = None
    total_count = 0

    for safe in safe_candidates:
        or_parts = [
            f"{_attr_ref(f)} LIKE '%{safe}%'" for f in info.service_filters
        ]
        where_clause = " OR ".join(or_parts)
        nrql = (
            f"SELECT count(*) as event_count "
            f"FROM {event_type} "
            f"WHERE ({where_clause}) "
            f"SINCE {since_minutes} minutes ago "
            f"{until_clause}"
        )

        count = await _safe_nrql_count(nrql, account_id, headers, endpoint)
        if count > 0:
            matched_candidate = safe
            total_count = count
            break

    if not matched_candidate:
        return None

    # ── Phase 2: Identify which filter attribute matched ──
    for fattr in info.service_filters:
        nrql = (
            f"SELECT count(*) as event_count "
            f"FROM {event_type} "
            f"WHERE {_attr_ref(fattr)} LIKE '%{matched_candidate}%' "
            f"SINCE {since_minutes} minutes ago "
            f"{until_clause}"
        )

        count = await _safe_nrql_count(nrql, account_id, headers, endpoint)
        if count > 0:
            return AvailableEventType(
                event_type=event_type,
                domain=info.domain,
                event_count=count,
                matched_filter=fattr,
                matched_value=matched_candidate,
                signals=info.signals,
            )

    # Phase 1 confirmed data but Phase 2 couldn't pinpoint the filter
    # (transient errors). Return with best-guess defaults.
    return AvailableEventType(
        event_type=event_type,
        domain=info.domain,
        event_count=total_count,
        matched_filter=info.service_filters[0],
        matched_value=matched_candidate,
        signals=info.signals,
    )


async def discover_available_data(
    service_candidates: list[str],
    anchor: Any,
    credentials: Credentials,
    intelligence: Any = None,
) -> DiscoveryResult:
    """Discover which event types have data for the given service candidates.

    Probes ALL event types in the EVENT_REGISTRY unconditionally in a single
    parallel pass, controlled by a semaphore for concurrency.  No tiering is
    applied — every domain (apm, k8s, logs, infra, browser, synthetics,
    messaging) is checked regardless of what other domains return.

    The discovery window scales with the investigation window: it uses
    ``max(since_minutes, 120)`` capped at 1440 minutes (24 h) so that
    data from earlier in long investigations is not missed.

    Args:
        service_candidates: List of candidate service names to check.
        anchor: InvestigationAnchor with since_minutes and until_clause.
        credentials: Active account credentials.
        intelligence: Unused (kept for backward compatibility).

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

    # Scale discovery window to the investigation window so that data
    # present earlier in long investigations (e.g. 24 h) is not missed.
    # Floor: DISCOVERY_WINDOW_MINUTES (120).  Ceiling: DISCOVERY_MAX_WINDOW_MINUTES (1440).
    discovery_since = min(
        max(anchor.since_minutes, DISCOVERY_WINDOW_MINUTES),
        DISCOVERY_MAX_WINDOW_MINUTES,
    )
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

    # ── Check ALL event types unconditionally ──
    # Every domain is probed in a single parallel pass controlled by a
    # semaphore.  No tiering — completeness over speed.
    all_names, all_coros = _build_tasks(set(EVENT_REGISTRY.keys()))
    total_checked = len(all_names)

    semaphore = asyncio.Semaphore(DISCOVERY_CONCURRENCY)

    async def _throttled(coro):
        async with semaphore:
            return await coro

    try:
        all_results = await asyncio.wait_for(
            asyncio.gather(
                *[_throttled(c) for c in all_coros],
                return_exceptions=True,
            ),
            timeout=DISCOVERY_TIMEOUT_S,
        )
        _process_results(all_names, all_results)
    except asyncio.TimeoutError:
        logger.warning(
            "Discovery timed out after %.0fs — using defaults",
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

    # Mark any event types not already categorized as unavailable.
    checked_set = set(available) | set(unavailable)
    for et in EVENT_REGISTRY:
        if et not in checked_set:
            unavailable.append(et)

    duration_ms = int(time.time() * 1000) - start_ms

    # ── Resolve & cache the actual entity names ──────────────────────
    # Discovery uses LIKE '%candidate%' which matches, but the real
    # appName / service.name in NR may differ from the candidate string.
    # Query for the actual unique values and cache them so every
    # subsequent tool uses the real name.
    await _resolve_and_cache_entity_names(
        available=available,
        service_candidates=service_candidates,
        account_id=account_id,
        headers=headers,
        endpoint=endpoint,
    )

    return DiscoveryResult(
        available=available,
        unavailable=unavailable,
        domains_with_data=sorted(domains_seen),
        service_filter_map=service_filter_map,
        discovery_duration_ms=duration_ms,
        total_event_types_checked=total_checked,
        discovery_timeout=timed_out,
    )


async def _resolve_and_cache_entity_names(
    available: dict[str, AvailableEventType],
    service_candidates: list[str],
    account_id: str,
    headers: dict[str, str],
    endpoint: str,
) -> None:
    """Query New Relic for the exact entity names and cache them.

    When discovery confirms data via ``LIKE '%candidate%'``, the real
    ``appName`` or ``service.name`` may be longer/different than the
    candidate (e.g. ``eswd-prod/presentationsdfinsh/presentation-service``
    vs the candidate ``eswd-prod/presentation-service``).

    This function issues a *single* ``SELECT uniques(attr)`` query for
    the most authoritative event type (Transaction first, then Log) to
    learn the exact entity name.  It caches the mapping in
    ``AccountContext`` so that **all subsequent tools** (logs, golden
    signals, K8s, APM metrics, etc.) reuse the correct name.

    Never raises — silently degrades if the query fails.
    """
    import httpx

    # Pick the best event type to resolve from.
    # Prefer Transaction (appName is the canonical APM entity name).
    resolve_event = None
    resolve_attr = None
    resolve_candidate = None

    for event_type in ("Transaction", "TransactionError", "Log"):
        avail = available.get(event_type)
        if avail:
            resolve_event = event_type
            resolve_attr = avail.matched_filter
            resolve_candidate = avail.matched_value
            break

    if not resolve_event or not resolve_attr or not resolve_candidate:
        return

    nrql = (
        f"SELECT uniques(`{resolve_attr}`) "
        f"FROM {resolve_event} "
        f"WHERE `{resolve_attr}` LIKE '%{resolve_candidate}%' "
        f"SINCE 7 days ago"
    )
    escaped_nrql = nrql.replace('"', '\\"')
    gql = GQL_NRQL_QUERY % (account_id, escaped_nrql)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
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

        if not results:
            return

        unique_names = results[0].get(f"uniques.{resolve_attr}", [])
        if not unique_names:
            return

        # Pick the best match among the unique names.
        # If there's only one, use it.  If multiple, pick the one that
        # best matches the original candidate (longest common substring).
        from difflib import SequenceMatcher

        best_name: str | None = None

        if len(unique_names) == 1:
            best_name = unique_names[0]
        else:
            best_score = -1.0
            for name in unique_names:
                score = SequenceMatcher(
                    None,
                    resolve_candidate.lower(),
                    name.lower(),
                ).ratio()
                if score > best_score:
                    best_score = score
                    best_name = name

        if not best_name or best_name == resolve_candidate:
            return

        # Cache the mapping: user input → real NR entity name.
        try:
            from core.context import AccountContext

            ctx = AccountContext()
            if ctx.is_connected():
                # Cache for every original candidate that the user provided.
                for candidate in service_candidates:
                    ctx.cache_resolved_name(candidate, best_name)
                # Also update the AvailableEventType matched_value to the
                # real name so queries built from discovery use it.
                for avail in available.values():
                    if avail.matched_value == resolve_candidate:
                        avail.matched_value = best_name
                logger.info(
                    "Resolved actual entity name: '%s' → '%s' (via %s.%s)",
                    resolve_candidate, best_name, resolve_event, resolve_attr,
                )
        except Exception:
            pass

    except Exception as exc:
        logger.debug("Entity name resolution failed: %s", exc)
