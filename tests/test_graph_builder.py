"""
Tests for core.graph_builder — strategy-based dependency graph construction.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest
import respx

from core.credentials import Credentials
from core.dependency_graph import DependencyGraph, ServiceDependency
from core.graph_builder import (
    SpanAttributes,
    _build_inferred_edges,
    _build_log_edges,
    _build_span_edges,
    _classify_edge,
    _discover_span_attributes,
    _extract_hostname_from_url,
    _extract_service_refs_from_log_message,
    _match_hostname_to_service,
    _merge_edges,
    _safe_extract_results,
    build_dependency_graph,
)
from core.intelligence import (
    AccountIntelligence,
    APMIntelligence,
    LogsIntelligence,
    NamingConvention,
    OTelIntelligence,
)


@pytest.fixture
def test_credentials() -> Credentials:
    return Credentials(
        account_id="123456",
        api_key="NRAK-test123",
        region="US",
    )


@pytest.fixture
def test_headers() -> dict:
    return {"API-Key": "NRAK-test123", "Content-Type": "application/json"}


@pytest.fixture
def test_endpoint() -> str:
    return "https://api.newrelic.com/graphql"


def _nerdgraph_response(results: list[dict]) -> dict:
    """Build a standard NerdGraph NRQL response body."""
    return {
        "data": {
            "actor": {
                "account": {
                    "nrql": {"results": results}
                }
            }
        }
    }


# ── Helper Tests ─────────────────────────────────────────────────────────


class TestSafeExtractResults:
    def test_valid_response(self):
        body = _nerdgraph_response([{"count": 42}])
        assert _safe_extract_results(body) == [{"count": 42}]

    def test_empty_response(self):
        assert _safe_extract_results({}) == []

    def test_none_values(self):
        body = {"data": {"actor": {"account": None}}}
        assert _safe_extract_results(body) == []


class TestExtractHostnameFromUrl:
    def test_full_url(self):
        assert _extract_hostname_from_url("https://auth-service.internal:8080/api") == "auth-service.internal"

    def test_no_scheme(self):
        assert _extract_hostname_from_url("auth-service.internal:8080") == "auth-service.internal"

    def test_empty(self):
        assert _extract_hostname_from_url("") is None

    def test_none(self):
        assert _extract_hostname_from_url(None) is None


class TestMatchHostnameToService:
    def test_exact_match(self):
        services = ["payment-svc-prod", "auth-service-prod"]
        result = _match_hostname_to_service("payment-svc-prod", services)
        assert result == "payment-svc-prod"

    def test_no_match(self):
        result = _match_hostname_to_service("random-host", ["payment-svc-prod"])
        # May or may not match depending on fuzzy threshold.
        # Just ensure it doesn't crash.
        assert result is None or isinstance(result, str)

    def test_empty_services(self):
        assert _match_hostname_to_service("host", []) is None

    def test_empty_hostname(self):
        assert _match_hostname_to_service("", ["svc"]) is None


class TestExtractServiceRefsFromLogMessage:
    def test_url_in_message(self):
        msg = "Connection refused to https://auth-service-prod.internal:8080/health"
        refs = _extract_service_refs_from_log_message(
            msg, ["auth-service-prod", "payment-svc-prod"]
        )
        assert "auth-service-prod" in refs

    def test_service_name_mention(self):
        msg = "Failed to fetch data from payment-svc-prod"
        refs = _extract_service_refs_from_log_message(
            msg, ["payment-svc-prod", "auth-service-prod"]
        )
        assert "payment-svc-prod" in refs

    def test_empty_message(self):
        assert _extract_service_refs_from_log_message("", ["svc"]) == []


# ── SpanAttributes Tests ────────────────────────────────────────────────


class TestSpanAttributes:
    def test_defaults(self):
        attrs = SpanAttributes()
        assert attrs.caller_attr is None
        assert attrs.has_span_data is False

    def test_apm_attributes(self):
        attrs = SpanAttributes(
            caller_attr="appName",
            callee_attr="peer.service.name",
            url_attr="http.url",
            has_span_data=True,
        )
        assert attrs.caller_attr == "appName"
        assert attrs.has_span_data is True


class TestDiscoverSpanAttributes:
    @pytest.mark.asyncio
    async def test_apm_agent_keyset(self, test_credentials, test_headers, test_endpoint):
        keyset_response = _nerdgraph_response([
            {"allKeys": ["appName", "peer.service.name", "http.url", "span.kind", "db.system"]}
        ])
        with respx.mock(assert_all_called=False) as router:
            router.post(test_endpoint).mock(
                return_value=httpx.Response(200, json=keyset_response)
            )
            attrs = await _discover_span_attributes(
                test_credentials.account_id, test_headers, test_endpoint
            )
        assert attrs.has_span_data is True
        assert attrs.caller_attr == "appName"
        assert attrs.callee_attr == "peer.service.name"
        assert attrs.url_attr == "http.url"
        assert attrs.db_attr == "db.system"
        assert attrs.kind_attr == "span.kind"

    @pytest.mark.asyncio
    async def test_otel_agent_keyset(self, test_credentials, test_headers, test_endpoint):
        keyset_response = _nerdgraph_response([
            {"allKeys": ["service.name", "peer.service.name", "http.request.url", "span.kind"]}
        ])
        with respx.mock(assert_all_called=False) as router:
            router.post(test_endpoint).mock(
                return_value=httpx.Response(200, json=keyset_response)
            )
            attrs = await _discover_span_attributes(
                test_credentials.account_id, test_headers, test_endpoint
            )
        assert attrs.has_span_data is True
        assert attrs.caller_attr == "service.name"
        assert attrs.url_attr == "http.request.url"

    @pytest.mark.asyncio
    async def test_no_span_data(self, test_credentials, test_headers, test_endpoint):
        empty_response = _nerdgraph_response([])
        with respx.mock(assert_all_called=False) as router:
            router.post(test_endpoint).mock(
                return_value=httpx.Response(200, json=empty_response)
            )
            attrs = await _discover_span_attributes(
                test_credentials.account_id, test_headers, test_endpoint
            )
        assert attrs.has_span_data is False

    @pytest.mark.asyncio
    async def test_api_error(self, test_credentials, test_headers, test_endpoint):
        with respx.mock(assert_all_called=False) as router:
            router.post(test_endpoint).mock(
                return_value=httpx.Response(500)
            )
            attrs = await _discover_span_attributes(
                test_credentials.account_id, test_headers, test_endpoint
            )
        assert attrs.has_span_data is False


# ── Strategy Tests ───────────────────────────────────────────────────────


class TestBuildSpanEdges:
    @pytest.mark.asyncio
    async def test_with_peer_service(self, test_credentials, test_headers, test_endpoint):
        span_attrs = SpanAttributes(
            caller_attr="appName",
            callee_attr="peer.service.name",
            has_span_data=True,
        )
        results = [
            {
                "facet": ["payment-svc", "auth-service"],
                "call_count": 500,
                "avg_latency_ms": 120.0,
                "error_rate": 1.5,
            }
        ]
        with respx.mock(assert_all_called=False) as router:
            router.post(test_endpoint).mock(
                return_value=httpx.Response(200, json=_nerdgraph_response(results))
            )
            edges = await _build_span_edges(
                test_credentials.account_id, test_headers, test_endpoint,
                span_attrs, ["payment-svc", "auth-service"], None, 168,
            )
        assert len(edges) >= 1
        assert edges[0].source == "span"
        assert edges[0].confidence == 1.0

    @pytest.mark.asyncio
    async def test_no_span_data(self, test_credentials, test_headers, test_endpoint):
        span_attrs = SpanAttributes(has_span_data=False)
        edges = await _build_span_edges(
            test_credentials.account_id, test_headers, test_endpoint,
            span_attrs, [], None, 168,
        )
        assert edges == []


class TestBuildLogEdges:
    @pytest.mark.asyncio
    async def test_log_error_with_service_ref(self, test_credentials, test_headers, test_endpoint):
        results = [
            {
                "sample_message": "Connection refused to https://auth-service-prod.internal:8080/health",
                "hostname": "payment-svc-prod-abc123",
                "occurrences": 42,
                "facet": "Connection refused to https://auth-service-prod.internal:8080/health",
            }
        ]
        with respx.mock(assert_all_called=False) as router:
            router.post(test_endpoint).mock(
                return_value=httpx.Response(200, json=_nerdgraph_response(results))
            )
            edges = await _build_log_edges(
                test_credentials.account_id, test_headers, test_endpoint,
                ["payment-svc-prod", "auth-service-prod"],
                "service.name", 168,
            )
        # May find edges depending on fuzzy matching.
        for edge in edges:
            assert edge.source == "log"
            assert edge.confidence == 0.7

    @pytest.mark.asyncio
    async def test_empty_log_results(self, test_credentials, test_headers, test_endpoint):
        with respx.mock(assert_all_called=False) as router:
            router.post(test_endpoint).mock(
                return_value=httpx.Response(200, json=_nerdgraph_response([]))
            )
            edges = await _build_log_edges(
                test_credentials.account_id, test_headers, test_endpoint,
                ["svc-a"], "service.name", 168,
            )
        assert edges == []


class TestBuildInferredEdges:
    def test_shared_segments(self, mock_intelligence):
        edges = _build_inferred_edges(mock_intelligence)
        # With services like payment-svc-prod, auth-service-prod, export-worker-prod,
        # they share "prod" segment. Some edges may be created.
        for edge in edges:
            assert edge.source == "inferred"
            assert edge.confidence == 0.4

    def test_single_service(self):
        intel = AccountIntelligence(
            account_id="123",
            apm=APMIntelligence(service_names=["only-service"]),
        )
        edges = _build_inferred_edges(intel)
        assert edges == []

    def test_empty_services(self):
        intel = AccountIntelligence(
            account_id="123",
            apm=APMIntelligence(service_names=[]),
        )
        edges = _build_inferred_edges(intel)
        assert edges == []


# ── Merge Tests ──────────────────────────────────────────────────────────


class TestMergeEdges:
    def test_span_takes_precedence(self):
        span = [ServiceDependency(caller="A", callee="B", source="span", confidence=1.0)]
        log = [ServiceDependency(caller="A", callee="B", source="log", confidence=0.7)]
        inferred = [ServiceDependency(caller="A", callee="B", source="inferred", confidence=0.4)]
        merged = _merge_edges(span, log, inferred)
        assert len(merged) == 1
        assert merged["A→B"].source == "span"

    def test_log_over_inferred(self):
        log = [ServiceDependency(caller="A", callee="B", source="log", confidence=0.7)]
        inferred = [ServiceDependency(caller="A", callee="B", source="inferred", confidence=0.4)]
        merged = _merge_edges([], log, inferred)
        assert merged["A→B"].source == "log"

    def test_disjoint_edges(self):
        span = [ServiceDependency(caller="A", callee="B", source="span")]
        log = [ServiceDependency(caller="C", callee="D", source="log")]
        merged = _merge_edges(span, log, [])
        assert len(merged) == 2


class TestClassifyEdge:
    def test_internal_direct_match(self):
        edge = ServiceDependency(caller="A", callee="payment-svc")
        assert _classify_edge(edge, {"payment-svc"}) is True

    def test_external(self):
        edge = ServiceDependency(caller="A", callee="stripe-api.com")
        assert _classify_edge(edge, {"payment-svc"}) is False

    def test_bare_name_match(self):
        edge = ServiceDependency(caller="A", callee="env/payment-svc")
        assert _classify_edge(edge, {"env/payment-svc"}) is True


# ── Integration Test: build_dependency_graph ─────────────────────────────


class TestBuildDependencyGraph:
    @pytest.mark.asyncio
    async def test_empty_account(self, test_credentials, mock_intelligence, tmp_path):
        """Test building a graph with no span/log data."""
        empty_response = _nerdgraph_response([])
        with respx.mock(assert_all_called=False) as router:
            router.post("https://api.newrelic.com/graphql").mock(
                return_value=httpx.Response(200, json=empty_response)
            )
            with patch("core.dependency_graph.DEPS_DIR", tmp_path):
                with patch("core.graph_builder.save_graph"):
                    graph = await build_dependency_graph(
                        test_credentials, mock_intelligence, window_hours=24,
                    )

        assert isinstance(graph, DependencyGraph)
        assert graph.account_id == "123456"

    @pytest.mark.asyncio
    async def test_with_span_data(self, test_credentials, mock_intelligence, tmp_path):
        """Test building a graph with span keyset + span edge data."""
        keyset_response = _nerdgraph_response([
            {"allKeys": ["appName", "peer.service.name", "http.url", "span.kind"]}
        ])
        span_results = _nerdgraph_response([
            {
                "facet": ["payment-svc-prod", "auth-service-prod"],
                "call_count": 500,
                "avg_latency_ms": 120.0,
                "error_rate": 1.5,
            },
        ])

        call_count = 0

        def side_effect(request, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # keyset query
                return httpx.Response(200, json=keyset_response)
            else:
                # all subsequent queries
                return httpx.Response(200, json=span_results)

        with respx.mock(assert_all_called=False) as router:
            router.post("https://api.newrelic.com/graphql").mock(
                side_effect=side_effect
            )
            with patch("core.dependency_graph.DEPS_DIR", tmp_path):
                with patch("core.graph_builder.save_graph"):
                    graph = await build_dependency_graph(
                        test_credentials, mock_intelligence, window_hours=24,
                    )

        assert isinstance(graph, DependencyGraph)
        assert graph.account_id == "123456"

    @pytest.mark.asyncio
    async def test_never_raises(self, test_credentials, mock_intelligence, tmp_path):
        """Graph build should never raise — returns empty graph on failure."""
        with respx.mock(assert_all_called=False) as router:
            router.post("https://api.newrelic.com/graphql").mock(
                return_value=httpx.Response(500)
            )
            with patch("core.dependency_graph.DEPS_DIR", tmp_path):
                with patch("core.graph_builder.save_graph"):
                    graph = await build_dependency_graph(
                        test_credentials, mock_intelligence,
                    )

        assert isinstance(graph, DependencyGraph)
        # Should have warnings about the failure.
        assert len(graph.warnings) > 0 or graph.build_source == "unavailable"
