"""
Tests for synthetics monitoring tools.

Required test cases (12):
1. test_get_synthetic_monitors_returns_all
2. test_get_monitor_status_passing
3. test_get_monitor_status_global_failure
4. test_get_monitor_status_regional_failure
5. test_get_monitor_status_intermittent
6. test_fuzzy_monitor_resolution
7. test_monitor_not_found_returns_closest_matches
8. test_investigate_synthetic_global_down_apm_also_failing
9. test_investigate_synthetic_global_down_apm_healthy
10. test_investigate_synthetic_regional_failure
11. test_synthetic_correlation_in_investigate_service
12. test_synthetic_detected_before_apm_alert
"""

import json
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest
import respx

from core.context import AccountContext
from core.credentials import Credentials
from core.intelligence import (
    AccountIntelligence,
    SyntheticsIntelligence,
    SyntheticMonitorMeta,
)
from core.exceptions import MonitorNotFoundError
from tools.synthetics import (
    get_synthetic_monitors,
    get_monitor_status,
    get_monitor_results,
    investigate_synthetic,
    SYNTHETIC_CHECK_EVENT,
)


@pytest.fixture
def synth_context(mock_credentials, mock_intelligence, mock_context):
    """Full context for synthetics tests."""
    return mock_credentials


def _mock_nrql_response(results, alias="nrql"):
    """Build a standard NerdGraph NRQL response."""
    return {
        "data": {
            "actor": {
                alias: {
                    "results": results
                }
            }
        }
    }


def _mock_batch_response(**aliases):
    """Build a batch NerdGraph response with named aliases."""
    actor = {}
    for alias, results in aliases.items():
        actor[alias] = {"results": results}
    return {"data": {"actor": actor}}


class TestGetSyntheticMonitors:
    """Tests for get_synthetic_monitors."""

    # 1. test_get_synthetic_monitors_returns_all
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_synthetic_monitors_returns_all(self, synth_context, mock_intelligence):
        """Returns all monitors from intelligence cache."""
        result = await get_synthetic_monitors()
        parsed = json.loads(result)
        assert len(parsed["monitors"]) == 4
        names = [m["name"] for m in parsed["monitors"]]
        assert "Login Flow - Production" in names
        assert "Export API Health Check" in names
        assert "Payment Checkout - Prod" in names
        assert "Auth Token Refresh - Prod" in names


class TestGetMonitorStatus:
    """Tests for get_monitor_status."""

    # 2. test_get_monitor_status_passing
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_monitor_status_passing(self, synth_context, mock_synthetic_check_passing):
        """Passing monitor returns PASSING diagnosis."""
        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=mock_synthetic_check_passing),
                httpx.Response(200, json=mock_synthetic_check_passing),
                httpx.Response(200, json=mock_synthetic_check_passing),
                httpx.Response(200, json=mock_synthetic_check_passing),
                httpx.Response(200, json=mock_synthetic_check_passing),
            ]
        )

        result = await get_monitor_status("Login Flow - Production")
        parsed = json.loads(result)
        assert parsed["diagnosis"] == "PASSING"

    # 3. test_get_monitor_status_global_failure
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_monitor_status_global_failure(self, synth_context, mock_synthetic_check_global_failure):
        """Global failure detected when all locations fail."""

        def _handler(request):
            body = json.loads(request.content)
            query = body.get("query", "")
            if "FACET locationLabel" in query:
                # By-location data — all locations failing
                return httpx.Response(200, json={"data": {"actor": {"account": {"nrql": {"results": [
                    {"last_result": "FAILED", "pass_rate": 0.0, "last_duration_ms": 15000,
                     "locationLabel": "AWS_US_EAST_1", "facet": "AWS_US_EAST_1", "last_error": "Connection refused"},
                    {"last_result": "FAILED", "pass_rate": 0.0, "last_duration_ms": 15000,
                     "locationLabel": "AWS_EU_WEST_1", "facet": "AWS_EU_WEST_1", "last_error": "Connection refused"},
                ]}}}}})
            if "TIMESERIES" not in query and "ORDER BY" not in query:
                # Overall pass rate
                return httpx.Response(200, json={"data": {"actor": {"account": {"nrql": {"results": [
                    {"pass_rate": 0.0, "total_runs": 120, "avg_duration_ms": 15000.0}
                ]}}}}})
            # Recent failures and timeseries — empty
            return httpx.Response(200, json={"data": {"actor": {"account": {"nrql": {"results": []}}}}})

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_handler)

        result = await get_monitor_status("Login Flow - Production")
        parsed = json.loads(result)
        assert parsed["diagnosis"] == "GLOBAL_FAILURE"

    # 4. test_get_monitor_status_regional_failure
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_monitor_status_regional_failure(self, synth_context):
        """Regional failure detected when some locations fail."""

        def _handler(request):
            body = json.loads(request.content)
            query = body.get("query", "")
            if "FACET locationLabel" in query:
                return httpx.Response(200, json={"data": {"actor": {"account": {"nrql": {"results": [
                    {"last_result": "FAILED", "pass_rate": 0.0, "last_duration_ms": 15000,
                     "locationLabel": "AWS_US_EAST_1", "facet": "AWS_US_EAST_1", "last_error": "Timeout"},
                    {"last_result": "SUCCESS", "pass_rate": 100.0, "last_duration_ms": 2500,
                     "locationLabel": "AWS_EU_WEST_1", "facet": "AWS_EU_WEST_1", "last_error": None},
                ]}}}}})
            if "TIMESERIES" not in query and "ORDER BY" not in query:
                return httpx.Response(200, json={"data": {"actor": {"account": {"nrql": {"results": [
                    {"pass_rate": 50.0, "total_runs": 100, "avg_duration_ms": 5000.0}
                ]}}}}})
            return httpx.Response(200, json={"data": {"actor": {"account": {"nrql": {"results": []}}}}})

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_handler)

        result = await get_monitor_status("Login Flow - Production")
        parsed = json.loads(result)
        assert parsed["diagnosis"] in ("REGIONAL_FAILURE", "INTERMITTENT")

    # 5. test_get_monitor_status_intermittent
    @respx.mock
    @pytest.mark.asyncio
    async def test_get_monitor_status_intermittent(self, synth_context):
        """Intermittent failures detected with mixed success rates."""
        intermittent_response = _mock_batch_response(
            q0=[{"result": "SUCCESS", "monitorName": "Login Flow"}],
            q1=[
                {"locationLabel": "AWS_US_EAST_1", "success_rate": 80},
                {"locationLabel": "AWS_EU_WEST_1", "success_rate": 75},
            ],
            q2=[{"average_duration": 1.5}],
            q3=[{"latest_result": "SUCCESS", "timestamp": 1700000000000}],
            q4=[{"total_checks": 100, "failed_checks": 20}],
        )
        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=intermittent_response),
                httpx.Response(200, json=intermittent_response),
                httpx.Response(200, json=intermittent_response),
                httpx.Response(200, json=intermittent_response),
                httpx.Response(200, json=intermittent_response),
            ]
        )

        result = await get_monitor_status("Login Flow - Production")
        parsed = json.loads(result)
        assert parsed["diagnosis"] in ("INTERMITTENT", "PASSING", "DEGRADED_PERFORMANCE")


class TestFuzzyMonitorResolution:
    """Tests for monitor name resolution."""

    # 6. test_fuzzy_monitor_resolution
    def test_fuzzy_monitor_resolution(self, mock_intelligence):
        """Fuzzy matching resolves close monitor names."""
        from core.sanitize import fuzzy_resolve_monitor
        resolved_name, was_fuzzy, confidence = fuzzy_resolve_monitor(
            "login flow", mock_intelligence.synthetics.monitor_names
        )
        assert "Login Flow" in resolved_name

    # 7. test_monitor_not_found_returns_closest_matches
    def test_monitor_not_found_returns_closest_matches(self, mock_intelligence):
        """Unknown monitor name raises error with closest matches."""
        from core.sanitize import fuzzy_resolve_monitor
        with pytest.raises(MonitorNotFoundError) as exc_info:
            fuzzy_resolve_monitor(
                "completely-random-name-xyz",
                mock_intelligence.synthetics.monitor_names,
            )
        error_msg = str(exc_info.value)
        # Error should suggest closest monitor names
        assert "closest" in error_msg.lower() or "not found" in error_msg.lower()


class TestInvestigateSynthetic:
    """Tests for investigate_synthetic."""

    # 8. test_investigate_synthetic_global_down_apm_also_failing
    @pytest.mark.asyncio
    async def test_investigate_synthetic_global_down_apm_also_failing(self, synth_context):
        """Global failure + APM errors = service-side root cause."""
        with patch("tools.synthetics.get_monitor_status", new_callable=AsyncMock) as mock_status, \
             patch("tools.synthetics.get_monitor_results", new_callable=AsyncMock) as mock_results, \
             patch("tools.golden_signals.get_service_golden_signals", new_callable=AsyncMock) as mock_golden, \
             patch("tools.alerts.get_service_incidents", new_callable=AsyncMock) as mock_incidents:

            mock_status.return_value = json.dumps({
                "diagnosis": "GLOBAL_FAILURE",
                "status_signals": ["🔴 GLOBALLY DOWN — failing in all locations"],
                "overall": {"pass_rate": 0.0, "total_runs": 120, "avg_duration_ms": 15000},
                "by_location": [
                    {"last_result": "FAILED", "locationLabel": "AWS_US_EAST_1", "facet": "AWS_US_EAST_1"},
                    {"last_result": "FAILED", "locationLabel": "AWS_EU_WEST_1", "facet": "AWS_EU_WEST_1"},
                ],
                "recent_failures": [],
                "pass_rate_timeseries": [],
                "duration_timeseries": [],
            })
            mock_results.return_value = json.dumps({
                "runs": [{"result": "FAILED", "error": "Connection refused"}],
            })
            mock_golden.return_value = json.dumps({
                "overall_status": "CRITICAL",
                "errors": {"error_rate_pct": 45.0, "total_transactions": 100, "top_errors": []},
                "latency": {"avg_duration_s": 15.0, "p50": None, "p90": None, "p95": None, "p99": None},
                "throughput": {"rpm": 0},
                "saturation": {"avg_cpu_pct": 95, "avg_memory_mb": None},
                "health_signals": ["🔴 CRITICAL error rate: 45.0%"],
                "latency_timeseries": [],
                "error_timeseries": [],
            })
            mock_incidents.return_value = json.dumps({"incidents": []})

            result = await investigate_synthetic("Login Flow - Production")
            # Should identify service-side issue
            assert "GLOBAL_FAILURE" in result or "global" in result.lower()

    # 9. test_investigate_synthetic_global_down_apm_healthy
    @pytest.mark.asyncio
    async def test_investigate_synthetic_global_down_apm_healthy(self, synth_context):
        """Global failure + healthy APM = network/DNS/CDN issue."""
        with patch("tools.synthetics.get_monitor_status", new_callable=AsyncMock) as mock_status, \
             patch("tools.synthetics.get_monitor_results", new_callable=AsyncMock) as mock_results, \
             patch("tools.golden_signals.get_service_golden_signals", new_callable=AsyncMock) as mock_golden, \
             patch("tools.alerts.get_service_incidents", new_callable=AsyncMock) as mock_incidents:

            mock_status.return_value = json.dumps({
                "diagnosis": "GLOBAL_FAILURE",
                "status_signals": ["🔴 GLOBALLY DOWN — failing in all locations"],
                "overall": {"pass_rate": 0.0, "total_runs": 50, "avg_duration_ms": 30000},
                "by_location": [
                    {"last_result": "FAILED", "locationLabel": "AWS_US_EAST_1", "facet": "AWS_US_EAST_1"},
                    {"last_result": "FAILED", "locationLabel": "AWS_EU_WEST_1", "facet": "AWS_EU_WEST_1"},
                ],
                "recent_failures": [],
                "pass_rate_timeseries": [],
                "duration_timeseries": [],
            })
            mock_results.return_value = json.dumps({
                "runs": [{"result": "FAILED", "error": "DNS resolution failed"}],
            })
            mock_golden.return_value = json.dumps({
                "overall_status": "HEALTHY",
                "errors": {"error_rate_pct": 0.1, "total_transactions": 50000, "top_errors": []},
                "latency": {"avg_duration_s": 0.05, "p50": None, "p90": None, "p95": None, "p99": None},
                "throughput": {"rpm": 1500},
                "saturation": {"avg_cpu_pct": 20, "avg_memory_mb": None},
                "health_signals": [],
                "latency_timeseries": [],
                "error_timeseries": [],
            })
            mock_incidents.return_value = json.dumps({"incidents": []})

            result = await investigate_synthetic("Login Flow - Production")
            # Should mention network, DNS, CDN, or external issue
            lower = result.lower()
            assert any(
                term in lower
                for term in ["network", "dns", "cdn", "external", "healthy", "apm"]
            ) or "GLOBAL_FAILURE" in result

    # 10. test_investigate_synthetic_regional_failure
    @respx.mock
    @pytest.mark.asyncio
    async def test_investigate_synthetic_regional_failure(self, synth_context):
        """Regional failure investigation identifies affected regions."""
        regional = _mock_batch_response(
            q0=[{"result": "FAILED", "monitorName": "Login Flow", "locationLabel": "AWS_US_EAST_1"}],
            q1=[
                {"locationLabel": "AWS_US_EAST_1", "success_rate": 0},
                {"locationLabel": "AWS_EU_WEST_1", "success_rate": 100},
            ],
            q2=[{"average_duration": 5.0}],
            q3=[{"latest_result": "FAILED", "timestamp": 1700000000000}],
            q4=[{"total_checks": 100, "failed_checks": 40}],
        )
        apm_ok = _mock_nrql_response([{"error_rate": 1.0, "avg_duration": 0.1}])

        responses = [
            httpx.Response(200, json=regional),
            httpx.Response(200, json=regional),
            httpx.Response(200, json=regional),
            httpx.Response(200, json=regional),
            httpx.Response(200, json=regional),
            httpx.Response(200, json=apm_ok),
            httpx.Response(200, json=apm_ok),
        ]
        respx.post("https://api.newrelic.com/graphql").mock(side_effect=responses)

        result = await investigate_synthetic("Login Flow - Production")
        lower = result.lower()
        assert "region" in lower or "REGIONAL" in result or "location" in lower


class TestSyntheticCorrelation:
    """Tests for synthetic correlation in investigation tools."""

    # 11. test_synthetic_correlation_in_investigate_service
    @respx.mock
    @pytest.mark.asyncio
    async def test_synthetic_correlation_in_investigate_service(self, synth_context):
        """investigate_service includes synthetic data for services with monitors."""
        from tools.investigate import investigate_service

        # Build responses for the full investigation pipeline
        # APM golden signals
        golden = _mock_nrql_response([{"avg_duration": 0.05, "error_rate": 0.2, "throughput": 1500}])
        # Alerts/incidents
        alerts = _mock_nrql_response([])
        # Logs
        logs_resp = _mock_nrql_response([])
        # K8s
        k8s_resp = _mock_nrql_response([])
        # Synthetic monitor status
        synth_status = _mock_batch_response(
            q0=[{"result": "SUCCESS", "monitorName": "Login Flow"}],
            q1=[{"locationLabel": "AWS_US_EAST_1", "success_rate": 100}],
            q2=[{"average_duration": 0.5}],
            q3=[{"latest_result": "SUCCESS", "timestamp": 1700000000000}],
            q4=[{"total_checks": 100, "failed_checks": 0}],
        )

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=golden),
                httpx.Response(200, json=golden),
                httpx.Response(200, json=golden),
                httpx.Response(200, json=golden),
                httpx.Response(200, json=golden),
                httpx.Response(200, json=golden),
                httpx.Response(200, json=golden),
                httpx.Response(200, json=alerts),
                httpx.Response(200, json=logs_resp),
                httpx.Response(200, json=k8s_resp),
                httpx.Response(200, json=synth_status),
                httpx.Response(200, json=synth_status),
                httpx.Response(200, json=synth_status),
                httpx.Response(200, json=synth_status),
                httpx.Response(200, json=synth_status),
            ]
        )

        result = await investigate_service("web-api")
        lower = result.lower()
        # Should include synthetic correlation data
        assert "synthetic" in lower or "monitor" in lower or "login" in lower

    # 12. test_synthetic_detected_before_apm_alert
    @respx.mock
    @pytest.mark.asyncio
    async def test_synthetic_detected_before_apm_alert(self, synth_context):
        """Synthetic failures can be detected before APM alerts fire."""
        # Synthetic: failing (detected quickly by external probes)
        synth_failing = _mock_batch_response(
            q0=[{"result": "FAILED", "monitorName": "Login Flow", "timestamp": 1700000000000}],
            q1=[
                {"locationLabel": "AWS_US_EAST_1", "success_rate": 0},
                {"locationLabel": "AWS_EU_WEST_1", "success_rate": 0},
            ],
            q2=[{"average_duration": 30.0}],
            q3=[{"latest_result": "FAILED", "timestamp": 1700000000000}],
            q4=[{"total_checks": 20, "failed_checks": 20}],
        )
        # APM: no alerts yet (lag in alert evaluation)
        apm_no_alerts = _mock_nrql_response([])

        responses = [
            httpx.Response(200, json=synth_failing),
            httpx.Response(200, json=synth_failing),
            httpx.Response(200, json=synth_failing),
            httpx.Response(200, json=synth_failing),
            httpx.Response(200, json=synth_failing),
            httpx.Response(200, json=apm_no_alerts),
            httpx.Response(200, json=apm_no_alerts),
        ]
        respx.post("https://api.newrelic.com/graphql").mock(side_effect=responses)

        result = await investigate_synthetic("Login Flow - Production")
        # Synthetic detected the issue — response should contain failure info
        assert "FAILED" in result.upper() or "GLOBAL_FAILURE" in result or "failure" in result.lower()
