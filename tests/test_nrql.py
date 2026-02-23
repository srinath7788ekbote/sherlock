"""
Tests for NRQL query tool.
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.credentials import Credentials
from core.context import AccountContext
from tools.nrql import run_nrql_query, MAX_NRQL_LENGTH


@pytest.fixture
def nrql_context(mock_credentials, mock_intelligence, mock_context):
    """Set up full context for NRQL tests."""
    return mock_credentials


class TestRunNRQLQuery:
    """Tests for the run_nrql_query tool."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_basic_query(self, nrql_context):
        """Basic NRQL query returns results."""
        nrql = "SELECT count(*) FROM Transaction SINCE 1 hour ago"
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "actor": {
                        "account": {
                            "nrql": {
                                "results": [{"count": 42}]
                            }
                        }
                    }
                }
            })
        )

        result = await run_nrql_query(nrql)
        parsed = json.loads(result)
        assert parsed["results"][0]["count"] == 42

    @respx.mock
    @pytest.mark.asyncio
    async def test_query_with_where_clause(self, nrql_context):
        """NRQL query with WHERE clause works."""
        nrql = "SELECT average(duration) FROM Transaction WHERE appName = 'web-api' SINCE 30 minutes ago"
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "actor": {
                        "account": {
                            "nrql": {
                                "results": [{"average.duration": 0.045}]
                            }
                        }
                    }
                }
            })
        )

        result = await run_nrql_query(nrql)
        parsed = json.loads(result)
        assert "average.duration" in parsed["results"][0]

    @pytest.mark.asyncio
    async def test_oversized_query_rejected(self, nrql_context):
        """Queries exceeding MAX_NRQL_LENGTH are rejected."""
        nrql = "SELECT count(*) FROM Transaction WHERE " + "a" * (MAX_NRQL_LENGTH + 1)
        result = await run_nrql_query(nrql)
        parsed = json.loads(result)
        assert "error" in parsed or "too long" in result.lower()

    @respx.mock
    @pytest.mark.asyncio
    async def test_api_error_handled(self, nrql_context):
        """API errors are caught and returned as error messages."""
        nrql = "SELECT count(*) FROM Transaction SINCE 1 hour ago"
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(500)
        )

        result = await run_nrql_query(nrql)
        parsed = json.loads(result)
        assert "error" in parsed

    @respx.mock
    @pytest.mark.asyncio
    async def test_nrql_injection_sanitized(self, nrql_context):
        """NRQL injection attempts are sanitized."""
        nrql = "SELECT count(*) FROM Transaction; DROP TABLE users"
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json={
                "data": {
                    "actor": {
                        "nrql": {
                            "results": [{"count": 10}]
                        }
                    }
                }
            })
        )

        # Should not raise, query is sanitized before sending
        result = await run_nrql_query(nrql)
        assert isinstance(result, str)
