"""
Dependency graph builder for Sherlock.

Builds a DependencyGraph from New Relic data using three strategies
that run in priority order with results merged:
  Strategy 1 — Span-Based Discovery (primary, confidence=1.0)
  Strategy 2 — Log-Based Discovery (fallback, confidence=0.7)
  Strategy 3 — Inferred from Naming Patterns (last resort, confidence=0.4)

Span attribute names are discovered dynamically via keyset() so the
builder works against any New Relic account regardless of instrumentation.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from core.credentials import Credentials
from core.dependency_graph import (
    DependencyGraph,
    DependencyNode,
    ServiceDependency,
    build_transitive_dependencies,
    save_graph,
)
from core.intelligence import AccountIntelligence
from core.sanitize import fuzzy_resolve_service_candidates

logger = logging.getLogger("sherlock.graph_builder")

# ── NerdGraph NRQL Template ──────────────────────────────────────────────

GQL_NRQL_QUERY = """
{
  actor {
    account(id: %s) {
      nrql(query: "%s") {
        results
      }
    }
  }
}
"""

# ── Span Keyset Discovery ───────────────────────────────────────────────

NRQL_SPAN_KEYSET = "SELECT keyset() FROM Span SINCE 1 hour ago LIMIT 1"

# ── Strategy 1: Span-Based NRQL Queries ─────────────────────────────────

NRQL_SPAN_DEPENDENCIES = """
SELECT count(*) as call_count,
       average(duration) * 1000 as avg_latency_ms,
       percentage(count(*), WHERE error IS true) as error_rate,
       latest(appName) as caller,
       latest(peer.service.name) as callee_otel,
       latest(db.system) as db_system
FROM Span
WHERE {caller_attr} IS NOT NULL
AND (peer.service.name IS NOT NULL
     OR db.system IS NOT NULL)
FACET {caller_attr}, peer.service.name
SINCE {window_hours} hours ago
LIMIT 2000
"""

NRQL_SPAN_APM = """
SELECT count(*) as call_count,
       average(duration) * 1000 as avg_latency_ms,
       percentage(count(*), WHERE error IS true) as error_rate
FROM Span
WHERE {caller_attr} IS NOT NULL
AND span.kind = 'client'
FACET {caller_attr}, http.url
SINCE {window_hours} hours ago
LIMIT 2000
"""

# ── Strategy 2: Log-Based NRQL Queries ──────────────────────────────────

NRQL_LOG_DEPENDENCIES = """
SELECT count(*) as occurrences,
       latest(message) as sample_message,
       latest(hostname) as hostname
FROM Log
WHERE message RLIKE '.*(connect|fetch|request|call|http|grpc).*(failed|error|timeout|refused|unreachable).*'
OR message RLIKE '.*(failed to (fetch|connect|reach|call)).*'
FACET message
SINCE {window_hours} hours ago
LIMIT 500
"""

# ── Graph Build Timeout ─────────────────────────────────────────────────

GRAPH_BUILD_TIMEOUT_S = 60


# ── SpanAttributes Dataclass ─────────────────────────────────────────────


@dataclass
class SpanAttributes:
    """Discovered Span attribute names for the account.

    Built from keyset() query results so the graph builder is
    attribute-agnostic across NR APM agent and OpenTelemetry accounts.
    """

    caller_attr: str | None = None
    """Attribute identifying the calling service: 'appName' or 'service.name'."""

    callee_attr: str | None = None
    """Attribute identifying the callee: 'peer.service.name' or None."""

    url_attr: str | None = None
    """HTTP URL attribute: 'http.url' or 'http.request.url' or None."""

    db_attr: str | None = None
    """Database system attribute: 'db.system' or None."""

    kind_attr: str | None = None
    """Span kind attribute: 'span.kind' or None."""

    has_span_data: bool = False
    """True if keyset() returned usable Span attributes."""


# ── Helper Functions ─────────────────────────────────────────────────────


def _safe_extract_results(body: dict) -> list[dict]:
    """Safely navigate data.actor.account.nrql.results."""
    d: Any = body if isinstance(body, dict) else {}
    for key in ("data", "actor", "account", "nrql", "results"):
        d = d.get(key) if isinstance(d, dict) else None
        if d is None:
            return []
    return d if isinstance(d, list) else []


def _extract_hostname_from_url(url: str) -> str | None:
    """Extract hostname from an HTTP URL string.

    Args:
        url: Raw URL like 'https://font-service.internal:8080/api/fonts'.

    Returns:
        Hostname string or None.
    """
    try:
        if not url:
            return None
        if "://" not in url:
            url = "http://" + url
        parsed = urlparse(url)
        hostname = parsed.hostname
        return hostname if hostname else None
    except Exception:
        return None


def _match_hostname_to_service(
    hostname: str,
    known_services: list[str],
    naming_convention: Any = None,
) -> str | None:
    """Try to match a hostname against known service names using fuzzy resolution.

    Args:
        hostname: Extracted hostname (e.g. 'font-service-backend').
        known_services: List of known APM/OTel service names.
        naming_convention: Optional NamingConvention for fuzzy matching.

    Returns:
        Best matching service name or None.
    """
    if not hostname or not known_services:
        return None

    matches = fuzzy_resolve_service_candidates(
        hostname,
        known_services,
        threshold=0.4,
        max_candidates=1,
    )
    if matches:
        return matches[0][0]
    return None


def _extract_service_refs_from_log_message(
    message: str,
    known_services: list[str],
) -> list[str]:
    """Extract service/host references from a log error message.

    Patterns checked:
      - URL pattern: https?://([^/]+)
      - Host:port pattern: ([a-z][a-z0-9-]+):\\d+
      - Known service name mentions in the message text

    Args:
        message: Log message text.
        known_services: Known service names for matching.

    Returns:
        List of matched service names.
    """
    refs: list[str] = []
    if not message:
        return refs

    # URL pattern.
    url_matches = re.findall(r"https?://([^/\s]+)", message, re.IGNORECASE)
    for hostname in url_matches:
        # Strip port.
        hostname = hostname.split(":")[0]
        match = _match_hostname_to_service(hostname, known_services)
        if match and match not in refs:
            refs.append(match)

    # Host:port pattern.
    host_port_matches = re.findall(r"([a-z][a-z0-9-]+):\d+", message, re.IGNORECASE)
    for hostname in host_port_matches:
        match = _match_hostname_to_service(hostname, known_services)
        if match and match not in refs:
            refs.append(match)

    # Direct service name mention.
    msg_lower = message.lower()
    for svc in known_services:
        # Extract bare name for matching.
        bare = svc.split("/")[-1] if "/" in svc else svc
        if bare.lower() in msg_lower and svc not in refs:
            refs.append(svc)

    return refs


async def _discover_span_attributes(
    account_id: str,
    headers: dict[str, str],
    endpoint: str,
) -> SpanAttributes:
    """Discover available Span attributes via keyset() query.

    Args:
        account_id: New Relic account ID.
        headers: API headers.
        endpoint: NerdGraph endpoint URL.

    Returns:
        SpanAttributes with discovered attribute names.
    """
    attrs = SpanAttributes()
    try:
        escaped = NRQL_SPAN_KEYSET.replace('"', '\\"')
        gql = GQL_NRQL_QUERY % (account_id, escaped)

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                endpoint,
                json={"query": gql},
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()

        results = _safe_extract_results(body)
        if not results:
            logger.info("No Span keyset data found for account %s", account_id)
            return attrs

        # keyset() returns a list of dicts with 'allKeys' or individual keys.
        all_keys: set[str] = set()
        for row in results:
            keys = row.get("allKeys", [])
            if isinstance(keys, list):
                all_keys.update(keys)
            # Some keyset() formats return keys directly.
            for k in row:
                if k != "allKeys":
                    all_keys.add(k)

        if not all_keys:
            logger.info("Span keyset() returned empty for account %s", account_id)
            return attrs

        attrs.has_span_data = True

        # Caller attribute: prefer appName (NR APM), fall back to service.name (OTel).
        if "appName" in all_keys:
            attrs.caller_attr = "appName"
        elif "service.name" in all_keys:
            attrs.caller_attr = "service.name"
        else:
            # Neither present — can't build span graph.
            attrs.has_span_data = False
            logger.info(
                "Span data exists but no caller attribute (appName/service.name) "
                "found for account %s",
                account_id,
            )
            return attrs

        # Callee attribute.
        if "peer.service.name" in all_keys:
            attrs.callee_attr = "peer.service.name"

        # URL attribute.
        if "http.url" in all_keys:
            attrs.url_attr = "http.url"
        elif "http.request.url" in all_keys:
            attrs.url_attr = "http.request.url"

        # Database system.
        if "db.system" in all_keys:
            attrs.db_attr = "db.system"

        # Span kind.
        if "span.kind" in all_keys:
            attrs.kind_attr = "span.kind"

        logger.info(
            "Span attributes discovered for account %s: caller=%s, callee=%s, "
            "url=%s, db=%s, kind=%s",
            account_id,
            attrs.caller_attr,
            attrs.callee_attr,
            attrs.url_attr,
            attrs.db_attr,
            attrs.kind_attr,
        )

    except Exception as exc:
        logger.warning("Span keyset discovery failed for account %s: %s", account_id, exc)

    return attrs


async def _run_nrql(
    nrql: str,
    account_id: str,
    headers: dict[str, str],
    endpoint: str,
) -> list[dict]:
    """Execute a single NRQL query via NerdGraph.

    Args:
        nrql: NRQL query string.
        account_id: New Relic account ID.
        headers: API headers.
        endpoint: NerdGraph endpoint URL.

    Returns:
        List of result dicts. Empty on error — never raises.
    """
    try:
        escaped = nrql.replace('"', '\\"')
        gql = GQL_NRQL_QUERY % (account_id, escaped)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                endpoint,
                json={"query": gql},
                headers=headers,
            )
            resp.raise_for_status()
            body = resp.json()

        return _safe_extract_results(body)
    except Exception as exc:
        logger.debug("NRQL query failed: %s", exc)
        return []


# ── Strategy 1: Span-Based Discovery ─────────────────────────────────────


async def _build_span_edges(
    account_id: str,
    headers: dict[str, str],
    endpoint: str,
    span_attrs: SpanAttributes,
    known_services: list[str],
    naming_convention: Any,
    window_hours: int,
) -> list[ServiceDependency]:
    """Build dependency edges from Span data.

    Args:
        account_id: New Relic account ID.
        headers: API headers.
        endpoint: NerdGraph endpoint.
        span_attrs: Discovered span attribute names.
        known_services: Known APM+OTel service names.
        naming_convention: NamingConvention from intelligence.
        window_hours: Discovery window in hours.

    Returns:
        List of ServiceDependency edges from span data.
    """
    edges: list[ServiceDependency] = []

    if not span_attrs.has_span_data or not span_attrs.caller_attr:
        return edges

    # Query 1: peer.service.name based dependencies.
    if span_attrs.callee_attr:
        nrql = NRQL_SPAN_DEPENDENCIES.format(
            caller_attr=span_attrs.caller_attr,
            window_hours=window_hours,
        )
        results = await _run_nrql(nrql, account_id, headers, endpoint)

        for row in results:
            caller = row.get(span_attrs.caller_attr) or row.get("caller") or ""
            callee = row.get("callee_otel") or row.get("peer.service.name") or ""
            db_system = row.get("db_system") or row.get("db.system") or ""

            # Handle facet arrays.
            if isinstance(row.get("facet"), list) and len(row["facet"]) >= 2:
                caller = caller or row["facet"][0] or ""
                callee = callee or row["facet"][1] or ""

            if not caller:
                continue

            # Determine callee name.
            effective_callee = callee
            if not effective_callee and db_system:
                effective_callee = f"[db:{db_system}]"

            if not effective_callee:
                continue

            call_count = int(row.get("call_count", 0) or 0)
            avg_latency = float(row.get("avg_latency_ms", 0) or 0)
            error_rate = float(row.get("error_rate", 0) or 0)

            edges.append(ServiceDependency(
                caller=caller,
                callee=effective_callee,
                call_count=call_count,
                error_rate=error_rate,
                avg_latency_ms=avg_latency,
                source="span",
                confidence=1.0,
                last_seen=datetime.now(timezone.utc),
            ))

    # Query 2: http.url based dependencies (client spans).
    if span_attrs.kind_attr and span_attrs.url_attr:
        nrql = NRQL_SPAN_APM.format(
            caller_attr=span_attrs.caller_attr,
            window_hours=window_hours,
        )
        # Replace http.url with actual URL attr if different.
        if span_attrs.url_attr != "http.url":
            nrql = nrql.replace("http.url", span_attrs.url_attr)

        results = await _run_nrql(nrql, account_id, headers, endpoint)

        for row in results:
            caller = ""
            url = ""

            if isinstance(row.get("facet"), list) and len(row["facet"]) >= 2:
                caller = row["facet"][0] or ""
                url = row["facet"][1] or ""
            else:
                caller = row.get(span_attrs.caller_attr, "")
                url = row.get(span_attrs.url_attr, row.get("http.url", ""))

            if not caller or not url:
                continue

            hostname = _extract_hostname_from_url(url)
            if not hostname:
                continue

            # Try to match hostname against known services.
            matched_service = _match_hostname_to_service(
                hostname, known_services, naming_convention,
            )

            callee = matched_service if matched_service else hostname
            call_count = int(row.get("call_count", 0) or 0)
            avg_latency = float(row.get("avg_latency_ms", 0) or 0)
            error_rate = float(row.get("error_rate", 0) or 0)

            edges.append(ServiceDependency(
                caller=caller,
                callee=callee,
                call_count=call_count,
                error_rate=error_rate,
                avg_latency_ms=avg_latency,
                source="span",
                confidence=1.0 if matched_service else 0.8,
                last_seen=datetime.now(timezone.utc),
            ))

    return edges


# ── Strategy 2: Log-Based Discovery ──────────────────────────────────────


async def _build_log_edges(
    account_id: str,
    headers: dict[str, str],
    endpoint: str,
    known_services: list[str],
    service_attribute: str,
    window_hours: int,
) -> list[ServiceDependency]:
    """Build dependency edges from Log error messages.

    Args:
        account_id: New Relic account ID.
        headers: API headers.
        endpoint: NerdGraph endpoint.
        known_services: Known service names.
        service_attribute: Log attribute for service name.
        window_hours: Discovery window.

    Returns:
        List of ServiceDependency edges from log data.
    """
    edges: list[ServiceDependency] = []

    nrql = NRQL_LOG_DEPENDENCIES.format(window_hours=window_hours)
    results = await _run_nrql(nrql, account_id, headers, endpoint)

    for row in results:
        message = row.get("sample_message") or row.get("message") or ""
        hostname = row.get("hostname", "")
        occurrences = int(row.get("occurrences", 0) or 0)
        facet_message = row.get("facet", "")

        effective_message = message or facet_message
        if not effective_message:
            continue

        # Extract callee references from the message.
        callees = _extract_service_refs_from_log_message(
            effective_message, known_services,
        )

        if not callees:
            continue

        # Try to determine caller from hostname or message context.
        caller = None
        if hostname:
            caller = _match_hostname_to_service(hostname, known_services)

        if not caller:
            # Try to find a service that is NOT the callee in the message.
            for svc in known_services:
                bare = svc.split("/")[-1] if "/" in svc else svc
                if bare.lower() in effective_message.lower() and svc not in callees:
                    caller = svc
                    break

        if not caller:
            # Use hostname as caller if available.
            caller = hostname if hostname else "unknown"

        for callee in callees:
            if caller == callee:
                continue
            edges.append(ServiceDependency(
                caller=caller,
                callee=callee,
                call_count=occurrences,
                error_rate=100.0,  # Log-based = errors only.
                avg_latency_ms=0.0,
                source="log",
                confidence=0.7,
                last_seen=datetime.now(timezone.utc),
            ))

    return edges


# ── Strategy 3: Inferred from Naming Patterns ───────────────────────────


def _build_inferred_edges(
    intelligence: AccountIntelligence,
) -> list[ServiceDependency]:
    """Infer dependency edges from shared naming segments.

    Services with shared name segments likely communicate.
    e.g. "export-orchestration-service" and "export-service"
    share "export" → likely related.

    Source = "inferred", confidence = 0.4.

    Args:
        intelligence: Account intelligence with naming convention.

    Returns:
        List of inferred ServiceDependency edges.
    """
    edges: list[ServiceDependency] = []
    nc = intelligence.naming_convention
    services = intelligence.apm.service_names

    if not services or len(services) < 2:
        return edges

    # Extract significant segments from each service name.
    sep = nc.separator or "-"
    service_segments: dict[str, set[str]] = {}

    for svc in services:
        bare = svc.split("/")[-1] if "/" in svc else svc
        parts = bare.split(sep) if sep != "/" else bare.split("-")
        # Filter out env values and very short segments.
        significant = {
            p.lower()
            for p in parts
            if len(p) > 2 and p.lower() not in {v.lower() for v in nc.env_values}
        }
        service_segments[svc] = significant

    # Find pairs with shared significant segments.
    svc_list = list(service_segments.keys())
    for i in range(len(svc_list)):
        for j in range(i + 1, len(svc_list)):
            svc_a = svc_list[i]
            svc_b = svc_list[j]
            shared = service_segments[svc_a] & service_segments[svc_b]
            if shared and len(shared) >= 1:
                # Heuristic: service with "orchestration", "gateway", "proxy"
                # in its name is likely the caller.
                caller_keywords = {"orchestration", "gateway", "proxy", "api", "router", "frontend"}
                a_has = bool(service_segments[svc_a] & caller_keywords)
                b_has = bool(service_segments[svc_b] & caller_keywords)

                if a_has and not b_has:
                    caller, callee = svc_a, svc_b
                elif b_has and not a_has:
                    caller, callee = svc_b, svc_a
                else:
                    # Alphabetical order as tiebreaker.
                    caller, callee = (svc_a, svc_b) if svc_a < svc_b else (svc_b, svc_a)

                edges.append(ServiceDependency(
                    caller=caller,
                    callee=callee,
                    call_count=0,
                    error_rate=0.0,
                    avg_latency_ms=0.0,
                    source="inferred",
                    confidence=0.4,
                    last_seen=datetime.now(timezone.utc),
                ))

    return edges


# ── Merge Logic ──────────────────────────────────────────────────────────


def _merge_edges(
    span_edges: list[ServiceDependency],
    log_edges: list[ServiceDependency],
    inferred_edges: list[ServiceDependency],
) -> dict[str, ServiceDependency]:
    """Merge edges from all strategies. Span takes precedence over log over inferred.

    Args:
        span_edges: Edges from Strategy 1.
        log_edges: Edges from Strategy 2.
        inferred_edges: Edges from Strategy 3.

    Returns:
        Dict of (caller, callee) key → ServiceDependency.
    """
    merged: dict[str, ServiceDependency] = {}

    # Add inferred first (lowest priority).
    for edge in inferred_edges:
        key = f"{edge.caller}→{edge.callee}"
        merged[key] = edge

    # Log edges override inferred.
    for edge in log_edges:
        key = f"{edge.caller}→{edge.callee}"
        merged[key] = edge

    # Span edges override everything.
    for edge in span_edges:
        key = f"{edge.caller}→{edge.callee}"
        merged[key] = edge

    return merged


def _classify_edge(
    edge: ServiceDependency,
    known_services_lower: set[str],
) -> bool:
    """Return True if the callee is a known (internal) service.

    Args:
        edge: The dependency edge.
        known_services_lower: Set of lowercased known service names.

    Returns:
        True if callee is internal.
    """
    callee_lower = edge.callee.lower()
    # Direct match.
    if callee_lower in known_services_lower:
        return True
    # Check bare name.
    bare = callee_lower.split("/")[-1] if "/" in callee_lower else callee_lower
    for known in known_services_lower:
        known_bare = known.split("/")[-1] if "/" in known else known
        if bare == known_bare:
            return True
    return False


# ── Main Build Function ─────────────────────────────────────────────────


async def build_dependency_graph(
    credentials: Credentials,
    intelligence: AccountIntelligence,
    window_hours: int = 168,
) -> DependencyGraph:
    """Build complete dependency graph for the account.

    Steps:
      1. Discover Span attributes via keyset()
      2. Run Strategy 1 (Span) queries
      3. Run Strategy 2 (Log) queries
      4. Optionally run Strategy 3 (Inferred) if coverage < 20%
      5. Merge results — span edges take precedence
      6. Filter internal vs external dependencies
      7. Build DependencyNode for each service
      8. Run build_transitive_dependencies() with cycle detection
      9. Calculate coverage_pct
      10. Save graph to disk
      11. Return DependencyGraph

    Timeout: 60 seconds total for all queries.
    On timeout: return partial graph with warning.
    Never raises — returns empty graph with warnings on error.

    Args:
        credentials: Active account credentials.
        intelligence: Learned AccountIntelligence.
        window_hours: Discovery window in hours (default 168 = 7 days).

    Returns:
        DependencyGraph (possibly empty with warnings on failure).
    """
    account_id = credentials.account_id
    endpoint = credentials.endpoint
    headers = {"API-Key": credentials.api_key, "Content-Type": "application/json"}

    graph = DependencyGraph(
        account_id=account_id,
        discovery_window_hours=window_hours,
    )

    # Collect all known service names.
    known_services = list(intelligence.apm.service_names)
    otel_services = getattr(intelligence.otel, "service_names", [])
    if otel_services:
        for svc in otel_services:
            if svc not in known_services:
                known_services.append(svc)

    known_services_lower = {s.lower() for s in known_services}

    try:
        # Step 1: Discover Span attributes.
        span_attrs = await _discover_span_attributes(account_id, headers, endpoint)

        span_edges: list[ServiceDependency] = []
        log_edges: list[ServiceDependency] = []
        inferred_edges: list[ServiceDependency] = []
        sources_used: set[str] = set()

        try:
            # Step 2: Strategy 1 — Span-based discovery.
            if span_attrs.has_span_data and span_attrs.caller_attr:
                span_edges = await asyncio.wait_for(
                    _build_span_edges(
                        account_id, headers, endpoint, span_attrs,
                        known_services, intelligence.naming_convention,
                        window_hours,
                    ),
                    timeout=GRAPH_BUILD_TIMEOUT_S / 2,
                )
                if span_edges:
                    sources_used.add("span")
                    logger.info(
                        "Strategy 1 (Span): %d edges discovered for account %s",
                        len(span_edges), account_id,
                    )
            else:
                graph.warnings.append("Span data unavailable, using log fallback")

            # Step 3: Strategy 2 — Log-based discovery.
            service_attr = intelligence.logs.service_attribute or "service.name"
            log_edges = await asyncio.wait_for(
                _build_log_edges(
                    account_id, headers, endpoint,
                    known_services, service_attr, window_hours,
                ),
                timeout=GRAPH_BUILD_TIMEOUT_S / 2,
            )
            if log_edges:
                sources_used.add("log")
                logger.info(
                    "Strategy 2 (Log): %d edges discovered for account %s",
                    len(log_edges), account_id,
                )

        except asyncio.TimeoutError:
            graph.warnings.append(
                "Graph build timed out — returning partial graph"
            )
            logger.warning(
                "Graph build timed out for account %s after %ds",
                account_id, GRAPH_BUILD_TIMEOUT_S,
            )

        # Step 4: Determine if we need Strategy 3.
        total_edges_so_far = len(span_edges) + len(log_edges)
        span_coverage = (
            len({e.caller for e in span_edges} | {e.callee for e in span_edges})
            / max(len(known_services), 1) * 100
        ) if known_services else 0

        if span_coverage < 20 and total_edges_so_far < len(known_services) * 0.2:
            inferred_edges = _build_inferred_edges(intelligence)
            if inferred_edges:
                sources_used.add("inferred")
                graph.warnings.append(
                    f"Low span coverage ({span_coverage:.0f}%), "
                    f"added {len(inferred_edges)} inferred edges"
                )

        # Step 5: Merge edges.
        merged = _merge_edges(span_edges, log_edges, inferred_edges)

        # Step 6: Classify internal vs external.
        internal_edges: dict[str, ServiceDependency] = {}
        external_deps: dict[str, list[str]] = {}

        for key, edge in merged.items():
            callee_is_internal = _classify_edge(edge, known_services_lower)
            caller_is_internal = edge.caller.lower() in known_services_lower

            if callee_is_internal:
                internal_edges[key] = edge
            elif caller_is_internal:
                # External dependency: store under caller.
                if edge.caller not in external_deps:
                    external_deps[edge.caller] = []
                if edge.callee not in external_deps[edge.caller]:
                    external_deps[edge.caller].append(edge.callee)

        graph.external_dependencies = external_deps

        # Step 7: Build DependencyNodes.
        nodes: dict[str, DependencyNode] = {}

        # Ensure all services from internal edges have nodes.
        for key, edge in internal_edges.items():
            for svc_name in (edge.caller, edge.callee):
                if svc_name not in nodes:
                    nodes[svc_name] = DependencyNode(service_name=svc_name)

        # Build adjacency.
        for key, edge in internal_edges.items():
            caller_node = nodes.get(edge.caller)
            callee_node = nodes.get(edge.callee)

            if caller_node and edge.callee not in caller_node.direct_dependencies:
                caller_node.direct_dependencies.append(edge.callee)
                caller_node.dependency_details[edge.callee] = edge

            if callee_node and edge.caller not in callee_node.direct_dependents:
                callee_node.direct_dependents.append(edge.caller)

        # Step 8: Build transitive dependencies with cycle detection.
        nodes, cycle_warnings = build_transitive_dependencies(nodes)
        graph.warnings.extend(cycle_warnings)

        # Step 9: Compute stats.
        graph.nodes = nodes
        graph.total_services = len(nodes)
        graph.total_edges = len(internal_edges)

        # Determine build source.
        if "span" in sources_used and "log" in sources_used:
            graph.build_source = "mixed"
        elif "span" in sources_used:
            graph.build_source = "span"
        elif "log" in sources_used:
            graph.build_source = "log"
        elif "inferred" in sources_used:
            graph.build_source = "inferred"
        else:
            graph.build_source = "unavailable"

        # Coverage: % of known APM services found as nodes.
        if known_services:
            services_in_graph = {n.lower() for n in nodes}
            matched_count = sum(
                1 for s in known_services if s.lower() in services_in_graph
            )
            graph.coverage_pct = round(matched_count / len(known_services) * 100, 1)
        else:
            graph.coverage_pct = 0.0

        if graph.coverage_pct < 30 and graph.total_edges > 0:
            graph.warnings.append(
                f"Low coverage: only {graph.coverage_pct:.0f}% of known "
                f"services found in dependency graph"
            )

        # Step 10: Save to disk.
        save_graph(graph)

        logger.info(
            "Dependency graph built for account %s: %d services, %d edges, "
            "%.1f%% coverage, source=%s",
            account_id, graph.total_services, graph.total_edges,
            graph.coverage_pct, graph.build_source,
        )

    except Exception as exc:
        logger.error("Dependency graph build failed for account %s: %s", account_id, exc)
        graph.warnings.append(f"Graph build failed: {exc}")
        graph.build_source = "unavailable"

    return graph
