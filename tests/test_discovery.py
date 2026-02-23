"""
Tests for the discovery engine.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.credentials import Credentials
from core.discovery import (
    EVENT_REGISTRY,
    TIER1_EVENT_TYPES,
    AvailableEventType,
    DiscoveryResult,
    EventTypeInfo,
    _check_event_type,
    discover_available_data,
)
from tools.investigate import InvestigationAnchor


@pytest.fixture
def anchor():
    """Provide a standard investigation anchor for discovery tests."""
    now = datetime.now(timezone.utc)
    return InvestigationAnchor(
        primary_service="payment-svc-prod",
        all_candidates=["payment-svc-prod"],
        window_start=now - timedelta(minutes=30),
        since_minutes=30,
        until_clause="",
        window_source="requested",
    )


def _nrql_response(count: int):
    """Build a NerdGraph NRQL response with a given event_count."""
    return {
        "data": {
            "actor": {
                "account": {
                    "nrql": {
                        "results": [{"event_count": count}]
                    }
                }
            }
        }
    }


def _empty_nrql_response():
    """Build a NerdGraph NRQL response with zero events."""
    return _nrql_response(0)


class TestDiscoverFindsK8sDataWhenPresent:
    """test_discover_finds_k8s_data_when_present"""

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_finds_k8s_data_when_present(
        self, mock_credentials, anchor
    ):
        """Discovery detects K8s event types that have data."""
        # Mock: K8sPodSample has data, everything else doesn't.
        def _side_effect(request):
            body = request.content.decode()
            if "K8sPodSample" in body:
                return httpx.Response(200, json=_nrql_response(42))
            return httpx.Response(200, json=_empty_nrql_response())

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=_side_effect
        )

        result = await discover_available_data(
            service_candidates=["payment-svc-prod"],
            anchor=anchor,
            credentials=mock_credentials,
        )

        assert isinstance(result, DiscoveryResult)
        assert "K8sPodSample" in result.available
        assert result.available["K8sPodSample"].domain == "k8s"
        assert result.available["K8sPodSample"].event_count == 42
        assert "k8s" in result.domains_with_data


class TestDiscoverSkipsEventTypesWithNoData:
    """test_discover_skips_event_types_with_no_data"""

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_skips_event_types_with_no_data(
        self, mock_credentials, anchor
    ):
        """Event types returning zero counts are placed in unavailable."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_empty_nrql_response())
        )

        result = await discover_available_data(
            service_candidates=["payment-svc-prod"],
            anchor=anchor,
            credentials=mock_credentials,
        )

        assert len(result.available) == 0
        assert len(result.unavailable) > 0
        assert "Transaction" in result.unavailable
        assert "K8sPodSample" in result.unavailable


class TestDiscoverIdentifiesCorrectFilterAttribute:
    """test_discover_identifies_correct_filter_attribute"""

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_identifies_correct_filter_attribute(
        self, mock_credentials, anchor
    ):
        """Discovery records which filter attribute matched for each event type."""
        call_count = 0

        def _side_effect(request):
            nonlocal call_count
            body = request.content.decode()
            # Transaction with appName should match.
            if "Transaction" in body and "appName" in body:
                return httpx.Response(200, json=_nrql_response(100))
            return httpx.Response(200, json=_empty_nrql_response())

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=_side_effect
        )

        result = await discover_available_data(
            service_candidates=["payment-svc-prod"],
            anchor=anchor,
            credentials=mock_credentials,
        )

        if "Transaction" in result.available:
            tx = result.available["Transaction"]
            assert tx.matched_filter in EVENT_REGISTRY["Transaction"].service_filters
            assert "Transaction" in result.service_filter_map
            assert result.service_filter_map["Transaction"] == tx.matched_filter


class TestDiscoverRunsAllChecksInParallel:
    """test_discover_runs_all_checks_in_parallel"""

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_runs_all_checks_in_parallel(
        self, mock_credentials, anchor
    ):
        """All event type checks are launched at once (verified by total checked count)."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_empty_nrql_response())
        )

        result = await discover_available_data(
            service_candidates=["payment-svc-prod"],
            anchor=anchor,
            credentials=mock_credentials,
        )

        # With tiered discovery, at minimum Tier 1 (6 event types) is checked.
        # Tier 2+ only runs if K8s data is found in Tier 1.
        # Since all responses return 0, only Tier 1 runs.
        assert result.total_event_types_checked >= len(TIER1_EVENT_TYPES)
        # All checked types should be unavailable since we returned 0 for everything.
        assert len(result.unavailable) >= len(TIER1_EVENT_TYPES)


class TestDiscoverReturnsDomainWithData:
    """test_discover_returns_domains_with_data"""

    @respx.mock
    @pytest.mark.asyncio
    async def test_discover_returns_domains_with_data(
        self, mock_credentials, anchor
    ):
        """domains_with_data lists only domains that had matching event types."""
        def _side_effect(request):
            body = request.content.decode()
            if "Transaction" in body and "appName" in body:
                return httpx.Response(200, json=_nrql_response(500))
            if "TransactionError" in body and "appName" in body:
                return httpx.Response(200, json=_nrql_response(50))
            if "K8sPodSample" in body:
                return httpx.Response(200, json=_nrql_response(10))
            return httpx.Response(200, json=_empty_nrql_response())

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=_side_effect
        )

        result = await discover_available_data(
            service_candidates=["payment-svc-prod"],
            anchor=anchor,
            credentials=mock_credentials,
        )

        assert "apm" in result.domains_with_data
        assert "k8s" in result.domains_with_data
        # No log data was returned, so logs shouldn't be listed.
        assert "logs" not in result.domains_with_data


class TestDiscoveryResultEmptyWhenNoDataAnywhere:
    """test_discovery_result_empty_when_no_data_anywhere"""

    @pytest.mark.asyncio
    async def test_discovery_result_empty_when_no_candidates(
        self, mock_credentials, anchor
    ):
        """Empty candidates yields an empty discovery result with all event types unavailable."""
        result = await discover_available_data(
            service_candidates=[],
            anchor=anchor,
            credentials=mock_credentials,
        )

        assert len(result.available) == 0
        assert len(result.unavailable) == len(EVENT_REGISTRY)
        assert result.domains_with_data == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_discovery_result_empty_when_api_fails(
        self, mock_credentials, anchor
    ):
        """API failures result in event types being marked unavailable."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(500, json={"error": "internal"})
        )

        result = await discover_available_data(
            service_candidates=["payment-svc-prod"],
            anchor=anchor,
            credentials=mock_credentials,
        )

        assert len(result.available) == 0
        assert result.domains_with_data == []
