"""
Tests for alert and incident tools.
"""

import json

import httpx
import pytest
import respx

from core.context import AccountContext
from tools.alerts import get_alerts, get_incidents, get_service_incidents


@pytest.fixture
def alert_context(mock_credentials, mock_intelligence, mock_context):
    """Full context for alert tests."""
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


class TestGetAlerts:
    """Tests for get_alerts."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_policies(self, alert_context):
        """Returns alert policies from NerdGraph."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "actor": {
                        "account": {
                            "alerts": {
                                "policiesSearch": {
                                    "policies": [
                                        {"id": "1", "name": "High CPU", "incidentPreference": "PER_POLICY"},
                                        {"id": "2", "name": "Error Rate", "incidentPreference": "PER_CONDITION"},
                                    ]
                                }
                            }
                        }
                    }
                }
            })
        )

        result = await get_alerts()
        parsed = json.loads(result)
        assert parsed["total_policies"] == 2
        assert parsed["policies"][0]["name"] == "High CPU"

    @respx.mock
    @pytest.mark.asyncio
    async def test_empty_policies(self, alert_context):
        """Handles account with no alert policies."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "actor": {
                        "account": {
                            "alerts": {
                                "policiesSearch": {"policies": []}
                            }
                        }
                    }
                }
            })
        )

        result = await get_alerts()
        parsed = json.loads(result)
        assert parsed["total_policies"] == 0

    @respx.mock
    @pytest.mark.asyncio
    async def test_api_error_handled(self, alert_context):
        """API errors are caught and returned."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(500)
        )

        result = await get_alerts()
        parsed = json.loads(result)
        assert "error" in parsed


class TestGetIncidents:
    """Tests for get_incidents."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_open_incidents(self, alert_context):
        """Returns open incidents."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response([
                {"incidentId": "INC1", "latest.event": "open", "latest.priority": "CRITICAL"},
                {"incidentId": "INC2", "latest.event": "open", "latest.priority": "WARNING"},
            ]))
        )

        result = await get_incidents("open")
        parsed = json.loads(result)
        assert parsed["total_incidents"] == 2
        assert parsed["state_filter"] == "open"

    @respx.mock
    @pytest.mark.asyncio
    async def test_closed_incidents(self, alert_context):
        """Returns closed incidents."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response([
                {"incidentId": "INC1", "latest.event": "closed"},
            ]))
        )

        result = await get_incidents("closed")
        parsed = json.loads(result)
        assert parsed["total_incidents"] == 1
        assert parsed["state_filter"] == "closed"

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_incidents(self, alert_context):
        """Handles zero incidents gracefully."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response([]))
        )

        result = await get_incidents("open")
        parsed = json.loads(result)
        assert parsed["total_incidents"] == 0


class TestGetServiceIncidents:
    """Tests for get_service_incidents."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_returns_service_incidents(self, alert_context):
        """Returns incidents for a known service."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response([
                {"incidentId": "INC1", "latest.event": "open", "latest.policyName": "Payment Service - Critical"},
            ]))
        )

        result = await get_service_incidents("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["total_incidents"] == 1
        assert parsed["service_name"] == "payment-svc-prod"

    @respx.mock
    @pytest.mark.asyncio
    async def test_fuzzy_resolves_service_name(self, alert_context):
        """Fuzzy resolution works for approximate names."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response([]))
        )

        result = await get_service_incidents("payment-svc")
        parsed = json.loads(result)
        assert parsed["service_name"] == "payment-svc-prod"

    @respx.mock
    @pytest.mark.asyncio
    async def test_unknown_service_still_queries(self, alert_context):
        """Unknown service name is still used for the query (no hard failure)."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_mock_nrql_response([]))
        )

        result = await get_service_incidents("totally-unknown-xyz")
        parsed = json.loads(result)
        assert parsed["total_incidents"] == 0
