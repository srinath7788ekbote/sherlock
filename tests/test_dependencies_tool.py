"""
Tests for tools.dependencies — get_service_dependencies tool.
"""

import json
from unittest.mock import patch

import pytest

from core.context import AccountContext
from core.dependency_graph import DependencyGraph
from tools.dependencies import get_service_dependencies


class TestGetServiceDependencies:
    """Test the get_service_dependencies tool."""

    @pytest.mark.asyncio
    async def test_no_graph_available(self, mock_context):
        """Should return error when no graph exists."""
        with patch("tools.dependencies.load_graph", return_value=None):
            result = await get_service_dependencies("payment-svc-prod")
        data = json.loads(result)
        assert data.get("error") is not None
        assert data["data_available"] is False

    @pytest.mark.asyncio
    async def test_service_found_both_directions(
        self, mock_context, mock_dependency_graph
    ):
        """Should return both upstream and downstream for known service."""
        with patch("tools.dependencies.load_graph", return_value=mock_dependency_graph):
            with patch("tools.dependencies.graph_is_stale", return_value=False):
                result = await get_service_dependencies(
                    "payment-svc-prod", direction="both"
                )
        data = json.loads(result)
        assert data["in_graph"] is True
        assert data["data_available"] is True
        assert "downstream" in data
        assert "upstream" in data
        assert data["downstream"]["count"] == 2  # auth + export
        assert data["upstream"]["count"] == 0  # payment has no callers

    @pytest.mark.asyncio
    async def test_downstream_only(self, mock_context, mock_dependency_graph):
        """Should return only downstream when direction='downstream'."""
        with patch("tools.dependencies.load_graph", return_value=mock_dependency_graph):
            with patch("tools.dependencies.graph_is_stale", return_value=False):
                result = await get_service_dependencies(
                    "payment-svc-prod", direction="downstream"
                )
        data = json.loads(result)
        assert "downstream" in data
        assert "upstream" not in data

    @pytest.mark.asyncio
    async def test_upstream_only(self, mock_context, mock_dependency_graph):
        """Should return only upstream when direction='upstream'."""
        with patch("tools.dependencies.load_graph", return_value=mock_dependency_graph):
            with patch("tools.dependencies.graph_is_stale", return_value=False):
                result = await get_service_dependencies(
                    "export-worker-prod", direction="upstream"
                )
        data = json.loads(result)
        assert "upstream" in data
        assert "downstream" not in data
        assert data["upstream"]["count"] == 2

    @pytest.mark.asyncio
    async def test_include_external(self, mock_context, mock_dependency_graph):
        """Should include external dependencies when asked."""
        with patch("tools.dependencies.load_graph", return_value=mock_dependency_graph):
            with patch("tools.dependencies.graph_is_stale", return_value=False):
                result = await get_service_dependencies(
                    "payment-svc-prod", include_external=True
                )
        data = json.loads(result)
        assert "external" in data["downstream"]
        assert "stripe-api.com" in data["downstream"]["external"]

    @pytest.mark.asyncio
    async def test_service_not_in_graph(self, mock_context, mock_dependency_graph):
        """Should handle service not found in graph."""
        with patch("tools.dependencies.load_graph", return_value=mock_dependency_graph):
            with patch("tools.dependencies.graph_is_stale", return_value=False):
                result = await get_service_dependencies("nonexistent-svc")
        data = json.loads(result)
        assert data["in_graph"] is False
        assert data["data_available"] is False

    @pytest.mark.asyncio
    async def test_health_warning_on_unhealthy_dep(
        self, mock_context, mock_dependency_graph
    ):
        """auth-service-prod → export-worker-prod has 15% error rate and 8000ms latency."""
        with patch("tools.dependencies.load_graph", return_value=mock_dependency_graph):
            with patch("tools.dependencies.graph_is_stale", return_value=False):
                result = await get_service_dependencies(
                    "auth-service-prod", direction="downstream"
                )
        data = json.loads(result)
        downstream_services = data["downstream"]["services"]
        assert len(downstream_services) >= 1
        export_dep = next(
            (s for s in downstream_services if s["service"] == "export-worker-prod"),
            None,
        )
        assert export_dep is not None
        assert "health_warning" in export_dep
        assert "error rate" in export_dep["health_warning"]
        assert "latency" in export_dep["health_warning"]

    @pytest.mark.asyncio
    async def test_graph_metadata_in_response(
        self, mock_context, mock_dependency_graph
    ):
        """Response should include graph metadata."""
        with patch("tools.dependencies.load_graph", return_value=mock_dependency_graph):
            with patch("tools.dependencies.graph_is_stale", return_value=False):
                result = await get_service_dependencies("payment-svc-prod")
        data = json.loads(result)
        meta = data["graph_metadata"]
        assert meta["source"] == "span"
        assert meta["coverage_pct"] == 100.0
        assert meta["total_services"] == 3
        assert meta["total_edges"] == 3
        assert meta["stale"] is False

    @pytest.mark.asyncio
    async def test_transitive_deps_in_response(
        self, mock_context, mock_dependency_graph
    ):
        """Response should include transitive dependency summary."""
        with patch("tools.dependencies.load_graph", return_value=mock_dependency_graph):
            with patch("tools.dependencies.graph_is_stale", return_value=False):
                result = await get_service_dependencies("payment-svc-prod")
        data = json.loads(result)
        assert "transitive_dependencies" in data
        assert data["transitive_dependencies"]["total"] == 2

    @pytest.mark.asyncio
    async def test_max_depth_clamping(self, mock_context, mock_dependency_graph):
        """max_depth should be clamped to 1-5."""
        with patch("tools.dependencies.load_graph", return_value=mock_dependency_graph):
            with patch("tools.dependencies.graph_is_stale", return_value=False):
                # Pass extreme values.
                result = await get_service_dependencies(
                    "payment-svc-prod", max_depth=100
                )
        data = json.loads(result)
        assert data["downstream"]["max_depth"] == 5  # Clamped from 100 to 5

    @pytest.mark.asyncio
    async def test_not_connected(self):
        """Should return error when no account is connected."""
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.clear()
        result = await get_service_dependencies("svc")
        data = json.loads(result)
        assert "error" in data
        AccountContext.reset_singleton()
