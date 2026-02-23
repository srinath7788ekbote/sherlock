"""
Kubernetes health tool for Sherlock.

Provides K8s cluster, namespace, and pod-level health data.
Internally uses the discovery engine and query builder for adaptive queries.
Kept as a standalone tool but delegates to the three-phase engine.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from client.newrelic import get_client
from core.context import AccountContext
from core.deeplinks import get_builder as _get_deeplink_builder
from core.discovery import (
    EVENT_REGISTRY,
    AvailableEventType,
    DiscoveryResult,
    discover_available_data,
)
from core.query_builder import (
    build_investigation_queries,
    get_health_check,
)
from core.sanitize import fuzzy_resolve_service, sanitize_service_name
from tools.investigate import InvestigationAnchor

logger = logging.getLogger("sherlock.tools.k8s")

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

# NRQL queries for K8s health (legacy — kept for fallback).
NRQL_POD_STATUS = (
    "SELECT latest(status), latest(isReady), latest(nodeName), "
    "latest(reason), latest(message) "
    "FROM K8sPodSample WHERE namespaceName = '%s' "
    "%s"
    "SINCE %d minutes ago FACET podName LIMIT 100"
)

NRQL_CONTAINER_RESTARTS = (
    "SELECT sum(restartCountDelta) as restarts "
    "FROM K8sContainerSample WHERE namespaceName = '%s' "
    "%s"
    "SINCE %d minutes ago FACET containerName, podName LIMIT 50"
)

NRQL_NODE_HEALTH = (
    "SELECT latest(cpuUsedCoreMilliseconds/cpuLimitCoreMilliseconds * 100) as cpu_pct, "
    "latest(memoryWorkingSetBytes/memoryLimitBytes * 100) as memory_pct "
    "FROM K8sContainerSample WHERE namespaceName = '%s' "
    "%s"
    "SINCE %d minutes ago FACET podName LIMIT 50"
)

NRQL_DEPLOYMENT_STATUS = (
    "SELECT latest(podsAvailable), latest(podsDesired), "
    "latest(podsUnavailable), latest(podsReady) "
    "FROM K8sDeploymentSample WHERE namespaceName = '%s' "
    "%s"
    "SINCE %d minutes ago FACET deploymentName LIMIT 50"
)


async def get_k8s_health(
    service_name: str | None = None,
    namespace: str | None = None,
    since_minutes: int = 30,
) -> str:
    """Get Kubernetes health data for a service or namespace.

    Uses the discovery engine to find what K8s data exists, then
    queries only discovered event types via the query builder.

    Args:
        service_name: Optional service name to filter (fuzzy resolved).
        namespace: Optional K8s namespace to scope the query.
        since_minutes: Time window in minutes.

    Returns:
        JSON string with K8s health data.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()

        if not intelligence.k8s.integrated:
            return json.dumps({
                "error": "Kubernetes integration not detected for this account.",
                "tool": "get_k8s_health",
                "hint": "Enable New Relic Kubernetes integration.",
                "data_available": False,
            })

        # Resolve namespace.
        resolved_ns = namespace
        was_fuzzy_ns = False
        if namespace:
            safe_ns = sanitize_service_name(namespace)
            try:
                resolved_ns, was_fuzzy_ns, _ = fuzzy_resolve_service(
                    safe_ns, intelligence.k8s.namespaces, threshold=0.5
                )
            except Exception:
                resolved_ns = safe_ns
        elif service_name:
            safe_name = sanitize_service_name(service_name)
            try:
                _, _, _ = fuzzy_resolve_service(
                    safe_name, intelligence.apm.service_names, threshold=0.5,
                    naming_convention=intelligence.naming_convention,
                )
            except Exception:
                pass
            # Use naming convention to map APM env segment to K8s namespace.
            nc = intelligence.naming_convention
            if nc.separator and nc.env_position and nc.apm_to_k8s_namespace_map:
                sep = nc.separator
                if sep in safe_name:
                    if nc.env_position == "prefix":
                        env_segment = safe_name.split(sep, 1)[0]
                    else:
                        env_segment = safe_name.rsplit(sep, 1)[-1]
                    mapped_ns = nc.apm_to_k8s_namespace_map.get(env_segment)
                    if mapped_ns:
                        resolved_ns = mapped_ns
            if not resolved_ns and intelligence.k8s.namespaces:
                resolved_ns = intelligence.k8s.namespaces[0]

        if not resolved_ns and intelligence.k8s.namespaces:
            resolved_ns = intelligence.k8s.namespaces[0]

        if not resolved_ns:
            return json.dumps({
                "error": "No namespace specified or detected.",
                "tool": "get_k8s_health",
                "hint": "Provide a namespace parameter.",
                "data_available": False,
            })

        # Resolve service name.
        resolved_svc = None
        was_fuzzy_svc = False
        if service_name:
            safe_name = sanitize_service_name(service_name)
            try:
                resolved_svc, was_fuzzy_svc, _ = fuzzy_resolve_service(
                    safe_name, intelligence.apm.service_names, threshold=0.5,
                    naming_convention=intelligence.naming_convention,
                )
            except Exception:
                resolved_svc = safe_name

        # Build candidates for discovery.
        candidates = [resolved_svc] if resolved_svc else [resolved_ns]

        # Build anchor for query builder.
        now = datetime.now(timezone.utc)
        anchor = InvestigationAnchor(
            primary_service=resolved_svc or resolved_ns,
            all_candidates=candidates,
            window_start=now - timedelta(minutes=since_minutes),
            since_minutes=since_minutes,
            until_clause="",
            window_source="requested",
        )

        # Discover available K8s data.
        discovery = await discover_available_data(
            service_candidates=candidates,
            anchor=anchor,
            credentials=credentials,
            intelligence=intelligence,
        )

        # Filter to only K8s domain.
        k8s_available = {
            k: v for k, v in discovery.available.items()
            if v.domain == "k8s"
        }

        if not k8s_available:
            # Fallback: try legacy queries.
            return await _legacy_k8s_health(
                resolved_ns, resolved_svc, service_name, namespace,
                since_minutes, was_fuzzy_ns, was_fuzzy_svc,
                credentials, intelligence, start,
            )

        # Build K8s-only discovery result.
        k8s_discovery = DiscoveryResult(
            available=k8s_available,
            unavailable=[k for k in discovery.unavailable if EVENT_REGISTRY.get(k, None) and EVENT_REGISTRY[k].domain == "k8s"],
            domains_with_data=["k8s"],
            service_filter_map={k: v for k, v in discovery.service_filter_map.items() if k in k8s_available},
            discovery_duration_ms=discovery.discovery_duration_ms,
            total_event_types_checked=discovery.total_event_types_checked,
        )

        # Build queries.
        queries = build_investigation_queries(
            discovery=k8s_discovery,
            anchor=anchor,
            namespace=resolved_ns,
            naming_convention=intelligence.naming_convention,
        )

        # Run ALL queries in parallel.
        async def _run_query(q):
            try:
                escaped_nrql = q.nrql.replace('"', '\\"')
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
                return (
                    body.get("data", {})
                    .get("actor", {})
                    .get("account", {})
                    .get("nrql", {})
                    .get("results", [])
                )
            except Exception:
                return []

        results = await asyncio.gather(
            *[_run_query(q) for q in queries],
            return_exceptions=True,
        )

        # Derive health signals.
        signals: list[str] = []
        raw_results: dict = {}

        for query, result in zip(queries, results):
            if isinstance(result, Exception):
                raw_results[query.signal] = []
                continue
            raw_results[query.signal] = result
            health_check = get_health_check(query.signal)
            query_signals = health_check(result)
            signals.extend(query_signals)

        duration_ms = int((time.time() - start) * 1000)
        response: dict = {
            "namespace": resolved_ns,
            "service_name": resolved_svc,
            "since_minutes": since_minutes,
            "health_signals": signals,
            "discovered_event_types": list(k8s_available.keys()),
            "queries_run": len(queries),
            "raw_data": raw_results,
            "duration_ms": duration_ms,
        }

        # Deep links — only when health_signals is non-empty.
        if signals:
            try:
                _builder = _get_deeplink_builder()
                if _builder and resolved_ns:
                    _bare_svc = resolved_svc or resolved_ns
                    nc = intelligence.naming_convention
                    if nc and getattr(nc, "separator", None) and resolved_svc:
                        sep = nc.separator
                        if sep in _bare_svc:
                            if getattr(nc, "k8s_deployment_name_format", "full") == "bare":
                                if getattr(nc, "env_position", None) == "prefix":
                                    _bare_svc = _bare_svc.split(sep, 1)[1]
                                elif getattr(nc, "env_position", None) == "suffix":
                                    _bare_svc = _bare_svc.rsplit(sep, 1)[0]
                    _restart_nrql = (
                        f"SELECT sum(restartCount) as restarts "
                        f"FROM K8sPodSample "
                        f"WHERE namespaceName = '{resolved_ns}' "
                        f"AND deploymentName LIKE '%{_bare_svc}%' "
                        f"TIMESERIES 5 minutes "
                        f"SINCE {since_minutes} minutes ago"
                    )
                    response["links"] = {
                        "k8s_explorer": _builder.k8s_explorer(resolved_ns),
                        "workload_view": _builder.k8s_workload(resolved_ns, _bare_svc),
                        "restart_chart": _builder.nrql_chart(_restart_nrql, since_minutes),
                    }
            except Exception:
                pass

        if was_fuzzy_ns:
            response["namespace_resolved_from"] = namespace
        if was_fuzzy_svc and service_name:
            response["service_resolved_from"] = service_name

        return json.dumps(response)

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_k8s_health",
            "hint": "Check namespace and service name.",
            "data_available": False,
        })


async def _legacy_k8s_health(
    resolved_ns: str,
    resolved_svc: str | None,
    service_name: str | None,
    namespace: str | None,
    since_minutes: int,
    was_fuzzy_ns: bool,
    was_fuzzy_svc: bool,
    credentials,
    intelligence,
    start_time: float,
) -> str:
    """Fallback to legacy hardcoded K8s queries when discovery finds nothing."""
    client = get_client()

    svc_filter = ""
    if resolved_svc:
        # Use bare service name if naming convention says K8s uses bare names.
        svc_for_filter = resolved_svc
        nc = intelligence.naming_convention
        if nc.k8s_deployment_name_format == "bare" and nc.separator:
            sep = nc.separator
            if sep in resolved_svc:
                if nc.env_position == "prefix":
                    svc_for_filter = resolved_svc.split(sep, 1)[1]
                elif nc.env_position == "suffix":
                    svc_for_filter = resolved_svc.rsplit(sep, 1)[0]
        svc_filter = f"AND deploymentName LIKE '%%{svc_for_filter}%%' "

    async def _nrql(nrql_str: str) -> list:
        escaped = nrql_str.replace('"', '\\"')
        query = GQL_NRQL_QUERY % (credentials.account_id, escaped)
        result = await client.query(query, timeout_override=20)
        return (
            result.get("data", {})
            .get("actor", {})
            .get("account", {})
            .get("nrql", {})
            .get("results", [])
        )

    pods_task = _nrql(NRQL_POD_STATUS % (resolved_ns, svc_filter, since_minutes))
    restarts_task = _nrql(NRQL_CONTAINER_RESTARTS % (resolved_ns, svc_filter, since_minutes))
    resources_task = _nrql(NRQL_NODE_HEALTH % (resolved_ns, svc_filter, since_minutes))
    deployments_task = _nrql(NRQL_DEPLOYMENT_STATUS % (resolved_ns, svc_filter, since_minutes))

    pods, restarts, resources, deployments = await asyncio.gather(
        pods_task, restarts_task, resources_task, deployments_task,
        return_exceptions=True,
    )

    pods = pods if not isinstance(pods, BaseException) else []
    restarts = restarts if not isinstance(restarts, BaseException) else []
    resources = resources if not isinstance(resources, BaseException) else []
    deployments = deployments if not isinstance(deployments, BaseException) else []

    signals: list[str] = []
    crashing_pods = [p for p in pods if p.get("latest.status") == "Failed"]
    not_ready_pods = [p for p in pods if p.get("latest.isReady") is False]
    restarting = [r for r in restarts if (r.get("restarts") or 0) > 5]

    if crashing_pods:
        signals.append(f"🔴 {len(crashing_pods)} pod(s) in Failed state")
    if not_ready_pods:
        signals.append(f"⚠️ {len(not_ready_pods)} pod(s) not ready")
    if restarting:
        signals.append(f"⚠️ {len(restarting)} container(s) restarting frequently")

    for dep in deployments:
        desired = dep.get("latest.podsDesired", 0) or 0
        available = dep.get("latest.podsAvailable", 0) or 0
        if desired > 0 and available < desired:
            dep_name = dep.get("deploymentName", dep.get("facet", "unknown"))
            signals.append(f"⚠️ Deployment {dep_name}: {available}/{desired} pods available")

    duration_ms = int((time.time() - start_time) * 1000)
    response: dict = {
        "namespace": resolved_ns,
        "service_name": resolved_svc,
        "since_minutes": since_minutes,
        "health_signals": signals,
        "pods": pods,
        "container_restarts": restarts,
        "resource_usage": resources,
        "deployments": deployments,
        "duration_ms": duration_ms,
    }

    # Deep links — only when health_signals is non-empty.
    if signals:
        try:
            _builder = _get_deeplink_builder()
            if _builder and resolved_ns:
                _bare_svc = resolved_svc or resolved_ns
                nc = intelligence.naming_convention
                if nc and getattr(nc, "separator", None) and resolved_svc:
                    sep = nc.separator
                    if sep in _bare_svc:
                        if getattr(nc, "k8s_deployment_name_format", "full") == "bare":
                            if getattr(nc, "env_position", None) == "prefix":
                                _bare_svc = _bare_svc.split(sep, 1)[1]
                            elif getattr(nc, "env_position", None) == "suffix":
                                _bare_svc = _bare_svc.rsplit(sep, 1)[0]
                _restart_nrql = (
                    f"SELECT sum(restartCount) as restarts "
                    f"FROM K8sPodSample "
                    f"WHERE namespaceName = '{resolved_ns}' "
                    f"AND deploymentName LIKE '%{_bare_svc}%' "
                    f"TIMESERIES 5 minutes "
                    f"SINCE {since_minutes} minutes ago"
                )
                response["links"] = {
                    "k8s_explorer": _builder.k8s_explorer(resolved_ns),
                    "workload_view": _builder.k8s_workload(resolved_ns, _bare_svc),
                    "restart_chart": _builder.nrql_chart(_restart_nrql, since_minutes),
                }
        except Exception:
            pass

    if was_fuzzy_ns:
        response["namespace_resolved_from"] = namespace
    if was_fuzzy_svc and service_name:
        response["service_resolved_from"] = service_name

    return json.dumps(response)

