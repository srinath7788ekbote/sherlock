"""
Service dependency mapping tool for Sherlock.

Provides the get_service_dependencies MCP tool that returns
upstream/downstream dependency information for a service.
"""

import json
import logging
from typing import Any

from core.context import AccountContext
from core.dependency_graph import (
    DependencyGraph,
    get_dependencies,
    get_dependents,
    find_path,
    load_graph,
    graph_is_stale,
)
from core.sanitize import fuzzy_resolve_service

logger = logging.getLogger("sherlock.tools.dependencies")


def _health_warning_for_dependency(
    graph: DependencyGraph,
    service_name: str,
    callee_name: str,
) -> str | None:
    """Check if a dependency has concerning metrics.

    Args:
        graph: The dependency graph.
        service_name: Caller service.
        callee_name: Callee service.

    Returns:
        Warning string or None if healthy.
    """
    node = graph.nodes.get(service_name)
    if not node:
        return None

    detail = node.dependency_details.get(callee_name)
    if not detail:
        return None

    warnings: list[str] = []

    if detail.error_rate > 10.0:
        warnings.append(f"high error rate ({detail.error_rate:.1f}%)")
    if detail.avg_latency_ms > 5000:
        warnings.append(f"high latency ({detail.avg_latency_ms:.0f}ms)")
    if detail.confidence < 0.6:
        warnings.append(f"low confidence ({detail.source} source)")

    return "; ".join(warnings) if warnings else None


def _format_dependency_detail(
    graph: DependencyGraph,
    caller: str,
    callee: str,
) -> dict[str, Any]:
    """Format a single dependency edge for output.

    Args:
        graph: The dependency graph.
        caller: Caller service name.
        callee: Callee service name.

    Returns:
        Dict with dependency detail.
    """
    node = graph.nodes.get(caller)
    result: dict[str, Any] = {
        "service": callee,
        "call_count": 0,
        "error_rate": 0.0,
        "avg_latency_ms": 0.0,
        "source": "unknown",
        "confidence": 0.0,
    }

    if node:
        detail = node.dependency_details.get(callee)
        if detail:
            result.update({
                "call_count": detail.call_count,
                "error_rate": detail.error_rate,
                "avg_latency_ms": detail.avg_latency_ms,
                "source": detail.source,
                "confidence": detail.confidence,
            })

    warning = _health_warning_for_dependency(graph, caller, callee)
    if warning:
        result["health_warning"] = warning

    return result


async def get_service_dependencies(
    service_name: str,
    direction: str = "both",
    include_external: bool = False,
    max_depth: int = 2,
) -> str:
    """Get service dependency information from the dependency graph.

    Returns upstream (callers) and/or downstream (callees) dependencies
    for a service, with health warnings and call chain details.

    Args:
        service_name: APM service name to look up.
        direction: 'downstream', 'upstream', or 'both'.
        include_external: Include external (non-NR) endpoints.
        max_depth: Maximum dependency depth (1-5, default 2).

    Returns:
        JSON string with dependency information.
    """
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()

        # Load the graph from disk.
        graph = load_graph(credentials.account_id)
        if not graph:
            return json.dumps({
                "error": "No dependency graph available for this account.",
                "tool": "get_service_dependencies",
                "hint": (
                    "Run connect_account first. The dependency graph is built "
                    "automatically during account connection."
                ),
                "data_available": False,
            })

        # Check staleness.
        stale = graph_is_stale(credentials.account_id)

        # Fuzzy resolve the service name.
        all_service_names = list(graph.nodes.keys())
        if service_name not in graph.nodes and all_service_names:
            try:
                resolved, was_fuzzy, conf = fuzzy_resolve_service(
                    service_name, all_service_names,
                )
                service_name = resolved
            except Exception:
                pass

        if service_name not in graph.nodes:
            # Service not found in graph.
            return json.dumps({
                "service": service_name,
                "in_graph": False,
                "graph_coverage": f"{graph.coverage_pct:.0f}%",
                "total_services_in_graph": graph.total_services,
                "hint": (
                    "This service was not found in the dependency graph. "
                    "It may not have span/log data, or the graph may need refreshing."
                ),
                "data_available": False,
            })

        # Clamp max_depth.
        max_depth = max(1, min(max_depth, 5))

        result: dict[str, Any] = {
            "service": service_name,
            "in_graph": True,
            "graph_metadata": {
                "built_at": graph.built_at.isoformat(),
                "source": graph.build_source,
                "coverage_pct": graph.coverage_pct,
                "total_services": graph.total_services,
                "total_edges": graph.total_edges,
                "stale": stale,
            },
        }

        node = graph.nodes[service_name]

        # Downstream dependencies (services this service calls).
        if direction in ("downstream", "both"):
            downstream_names = get_dependencies(graph, service_name, max_depth)
            downstream = [
                _format_dependency_detail(graph, service_name, dep)
                for dep in downstream_names
            ]
            result["downstream"] = {
                "count": len(downstream),
                "max_depth": max_depth,
                "services": downstream,
            }

            # External dependencies.
            if include_external:
                ext = graph.external_dependencies.get(service_name, [])
                result["downstream"]["external"] = ext

        # Upstream dependencies (services that call this service).
        if direction in ("upstream", "both"):
            upstream_names = get_dependents(graph, service_name)
            upstream = []
            for caller in upstream_names:
                detail = _format_dependency_detail(graph, caller, service_name)
                detail["service"] = caller  # Swap to show the caller.
                upstream.append(detail)
            result["upstream"] = {
                "count": len(upstream),
                "services": upstream,
            }

        # Transitive dependency summary.
        result["transitive_dependencies"] = {
            "total": len(node.transitive_dependencies),
            "services": node.transitive_dependencies,
        }

        # Graph warnings relevant to this service.
        svc_warnings = [
            w for w in graph.warnings
            if service_name.lower() in w.lower()
        ]
        if svc_warnings:
            result["warnings"] = svc_warnings

        result["data_available"] = True
        return json.dumps(result)

    except Exception as exc:
        logger.error("get_service_dependencies failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "get_service_dependencies",
            "hint": "Ensure you are connected first.",
            "data_available": False,
        })
