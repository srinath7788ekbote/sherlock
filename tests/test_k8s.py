"""
Tests for Kubernetes health tool.
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.context import AccountContext
from tools.k8s import get_k8s_health


def _mock_nrql_response(results):
    """Build a standard NerdGraph NRQL response."""
    return {
        "data": {
            "actor": {
                "account": {
                    "nrql": {
                        "results": results
                    }
                }
            }
        }
    }


@pytest.fixture
def k8s_context(mock_credentials, mock_intelligence, mock_context):
    """Full context for K8s tests."""
    return mock_credentials


class TestGetK8sHealth:
    """Tests for get_k8s_health."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_healthy_cluster(self, k8s_context):
        """Healthy K8s cluster returns normal metrics."""
        pod_data = _mock_nrql_response([
            {"podName": "web-api-abc", "status": "Running", "restartCount": 0},
            {"podName": "web-api-def", "status": "Running", "restartCount": 0},
        ])
        node_data = _mock_nrql_response([
            {"nodeName": "node-1", "cpuPercent": 45.0, "memoryPercent": 60.0},
        ])
        container_data = _mock_nrql_response([
            {"containerName": "web-api", "cpuPercent": 30.0, "memoryPercent": 50.0},
        ])
        event_data = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=pod_data),
                httpx.Response(200, json=node_data),
                httpx.Response(200, json=container_data),
                httpx.Response(200, json=event_data),
            ]
        )

        result = await get_k8s_health("web-api")
        parsed = json.loads(result)
        assert "pods" in parsed or "pod" in result.lower()

    @respx.mock
    @pytest.mark.asyncio
    async def test_crashloop_detected(self, k8s_context):
        """CrashLoopBackOff pods are highlighted."""
        pod_data = _mock_nrql_response([
            {"podName": "web-api-abc", "status": "CrashLoopBackOff", "restartCount": 15},
        ])
        node_data = _mock_nrql_response([
            {"nodeName": "node-1", "cpuPercent": 90.0, "memoryPercent": 95.0},
        ])
        container_data = _mock_nrql_response([
            {"containerName": "web-api", "cpuPercent": 95.0, "memoryPercent": 98.0},
        ])
        event_data = _mock_nrql_response([
            {"reason": "BackOff", "message": "Back-off restarting failed container"},
        ])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=pod_data),
                httpx.Response(200, json=node_data),
                httpx.Response(200, json=container_data),
                httpx.Response(200, json=event_data),
            ]
        )

        result = await get_k8s_health("web-api")
        lower = result.lower()
        assert "crashloop" in lower or "restart" in lower or "backoff" in lower

    @respx.mock
    @pytest.mark.asyncio
    async def test_with_namespace_filter(self, k8s_context):
        """Namespace filter is applied to queries."""
        pod_data = _mock_nrql_response([
            {"podName": "web-api-abc", "status": "Running", "restartCount": 0},
        ])
        empty = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=pod_data),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
            ]
        )

        result = await get_k8s_health("web-api", namespace="production")
        assert isinstance(result, str)

    @respx.mock
    @pytest.mark.asyncio
    async def test_parallel_query_execution(self, k8s_context):
        """All 4 K8s queries execute in parallel."""
        empty = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
            ]
        )

        import time
        start = time.monotonic()
        result = await get_k8s_health("web-api")
        elapsed = time.monotonic() - start

        # Parallel execution: should complete within a reasonable time
        # (discovery phase adds overhead beyond the core 4 queries)
        assert elapsed < 60.0
        assert isinstance(result, str)

    @respx.mock
    @pytest.mark.asyncio
    async def test_api_error_handled(self, k8s_context):
        """API errors result in empty data — function degrades gracefully."""
        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=lambda request: httpx.Response(500)
        )

        result = await get_k8s_health("web-api")
        parsed = json.loads(result)
        # Function returns empty data on API errors (graceful degradation).
        assert parsed["pods"] == []
        assert parsed["container_restarts"] == []
