"""
Golden signals tool for Sherlock.

Retrieves the four golden signals (latency, traffic, errors, saturation)
for a given APM service, plus timeseries data for trend detection.
Internally uses the discovery engine and query builder for adaptive queries,
with legacy queries as fallback.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from tools.investigate import _safe_extract_results, _strip_null_timeseries

import httpx

from client.newrelic import get_client
from core.context import AccountContext
from core.deeplinks import get_builder as _get_deeplink_builder
from core.discovery import (
    EVENT_REGISTRY,
    AvailableEventType,
    DiscoveryResult,
    discover_available_data,
)
from core.query_builder import (
    build_investigation_queries,
    get_health_check,
)
from core.sanitize import check_env_mismatch, fuzzy_resolve_service, sanitize_service_name
from tools.investigate import InvestigationAnchor

logger = logging.getLogger("sherlock.tools.golden_signals")

# NerdGraph NRQL query template.
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

# Golden signal NRQL queries (legacy — kept for fallback).
NRQL_LATENCY = (
    "SELECT average(duration) as avg_duration, "
    "percentile(duration, 50, 90, 95, 99) "
    "FROM Transaction WHERE appName = '%s' "
    "SINCE %d minutes ago"
)

NRQL_THROUGHPUT = (
    "SELECT rate(count(*), 1 minute) as rpm "
    "FROM Transaction WHERE appName = '%s' "
    "SINCE %d minutes ago"
)

NRQL_ERROR_RATE = (
    "SELECT percentage(count(*), WHERE error IS true) as error_rate, "
    "count(*) as total_transactions "
    "FROM Transaction WHERE appName = '%s' "
    "SINCE %d minutes ago"
)

NRQL_SATURATION = (
    "SELECT average(cpuPercent) as avg_cpu, "
    "average(memoryResidentSizeBytes/1024/1024) as avg_memory_mb "
    "FROM Transaction WHERE appName = '%s' "
    "SINCE %d minutes ago"
)

NRQL_LATENCY_TIMESERIES = (
    "SELECT average(duration) as avg_duration "
    "FROM Transaction WHERE appName = '%s' "
    "TIMESERIES %d minutes SINCE %d minutes ago"
)

NRQL_ERROR_TIMESERIES = (
    "SELECT percentage(count(*), WHERE error IS true) as error_rate "
    "FROM Transaction WHERE appName = '%s' "
    "TIMESERIES %d minutes SINCE %d minutes ago"
)


def _timeseries_bucket(since_minutes: int) -> int:
    """Auto-scale TIMESERIES bucket to stay within NRQL 366-bucket limit."""
    return max(5, -(-since_minutes // 366))  # ceil division

NRQL_TOP_ERRORS = (
    "SELECT count(*) FROM TransactionError WHERE appName = '%s' "
    "FACET error.class, error.message "
    "SINCE %d minutes ago LIMIT 10"
)

# Thresholds for signal detection.
LATENCY_P99_WARN_MS = 5.0
ERROR_RATE_WARN_PCT = 5.0
ERROR_RATE_CRITICAL_PCT = 20.0
CPU_WARN_PCT = 80.0


async def get_service_golden_signals(
    service_name: str, since_minutes: int = 30
) -> str:
    """Get the four golden signals for an APM service.

    Uses the discovery engine to check what APM data exists, then
    queries discovered event types via the query builder. Falls back
    to legacy queries if discovery finds nothing.

    Args:
        service_name: APM service name (fuzzy resolved).
        since_minutes: Time window in minutes.

    Returns:
        JSON string with golden signals data and health assessment.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        safe_name = sanitize_service_name(service_name)
        resolved_name, was_fuzzy, confidence = fuzzy_resolve_service(
            safe_name, intelligence.apm.service_names,
            naming_convention=intelligence.naming_convention,
        )

        # Build anchor for query builder.
        now = datetime.now(timezone.utc)
        anchor = InvestigationAnchor(
            primary_service=resolved_name,
            all_candidates=[resolved_name],
            window_start=now - timedelta(minutes=since_minutes),
            since_minutes=since_minutes,
            until_clause="",
            window_source="requested",
        )

        # Discover available APM data.
        discovery = await discover_available_data(
            service_candidates=[resolved_name],
            anchor=anchor,
            credentials=credentials,
        )

        # After discovery, the cache may contain the real entity name.
        cached = ctx.get_cached_resolution(service_name)
        if cached and cached != anchor.primary_service:
            anchor.primary_service = cached
            anchor.all_candidates = [cached]
            resolved_name = cached

        # Filter to APM domain.
        apm_available = {
            k: v for k, v in discovery.available.items()
            if v.domain == "apm"
        }

        if not apm_available:
            # Fallback to legacy queries.
            return await _legacy_golden_signals(
                resolved_name, was_fuzzy, service_name,
                since_minutes, credentials, client, start,
            )

        # Build APM-only discovery result.
        apm_discovery = DiscoveryResult(
            available=apm_available,
            unavailable=[],
            domains_with_data=["apm"],
            service_filter_map={k: v for k, v in discovery.service_filter_map.items() if k in apm_available},
            discovery_duration_ms=discovery.discovery_duration_ms,
            total_event_types_checked=discovery.total_event_types_checked,
        )

        # Build queries.
        severity_attr = intelligence.logs.severity_attribute or "level"
        queries = build_investigation_queries(
            discovery=apm_discovery,
            anchor=anchor,
            severity_attr=severity_attr,
            naming_convention=intelligence.naming_convention,
        )

        # Run ALL queries in parallel.
        async def _run_query(q):
            try:
                escaped_nrql = q.nrql.replace('"', '\\"')
                gql = GQL_NRQL_QUERY % (credentials.account_id, escaped_nrql)
                headers = {
                    "API-Key": credentials.api_key,
                    "Content-Type": "application/json",
                }
                async with httpx.AsyncClient(timeout=15) as client_http:
                    resp = await client_http.post(
                        credentials.endpoint,
                        json={"query": gql},
                        headers=headers,
                    )
                    resp.raise_for_status()
                    body = resp.json()
                return _safe_extract_results(body)
            except Exception:
                return []

        results = await asyncio.gather(
            *[_run_query(q) for q in queries],
            return_exceptions=True,
        )

        # Derive health signals.
        signals: list[str] = []
        raw_results: dict = {}

        for query, result in zip(queries, results):
            if isinstance(result, Exception):
                raw_results[query.signal] = []
                continue
            raw_results[query.signal] = result
            health_check = get_health_check(query.signal)
            query_signals = health_check(result)
            signals.extend(query_signals)

        # Overall status.
        if any("🔴" in s for s in signals):
            overall = "CRITICAL"
        elif any("⚠️" in s for s in signals):
            overall = "WARNING"
        else:
            overall = "HEALTHY"

        duration_ms = int((time.time() - start) * 1000)
        response: dict = {
            "service_name": resolved_name,
            "since_minutes": since_minutes,
            "overall_status": overall,
            "health_signals": signals,
            "discovered_event_types": list(apm_available.keys()),
            "queries_run": len(queries),
            "raw_data": raw_results,
            "duration_ms": duration_ms,
        }

        # Deep links — only when health_signals is non-empty.
        if signals:
            try:
                _builder = _get_deeplink_builder()
                if _builder:
                    _guid = intelligence.apm.service_guids.get(resolved_name)
                    _err_nrql = (
                        f"SELECT percentage(count(*), WHERE error IS true) as error_rate "
                        f"FROM Transaction WHERE appName='{resolved_name}' "
                        f"TIMESERIES 5 minutes SINCE {since_minutes} minutes ago"
                    )
                    _p95_nrql = (
                        f"SELECT percentile(duration, 95) as p95 "
                        f"FROM Transaction WHERE appName='{resolved_name}' "
                        f"TIMESERIES 5 minutes SINCE {since_minutes} minutes ago"
                    )
                    _tput_nrql = (
                        f"SELECT rate(count(*), 1 minute) as rpm "
                        f"FROM Transaction WHERE appName='{resolved_name}' "
                        f"TIMESERIES 5 minutes SINCE {since_minutes} minutes ago"
                    )
                    response["links"] = {
                        "service_overview": _builder.entity_link(_guid) if _guid else None,
                        "error_chart": _builder.nrql_chart(_err_nrql, since_minutes),
                        "latency_chart": _builder.nrql_chart(_p95_nrql, since_minutes),
                        "throughput_chart": _builder.nrql_chart(_tput_nrql, since_minutes),
                    }
            except Exception:
                pass

        if was_fuzzy:
            response["resolved_from"] = service_name
            response["note"] = f"Fuzzy matched '{service_name}' → '{resolved_name}'"
            env_warn = check_env_mismatch(
                service_name, resolved_name, intelligence.naming_convention,
            )
            if env_warn:
                response["warnings"] = [env_warn]

        return json.dumps(response)

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_service_golden_signals",
            "hint": "Check service name. Use get_apm_applications() to list all.",
            "data_available": False,
        })


async def _legacy_golden_signals(
    resolved_name: str,
    was_fuzzy: bool,
    service_name: str,
    since_minutes: int,
    credentials,
    client,
    start_time: float,
) -> str:
    """Fallback to legacy golden signal queries."""

    async def _nrql(nrql_str: str) -> list:
        escaped = nrql_str.replace('"', '\\"')
        query = GQL_NRQL_QUERY % (credentials.account_id, escaped)
        result = await client.query(query, timeout_override=20)
        return _safe_extract_results(result)

    # Run all golden signal queries in parallel.
    bucket = _timeseries_bucket(since_minutes)
    latency_task = _nrql(NRQL_LATENCY % (resolved_name, since_minutes))
    throughput_task = _nrql(NRQL_THROUGHPUT % (resolved_name, since_minutes))
    error_task = _nrql(NRQL_ERROR_RATE % (resolved_name, since_minutes))
    saturation_task = _nrql(NRQL_SATURATION % (resolved_name, since_minutes))
    latency_ts_task = _nrql(NRQL_LATENCY_TIMESERIES % (resolved_name, bucket, since_minutes))
    error_ts_task = _nrql(NRQL_ERROR_TIMESERIES % (resolved_name, bucket, since_minutes))
    top_errors_task = _nrql(NRQL_TOP_ERRORS % (resolved_name, since_minutes))

    results = await asyncio.gather(
        latency_task, throughput_task, error_task, saturation_task,
        latency_ts_task, error_ts_task, top_errors_task,
        return_exceptions=True,
    )

    latency = results[0] if not isinstance(results[0], BaseException) else []
    throughput = results[1] if not isinstance(results[1], BaseException) else []
    errors = results[2] if not isinstance(results[2], BaseException) else []
    saturation = results[3] if not isinstance(results[3], BaseException) else []
    latency_ts = results[4] if not isinstance(results[4], BaseException) else []
    error_ts = results[5] if not isinstance(results[5], BaseException) else []
    top_errors = results[6] if not isinstance(results[6], BaseException) else []

    # Extract scalar values.
    latency_data = latency[0] if latency else {}
    throughput_data = throughput[0] if throughput else {}
    error_data = errors[0] if errors else {}
    saturation_data = saturation[0] if saturation else {}

    # Derive health signals.
    signals: list[str] = []
    error_rate = error_data.get("error_rate", 0) or 0
    avg_duration = latency_data.get("avg_duration", 0) or 0
    p99 = latency_data.get("percentile.duration.99", 0) or 0
    avg_cpu = saturation_data.get("avg_cpu", 0) or 0

    if error_rate >= ERROR_RATE_CRITICAL_PCT:
        signals.append(f"🔴 CRITICAL error rate: {error_rate:.1f}%")
    elif error_rate >= ERROR_RATE_WARN_PCT:
        signals.append(f"⚠️ Elevated error rate: {error_rate:.1f}%")

    if p99 >= LATENCY_P99_WARN_MS:
        signals.append(f"⚠️ High P99 latency: {p99:.2f}s")

    if avg_cpu >= CPU_WARN_PCT:
        signals.append(f"⚠️ High CPU saturation: {avg_cpu:.1f}%")

    rpm = throughput_data.get("rpm", 0) or 0
    if rpm == 0:
        signals.append("🔴 ZERO throughput — service may be down")

    # Overall status.
    if any("🔴" in s for s in signals):
        overall = "CRITICAL"
    elif any("⚠️" in s for s in signals):
        overall = "WARNING"
    else:
        overall = "HEALTHY"

    duration_ms = int((time.time() - start_time) * 1000)
    response: dict = {
        "service_name": resolved_name,
        "since_minutes": since_minutes,
        "overall_status": overall,
        "health_signals": signals,
        "latency": {
            "avg_duration_s": avg_duration,
            "p50": latency_data.get("percentile.duration.50"),
            "p90": latency_data.get("percentile.duration.90"),
            "p95": latency_data.get("percentile.duration.95"),
            "p99": p99,
        },
        "throughput": {
            "rpm": rpm,
        },
        "errors": {
            "error_rate_pct": error_rate,
            "total_transactions": error_data.get("total_transactions", 0),
            "top_errors": top_errors,
        },
        "saturation": {
            "avg_cpu_pct": avg_cpu,
            "avg_memory_mb": saturation_data.get("avg_memory_mb"),
        },
        "latency_timeseries": _strip_null_timeseries(latency_ts),
        "error_timeseries": _strip_null_timeseries(error_ts),
        "duration_ms": duration_ms,
    }

    # Deep links — only when health_signals is non-empty.
    if signals:
        try:
            _builder = _get_deeplink_builder()
            if _builder:
                _guid = intelligence.apm.service_guids.get(resolved_name) if hasattr(intelligence, 'apm') else None
                _err_nrql = (
                    f"SELECT percentage(count(*), WHERE error IS true) as error_rate "
                    f"FROM Transaction WHERE appName='{resolved_name}' "
                    f"TIMESERIES 5 minutes SINCE {since_minutes} minutes ago"
                )
                _p95_nrql = (
                    f"SELECT percentile(duration, 95) as p95 "
                    f"FROM Transaction WHERE appName='{resolved_name}' "
                    f"TIMESERIES 5 minutes SINCE {since_minutes} minutes ago"
                )
                _tput_nrql = (
                    f"SELECT rate(count(*), 1 minute) as rpm "
                    f"FROM Transaction WHERE appName='{resolved_name}' "
                    f"TIMESERIES 5 minutes SINCE {since_minutes} minutes ago"
                )
                response["links"] = {
                    "service_overview": _builder.entity_link(_guid) if _guid else None,
                    "error_chart": _builder.nrql_chart(_err_nrql, since_minutes),
                    "latency_chart": _builder.nrql_chart(_p95_nrql, since_minutes),
                    "throughput_chart": _builder.nrql_chart(_tput_nrql, since_minutes),
                }
        except Exception:
            pass

    if was_fuzzy:
        response["resolved_from"] = service_name
        response["note"] = f"Fuzzy matched '{service_name}' → '{resolved_name}'"

    return json.dumps(response)

