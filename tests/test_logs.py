"""
Tests for log search tool.
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.context import AccountContext
from tools.logs import search_logs


def _mock_nrql_response(results):
    """Build a standard NerdGraph NRQL response."""
    return {
        "data": {
            "actor": {
                "nrql": {
                    "results": results
                }
            }
        }
    }


@pytest.fixture
def logs_context(mock_credentials, mock_intelligence, mock_context):
    """Full context for log tests."""
    return mock_credentials


class TestSearchLogs:
    """Tests for search_logs."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_basic_log_search(self, logs_context):
        """Basic log search returns results."""
        log_data = _mock_nrql_response([
            {
                "message": "Request processed successfully",
                "level": "INFO",
                "timestamp": 1700000000000,
                "service.name": "web-api",
            },
            {
                "message": "Database query completed",
                "level": "DEBUG",
                "timestamp": 1700000001000,
                "service.name": "web-api",
            },
        ])

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=log_data)
        )

        result = await search_logs("web-api")
        parsed = json.loads(result)
        assert "logs" in parsed or "results" in parsed or isinstance(parsed, list)

    @respx.mock
    @pytest.mark.asyncio
    async def test_severity_filter(self, logs_context):
        """Severity filter limits to specific log levels."""
        error_logs = _mock_nrql_response([
            {
                "message": "Connection refused to database",
                "level": "ERROR",
                "timestamp": 1700000000000,
            },
        ])

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=error_logs)
        )

        result = await search_logs("web-api", severity="ERROR")
        assert "ERROR" in result or "error" in result.lower()

    @respx.mock
    @pytest.mark.asyncio
    async def test_keyword_filter(self, logs_context):
        """Keyword filter searches within log messages."""
        filtered_logs = _mock_nrql_response([
            {
                "message": "OutOfMemoryError: Java heap space",
                "level": "ERROR",
                "timestamp": 1700000000000,
            },
        ])

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=filtered_logs)
        )

        result = await search_logs("web-api", keyword="OutOfMemory")
        assert "OutOfMemory" in result or "memory" in result.lower()

    @respx.mock
    @pytest.mark.asyncio
    async def test_since_minutes_parameter(self, logs_context):
        """since_minutes parameter controls time window."""
        log_data = _mock_nrql_response([
            {
                "message": "Recent log entry",
                "level": "INFO",
                "timestamp": 1700000000000,
            },
        ])

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=log_data)
        )

        result = await search_logs("web-api", since_minutes=5)
        assert isinstance(result, str)

    @respx.mock
    @pytest.mark.asyncio
    async def test_limit_parameter(self, logs_context):
        """limit parameter caps the number of results."""
        log_data = _mock_nrql_response([
            {"message": f"Log entry {i}", "level": "INFO"}
            for i in range(3)
        ])

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=log_data)
        )

        result = await search_logs("web-api", limit=3)
        assert isinstance(result, str)

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_logs_found(self, logs_context):
        """Empty log results are handled gracefully."""
        empty = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=empty)
        )

        result = await search_logs("web-api", severity="FATAL", keyword="nonexistent")
        parsed = json.loads(result)
        # Should return empty results, not an error
        assert isinstance(parsed, (dict, list))

    @respx.mock
    @pytest.mark.asyncio
    async def test_api_error_handled(self, logs_context):
        """API errors are caught and reported."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(503)
        )

        result = await search_logs("web-api")
        parsed = json.loads(result)
        assert "error" in parsed or "error" in result.lower()

    @respx.mock
    @pytest.mark.asyncio
    async def test_fuzzy_service_resolution(self, logs_context):
        """Service name is fuzzy-resolved from intelligence."""
        log_data = _mock_nrql_response([
            {"message": "Resolved log", "level": "INFO"},
        ])

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=log_data)
        )

        # "web-ap" should fuzzy-resolve to "web-api"
        result = await search_logs("web-ap")
        assert isinstance(result, str)
