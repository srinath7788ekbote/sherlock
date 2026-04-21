"""
Kubernetes health tool for Sherlock.

Provides K8s cluster, namespace, and pod-level health data
using direct NRQL queries against K8s integration event types.
"""

import asyncio
import json
import logging
import time

from client.newrelic import get_client
from core.context import AccountContext
from core.deeplinks import get_builder as _get_deeplink_builder
from core.sanitize import fuzzy_resolve_service, sanitize_service_name

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

# NRQL queries for K8s health.
# Each template has 5 format slots:
#   1. namespaceName  2. svc_filter  3. cluster_filter  4. since_minutes  5. facet_prefix
NRQL_POD_STATUS = (
    "SELECT latest(status), latest(isReady), latest(nodeName), "
    "latest(reason), latest(message) "
    "FROM K8sPodSample WHERE namespaceName = '%s' "
    "%s"
    "%s"
    "SINCE %d minutes ago FACET %spodName LIMIT 100"
)

NRQL_CONTAINER_RESTARTS = (
    "SELECT sum(restartCountDelta) as restarts "
    "FROM K8sContainerSample WHERE namespaceName = '%s' "
    "%s"
    "%s"
    "SINCE %d minutes ago FACET %scontainerName, podName LIMIT 50"
)

NRQL_NODE_HEALTH = (
    "SELECT latest(cpuUsedCoreMilliseconds/cpuLimitCoreMilliseconds * 100) as cpu_pct, "
    "latest(memoryWorkingSetBytes/memoryLimitBytes * 100) as memory_pct "
    "FROM K8sContainerSample WHERE namespaceName = '%s' "
    "%s"
    "%s"
    "SINCE %d minutes ago FACET %spodName LIMIT 50"
)

NRQL_DEPLOYMENT_STATUS = (
    "SELECT latest(podsAvailable), latest(podsDesired), "
    "latest(podsUnavailable), latest(podsReady) "
    "FROM K8sDeploymentSample WHERE namespaceName = '%s' "
    "%s"
    "%s"
    "SINCE %d minutes ago FACET %sdeploymentName LIMIT 50"
)


def _resolve_cluster_mode(
    intelligence,
    cluster_name: str | None,
) -> tuple[str, str, str, str]:
    """Determine cluster filter clause, facet prefix, resolved cluster, and mode label.

    Returns:
        (cluster_filter, facet_prefix, resolved_cluster, mode_label)
        - cluster_filter: NRQL fragment, empty or "AND clusterName = 'X' "
        - facet_prefix: "" or "clusterName, " (only non-empty in breakdown mode)
        - resolved_cluster: the actual cluster name used, "" if none, "<breakdown>" if breakdown
        - mode_label: "none" | "single" | "explicit" | "breakdown"
    """
    clusters = getattr(intelligence, "k8s", None)
    cluster_list = getattr(clusters, "cluster_names", None) or [] if clusters else []
    n = len(cluster_list)

    # Mode: none (0 clusters known)
    if n == 0:
        return "", "", "", "none"

    # Mode: explicit cluster_name on any account
    if cluster_name:
        safe = sanitize_service_name(cluster_name)
        try:
            resolved, _was_fuzzy, _ = fuzzy_resolve_service(
                safe, cluster_list, threshold=0.5,
            )
        except Exception:
            resolved = safe
        return f"AND clusterName = '{resolved}' ", "", resolved, "explicit"

    # Mode: single (exactly 1 cluster, no explicit override)
    if n == 1:
        return f"AND clusterName = '{cluster_list[0]}' ", "", cluster_list[0], "single"

    # Mode: breakdown (2+ clusters, no explicit cluster_name)
    return "", "clusterName, ", "<breakdown>", "breakdown"


async def get_k8s_health(
    service_name: str | None = None,
    namespace: str | None = None,
    since_minutes: int = 30,
    cluster_name: str | None = None,
) -> str:
    """Get Kubernetes health data for a service or namespace.

    Runs direct NRQL queries against K8s event types for pod status,
    container restarts, resource usage, and deployment health.

    Args:
        service_name: Optional service name to filter (fuzzy resolved).
        namespace: Optional K8s namespace to scope the query.
        since_minutes: Time window in minutes.
        cluster_name: Optional K8s cluster to scope. Behavior:
            - If account has 0 known clusters: parameter ignored.
            - If account has 1 known cluster: auto-filtered to that cluster.
            - If account has 2+ clusters AND cluster_name provided:
              query is filtered to that cluster (fuzzy-resolved).
            - If account has 2+ clusters AND cluster_name omitted:
              response includes per-cluster breakdown facet.

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

        # Run direct NRQL queries for K8s health.
        return await _k8s_health_queries(
                resolved_ns, resolved_svc, service_name, namespace,
                since_minutes, was_fuzzy_ns, was_fuzzy_svc,
                credentials, intelligence, start, cluster_name,
            )

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_k8s_health",
            "hint": "Check namespace and service name.",
            "data_available": False,
        })


async def _k8s_health_queries(
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
    cluster_name: str | None = None,
) -> str:
    """Run direct NRQL queries for K8s pod, container, node, and deployment health."""
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

    cluster_filter, facet_prefix, resolved_cluster, cluster_mode = _resolve_cluster_mode(
        intelligence, cluster_name,
    )

    pods_task = _nrql(NRQL_POD_STATUS % (resolved_ns, svc_filter, cluster_filter, since_minutes, facet_prefix))
    restarts_task = _nrql(NRQL_CONTAINER_RESTARTS % (resolved_ns, svc_filter, cluster_filter, since_minutes, facet_prefix))
    resources_task = _nrql(NRQL_NODE_HEALTH % (resolved_ns, svc_filter, cluster_filter, since_minutes, facet_prefix))
    deployments_task = _nrql(NRQL_DEPLOYMENT_STATUS % (resolved_ns, svc_filter, cluster_filter, since_minutes, facet_prefix))

    pods, restarts, resources, deployments = await asyncio.gather(
        pods_task, restarts_task, resources_task, deployments_task,
        return_exceptions=True,
    )

    pods = pods if not isinstance(pods, BaseException) else []
    restarts = restarts if not isinstance(restarts, BaseException) else []
    resources = resources if not isinstance(resources, BaseException) else []
    deployments = deployments if not isinstance(deployments, BaseException) else []

    signals: list[str] = []
    is_breakdown = bool(facet_prefix)

    def _cluster_tag(row: dict) -> str:
        """Return '[cluster] ' prefix when in breakdown mode."""
        if not is_breakdown:
            return ""
        c = row.get("clusterName") or "?"
        return f"[{c}] "

    crashing_pods = [p for p in pods if p.get("latest.status") == "Failed"]
    not_ready_pods = [p for p in pods if p.get("latest.isReady") is False]
    restarting = [r for r in restarts if (r.get("restarts") or 0) > 5]

    if is_breakdown:
        # In breakdown mode, group signals per cluster to prevent conflation.
        for p in crashing_pods:
            signals.append(f"🔴 {_cluster_tag(p)}Pod {p.get('podName', '?')} in Failed state")
        for p in not_ready_pods:
            signals.append(f"⚠️ {_cluster_tag(p)}Pod {p.get('podName', '?')} not ready")
        for r in restarting:
            signals.append(f"⚠️ {_cluster_tag(r)}Container {r.get('containerName', '?')} restarting frequently")
    else:
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
            signals.append(f"⚠️ {_cluster_tag(dep)}Deployment {dep_name}: {available}/{desired} pods available")

    duration_ms = int((time.time() - start_time) * 1000)
    clusters_known = []
    k8s_intel = getattr(intelligence, "k8s", None)
    if k8s_intel:
        clusters_known = list(getattr(k8s_intel, "cluster_names", None) or [])
    response: dict = {
        "namespace": resolved_ns,
        "service_name": resolved_svc,
        "cluster_mode": cluster_mode,
        "cluster_name": resolved_cluster if resolved_cluster != "<breakdown>" else None,
        "clusters_known": clusters_known,
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

                if is_breakdown:
                    # Breakdown mode: per-cluster links.
                    clusters_in_data = sorted({
                        row.get("clusterName")
                        for row in list(pods) + list(deployments)
                        if row.get("clusterName")
                    })
                    links_by_cluster: dict = {}
                    for cluster in clusters_in_data:
                        links_by_cluster[cluster] = {
                            "k8s_explorer": _builder.k8s_explorer(resolved_ns, cluster=cluster),
                            "workload_view": _builder.k8s_workload(resolved_ns, _bare_svc, cluster=cluster),
                        }
                    response["links_by_cluster"] = links_by_cluster
                else:
                    # Single/explicit/none mode: single link set, cluster-scoped when known.
                    cluster_kw = {"cluster": resolved_cluster} if resolved_cluster else {}
                    response["links"] = {
                        "k8s_explorer": _builder.k8s_explorer(resolved_ns, **cluster_kw),
                        "workload_view": _builder.k8s_workload(resolved_ns, _bare_svc, **cluster_kw),
                        "restart_chart": _builder.nrql_chart(_restart_nrql, since_minutes),
                    }
        except Exception:
            pass

    if was_fuzzy_ns:
        response["namespace_resolved_from"] = namespace
    if was_fuzzy_svc and service_name:
        response["service_resolved_from"] = service_name

    return json.dumps(response)

