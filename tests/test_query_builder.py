"""
Tests for the adaptive query builder.
"""

from datetime import datetime, timedelta, timezone

import pytest

from core.discovery import (
    AvailableEventType,
    DiscoveryResult,
    EVENT_REGISTRY,
)
from core.query_builder import (
    HEALTH_CHECKS,
    SIGNAL_QUERIES,
    InvestigationQuery,
    SignalQuery,
    build_investigation_queries,
    get_health_check,
    _check_pod_status,
    _check_replica_health,
    _check_error_rate,
    _check_oom,
    _check_error_logs,
    _check_queue_depth,
)
from tools.investigate import InvestigationAnchor


@pytest.fixture
def anchor():
    """Provide a standard investigation anchor."""
    now = datetime.now(timezone.utc)
    return InvestigationAnchor(
        primary_service="payment-svc-prod",
        all_candidates=["payment-svc-prod"],
        window_start=now - timedelta(minutes=30),
        since_minutes=30,
        until_clause="",
        window_source="requested",
    )


def _make_discovery(event_types: dict[str, str]) -> DiscoveryResult:
    """Build a DiscoveryResult with given event_type → matched_filter pairs."""
    available: dict[str, AvailableEventType] = {}
    service_filter_map: dict[str, str] = {}
    domains: set[str] = set()

    for et_name, filter_attr in event_types.items():
        info = EVENT_REGISTRY.get(et_name)
        if not info:
            continue
        available[et_name] = AvailableEventType(
            event_type=et_name,
            domain=info.domain,
            event_count=100,
            matched_filter=filter_attr,
            matched_value="payment-svc-prod",
            signals=info.signals,
        )
        service_filter_map[et_name] = filter_attr
        domains.add(info.domain)

    all_event_types = set(EVENT_REGISTRY.keys())
    unavailable = sorted(all_event_types - set(event_types.keys()))

    return DiscoveryResult(
        available=available,
        unavailable=unavailable,
        domains_with_data=sorted(domains),
        service_filter_map=service_filter_map,
        discovery_duration_ms=50,
        total_event_types_checked=len(EVENT_REGISTRY),
    )


class TestBuildQueriesOnlyForDiscoveredEventTypes:
    """test_build_queries_only_for_discovered_event_types"""

    def test_queries_only_for_discovered_types(self, anchor):
        """No queries are generated for event types not in discovery."""
        discovery = _make_discovery({
            "Transaction": "appName",
        })

        queries = build_investigation_queries(
            discovery=discovery, anchor=anchor
        )

        event_types_queried = {q.event_type for q in queries}
        # Only Transaction-related queries should exist.
        assert "Transaction" in event_types_queried
        assert "K8sPodSample" not in event_types_queried
        assert "Log" not in event_types_queried

    def test_empty_discovery_yields_no_queries(self, anchor):
        """When nothing was discovered, no queries are built."""
        discovery = _make_discovery({})
        queries = build_investigation_queries(
            discovery=discovery, anchor=anchor
        )
        assert queries == []


class TestBuildQueriesSubstitutesCorrectFilterAttribute:
    """test_build_queries_substitutes_correct_filter_attribute"""

    def test_filter_attribute_in_nrql(self, anchor):
        """The NRQL contains the correct filter attribute from discovery."""
        discovery = _make_discovery({
            "Transaction": "appName",
            "TransactionError": "appName",
        })

        queries = build_investigation_queries(
            discovery=discovery, anchor=anchor
        )

        for q in queries:
            # The NRQL should reference the matched filter.
            assert "appName" in q.nrql or "payment-svc-prod" in q.nrql

    def test_different_filter_per_event_type(self, anchor):
        """Different event types can use different filter attributes."""
        discovery = _make_discovery({
            "K8sPodSample": "deploymentName",
            "Log": "service.name",
        })

        queries = build_investigation_queries(
            discovery=discovery, anchor=anchor
        )

        k8s_queries = [q for q in queries if q.event_type == "K8sPodSample"]
        log_queries = [q for q in queries if q.event_type == "Log"]

        if k8s_queries:
            assert "deploymentName" in k8s_queries[0].nrql
        if log_queries:
            assert "service.name" in log_queries[0].nrql


class TestBuildQueriesIncludesNamespaceFilterForK8s:
    """test_build_queries_includes_namespace_filter_for_k8s"""

    def test_namespace_filter_included(self, anchor):
        """K8s queries include namespace filter when provided."""
        discovery = _make_discovery({
            "K8sPodSample": "deploymentName",
        })

        queries = build_investigation_queries(
            discovery=discovery,
            anchor=anchor,
            namespace="payments-prod",
        )

        k8s_queries = [q for q in queries if q.domain == "k8s"]
        assert len(k8s_queries) > 0
        for q in k8s_queries:
            assert "payments-prod" in q.nrql

    def test_no_namespace_filter_when_not_provided(self, anchor):
        """K8s queries omit namespace filter when not provided."""
        discovery = _make_discovery({
            "K8sPodSample": "deploymentName",
        })

        queries = build_investigation_queries(
            discovery=discovery, anchor=anchor, namespace=None
        )

        k8s_queries = [q for q in queries if q.domain == "k8s"]
        assert len(k8s_queries) > 0
        for q in k8s_queries:
            assert "namespaceName =" not in q.nrql


class TestHealthCheckPodStatusDetectsFailures:
    """test_health_check_pod_status_detects_failures"""

    def test_detects_crashloopbackoff(self):
        """CrashLoopBackOff pods are flagged as critical."""
        results = [
            {"status": "CrashLoopBackOff", "podName": "web-api-abc", "current_restarts": 12}
        ]
        findings = _check_pod_status(results)
        assert any("🔴" in f and "web-api-abc" in f for f in findings)

    def test_detects_not_ready(self):
        """Pods that are not ready are flagged as warnings."""
        results = [
            {"status": "Running", "podName": "web-api-def", "ready": False, "current_restarts": 0}
        ]
        findings = _check_pod_status(results)
        assert any("⚠️" in f and "not ready" in f for f in findings)

    def test_healthy_pod_no_findings(self):
        """Healthy pods produce no findings."""
        results = [
            {"status": "Running", "podName": "web-api-ghi", "ready": True, "current_restarts": 0}
        ]
        findings = _check_pod_status(results)
        assert len(findings) == 0

    def test_high_restarts_flagged(self):
        """Pods with > 5 restarts are flagged."""
        results = [
            {"status": "Running", "podName": "web-api-jkl", "ready": True, "current_restarts": 10}
        ]
        findings = _check_pod_status(results)
        assert any("restart" in f.lower() for f in findings)


class TestHealthCheckErrorRateThresholds:
    """test_health_check_error_rate_thresholds"""

    def test_critical_error_rate(self):
        """Error rate >= 20% → CRITICAL."""
        results = [{"error_rate": 25.0, "throughput": 500, "p99_latency": 1.0}]
        findings = _check_error_rate(results)
        assert any("🔴" in f and "CRITICAL error rate" in f for f in findings)

    def test_warning_error_rate(self):
        """Error rate >= 5% but < 20% → WARNING."""
        results = [{"error_rate": 8.0, "throughput": 500, "p99_latency": 1.0}]
        findings = _check_error_rate(results)
        assert any("⚠️" in f and "Elevated error rate" in f for f in findings)

    def test_healthy_error_rate(self):
        """Error rate < 5% → no error finding."""
        results = [{"error_rate": 1.0, "throughput": 500, "p99_latency": 1.0}]
        findings = _check_error_rate(results)
        error_findings = [f for f in findings if "error rate" in f.lower()]
        assert len(error_findings) == 0

    def test_zero_throughput_critical(self):
        """Zero throughput is flagged as critical."""
        results = [{"error_rate": 0, "throughput": 0, "p99_latency": 0}]
        findings = _check_error_rate(results)
        assert any("🔴" in f and "ZERO throughput" in f for f in findings)


class TestGetHealthCheckReturnsCallable:
    """test_get_health_check_returns_callable"""

    def test_known_signal_returns_function(self):
        """Known signal names return their health check function."""
        hc = get_health_check("pod_status")
        assert callable(hc)
        assert hc is _check_pod_status

    def test_unknown_signal_returns_noop(self):
        """Unknown signals return a no-op that produces empty list."""
        hc = get_health_check("totally_unknown_signal")
        assert callable(hc)
        assert hc([{"some": "data"}]) == []


class TestSignalQueriesRegistryCompleteness:
    """test_signal_queries_registry_completeness"""

    def test_all_registry_signals_have_queries(self):
        """Every domain in EVENT_REGISTRY has at least one event type with a
        queryable signal in SIGNAL_QUERIES, ensuring `build_investigation_queries`
        can generate queries for every discovered domain."""
        domains: dict[str, list[str]] = {}
        for et_name, info in EVENT_REGISTRY.items():
            domains.setdefault(info.domain, [])
            if any(signal in SIGNAL_QUERIES for signal in info.signals):
                domains[info.domain].append(et_name)

        missing_domains = [d for d, ets in domains.items() if not ets]
        assert missing_domains == [], (
            f"Domains without any queryable event type: {missing_domains}"
        )

    def test_all_registry_signals_have_health_checks(self):
        """Every signal referenced in EVENT_REGISTRY has a HEALTH_CHECKS entry."""
        missing = []
        for et_name, info in EVENT_REGISTRY.items():
            for signal in info.signals:
                if signal not in HEALTH_CHECKS:
                    missing.append(f"{et_name}.{signal}")
        assert missing == [], f"Signals without health checks: {missing}"


class TestHealthCheckOOM:
    """Additional health check tests for OOMKill detection."""

    def test_oom_detected(self):
        """OOMKill events are flagged as critical."""
        results = [{"oom_count": 3, "podName": "api-pod-xyz", "memory_mb": 512, "limit_mb": 512}]
        findings = _check_oom(results)
        assert any("🔴" in f and "OOMKilled" in f for f in findings)

    def test_no_oom(self):
        """Zero OOM count produces no findings."""
        results = [{"oom_count": 0, "podName": "api-pod-xyz"}]
        findings = _check_oom(results)
        assert len(findings) == 0


class TestHealthCheckErrorLogs:
    """Health check tests for error log analysis."""

    def test_many_error_logs_critical(self):
        """More than 50 error logs → critical."""
        results = [{"message": f"Error {i}"} for i in range(60)]
        findings = _check_error_logs(results)
        assert any("🔴" in f for f in findings)

    def test_few_error_logs_info(self):
        """Fewer than 10 error logs → info."""
        results = [{"message": "Error 1"}, {"message": "Error 2"}]
        findings = _check_error_logs(results)
        assert any("ℹ️" in f for f in findings)

    def test_repeated_pattern_detected(self):
        """Repeated log messages are flagged — 'Connection timeout' triggers dependency detection."""
        results = [{"message": "Connection timeout"} for _ in range(10)]
        findings = _check_error_logs(results)
        # "Connection timeout" matches dependency pattern, so it's reported as DEPENDENCY FAILURE.
        assert any("DEPENDENCY FAILURE" in f for f in findings)


class TestHealthCheckQueueDepth:
    """Health check tests for queue depth analysis."""

    def test_large_backlog_critical(self):
        """Queue depth > 10000 → critical."""
        results = [{"queue_depth": 15000, "entityName": "order-queue"}]
        findings = _check_queue_depth(results)
        assert any("🔴" in f and "backlog" in f.lower() for f in findings)

    def test_stale_messages_critical(self):
        """Oldest message > 1h → critical."""
        results = [{"queue_depth": 100, "oldest_message_age_sec": 7200, "entityName": "order-queue"}]
        findings = _check_queue_depth(results)
        assert any("🔴" in f and "Stale" in f for f in findings)
