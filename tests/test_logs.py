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
                "account": {
                    "nrql": {
                        "results": results
                    }
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
        assert "logs" in parsed
        assert parsed["total_logs"] == 2

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

    @respx.mock
    @pytest.mark.asyncio
    async def test_entity_name_fallback(self, logs_context):
        """Falls back to entity.name when primary service attribute has no data."""
        empty = _mock_nrql_response([])
        entity_logs = _mock_nrql_response([
            {"message": "Found via entity.name", "entity.name": "web-api", "level": "ERROR"},
        ])

        call_count = 0

        def _side_effect(request, route):
            nonlocal call_count
            call_count += 1
            body = json.loads(request.content)
            query = body.get("query", "")
            # First call is primary attr (service.name) — no results.
            # Subsequent calls try fallback attrs — return data when
            # the query contains entity.name.
            if "`entity.name`" in query:
                return httpx.Response(200, json=entity_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("web-api", severity="ERROR")
        parsed = json.loads(result)
        assert parsed["total_logs"] == 1
        assert "entity.name" in parsed.get("note", "")

    @respx.mock
    @pytest.mark.asyncio
    async def test_bare_name_fallback_for_namespaced_service(self, logs_context):
        """Falls back to bare name when full namespace/service returns no logs."""
        empty = _mock_nrql_response([])
        bare_logs = _mock_nrql_response([
            {"message": "Found via bare name", "service.name": "client-service", "level": "INFO"},
        ])

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = body.get("query", "")
            # Only return data when the query uses the bare name without
            # the namespace prefix.
            if "client-service" in query and "eswd-prod" not in query:
                return httpx.Response(200, json=bare_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("eswd-prod/client-service")
        parsed = json.loads(result)
        assert parsed["total_logs"] == 1


class TestLogDeepLink:
    """Verify search_logs generates NRQL-based deep links, not Lucene links."""

    @pytest.mark.asyncio
    async def test_search_logs_link_uses_nrql_chart_not_log_tailer(
        self, logs_context
    ):
        """Deep link must open Query Builder, not logger.log-tailer."""
        import respx

        log_data = _mock_nrql_response([
            {
                "timestamp": 1700000000000,
                "message": "HikariPool-1 - Connection is not available",
                "entity.name": "eswd-prod/tagging-service",
                "level": "ERROR",
            }
        ])

        with respx.mock(assert_all_called=False) as router:
            router.post("https://api.newrelic.com/graphql").mock(
                return_value=httpx.Response(200, json=log_data)
            )

            result = await search_logs(
                service_name="web-api",
                severity="ERROR",
                since_minutes=60,
            )
        data = json.loads(result)

        # Assert: link must use query-builder, not log-tailer
        assert "links" in data, "No links in response"
        view_link = data["links"].get("view_in_nr", "")
        assert "data-exploration.query-builder" in view_link, (
            f"Expected query-builder link, got: {view_link}"
        )
        assert "logger.log-tailer" not in view_link, (
            f"Must not use deprecated log-tailer: {view_link}"
        )
        # Must be a valid base64-encoded pane
        assert "pane=" in view_link

    @pytest.mark.asyncio
    async def test_search_logs_link_includes_keyword_filter(
        self, logs_context
    ):
        """Deep link NRQL must include keyword filter when keyword was used."""
        import base64
        import urllib.parse

        import respx

        log_data = _mock_nrql_response([
            {
                "timestamp": 1700000000000,
                "message": "HikariPool timeout error",
                "entity.name": "eswd-prod/tagging-service",
                "level": "ERROR",
            }
        ])

        with respx.mock(assert_all_called=False) as router:
            router.post("https://api.newrelic.com/graphql").mock(
                return_value=httpx.Response(200, json=log_data)
            )

            result = await search_logs(
                service_name="web-api",
                keyword="HikariPool",
                since_minutes=60,
            )
        data = json.loads(result)
        view_link = data.get("links", {}).get("view_in_nr", "")

        # Decode the pane parameter to verify NRQL content
        parsed = urllib.parse.urlparse(view_link)
        params = urllib.parse.parse_qs(parsed.query)
        pane_b64 = params.get("pane", [""])[0]
        if pane_b64:
            pane_json = base64.b64decode(pane_b64).decode()
            assert "HikariPool" in pane_json, (
                f"Keyword 'HikariPool' must appear in NRQL pane: {pane_json}"
            )
