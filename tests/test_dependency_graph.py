"""
Tests for core.dependency_graph — models, traversal, persistence, and cycle detection.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from core.dependency_graph import (
    DEPS_DIR,
    DependencyGraph,
    DependencyNode,
    ServiceDependency,
    build_transitive_dependencies,
    find_path,
    get_dependencies,
    get_dependents,
    graph_is_stale,
    load_graph,
    save_graph,
)


# ── Model Tests ──────────────────────────────────────────────────────────


class TestServiceDependency:
    """ServiceDependency model tests."""

    def test_defaults(self):
        dep = ServiceDependency(caller="A", callee="B")
        assert dep.caller == "A"
        assert dep.callee == "B"
        assert dep.call_count == 0
        assert dep.error_rate == 0.0
        assert dep.avg_latency_ms == 0.0
        assert dep.source == "span"
        assert dep.confidence == 1.0
        assert dep.last_seen is not None

    def test_custom_values(self):
        dep = ServiceDependency(
            caller="svc-a",
            callee="svc-b",
            call_count=1500,
            error_rate=5.2,
            avg_latency_ms=320.0,
            source="log",
            confidence=0.7,
        )
        assert dep.call_count == 1500
        assert dep.error_rate == 5.2
        assert dep.source == "log"
        assert dep.confidence == 0.7


class TestDependencyNode:
    """DependencyNode model tests."""

    def test_defaults(self):
        node = DependencyNode(service_name="test-svc")
        assert node.service_name == "test-svc"
        assert node.direct_dependencies == []
        assert node.direct_dependents == []
        assert node.transitive_dependencies == []
        assert node.dependency_details == {}

    def test_with_dependencies(self):
        dep = ServiceDependency(caller="A", callee="B", call_count=100)
        node = DependencyNode(
            service_name="A",
            direct_dependencies=["B"],
            dependency_details={"B": dep},
        )
        assert "B" in node.direct_dependencies
        assert node.dependency_details["B"].call_count == 100


class TestDependencyGraph:
    """DependencyGraph model tests."""

    def test_defaults(self):
        g = DependencyGraph(account_id="123")
        assert g.account_id == "123"
        assert g.total_services == 0
        assert g.total_edges == 0
        assert g.nodes == {}
        assert g.build_source == "unavailable"
        assert g.coverage_pct == 0.0
        assert g.warnings == []
        assert g.external_dependencies == {}

    def test_from_fixture(self, mock_dependency_graph):
        g = mock_dependency_graph
        assert g.account_id == "123456"
        assert g.total_services == 3
        assert g.total_edges == 3
        assert g.build_source == "span"
        assert g.coverage_pct == 100.0
        assert "payment-svc-prod" in g.nodes
        assert "auth-service-prod" in g.nodes
        assert "export-worker-prod" in g.nodes


# ── Traversal Tests ──────────────────────────────────────────────────────


class TestBuildTransitiveDependencies:
    """Test build_transitive_dependencies BFS with cycle detection."""

    def test_linear_chain(self):
        """A → B → C → D."""
        nodes = {
            "A": DependencyNode(service_name="A", direct_dependencies=["B"]),
            "B": DependencyNode(service_name="B", direct_dependencies=["C"]),
            "C": DependencyNode(service_name="C", direct_dependencies=["D"]),
            "D": DependencyNode(service_name="D"),
        }
        nodes, warnings = build_transitive_dependencies(nodes)
        assert set(nodes["A"].transitive_dependencies) == {"B", "C", "D"}
        assert set(nodes["B"].transitive_dependencies) == {"C", "D"}
        assert nodes["C"].transitive_dependencies == ["D"]
        assert nodes["D"].transitive_dependencies == []
        assert warnings == []

    def test_diamond_graph(self):
        """A → B, A → C, B → D, C → D."""
        nodes = {
            "A": DependencyNode(service_name="A", direct_dependencies=["B", "C"]),
            "B": DependencyNode(service_name="B", direct_dependencies=["D"]),
            "C": DependencyNode(service_name="C", direct_dependencies=["D"]),
            "D": DependencyNode(service_name="D"),
        }
        nodes, warnings = build_transitive_dependencies(nodes)
        assert set(nodes["A"].transitive_dependencies) == {"B", "C", "D"}
        assert warnings == []

    def test_cycle_detection(self):
        """A → B → C → A (cycle back to root)."""
        nodes = {
            "A": DependencyNode(service_name="A", direct_dependencies=["B"]),
            "B": DependencyNode(service_name="B", direct_dependencies=["C"]),
            "C": DependencyNode(service_name="C", direct_dependencies=["A"]),
        }
        nodes, warnings = build_transitive_dependencies(nodes)
        assert len(warnings) > 0
        assert any("Cycle detected" in w for w in warnings)
        # A should still reach B and C.
        assert set(nodes["A"].transitive_dependencies) == {"B", "C"}

    def test_self_loop(self):
        """A → A (self-reference)."""
        nodes = {
            "A": DependencyNode(service_name="A", direct_dependencies=["A"]),
        }
        nodes, warnings = build_transitive_dependencies(nodes)
        assert len(warnings) > 0
        assert nodes["A"].transitive_dependencies == []

    def test_empty_graph(self):
        nodes, warnings = build_transitive_dependencies({})
        assert nodes == {}
        assert warnings == []


class TestGetDependencies:
    """Test get_dependencies with depth limits."""

    def test_all_transitive(self, mock_dependency_graph):
        deps = get_dependencies(mock_dependency_graph, "payment-svc-prod")
        assert set(deps) == {"auth-service-prod", "export-worker-prod"}

    def test_depth_1(self, mock_dependency_graph):
        deps = get_dependencies(mock_dependency_graph, "payment-svc-prod", max_depth=1)
        assert set(deps) == {"auth-service-prod", "export-worker-prod"}

    def test_leaf_node(self, mock_dependency_graph):
        deps = get_dependencies(mock_dependency_graph, "export-worker-prod")
        assert deps == []

    def test_unknown_service(self, mock_dependency_graph):
        deps = get_dependencies(mock_dependency_graph, "nonexistent-svc")
        assert deps == []


class TestGetDependents:
    """Test get_dependents (upstream callers)."""

    def test_upstream_callers(self, mock_dependency_graph):
        dependents = get_dependents(mock_dependency_graph, "export-worker-prod")
        assert set(dependents) == {"payment-svc-prod", "auth-service-prod"}

    def test_no_callers(self, mock_dependency_graph):
        dependents = get_dependents(mock_dependency_graph, "payment-svc-prod")
        assert dependents == []

    def test_unknown_service(self, mock_dependency_graph):
        dependents = get_dependents(mock_dependency_graph, "nonexistent")
        assert dependents == []


class TestFindPath:
    """Test find_path BFS shortest path."""

    def test_direct_path(self, mock_dependency_graph):
        path = find_path(mock_dependency_graph, "payment-svc-prod", "auth-service-prod")
        assert path is not None
        assert path[0] == "payment-svc-prod"
        assert path[-1] == "auth-service-prod"

    def test_transitive_path(self, mock_dependency_graph):
        path = find_path(mock_dependency_graph, "payment-svc-prod", "export-worker-prod")
        assert path is not None
        assert path[0] == "payment-svc-prod"
        assert path[-1] == "export-worker-prod"

    def test_no_path(self, mock_dependency_graph):
        path = find_path(mock_dependency_graph, "export-worker-prod", "payment-svc-prod")
        assert path is None

    def test_same_service(self, mock_dependency_graph):
        path = find_path(mock_dependency_graph, "payment-svc-prod", "payment-svc-prod")
        # Same service = trivial path.
        assert path is not None
        assert path == ["payment-svc-prod"]


# ── Persistence Tests ────────────────────────────────────────────────────


class TestPersistence:
    """Test save_graph, load_graph, graph_is_stale."""

    def test_save_and_load(self, mock_dependency_graph, tmp_path):
        with patch("core.dependency_graph.DEPS_DIR", tmp_path):
            save_graph(mock_dependency_graph)
            loaded = load_graph("123456")
            assert loaded is not None
            assert loaded.account_id == "123456"
            assert loaded.total_services == 3
            assert loaded.total_edges == 3
            assert "payment-svc-prod" in loaded.nodes

    def test_load_nonexistent(self, tmp_path):
        with patch("core.dependency_graph.DEPS_DIR", tmp_path):
            loaded = load_graph("nonexistent")
            assert loaded is None

    def test_graph_is_stale_fresh(self, mock_dependency_graph, tmp_path):
        with patch("core.dependency_graph.DEPS_DIR", tmp_path):
            save_graph(mock_dependency_graph)
            assert not graph_is_stale("123456")

    def test_graph_is_stale_old(self, mock_dependency_graph, tmp_path):
        with patch("core.dependency_graph.DEPS_DIR", tmp_path):
            mock_dependency_graph.built_at = datetime.now(timezone.utc) - timedelta(hours=25)
            save_graph(mock_dependency_graph)
            assert graph_is_stale("123456")

    def test_graph_is_stale_missing(self, tmp_path):
        with patch("core.dependency_graph.DEPS_DIR", tmp_path):
            assert graph_is_stale("nonexistent")

    def test_save_creates_directory(self, mock_dependency_graph, tmp_path):
        subdir = tmp_path / "nested" / "deps"
        with patch("core.dependency_graph.DEPS_DIR", subdir):
            save_graph(mock_dependency_graph)
            assert subdir.exists()
            loaded = load_graph("123456")
            assert loaded is not None
