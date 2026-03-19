"""
Adaptive query builder for Sherlock.

DEPRECATED: This module is part of the monolith investigation architecture.
The agent-team architecture uses direct NRQL queries in each domain tool
instead. Kept for backward compatibility with tools/investigate.py (also
deprecated). Do not add new code here.

Original purpose: Takes a DiscoveryResult and builds investigation queries
for what was actually found.
"""

import logging
from typing import Any, Callable

from pydantic import BaseModel, Field

from core.discovery import DiscoveryResult, EVENT_REGISTRY

logger = logging.getLogger("sherlock.query_builder")


# ── Pydantic Models ─────────────────────────────────────────────────────


class SignalQuery(BaseModel):
    """A signal query template with health check function."""

    nrql: str
    description: str = ""

    class Config:
        arbitrary_types_allowed = True


class InvestigationQuery(BaseModel):
    """A fully substituted query ready to execute."""

    signal: str
    event_type: str
    domain: str
    nrql: str

    class Config:
        arbitrary_types_allowed = True


# ── Health Check Functions ───────────────────────────────────────────────


def _check_pod_status(results: list[dict]) -> list[str]:
    """Analyze pod status results for issues."""
    findings = []
    if not results or not isinstance(results, list):
        return findings
    for row in results:
        if not isinstance(row, dict):
            continue
        status = row.get("status", row.get("latest.status", ""))
        pod = row.get("podName", row.get("facet", "unknown"))
        restarts = row.get("current_restarts", row.get("max_restarts", 0)) or 0
        ready = row.get("ready", row.get("latest.isReady", True))

        if status in ("Failed", "CrashLoopBackOff", "Error", "Unknown"):
            findings.append(f"🔴 Pod {pod} in {status} state")
        elif not ready and ready is not None:
            findings.append(f"⚠️ Pod {pod} not ready")
        if restarts > 5:
            findings.append(f"⚠️ Pod {pod} has {restarts} restarts")
    return findings


def _check_replica_health(results: list[dict]) -> list[str]:
    """Analyze deployment replica health."""
    findings = []
    if not results or not isinstance(results, list):
        return findings
    for row in results:
        if not isinstance(row, dict):
            continue
        desired = row.get("current_desired", row.get("max_desired", 0)) or 0
        ready = row.get("current_ready", row.get("min_ready", 0)) or 0
        dep = row.get("deploymentName", row.get("facet", "unknown"))
        unavailable = row.get("unavailable", 0) or 0

        if desired > 0 and ready < desired:
            findings.append(
                f"🔴 Deployment {dep}: {ready}/{desired} replicas ready "
                f"({unavailable} unavailable)"
            )
    return findings


def _check_hpa_scaling(results: list[dict]) -> list[str]:
    """Analyze HPA scaling events."""
    findings = []
    if not results or not isinstance(results, list):
        return findings
    for row in results:
        if not isinstance(row, dict):
            continue
        current = row.get("current", 0) or 0
        desired = row.get("desired", 0) or 0
        hpa_max = row.get("hpa_max", 0) or 0
        hpa_name = row.get("horizontalPodAutoscalerName", row.get("facet", "unknown"))

        if current >= hpa_max and hpa_max > 0:
            findings.append(
                f"🔴 HPA {hpa_name} at max capacity: {current}/{hpa_max} replicas"
            )
        elif current != desired and desired > 0:
            findings.append(
                f"⚠️ HPA {hpa_name} scaling: current={current}, desired={desired}"
            )
    return findings


def _check_oom(results: list[dict]) -> list[str]:
    """Analyze OOMKill events."""
    findings = []
    if not results or not isinstance(results, list):
        return findings
    for row in results:
        if not isinstance(row, dict):
            continue
        count = row.get("oom_count", row.get("count", 0)) or 0
        pod = row.get("podName", row.get("latest.podName", "unknown"))
        memory_mb = row.get("memory_mb", 0) or 0
        limit_mb = row.get("limit_mb", 0) or 0

        if count > 0:
            msg = f"🔴 OOMKilled: {pod} ({count}x)"
            if memory_mb and limit_mb:
                msg += f" — used {memory_mb:.0f}MB / {limit_mb:.0f}MB limit"
            findings.append(msg)
    return findings


def _check_resources(results: list[dict]) -> list[str]:
    """Analyze resource usage vs limits."""
    findings = []
    if not results or not isinstance(results, list):
        return findings
    for row in results:
        if not isinstance(row, dict):
            continue
        avg_cpu = row.get("avg_cpu", 0) or 0
        peak_cpu = row.get("peak_cpu", 0) or 0
        cpu_limit = row.get("cpu_limit", 0) or 0
        avg_mem = row.get("avg_mem_mb", 0) or 0
        peak_mem = row.get("peak_mem_mb", 0) or 0
        mem_limit = row.get("mem_limit_mb", 0) or 0
        pct_near = row.get("pct_near_mem_limit", 0) or 0

        if cpu_limit and peak_cpu > cpu_limit * 0.9:
            findings.append(
                f"🔴 CPU near limit: peak {peak_cpu:.2f} / limit {cpu_limit:.2f} cores"
            )
        elif cpu_limit and avg_cpu > cpu_limit * 0.7:
            findings.append(
                f"⚠️ CPU elevated: avg {avg_cpu:.2f} / limit {cpu_limit:.2f} cores"
            )

        if mem_limit and peak_mem > mem_limit * 0.9:
            findings.append(
                f"🔴 Memory near limit: peak {peak_mem:.0f}MB / limit {mem_limit:.0f}MB"
            )
        elif pct_near > 50:
            findings.append(
                f"⚠️ {pct_near:.0f}% of samples near memory limit"
            )
    return findings


def _check_k8s_events(results: list[dict]) -> list[str]:
    """Analyze Kubernetes infrastructure events."""
    findings = []
    if not results or not isinstance(results, list):
        return findings
    critical_reasons = {"OOMKilling", "BackOff", "Failed", "Evicted", "Killing", "Unhealthy"}
    for row in results:
        if not isinstance(row, dict):
            continue
        reason = row.get("reason", row.get("latest.reason", ""))
        message = row.get("message", row.get("latest.message", ""))
        count = row.get("occurrences", row.get("count", 1)) or 1
        obj = row.get("involvedObjectName", row.get("facet", ""))

        if reason in critical_reasons:
            findings.append(f"🔴 K8s event: {reason} on {obj} ({count}x) — {message[:120]}")
        elif reason:
            findings.append(f"⚠️ K8s event: {reason} on {obj} ({count}x)")
    return findings


def _check_error_rate(results: list[dict]) -> list[str]:
    """Analyze APM error rate and latency."""
    findings = []
    if not results or not isinstance(results, list):
        return findings
    for row in results:
        if not isinstance(row, dict):
            continue
        error_rate = row.get("error_rate", 0) or 0
        throughput = row.get("throughput", 0) or 0
        p95 = row.get("p95_latency", 0) or 0
        p99 = row.get("p99_latency", 0) or 0
        peak_error = row.get("peak_error_rate", 0) or 0
        min_throughput = row.get("min_throughput", 0)

        if error_rate >= 20:
            findings.append(f"🔴 CRITICAL error rate: {error_rate:.1f}% (peak: {peak_error:.1f}%)")
        elif error_rate >= 5:
            findings.append(f"⚠️ Elevated error rate: {error_rate:.1f}%")

        if throughput == 0:
            findings.append("🔴 ZERO throughput — service may be down")

        if p99 >= 5.0:
            findings.append(f"⚠️ High P99 latency: {p99:.2f}s")
        elif p95 >= 3.0:
            findings.append(f"⚠️ High P95 latency: {p95:.2f}s")
    return findings


def _check_error_classes(results: list[dict]) -> list[str]:
    """Analyze error classes and messages."""
    findings = []
    if not results or not isinstance(results, list):
        return findings
    for row in results:
        if not isinstance(row, dict):
            continue
        error_class = row.get("errorClass", row.get("facet", "Unknown"))
        count = row.get("count", 0) or 0
        sample_msg = row.get("sample_message", "")

        if count > 0:
            msg = f"Error: {error_class} ({count}x)"
            if sample_msg:
                msg += f" — {sample_msg[:80]}"
            findings.append(msg)
    return findings


def _check_slow_queries(results: list[dict]) -> list[str]:
    """Analyze slow database queries."""
    findings = []
    if not results or not isinstance(results, list):
        return findings
    for row in results:
        if not isinstance(row, dict):
            continue
        avg_dur = row.get("avg_duration", 0) or 0
        max_dur = row.get("max_duration", 0) or 0
        table = row.get("table", row.get("facet", "unknown"))
        operation = row.get("operationName", "")

        if max_dur > 5.0:
            findings.append(f"🔴 Slow DB query: {operation} on {table} — max {max_dur:.2f}s")
        elif avg_dur > 1.0:
            findings.append(f"⚠️ Slow DB query: {operation} on {table} — avg {avg_dur:.2f}s")
    return findings


def _check_external_calls(results: list[dict]) -> list[str]:
    """Analyze external HTTP/service calls."""
    findings = []
    if not results or not isinstance(results, list):
        return findings
    for row in results:
        if not isinstance(row, dict):
            continue
        url = row.get("http.url", row.get("facet", "unknown"))
        error_rate = row.get("error_rate", 0) or 0
        avg_dur = row.get("avg_duration", 0) or 0
        count = row.get("call_count", 0) or 0

        if error_rate > 10:
            findings.append(f"🔴 External call failing: {url} — {error_rate:.1f}% errors ({count} calls)")
        elif avg_dur > 3.0:
            findings.append(f"⚠️ Slow external call: {url} — avg {avg_dur:.2f}s")
    return findings


def _check_error_logs(results: list[dict]) -> list[str]:
    """Analyze error logs with pattern detection.

    Detects specific failure patterns:
      - Application crashes (fatal errors, panics, unhandled exceptions)
      - Dependency failures (connection refused, unreachable, with URL extraction)
      - Memory pressure (OOM, heap, GC overhead)

    Falls back to generic count only when no specific pattern is matched.
    """
    import re as _re

    findings = []
    if not results or not isinstance(results, list):
        return findings

    count = len(results)
    if count == 0:
        return findings

    CRASH_PATTERNS = [
        "application run failed", "unhandled exception", "fatal error",
        "panic", "process exited", "crashed", "abort", "segfault",
        "application startup failed", "terminated unexpectedly",
    ]

    DEPENDENCY_PATTERNS = [
        "failed to fetch", "io error", "connection refused",
        "connection timeout", "unreachable", "no route to host",
        "failed to connect", "socket", "network error",
        "http error", "service unavailable", "connection reset",
    ]

    MEMORY_PATTERNS = [
        "out of memory", "oom", "memory limit",
        "java.lang.outofmemoryerror", "gc overhead", "heap space",
        "memory allocation failed",
    ]

    _URL_RE = _re.compile(r"https?://([^/:\s]+)(?::\d+)?")
    _HOST_PORT_RE = _re.compile(r"([a-zA-Z0-9][\w.-]+):(\d{2,5})")

    crash_events: list[dict] = []
    dependency_failures: dict[str, list[dict]] = {}
    memory_events: list[dict] = []
    pattern_matched = False

    for row in results:
        if not isinstance(row, dict):
            continue
        msg = str(row.get("message", "")).lower()
        if not msg:
            continue

        # Check crash patterns.
        is_crash = False
        for pattern in CRASH_PATTERNS:
            if pattern in msg:
                crash_events.append(row)
                pattern_matched = True
                is_crash = True
                break
        if is_crash:
            continue

        # Check dependency patterns.
        is_dep = False
        for pattern in DEPENDENCY_PATTERNS:
            if pattern in msg:
                # Extract dependency hostname.
                original_msg = str(row.get("message", ""))
                dep_name = None
                url_match = _URL_RE.search(original_msg)
                if url_match:
                    dep_name = url_match.group(1)
                else:
                    host_match = _HOST_PORT_RE.search(original_msg)
                    if host_match:
                        dep_name = host_match.group(1)

                if not dep_name:
                    dep_name = "unknown-dependency"

                dependency_failures.setdefault(dep_name, []).append(row)
                pattern_matched = True
                is_dep = True
                break
        if is_dep:
            continue

        # Check memory patterns.
        for pattern in MEMORY_PATTERNS:
            if pattern in msg:
                memory_events.append(row)
                pattern_matched = True
                break

    # ── Generate findings from detected patterns ──

    if crash_events:
        pod_names: set[str] = set()
        for ev in crash_events:
            if not isinstance(ev, dict):
                continue
            pod = ev.get("hostname", ev.get("podName", ev.get("host", "")))
            if pod:
                pod_names.add(str(pod))
        pod_str = ", ".join(sorted(pod_names)[:5]) if pod_names else "unknown"
        findings.append(
            f"🔴 APPLICATION CRASHES: {len(crash_events)} crash event(s) "
            f"on pods: {pod_str}"
        )

    for dep_name, dep_events in dependency_failures.items():
        sample_msg = str(dep_events[0].get("message", ""))[:200] if dep_events else ""
        url_match = _URL_RE.search(sample_msg)
        sample_url = url_match.group(0) if url_match else dep_name
        findings.append(
            f"🔴 DEPENDENCY FAILURE: {dep_name} unreachable "
            f"— {len(dep_events)} error(s). Sample: {sample_url}"
        )

    if memory_events:
        findings.append(
            f"⚠️ MEMORY PRESSURE: {len(memory_events)} memory-related "
            f"error(s) detected in logs"
        )

    # Only show generic count if no specific pattern matched.
    if not pattern_matched:
        if count > 50:
            findings.append(f"🔴 {count} error/critical log entries in window")
        elif count > 10:
            findings.append(f"⚠️ {count} error/critical log entries in window")
        elif count > 0:
            findings.append(f"ℹ️ {count} error/critical log entries in window")

        # Extract top patterns for generic case.
        patterns: dict[str, int] = {}
        for row in results[:50]:
            if not isinstance(row, dict):
                continue
            msg = str(row.get("message", ""))[:100]
            if msg:
                patterns[msg] = patterns.get(msg, 0) + 1

        if patterns:
            top_msg = max(patterns, key=patterns.get)  # type: ignore[arg-type]
            top_count = patterns[top_msg]
            if top_count > 3:
                findings.append(f"Repeated log pattern ({top_count}x): {top_msg}")

    return findings


def _check_synthetic_pass_rate(results: list[dict]) -> list[str]:
    """Analyze synthetic monitor pass rate."""
    findings = []
    if not results or not isinstance(results, list):
        return findings
    for row in results:
        if not isinstance(row, dict):
            continue
        pass_rate = row.get("pass_rate", 100) or 100
        total_runs = row.get("total_runs", 0) or 0

        if total_runs > 0:
            if pass_rate < 50:
                findings.append(f"🔴 Synthetic monitor failing: {pass_rate:.1f}% pass rate ({total_runs} runs)")
            elif pass_rate < 90:
                findings.append(f"⚠️ Synthetic monitor degraded: {pass_rate:.1f}% pass rate")
    return findings


def _check_queue_depth(results: list[dict]) -> list[str]:
    """Analyze queue depth and message age."""
    findings = []
    if not results or not isinstance(results, list):
        return findings
    for row in results:
        if not isinstance(row, dict):
            continue
        depth = row.get("queue_depth", 0) or 0
        age = row.get("oldest_message_age_sec", 0) or 0
        entity = row.get("entityName", row.get("facet", "unknown"))

        if depth > 10000:
            findings.append(f"🔴 Queue backlog: {entity} has {depth} messages")
        elif depth > 1000:
            findings.append(f"⚠️ Queue growing: {entity} has {depth} messages")

        if age > 3600:
            findings.append(f"🔴 Stale messages: {entity} oldest message is {age/3600:.1f}h old")
        elif age > 600:
            findings.append(f"⚠️ Queue delay: {entity} oldest message is {age/60:.0f}min old")
    return findings


def _spike_analysis(results: list[dict], metric_name: str) -> list[str]:
    """Analyze timeseries data for spikes."""
    findings = []
    if not results or not isinstance(results, list) or len(results) < 3:
        return findings

    values = []
    for row in results:
        if not isinstance(row, dict):
            continue
        val = row.get(metric_name, row.get("error_rate", row.get("restarts", 0))) or 0
        values.append(float(val))

    if not values:
        return findings

    avg = sum(values) / len(values)
    peak = max(values)

    if avg > 0 and peak > avg * 3:
        findings.append(
            f"⚠️ Spike detected in {metric_name}: peak {peak:.2f} vs avg {avg:.2f} "
            f"({peak/avg:.1f}x spike)"
        )

    return findings


# ── Health Check Registry ────────────────────────────────────────────────

HEALTH_CHECKS: dict[str, Callable[[list[dict]], list[str]]] = {
    "pod_status": _check_pod_status,
    "restarts": _check_pod_status,
    "replica_health": _check_replica_health,
    "replicaset_health": _check_replica_health,
    "hpa_scaling": _check_hpa_scaling,
    "oom_kills": _check_oom,
    "resource_usage": _check_resources,
    "resource_limits": _check_resources,
    "throttling": _check_resources,
    "k8s_events": _check_k8s_events,
    "warnings": _check_k8s_events,
    "errors": _check_k8s_events,
    "node_pressure": _check_resources,
    "node_capacity": _check_resources,
    "disk_pressure": _check_resources,
    "statefulset_health": _check_replica_health,
    "daemonset_health": _check_replica_health,
    "job_completion": _check_pod_status,
    "error_rate": _check_error_rate,
    "latency": _check_error_rate,
    "throughput": _check_error_rate,
    "apdex": _check_error_rate,
    "error_classes": _check_error_classes,
    "error_messages": _check_error_classes,
    "distributed_traces": _check_external_calls,
    "external_calls": _check_external_calls,
    "slow_spans": _check_external_calls,
    "db_performance": _check_slow_queries,
    "slow_queries": _check_slow_queries,
    "stack_traces": _check_error_classes,
    "error_detail": _check_error_classes,
    "error_logs": _check_error_logs,
    "warn_logs": _check_error_logs,
    "log_patterns": _check_error_logs,
    "cpu_usage": _check_resources,
    "memory_usage": _check_resources,
    "disk_usage": _check_resources,
    "network_errors": _check_external_calls,
    "bandwidth": _check_external_calls,
    "process_cpu": _check_resources,
    "process_memory": _check_resources,
    "pass_rate": _check_synthetic_pass_rate,
    "failures": _check_synthetic_pass_rate,
    "location_status": _check_synthetic_pass_rate,
    "failed_requests": _check_synthetic_pass_rate,
    "response_codes": _check_synthetic_pass_rate,
    "page_load_time": _check_error_rate,
    "js_errors": _check_error_classes,
    "user_impact": _check_error_rate,
    "ajax_errors": _check_external_calls,
    "api_call_failures": _check_external_calls,
    "js_error_rate": _check_error_classes,
    "js_error_classes": _check_error_classes,
    "queue_depth": _check_queue_depth,
    "message_age": _check_queue_depth,
    "dlq_count": _check_queue_depth,
    "broker_health": _check_queue_depth,
    "error_rate_timeseries": lambda r: _spike_analysis(r, "error_rate"),
    "restart_timeseries": lambda r: _spike_analysis(r, "restarts"),
}


# ── Signal Query Templates ──────────────────────────────────────────────

SIGNAL_QUERIES: dict[str, SignalQuery] = {
    "pod_status": SignalQuery(
        nrql=(
            "SELECT latest(status) as status, "
            "min(restartCount) as min_restarts, "
            "max(restartCount) as max_restarts, "
            "latest(restartCount) as current_restarts, "
            "latest(isReady) as ready, "
            "latest(namespace) as namespace, "
            "uniqueCount(podName) as total_pods_seen, "
            "latest(nodeName) as node "
            "FROM K8sPodSample "
            "WHERE `{filter_attr}` LIKE '%{service}%' {ns_filter} "
            "FACET podName "
            "SINCE {since} minutes ago {until} "
            "LIMIT 50"
        ),
        description="Pod status and restart counts",
    ),
    "replica_health": SignalQuery(
        nrql=(
            "SELECT min(podsDesired) as min_desired, "
            "max(podsDesired) as max_desired, "
            "min(podsReady) as min_ready, "
            "max(podsReady) as max_ready, "
            "latest(podsDesired) as current_desired, "
            "latest(podsReady) as current_ready, "
            "latest(podsUnavailable) as unavailable "
            "FROM K8sDeploymentSample "
            "WHERE `{filter_attr}` LIKE '%{service}%' {ns_filter} "
            "FACET deploymentName "
            "SINCE {since} minutes ago {until}"
        ),
        description="Deployment scaling and availability",
    ),
    "replicaset_health": SignalQuery(
        nrql=(
            "SELECT latest(podsDesired) as desired, "
            "latest(podsReady) as ready, "
            "latest(podsUnavailable) as unavailable "
            "FROM K8sReplicaSetSample "
            "WHERE `{filter_attr}` LIKE '%{service}%' {ns_filter} "
            "FACET replicaSetName "
            "SINCE {since} minutes ago {until}"
        ),
        description="ReplicaSet health",
    ),
    "hpa_scaling": SignalQuery(
        nrql=(
            "SELECT latest(currentReplicas) as current, "
            "latest(desiredReplicas) as desired, "
            "min(currentReplicas) as min_replicas, "
            "max(currentReplicas) as max_replicas, "
            "latest(minReplicas) as hpa_min, "
            "latest(maxReplicas) as hpa_max "
            "FROM K8sHpaSample "
            "WHERE `{filter_attr}` LIKE '%{service}%' {ns_filter} "
            "FACET horizontalPodAutoscalerName "
            "SINCE {since} minutes ago {until}"
        ),
        description="HPA scaling events",
    ),
    "oom_kills": SignalQuery(
        nrql=(
            "SELECT count(*) as oom_count, "
            "latest(podName), "
            "latest(containerName), "
            "latest(memoryUsedBytes)/1e6 as memory_mb, "
            "latest(memoryLimitBytes)/1e6 as limit_mb "
            "FROM K8sContainerSample "
            "WHERE `{filter_attr}` LIKE '%{service}%' {ns_filter} "
            "AND reason = 'OOMKilled' "
            "FACET podName, containerName "
            "SINCE {since} minutes ago {until} "
            "LIMIT 20"
        ),
        description="OOMKill detection",
    ),
    "resource_usage": SignalQuery(
        nrql=(
            "SELECT average(cpuUsedCores) as avg_cpu, "
            "max(cpuUsedCores) as peak_cpu, "
            "latest(cpuLimitCores) as cpu_limit, "
            "average(memoryUsedBytes)/1e6 as avg_mem_mb, "
            "max(memoryUsedBytes)/1e6 as peak_mem_mb, "
            "latest(memoryLimitBytes)/1e6 as mem_limit_mb, "
            "percentage(count(*), WHERE memoryUsedBytes > memoryLimitBytes * 0.9) as pct_near_mem_limit "
            "FROM K8sContainerSample "
            "WHERE `{filter_attr}` LIKE '%{service}%' {ns_filter} "
            "SINCE {since} minutes ago {until}"
        ),
        description="CPU and memory usage vs limits",
    ),
    "k8s_events": SignalQuery(
        nrql=(
            "SELECT latest(reason) as reason, "
            "latest(message) as message, "
            "count(*) as occurrences "
            "FROM InfrastructureEvent "
            "WHERE category = 'kubernetes' "
            "AND (involvedObjectName LIKE '%{service}%' "
            "OR reason IN ('OOMKilling','BackOff','Failed','Evicted','Killing','Unhealthy')) "
            "{ns_event_filter} "
            "FACET reason, involvedObjectName "
            "SINCE {since} minutes ago {until} "
            "LIMIT 25"
        ),
        description="Kubernetes warning events",
    ),
    "error_rate": SignalQuery(
        nrql=(
            "SELECT percentage(count(*), WHERE error IS true) as error_rate, "
            "rate(count(*), 1 minute) as throughput, "
            "average(duration) as avg_latency, "
            "percentile(duration, 95) as p95_latency, "
            "percentile(duration, 99) as p99_latency, "
            "apdex(duration, 0.5) as apdex, "
            "max(percentage(count(*), WHERE error IS true)) as peak_error_rate, "
            "min(rate(count(*), 1 minute)) as min_throughput "
            "FROM Transaction "
            "WHERE `{filter_attr}` LIKE '%{service}%' "
            "SINCE {since} minutes ago {until}"
        ),
        description="APM error rate and latency",
    ),
    "error_classes": SignalQuery(
        nrql=(
            "SELECT count(*) as count, "
            "latest(errorMessage) as sample_message "
            "FROM TransactionError "
            "WHERE `{filter_attr}` LIKE '%{service}%' "
            "FACET errorClass "
            "SINCE {since} minutes ago {until} "
            "LIMIT 10"
        ),
        description="Top error classes and messages",
    ),
    "slow_queries": SignalQuery(
        nrql=(
            "SELECT count(*) as query_count, "
            "average(duration) as avg_duration, "
            "max(duration) as max_duration, "
            "latest(databaseCallCount) as call_count "
            "FROM DatastoreSegment "
            "WHERE `{filter_attr}` LIKE '%{service}%' "
            "FACET datastoreType, `table`, operationName "
            "ORDER BY average(duration) DESC "
            "SINCE {since} minutes ago {until} "
            "LIMIT 10"
        ),
        description="Slow database queries",
    ),
    "external_calls": SignalQuery(
        nrql=(
            "SELECT count(*) as call_count, "
            "average(duration) as avg_duration, "
            "percentage(count(*), WHERE httpResponseCode >= 500) as error_rate "
            "FROM Span "
            "WHERE `{filter_attr}` LIKE '%{service}%' "
            "AND span.kind = 'client' "
            "FACET http.url, db.system "
            "SINCE {since} minutes ago {until} "
            "LIMIT 10"
        ),
        description="External HTTP and service calls",
    ),
    "error_logs": SignalQuery(
        nrql=(
            "SELECT timestamp, level, message, "
            "traceId, hostname "
            "FROM Log "
            "WHERE `{filter_attr}` LIKE '%{service}%' "
            "AND `{severity_attr}` IN ('ERROR','CRITICAL','FATAL','error','critical','fatal') "
            "SINCE {since} minutes ago {until} "
            "ORDER BY timestamp DESC "
            "LIMIT 100"
        ),
        description="Error and critical log lines",
    ),
    "pass_rate": SignalQuery(
        nrql=(
            "SELECT percentage(count(*), WHERE result = 'SUCCESS') as pass_rate, "
            "count(*) as total_runs, "
            "average(duration) as avg_duration_ms "
            "FROM SyntheticCheck "
            "WHERE `{filter_attr}` LIKE '%{service}%' "
            "SINCE {since} minutes ago {until}"
        ),
        description="Synthetic monitor pass rate",
    ),
    "queue_depth": SignalQuery(
        nrql=(
            "SELECT latest(`provider.approximateNumberOfMessages`) as queue_depth, "
            "latest(`provider.approximateAgeOfOldestMessage`) as oldest_message_age_sec, "
            "latest(`provider.numberOfMessagesSent`) as messages_sent, "
            "latest(`provider.numberOfMessagesReceived`) as messages_received "
            "FROM QueueSample "
            "WHERE `{filter_attr}` LIKE '%{service}%' "
            "SINCE {since} minutes ago {until} "
            "FACET entityName"
        ),
        description="Queue depth and message age",
    ),
    "error_rate_timeseries": SignalQuery(
        nrql=(
            "SELECT percentage(count(*), WHERE error IS true) as error_rate "
            "FROM Transaction "
            "WHERE `{filter_attr}` LIKE '%{service}%' "
            "TIMESERIES {timeseries_bucket} minutes "
            "SINCE {since} minutes ago {until}"
        ),
        description="Error rate over time for spike detection",
    ),
    "restart_timeseries": SignalQuery(
        nrql=(
            "SELECT sum(restartCount) as restarts "
            "FROM K8sPodSample "
            "WHERE `{filter_attr}` LIKE '%{service}%' {ns_filter} "
            "TIMESERIES {timeseries_bucket} minutes "
            "SINCE {since} minutes ago {until}"
        ),
        description="Restart count over time",
    ),
    # ── Infrastructure signals ──────────────────────────────────────
    "cpu_usage": SignalQuery(
        nrql=(
            "SELECT average(cpuPercent) as avg_cpu, "
            "max(cpuPercent) as peak_cpu, "
            "average(memoryUsedBytes)/1e6 as avg_mem_mb, "
            "max(memoryUsedBytes)/1e6 as peak_mem_mb, "
            "average(diskUsedPercent) as avg_disk_pct "
            "FROM SystemSample "
            "WHERE `{filter_attr}` LIKE '%{service}%' "
            "SINCE {since} minutes ago {until}"
        ),
        description="Host CPU, memory, and disk usage",
    ),
    # ── Browser signals ─────────────────────────────────────────────
    "page_load_time": SignalQuery(
        nrql=(
            "SELECT average(duration) as avg_page_load, "
            "percentile(duration, 95) as p95_page_load, "
            "count(*) as page_views, "
            "percentage(count(*), WHERE firstContentfulPaint > 3) as slow_fcp_pct "
            "FROM PageView "
            "WHERE `{filter_attr}` LIKE '%{service}%' "
            "SINCE {since} minutes ago {until}"
        ),
        description="Browser page load performance",
    ),
}


# ── Query Builder ────────────────────────────────────────────────────────


def build_investigation_queries(
    discovery: DiscoveryResult,
    anchor: Any,
    namespace: str | None = None,
    severity_attr: str = "level",
    naming_convention: Any = None,
) -> list[InvestigationQuery]:
    """Build investigation queries from discovery results.

    For each available event type, looks up its signals in SIGNAL_QUERIES,
    substitutes template variables, and returns ready-to-run queries.

    No hardcoded query lists. If an event type wasn't discovered to have
    data, no queries are built for it.

    Args:
        discovery: DiscoveryResult from the discovery phase.
        anchor: InvestigationAnchor with since_minutes, until_clause, primary_service.
        namespace: Optional K8s namespace to add as filter.
        severity_attr: Log severity attribute name from intelligence.

    Returns:
        List of InvestigationQuery objects ready to execute.
    """
    queries: list[InvestigationQuery] = []
    seen_signals: set[str] = set()

    ns_filter = f"AND namespaceName = '{namespace}'" if namespace else ""
    ns_event_filter = (
        f"AND involvedObjectNamespace = '{namespace}'" if namespace else ""
    )

    # Auto-scale TIMESERIES bucket to stay within NRQL 366-bucket limit.
    _NRQL_MAX_BUCKETS = 366
    since_min = getattr(anchor, "since_minutes", 30) or 30
    timeseries_bucket = max(5, -(-since_min // _NRQL_MAX_BUCKETS))  # ceil division

    for event_type_name, available in discovery.available.items():
        # Get the event type info from the registry.
        info = EVENT_REGISTRY.get(event_type_name)
        if not info:
            continue

        filter_attr = discovery.service_filter_map.get(
            event_type_name, available.matched_filter
        )
        service = anchor.primary_service

        # For K8s queries, use bare service name when naming convention says so.
        if info.domain == "k8s" and naming_convention:
            nc = naming_convention
            if (
                getattr(nc, "k8s_deployment_name_format", "full") == "bare"
                and getattr(nc, "separator", None)
            ):
                sep = nc.separator
                if sep in service:
                    if getattr(nc, "env_position", None) == "prefix":
                        service = service.split(sep, 1)[1]
                    elif getattr(nc, "env_position", None) == "suffix":
                        service = service.rsplit(sep, 1)[0]

        for signal_name in info.signals:
            # Avoid duplicate signals (e.g. two event types for same signal).
            if signal_name in seen_signals:
                continue

            signal_query = SIGNAL_QUERIES.get(signal_name)
            if not signal_query:
                continue

            seen_signals.add(signal_name)

            # Substitute template variables.
            nrql = signal_query.nrql.format(
                filter_attr=filter_attr,
                service=service,
                since=anchor.since_minutes,
                until=anchor.until_clause,
                ns_filter=ns_filter,
                ns_event_filter=ns_event_filter,
                severity_attr=severity_attr,
                timeseries_bucket=timeseries_bucket,
            )

            queries.append(
                InvestigationQuery(
                    signal=signal_name,
                    event_type=event_type_name,
                    domain=available.domain,
                    nrql=nrql,
                )
            )

    # Add timeseries queries for spike detection if we have APM or K8s data.
    if "apm" in discovery.domains_with_data and "error_rate_timeseries" not in seen_signals:
        # Find a Transaction filter.
        tx_avail = discovery.available.get("Transaction")
        if tx_avail:
            ts_query = SIGNAL_QUERIES.get("error_rate_timeseries")
            if ts_query:
                nrql = ts_query.nrql.format(
                    filter_attr=tx_avail.matched_filter,
                    service=anchor.primary_service,
                    since=anchor.since_minutes,
                    until=anchor.until_clause,
                    ns_filter=ns_filter,
                    ns_event_filter=ns_event_filter,
                    severity_attr=severity_attr,
                    timeseries_bucket=timeseries_bucket,
                )
                queries.append(
                    InvestigationQuery(
                        signal="error_rate_timeseries",
                        event_type="Transaction",
                        domain="apm",
                        nrql=nrql,
                    )
                )

    if "k8s" in discovery.domains_with_data and "restart_timeseries" not in seen_signals:
        pod_avail = discovery.available.get("K8sPodSample")
        if pod_avail:
            ts_query = SIGNAL_QUERIES.get("restart_timeseries")
            if ts_query:
                # Apply bare-name stripping for K8s timeseries too.
                ts_service = anchor.primary_service
                if naming_convention:
                    nc = naming_convention
                    if (
                        getattr(nc, "k8s_deployment_name_format", "full") == "bare"
                        and getattr(nc, "separator", None)
                    ):
                        sep = nc.separator
                        if sep in ts_service:
                            if getattr(nc, "env_position", None) == "prefix":
                                ts_service = ts_service.split(sep, 1)[1]
                            elif getattr(nc, "env_position", None) == "suffix":
                                ts_service = ts_service.rsplit(sep, 1)[0]
                nrql = ts_query.nrql.format(
                    filter_attr=pod_avail.matched_filter,
                    service=ts_service,
                    since=anchor.since_minutes,
                    until=anchor.until_clause,
                    ns_filter=ns_filter,
                    ns_event_filter=ns_event_filter,
                    severity_attr=severity_attr,
                    timeseries_bucket=timeseries_bucket,
                )
                queries.append(
                    InvestigationQuery(
                        signal="restart_timeseries",
                        event_type="K8sPodSample",
                        domain="k8s",
                        nrql=nrql,
                    )
                )

    return queries


def get_health_check(signal_name: str) -> Callable[[list[dict]], list[str]]:
    """Get the health check function for a signal.

    Args:
        signal_name: The signal identifier.

    Returns:
        A callable that takes query results and returns finding strings.
    """
    return HEALTH_CHECKS.get(signal_name, lambda r: [])
