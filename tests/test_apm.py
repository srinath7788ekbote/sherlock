"""
Tests for APM tools (applications, metrics, deployments).
"""

import json

import httpx
import pytest
import respx

from core.context import AccountContext
from tools.apm import get_apm_applications, get_app_metrics, get_deployments


@pytest.fixture
def apm_context(mock_credentials, mock_intelligence, mock_context):
    """Full context for APM tests."""
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


class TestGetApmApplications:
    """Tests for get_apm_applications."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_applications(self, apm_context):
        """Returns APM applications from NerdGraph entity search."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "actor": {
                        "entitySearch": {
                            "results": {
                                "entities": [
                                    {
                                        "guid": "GUID1", "name": "payment-svc-prod",
                                        "alertSeverity": "NOT_ALERTING",
                                        "reporting": True,
                                        "tags": [
                                            {"key": "language", "values": ["java"]},
                                            {"key": "environment", "values": ["prod"]},
                                        ],
                                    },
                                    {
                                        "guid": "GUID2", "name": "auth-service-prod",
                                        "alertSeverity": "CRITICAL",
                                        "reporting": True,
                                        "tags": [
                                            {"key": "language", "values": ["python"]},
                                        ],
                                    },
                                ]
                            }
                        }
                    }
                }
            })
        )

        result = await get_apm_applications()
        parsed = json.loads(result)
        assert parsed["total_applications"] == 2
        names = [a["name"] for a in parsed["applications"]]
        assert "payment-svc-prod" in names
        assert parsed["applications"][0]["language"] == "java"

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_applications(self, apm_context):
        """Handles account with no APM apps."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "actor": {
                        "entitySearch": {
                            "results": {"entities": []}
                        }
                    }
                }
            })
        )

        result = await get_apm_applications()
        parsed = json.loads(result)
        assert parsed["total_applications"] == 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_api_error_handled(self, apm_context):
        """API errors are caught and returned."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(500)
        )

        result = await get_apm_applications()
        parsed = json.loads(result)
        assert "error" in parsed


class TestGetAppMetrics:
    """Tests for get_app_metrics."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_metrics(self, apm_context):
        """Returns metrics for a known APM app."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response([
                {"avg_response_time": 0.045, "throughput": 1500, "error_rate": 0.5}
            ]))
        )

        result = await get_app_metrics("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["app_name"] == "payment-svc-prod"
        assert parsed["metrics"]["avg_response_time"] == 0.045

    @respx.mock
    @pytest.mark.asyncio
    async def test_fuzzy_resolution(self, apm_context):
        """Fuzzy-resolves approximate app name."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response([
                {"avg_response_time": 0.1, "throughput": 500, "error_rate": 1.0}
            ]))
        )

        result = await get_app_metrics("payment-svc")
        parsed = json.loads(result)
        assert parsed["app_name"] == "payment-svc-prod"
        assert "resolved_from" in parsed

    @pytest.mark.asyncio
    async def test_unknown_app_returns_error(self, apm_context):
        """Unknown app name returns error."""
        result = await get_app_metrics("totally-unknown-xyz")
        parsed = json.loads(result)
        assert "error" in parsed

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_metrics(self, apm_context):
        """Handles empty metrics gracefully."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response([]))
        )

        result = await get_app_metrics("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["metrics"] == {}


class TestGetDeployments:
    """Tests for get_deployments."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_deployments(self, apm_context):
        """Returns deployment history."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response([
                {"latest.revision": "v1.2.3", "latest.user": "deployer", "latest.timestamp": 1700000000},
                {"latest.revision": "v1.2.2", "latest.user": "deployer", "latest.timestamp": 1699000000},
            ]))
        )

        result = await get_deployments("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["total_deployments"] == 2
        assert parsed["app_name"] == "payment-svc-prod"

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_deployments(self, apm_context):
        """Handles no deployments gracefully."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response([]))
        )

        result = await get_deployments("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["total_deployments"] == 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_fuzzy_resolution(self, apm_context):
        """Fuzzy-resolves approximate app name."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response([]))
        )

        result = await get_deployments("auth-service")
        parsed = json.loads(result)
        assert parsed["app_name"] == "auth-service-prod"
