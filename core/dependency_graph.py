"""
Service dependency graph model and operations for Sherlock.

Owns everything related to dependency graph storage, traversal,
and cycle detection. No other module touches graph logic directly.

Graph nodes use the full service name as stored in AccountIntelligence
(e.g. "eswd-prod/pdf-export-service") so that fuzzy resolution and
NamingConvention-aware matching work seamlessly.
"""

import json
import logging
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger("sherlock.dependency_graph")

# ── Disk Storage Constants ───────────────────────────────────────────────

DEPS_DIR = Path(__file__).resolve().parent.parent / ".sherlock" / "deps"
DEPS_FILE_PATTERN = "{account_id}.json"

# Default TTL: 24 hours (graph structure changes slowly).
DEFAULT_GRAPH_TTL_HOURS = 24


# ── Pydantic Models ─────────────────────────────────────────────────────


class ServiceDependency(BaseModel):
    """A single directed dependency edge between two services."""

    caller: str
    """Service that makes the call."""

    callee: str
    """Service being called."""

    call_count: int = 0
    """Number of calls observed in the discovery window."""

    error_rate: float = 0.0
    """Percentage of calls that errored (0.0–100.0)."""

    avg_latency_ms: float = 0.0
    """Average call duration in milliseconds."""

    source: str = "span"
    """Discovery source: 'span' | 'log' | 'inferred'."""

    confidence: float = 1.0
    """Confidence score 0.0–1.0. span=1.0, log=0.7, inferred=0.4."""

    last_seen: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    """When this dependency was last observed."""


class DependencyNode(BaseModel):
    """A single service node in the dependency graph."""

    service_name: str
    """Full service name as stored in AccountIntelligence."""

    direct_dependencies: list[str] = Field(default_factory=list)
    """Services this node directly calls."""

    direct_dependents: list[str] = Field(default_factory=list)
    """Services that directly call this node."""

    transitive_dependencies: list[str] = Field(default_factory=list)
    """All services reachable from this node (all hops). Pre-computed, cycle-safe."""

    dependency_details: dict[str, ServiceDependency] = Field(default_factory=dict)
    """callee_name → ServiceDependency detail for each direct dependency."""


class DependencyGraph(BaseModel):
    """Complete dependency graph for one New Relic account."""

    account_id: str
    """New Relic account ID."""

    built_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    """When this graph was built."""

    discovery_window_hours: int = 168
    """How many hours of data were analysed (default 7 days)."""

    total_services: int = 0
    """Number of service nodes in the graph."""

    total_edges: int = 0
    """Number of directed dependency edges."""

    nodes: dict[str, DependencyNode] = Field(default_factory=dict)
    """service_name → DependencyNode."""

    build_source: str = "unavailable"
    """Primary data source: 'span' | 'log' | 'mixed' | 'unavailable'."""

    coverage_pct: float = 0.0
    """Percentage of known APM services found in the graph."""

    warnings: list[str] = Field(default_factory=list)
    """Informational warnings (e.g. cycles, fallback used)."""

    external_dependencies: dict[str, list[str]] = Field(default_factory=dict)
    """service_name → list of external (non-NR) endpoints it calls."""


# ── Graph Operations ─────────────────────────────────────────────────────


def build_transitive_dependencies(
    nodes: dict[str, DependencyNode],
) -> tuple[dict[str, DependencyNode], list[str]]:
    """Pre-compute transitive_dependencies for every node using iterative BFS.

    Cycle detection:
        Tracks a visited set per traversal. If a node is encountered
        that is already in the current traversal path, a cycle is
        detected. The cycle edge is NOT followed — traversal continues
        normally for the rest of the graph.

    Args:
        nodes: Dict of service_name → DependencyNode with direct_dependencies
               already populated.

    Returns:
        Tuple of (updated nodes dict, list of cycle warning strings).
    """
    cycle_warnings: list[str] = []

    for root_name, root_node in nodes.items():
        visited: set[str] = set()
        transitive: list[str] = []
        queue: deque[tuple[str, list[str]]] = deque()

        # Seed the BFS with direct dependencies.
        for dep in root_node.direct_dependencies:
            queue.append((dep, [root_name, dep]))

        while queue:
            current, path = queue.popleft()

            if current in visited:
                continue

            if current == root_name:
                # Cycle back to root detected.
                cycle_path = " → ".join(path)
                warning = f"Cycle detected: {cycle_path}"
                if warning not in cycle_warnings:
                    cycle_warnings.append(warning)
                    logger.warning(warning)
                continue

            visited.add(current)
            transitive.append(current)

            # Enqueue children of current node.
            current_node = nodes.get(current)
            if current_node:
                for child in current_node.direct_dependencies:
                    if child in visited:
                        continue
                    if child == root_name:
                        cycle_path = " → ".join(path + [child])
                        warning = f"Cycle detected: {cycle_path}"
                        if warning not in cycle_warnings:
                            cycle_warnings.append(warning)
                            logger.warning(warning)
                        continue
                    # Check for non-root cycles in path.
                    if child in path:
                        cycle_path = " → ".join(path + [child])
                        warning = f"Cycle detected: {cycle_path}"
                        if warning not in cycle_warnings:
                            cycle_warnings.append(warning)
                            logger.warning(warning)
                        continue
                    queue.append((child, path + [child]))

        root_node.transitive_dependencies = transitive

    return nodes, cycle_warnings


def get_dependencies(
    graph: DependencyGraph,
    service_name: str,
    max_depth: int | None = None,
) -> list[str]:
    """Get all dependencies for a service up to max_depth hops.

    Args:
        graph: The dependency graph.
        service_name: Service to query.
        max_depth: None → all transitive dependencies.
                   1 → direct dependencies only.
                   2 → direct + their direct deps. Etc.

    Returns:
        List of dependent service names. Empty if service not in graph.
        Never raises.
    """
    try:
        node = graph.nodes.get(service_name)
        if not node:
            return []

        if max_depth is None:
            return list(node.transitive_dependencies)

        if max_depth == 1:
            return list(node.direct_dependencies)

        # BFS up to max_depth.
        visited: set[str] = set()
        result: list[str] = []
        queue: deque[tuple[str, int]] = deque()

        for dep in node.direct_dependencies:
            queue.append((dep, 1))

        while queue:
            current, depth = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            result.append(current)

            if depth < max_depth:
                current_node = graph.nodes.get(current)
                if current_node:
                    for child in current_node.direct_dependencies:
                        if child not in visited:
                            queue.append((child, depth + 1))

        return result
    except Exception:
        return []


def get_dependents(
    graph: DependencyGraph,
    service_name: str,
) -> list[str]:
    """Get all services that depend on this service (callers).

    Useful for blast radius analysis: if service_name is broken,
    who is affected?

    Args:
        graph: The dependency graph.
        service_name: Service to query.

    Returns:
        List of caller service names. Empty if service not in graph.
        Never raises.
    """
    try:
        node = graph.nodes.get(service_name)
        if not node:
            return []
        return list(node.direct_dependents)
    except Exception:
        return []


def find_path(
    graph: DependencyGraph,
    from_service: str,
    to_service: str,
) -> list[str] | None:
    """Find the shortest path between two services in the graph.

    Uses BFS for shortest-path discovery.

    Args:
        graph: The dependency graph.
        from_service: Starting service name.
        to_service: Target service name.

    Returns:
        Ordered list of service names representing the call chain,
        or None if no path exists.
        Example: ["pdf-export-service", "font-service-backend"]
        Never raises.
    """
    try:
        if from_service not in graph.nodes or to_service not in graph.nodes:
            return None

        if from_service == to_service:
            return [from_service]

        visited: set[str] = set()
        queue: deque[list[str]] = deque()
        queue.append([from_service])
        visited.add(from_service)

        while queue:
            path = queue.popleft()
            current = path[-1]

            current_node = graph.nodes.get(current)
            if not current_node:
                continue

            for neighbor in current_node.direct_dependencies:
                if neighbor == to_service:
                    return path + [neighbor]
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(path + [neighbor])

        return None
    except Exception:
        return None


# ── Disk Storage ─────────────────────────────────────────────────────────


def _ensure_deps_dir() -> None:
    """Create the deps directory if it does not exist."""
    DEPS_DIR.mkdir(parents=True, exist_ok=True)


def save_graph(graph: DependencyGraph) -> None:
    """Persist a DependencyGraph to disk as JSON.

    Args:
        graph: The graph to save.
    """
    try:
        _ensure_deps_dir()
        file_path = DEPS_DIR / DEPS_FILE_PATTERN.format(account_id=graph.account_id)
        data = graph.model_dump(mode="json")
        file_path.write_text(json.dumps(data, default=str, indent=2), encoding="utf-8")
        logger.info(
            "Dependency graph saved for account %s (%d services, %d edges)",
            graph.account_id,
            graph.total_services,
            graph.total_edges,
        )
    except OSError as exc:
        logger.warning("Failed to save dependency graph for account %s: %s", graph.account_id, exc)


def load_graph(account_id: str) -> DependencyGraph | None:
    """Load a DependencyGraph from disk.

    Args:
        account_id: The New Relic account ID.

    Returns:
        DependencyGraph or None if not found / corrupt.
    """
    try:
        file_path = DEPS_DIR / DEPS_FILE_PATTERN.format(account_id=account_id)
        if not file_path.exists():
            return None
        raw = json.loads(file_path.read_text(encoding="utf-8"))
        return DependencyGraph(**raw)
    except (json.JSONDecodeError, KeyError, OSError, Exception) as exc:
        logger.warning("Failed to load dependency graph for account %s: %s", account_id, exc)
        return None


def graph_is_stale(account_id: str, ttl_hours: int = DEFAULT_GRAPH_TTL_HOURS) -> bool:
    """Check whether the cached dependency graph is stale or missing.

    Args:
        account_id: The New Relic account ID.
        ttl_hours: Maximum age in hours before the graph is considered stale.

    Returns:
        True if graph is stale, missing, or corrupt.
    """
    try:
        graph = load_graph(account_id)
        if graph is None:
            return True
        age_seconds = (datetime.now(timezone.utc) - graph.built_at).total_seconds()
        return age_seconds > (ttl_hours * 3600)
    except Exception:
        return True
