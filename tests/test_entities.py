"""
Tests for entity GUID resolution tool.
"""

import json

import httpx
import pytest
import respx

from core.context import AccountContext
from tools.entities import get_entity_guid


@pytest.fixture
def entity_context(mock_credentials, mock_intelligence, mock_context):
    """Full context for entity tests."""
    return mock_credentials


class TestGetEntityGuid:
    """Tests for get_entity_guid."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_resolves_entity(self, entity_context):
        """Resolves an entity name to GUID."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "actor": {
                        "entitySearch": {
                            "results": {
                                "entities": [
                                    {
                                        "guid": "GUID-123",
                                        "name": "payment-svc-prod",
                                        "type": "APPLICATION",
                                        "domain": "APM",
                                        "alertSeverity": "NOT_ALERTING",
                                    }
                                ]
                            }
                        }
                    }
                }
            })
        )

        result = await get_entity_guid("payment-svc-prod")
        parsed = json.loads(result)
        assert parsed["matches"] == 1
        assert parsed["entities"][0]["guid"] == "GUID-123"
        assert parsed["entities"][0]["domain"] == "APM"

    @respx.mock
    @pytest.mark.asyncio
    async def test_entity_not_found(self, entity_context):
        """Returns error when entity not found."""
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

        result = await get_entity_guid("nonexistent-entity")
        parsed = json.loads(result)
        assert "error" in parsed

    @respx.mock
    @pytest.mark.asyncio
    async def test_domain_filter(self, entity_context):
        """Domain filter is applied to the query."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "actor": {
                        "entitySearch": {
                            "results": {
                                "entities": [
                                    {
                                        "guid": "SYNTH-GUID",
                                        "name": "Login Flow",
                                        "type": "MONITOR",
                                        "domain": "SYNTH",
                                        "alertSeverity": None,
                                    }
                                ]
                            }
                        }
                    }
                }
            })
        )

        result = await get_entity_guid("Login Flow", domain="SYNTH")
        parsed = json.loads(result)
        assert parsed["matches"] == 1
        assert parsed["entities"][0]["domain"] == "SYNTH"

    @respx.mock
    @pytest.mark.asyncio
    async def test_multiple_matches(self, entity_context):
        """Returns multiple matching entities."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "actor": {
                        "entitySearch": {
                            "results": {
                                "entities": [
                                    {"guid": "G1", "name": "my-service", "type": "APPLICATION", "domain": "APM", "alertSeverity": ""},
                                    {"guid": "G2", "name": "my-service", "type": "HOST", "domain": "INFRA", "alertSeverity": ""},
                                ]
                            }
                        }
                    }
                }
            })
        )

        result = await get_entity_guid("my-service")
        parsed = json.loads(result)
        assert parsed["matches"] == 2

    @respx.mock
    @pytest.mark.asyncio
    async def test_api_error_handled(self, entity_context):
        """API errors are caught and returned."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(500)
        )

        result = await get_entity_guid("anything")
        parsed = json.loads(result)
        assert "error" in parsed
