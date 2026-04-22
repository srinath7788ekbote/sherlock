"""
Tests for golden signals tool.
"""

import json

import httpx
import pytest
import respx

from core.context import AccountContext
from tools.golden_signals import get_service_golden_signals


@pytest.fixture
def gs_context(mock_credentials, mock_intelligence, mock_context):
    """Full context for golden signals tests."""
    return mock_credentials


def _mock_nrql_response(results):
    return {
        "data": {
            "actor": {
                "account": {
                    "nrql": {"results": results}
                }
            }
        }
    }


class TestGetServiceGoldenSignals:
    """Tests for get_service_golden_signals."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_healthy_service(self, gs_context):
        """Healthy service returns HEALTHY status."""
        healthy = _mock_nrql_response([{
            "avg_duration": 0.05,
            "percentile.duration.50": 0.03,
            "percentile.duration.90": 0.08,
            "percentile.duration.95": 0.12,
            "percentile.duration.99": 0.25,
        }])
        throughput = _mock_nrql_response([{"rpm": 1200}])
        errors = _mock_nrql_response([{"error_rate": 0.5, "total_transactions": 36000}])
        saturation = _mock_nrql_response([{"avg_cpu": 40, "avg_memory_mb": 512}])
        empty_ts = _mock_nrql_response([])
        top_errors = _mock_nrql_response([])

        def _route(request):
            body = request.content.decode()
            if "event_count" in body:
                return httpx.Response(200, json=_mock_nrql_response([{"event_count": 0}]))
            if "percentile(duration" in body and "TIMESERIES" not in body:
                return httpx.Response(200, json=healthy)
            if "rate(count" in body:
                return httpx.Response(200, json=throughput)
            if "error IS true" in body and "TIMESERIES" not in body:
                return httpx.Response(200, json=errors)
            if "cpuPercent" in body:
                return httpx.Response(200, json=saturation)
            return httpx.Response(200, json=_mock_nrql_response([]))

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_route)

        result = await get_service_golden_signals("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["overall_status"] == "HEALTHY"
        assert parsed["service_name"] == "payment-svc-prod"
        assert parsed["latency"]["avg_duration_s"] == 0.05
        assert parsed["throughput"]["rpm"] == 1200
        assert parsed["errors"]["error_rate_pct"] == 0.5

    @respx.mock
    @pytest.mark.asyncio
    async def test_critical_error_rate(self, gs_context):
        """High error rate returns CRITICAL status."""
        latency = _mock_nrql_response([{"avg_duration": 2.0, "percentile.duration.99": 8.0}])
        throughput = _mock_nrql_response([{"rpm": 500}])
        errors = _mock_nrql_response([{"error_rate": 45.0, "total_transactions": 1000}])
        saturation = _mock_nrql_response([{"avg_cpu": 90, "avg_memory_mb": 1024}])
        empty_ts = _mock_nrql_response([])

        def _route(request):
            body = request.content.decode()
            if "event_count" in body:
                return httpx.Response(200, json=_mock_nrql_response([{"event_count": 0}]))
            if "percentile(duration" in body and "TIMESERIES" not in body:
                return httpx.Response(200, json=latency)
            if "rate(count" in body:
                return httpx.Response(200, json=throughput)
            if "error IS true" in body and "TIMESERIES" not in body:
                return httpx.Response(200, json=errors)
            if "cpuPercent" in body:
                return httpx.Response(200, json=saturation)
            return httpx.Response(200, json=_mock_nrql_response([]))

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_route)

        result = await get_service_golden_signals("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["overall_status"] == "CRITICAL"
        assert any("error rate" in s.lower() for s in parsed["health_signals"])

    @respx.mock
    @pytest.mark.asyncio
    async def test_zero_throughput(self, gs_context):
        """Zero throughput returns CRITICAL with down signal."""
        latency = _mock_nrql_response([{"avg_duration": 0}])
        throughput = _mock_nrql_response([{"rpm": 0}])
        errors = _mock_nrql_response([{"error_rate": 0, "total_transactions": 0}])
        saturation = _mock_nrql_response([{"avg_cpu": 0}])
        empty_ts = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=latency),
                httpx.Response(200, json=throughput),
                httpx.Response(200, json=errors),
                httpx.Response(200, json=saturation),
                httpx.Response(200, json=empty_ts),
                httpx.Response(200, json=empty_ts),
                httpx.Response(200, json=empty_ts),
            ]
        )

        result = await get_service_golden_signals("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["overall_status"] == "CRITICAL"
        assert any("throughput" in s.lower() for s in parsed["health_signals"])

    @respx.mock
    @pytest.mark.asyncio
    async def test_warning_status(self, gs_context):
        """Elevated error rate returns WARNING status."""
        latency = _mock_nrql_response([{"avg_duration": 0.1, "percentile.duration.99": 0.5}])
        throughput = _mock_nrql_response([{"rpm": 800}])
        errors = _mock_nrql_response([{"error_rate": 10.0, "total_transactions": 5000}])
        saturation = _mock_nrql_response([{"avg_cpu": 50}])
        empty_ts = _mock_nrql_response([])

        def _route(request):
            body = request.content.decode()
            if "event_count" in body:
                return httpx.Response(200, json=_mock_nrql_response([{"event_count": 0}]))
            if "percentile(duration" in body and "TIMESERIES" not in body:
                return httpx.Response(200, json=latency)
            if "rate(count" in body:
                return httpx.Response(200, json=throughput)
            if "error IS true" in body and "TIMESERIES" not in body:
                return httpx.Response(200, json=errors)
            if "cpuPercent" in body:
                return httpx.Response(200, json=saturation)
            return httpx.Response(200, json=_mock_nrql_response([]))

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_route)

        result = await get_service_golden_signals("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["overall_status"] == "WARNING"

    @respx.mock
    @pytest.mark.asyncio
    async def test_fuzzy_service_resolution(self, gs_context):
        """Fuzzy resolves service name."""
        healthy = _mock_nrql_response([{"avg_duration": 0.05}])
        throughput = _mock_nrql_response([{"rpm": 1000}])
        errors = _mock_nrql_response([{"error_rate": 0.1, "total_transactions": 10000}])
        saturation = _mock_nrql_response([{"avg_cpu": 30}])
        empty_ts = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=healthy),
                httpx.Response(200, json=throughput),
                httpx.Response(200, json=errors),
                httpx.Response(200, json=saturation),
                httpx.Response(200, json=empty_ts),
                httpx.Response(200, json=empty_ts),
                httpx.Response(200, json=empty_ts),
            ]
        )

        result = await get_service_golden_signals("payment-svc")
        parsed = json.loads(result)
        assert parsed["service_name"] == "payment-svc-prod"
        assert "resolved_from" in parsed

    @pytest.mark.asyncio
    async def test_unknown_service_returns_error(self, gs_context):
        """Unknown service returns error JSON."""
        result = await get_service_golden_signals("completely-unknown-xyz")
        parsed = json.loads(result)
        assert "error" in parsed

    @respx.mock
    @pytest.mark.asyncio
    async def test_api_error_handled(self, gs_context):
        """API errors are caught gracefully."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(500)
        )

        result = await get_service_golden_signals("payment-svc-prod")
        parsed = json.loads(result)
        # Either returns error or degrades with empty data
        assert isinstance(parsed, dict)

    @respx.mock
    @pytest.mark.asyncio
    async def test_otel_service_uses_span_queries(self, gs_context):
        """OTel service (Span data, no Transaction) returns instrumentation='otel'."""
        def _route(request):
            body = request.content.decode()
            # OTel detection: Span count > 0, Transaction count = 0.
            if "FROM Span" in body and "event_count" in body:
                return httpx.Response(
                    200, json=_mock_nrql_response([{"event_count": 500}])
                )
            if "FROM Transaction" in body and "event_count" in body:
                return httpx.Response(
                    200, json=_mock_nrql_response([{"event_count": 0}])
                )
            # OTel golden signals combined query.
            if "FROM Span" in body and "otel.status_code" in body and "TIMESERIES" not in body:
                return httpx.Response(200, json=_mock_nrql_response([{
                    "error_rate": 2.0,
                    "avg_duration": 0.12,
                    "percentile.duration.50": 0.08,
                    "percentile.duration.90": 0.15,
                    "percentile.duration.95": 0.20,
                    "percentile.duration.99": 0.35,
                    "rpm": 600,
                }]))
            return httpx.Response(200, json=_mock_nrql_response([]))

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_route)

        result = await get_service_golden_signals("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["instrumentation"] == "otel"
        assert parsed["overall_status"] == "HEALTHY"
        assert parsed["latency"]["avg_duration_s"] == 0.12
        assert parsed["throughput"]["rpm"] == 600
        assert any("OTel" in w for w in parsed.get("warnings", []))


class TestGoldenSignalsGUIDResolution:
    """Tests for GUID ambiguity handling in golden signals deep links."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_response_omits_entity_links_when_guid_ambiguous(self, gs_context, mock_intelligence):
        """When two reporting entities share a name, entity-view links should be None
        but chart links should still be present."""
        # Inject ambiguous GUID state: two reporting candidates for the same name.
        mock_intelligence.apm.service_guid_candidates["payment-svc-prod"] = [
            {"guid": "guid-cluster-a", "reporting": True, "tags": {}, "alert_severity": ""},
            {"guid": "guid-cluster-b", "reporting": True, "tags": {}, "alert_severity": ""},
        ]
        mock_intelligence.apm.reporting_guids = {"guid-cluster-a", "guid-cluster-b"}

        def _route(request):
            body = request.content.decode()
            if "event_count" in body:
                return httpx.Response(200, json=_mock_nrql_response([{"event_count": 0}]))
            if "percentile(duration" in body and "TIMESERIES" not in body:
                return httpx.Response(200, json=_mock_nrql_response([{
                    "avg_duration": 0.05,
                    "percentile.duration.50": 0.03,
                    "percentile.duration.90": 0.08,
                    "percentile.duration.95": 0.12,
                    "percentile.duration.99": 0.25,
                }]))
            if "rate(count" in body:
                return httpx.Response(200, json=_mock_nrql_response([{"rpm": 100}]))
            if "error IS true" in body and "TIMESERIES" not in body:
                return httpx.Response(200, json=_mock_nrql_response([{"error_rate": 1.0, "total_transactions": 1000}]))
            if "cpuPercent" in body:
                return httpx.Response(200, json=_mock_nrql_response([{"avg_cpu": 20}]))
            return httpx.Response(200, json=_mock_nrql_response([]))

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_route)

        result = await get_service_golden_signals("payment-svc-prod")
        parsed = json.loads(result)

        links = parsed.get("links", {})
        # Entity-view links should be None (ambiguous GUID).
        assert links.get("service_overview") is None
        assert links.get("errors_inbox") is None
        assert links.get("transactions") is None
        # Chart links should still be present (filter by appName, safe).
        assert links.get("error_chart") is not None
        assert links.get("latency_chart") is not None
        assert links.get("throughput_chart") is not None
        # Should have a warning about ambiguous GUID.
        warnings = parsed.get("warnings", [])
        assert any("ambiguous" in w.lower() for w in warnings)

    @respx.mock
    @pytest.mark.asyncio
    async def test_response_includes_entity_links_when_guid_unambiguous(self, gs_context, mock_intelligence):
        """Normal case: single reporting candidate → all links present."""
        # Ensure single candidate (default from conftest should be fine).
        mock_intelligence.apm.service_guid_candidates["payment-svc-prod"] = [
            {"guid": "MTIzNDU2fEFQTXxBUFBMSUNBVElPTnwx", "reporting": True, "tags": {}, "alert_severity": "NOT_ALERTING"},
        ]
        mock_intelligence.apm.reporting_guids = {"MTIzNDU2fEFQTXxBUFBMSUNBVElPTnwx"}

        def _route(request):
            body = request.content.decode()
            if "event_count" in body:
                return httpx.Response(200, json=_mock_nrql_response([{"event_count": 0}]))
            if "percentile(duration" in body and "TIMESERIES" not in body:
                return httpx.Response(200, json=_mock_nrql_response([{
                    "avg_duration": 0.05,
                    "percentile.duration.50": 0.03,
                    "percentile.duration.90": 0.08,
                    "percentile.duration.95": 0.12,
                    "percentile.duration.99": 0.25,
                }]))
            if "rate(count" in body:
                return httpx.Response(200, json=_mock_nrql_response([{"rpm": 1000}]))
            if "error IS true" in body and "TIMESERIES" not in body:
                return httpx.Response(200, json=_mock_nrql_response([{"error_rate": 0.5, "total_transactions": 5000}]))
            if "cpuPercent" in body:
                return httpx.Response(200, json=_mock_nrql_response([{"avg_cpu": 30}]))
            return httpx.Response(200, json=_mock_nrql_response([]))

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_route)

        result = await get_service_golden_signals("payment-svc-prod")
        parsed = json.loads(result)

        links = parsed.get("links", {})
        # All entity-view links should be present.
        assert links.get("service_overview") is not None
        assert links.get("errors_inbox") is not None
        assert links.get("transactions") is not None
        # Chart links also present.
        assert links.get("error_chart") is not None
        assert links.get("latency_chart") is not None
        assert links.get("throughput_chart") is not None
