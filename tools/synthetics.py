"""
Synthetic monitoring tools for Sherlock.

Provides tools to list synthetic monitors, check monitor status,
get raw results, and perform deep investigation of synthetic failures.
"""

import asyncio
import json
import logging
import time

from client.newrelic import get_client
from core.context import AccountContext
from core.exceptions import MonitorNotFoundError
from core.sanitize import fuzzy_resolve_monitor, sanitize_service_name

logger = logging.getLogger("sherlock.tools.synthetics")

# Event type constants.
SYNTHETIC_CHECK_EVENT = "SyntheticCheck"
SYNTHETIC_REQUEST_EVENT = "SyntheticRequest"
MONITOR_RESULT_PASSED = "SUCCESS"
MONITOR_RESULT_FAILED = "FAILED"

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

# ── NRQL Templates ──────────────────────────────────────────────────────

NRQL_OVERALL_PASS_RATE = (
    "SELECT percentage(count(*), WHERE result = 'SUCCESS') as pass_rate, "
    "count(*) as total_runs, "
    "average(duration) as avg_duration_ms "
    "FROM SyntheticCheck "
    "WHERE monitorName = '%s' "
    "SINCE %d minutes ago"
)

NRQL_BY_LOCATION = (
    "SELECT latest(result) as last_result, "
    "percentage(count(*), WHERE result = 'SUCCESS') as pass_rate, "
    "latest(duration) as last_duration_ms, "
    "latest(error) as last_error "
    "FROM SyntheticCheck "
    "WHERE monitorName = '%s' "
    "FACET locationLabel "
    "SINCE %d minutes ago "
    "LIMIT 20"
)

NRQL_RECENT_FAILURES = (
    "SELECT timestamp, locationLabel, result, duration, "
    "error "
    "FROM SyntheticCheck "
    "WHERE monitorName = '%s' "
    "AND result = 'FAILED' "
    "SINCE %d minutes ago "
    "ORDER BY timestamp DESC "
    "LIMIT 20"
)

NRQL_PASS_RATE_TIMESERIES = (
    "SELECT percentage(count(*), WHERE result = 'SUCCESS') as pass_rate "
    "FROM SyntheticCheck "
    "WHERE monitorName = '%s' "
    "TIMESERIES 5 minutes "
    "SINCE %d minutes ago"
)

NRQL_DURATION_TIMESERIES = (
    "SELECT average(duration) as avg_ms, max(duration) as max_ms "
    "FROM SyntheticCheck "
    "WHERE monitorName = '%s' "
    "TIMESERIES 5 minutes "
    "SINCE %d minutes ago"
)

NRQL_MONITOR_RAW_RESULTS = (
    "SELECT timestamp, locationLabel, result, duration, "
    "error "
    "FROM SyntheticCheck "
    "WHERE monitorName = '%s' "
    "%s"
    "SINCE %d minutes ago "
    "ORDER BY timestamp DESC "
    "LIMIT %d"
)

NRQL_SYNTHETIC_REQUESTS = (
    "SELECT timestamp, URL, verb as method, responseCode, duration "
    "FROM SyntheticRequest "
    "WHERE monitorName = '%s' "
    "SINCE %d minutes ago "
    "ORDER BY timestamp DESC "
    "LIMIT %d"
)

# ── Thresholds ───────────────────────────────────────────────────────────

PASS_RATE_CRITICAL = 50.0
PASS_RATE_WARN = 90.0
DURATION_WARN_MS = 10000

# Per-source timeout for investigation tasks (seconds).
INVESTIGATION_SOURCE_TIMEOUT_S = 20


def _resolve_monitor(
    monitor_name: str, monitor_names: list[str]
) -> tuple[str, bool, float]:
    """Resolve monitor name with fuzzy matching.

    Args:
        monitor_name: User-provided monitor name.
        monitor_names: Known monitor names from intelligence.

    Returns:
        Tuple of (resolved_name, was_fuzzy, confidence).
    """
    return fuzzy_resolve_monitor(monitor_name, monitor_names, threshold=0.5)


async def get_synthetic_monitors() -> str:
    """Get all synthetic monitors for the active account.

    Uses cached intelligence — no live API call if cache is fresh.

    Returns:
        JSON string with monitor list and summary statistics.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()

        if not intelligence.synthetics.enabled:
            return json.dumps({
                "error": "Synthetic monitoring is not enabled for this account.",
                "tool": "get_synthetic_monitors",
                "hint": "Set up synthetic monitors in New Relic.",
                "data_available": False,
            })

        monitors = []
        enabled_count = 0
        disabled_count = 0
        muted_count = 0
        by_type: dict[str, int] = {}
        currently_failing: list[str] = []

        for name, meta in intelligence.synthetics.monitor_map.items():
            status_upper = (meta.status or "ENABLED").upper()
            if status_upper == "ENABLED":
                enabled_count += 1
            elif status_upper == "DISABLED":
                disabled_count += 1
            elif status_upper == "MUTED":
                muted_count += 1
            else:
                enabled_count += 1

            mon_type = meta.type or "UNKNOWN"
            by_type[mon_type] = by_type.get(mon_type, 0) + 1

            monitors.append({
                "name": name,
                "guid": meta.guid,
                "type": mon_type,
                "status": status_upper,
                "period": meta.period,
                "locations": meta.locations,
                "associated_service": meta.associated_service,
                "alert_severity": "",
            })

        duration_ms = int((time.time() - start) * 1000)
        return json.dumps({
            "total": intelligence.synthetics.total_count,
            "enabled": enabled_count,
            "disabled": disabled_count,
            "muted": muted_count,
            "monitors": monitors,
            "by_type": by_type,
            "currently_failing": currently_failing,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_synthetic_monitors",
            "hint": "Ensure you are connected.",
            "data_available": False,
        })


async def get_monitor_status(
    monitor_name: str, since_minutes: int = 60
) -> str:
    """Get detailed status for a specific synthetic monitor.

    Fuzzy resolves the monitor name and runs five NRQL queries in parallel:
    overall pass/fail rate, by-location breakdown, recent failures,
    pass rate timeseries, and duration timeseries.

    Args:
        monitor_name: Synthetic monitor name (fuzzy resolved).
        since_minutes: Time window in minutes.

    Returns:
        JSON string with comprehensive monitor status and diagnosis.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        # Fuzzy resolve.
        try:
            resolved_name, was_fuzzy, confidence = _resolve_monitor(
                monitor_name, intelligence.synthetics.monitor_names
            )
        except MonitorNotFoundError as mnf:
            return json.dumps({
                "error": f"Monitor '{monitor_name}' not found.",
                "tool": "get_monitor_status",
                "closest_matches": mnf.closest_matches,
                "known_monitors": mnf.known_monitors[:10],
                "hint": "Check the monitor name.",
                "data_available": False,
            })

        if was_fuzzy:
            logger.warning(
                "Fuzzy monitor resolution: '%s' → '%s' (confidence: %.2f)",
                monitor_name, resolved_name, confidence,
            )

        # Get monitor metadata from intelligence.
        monitor_meta = intelligence.synthetics.monitor_map.get(resolved_name)
        monitor_type = monitor_meta.type if monitor_meta else "UNKNOWN"
        associated_service = monitor_meta.associated_service if monitor_meta else None

        safe_name = sanitize_service_name(resolved_name)

        async def _nrql(nrql_str: str) -> list:
            escaped = nrql_str.replace('"', '\\"')
            query = GQL_NRQL_QUERY % (credentials.account_id, escaped)
            result = await client.query(query, timeout_override=20)
            return (
                result.get("data", {})
                .get("actor", {})
                .get("account", {})
                .get("nrql", {})
                .get("results", [])
            )

        # Run all 5 queries in parallel.
        overall_task = _nrql(NRQL_OVERALL_PASS_RATE % (safe_name, since_minutes))
        location_task = _nrql(NRQL_BY_LOCATION % (safe_name, since_minutes))
        failures_task = _nrql(NRQL_RECENT_FAILURES % (safe_name, since_minutes))
        pass_ts_task = _nrql(NRQL_PASS_RATE_TIMESERIES % (safe_name, since_minutes))
        dur_ts_task = _nrql(NRQL_DURATION_TIMESERIES % (safe_name, since_minutes))

        results = await asyncio.gather(
            overall_task, location_task, failures_task, pass_ts_task, dur_ts_task,
            return_exceptions=True,
        )

        overall = results[0] if not isinstance(results[0], BaseException) else []
        by_location = results[1] if not isinstance(results[1], BaseException) else []
        recent_failures = results[2] if not isinstance(results[2], BaseException) else []
        pass_ts = results[3] if not isinstance(results[3], BaseException) else []
        dur_ts = results[4] if not isinstance(results[4], BaseException) else []

        overall_data = overall[0] if overall else {}
        pass_rate_raw = overall_data.get("pass_rate")
        pass_rate = pass_rate_raw if pass_rate_raw is not None else 100
        total_runs = overall_data.get("total_runs", 0) or 0
        avg_duration = overall_data.get("avg_duration_ms", 0) or 0

        # Analyze locations.
        failing_locations: list[str] = []
        total_locations = len(by_location)
        for loc in by_location:
            loc_result = loc.get("last_result", "")
            loc_name = loc.get("locationLabel", loc.get("facet", "unknown"))
            if loc_result == MONITOR_RESULT_FAILED:
                failing_locations.append(loc_name)

        # Derive status signals.
        status_signals: list[str] = []

        if pass_rate < PASS_RATE_CRITICAL:
            status_signals.append(
                f"🔴 MONITOR FAILING: {pass_rate:.1f}% success rate"
            )
        elif pass_rate < PASS_RATE_WARN:
            status_signals.append(
                f"⚠️ Intermittent failures: {pass_rate:.1f}% success"
            )

        if failing_locations:
            if len(failing_locations) == total_locations and total_locations > 0:
                status_signals.append(
                    "🔴 GLOBALLY DOWN — failing in all locations"
                )
            elif len(failing_locations) == 1:
                status_signals.append(
                    f"⚠️ Regional issue — failing only in {failing_locations[0]}"
                )
            else:
                status_signals.append(
                    f"🔴 FAILING IN: {', '.join(failing_locations)}"
                )

        if avg_duration > DURATION_WARN_MS:
            status_signals.append(
                f"⚠️ Slow monitor: avg {avg_duration:.0f}ms"
            )

        # Check for duration spikes in timeseries.
        if dur_ts and len(dur_ts) >= 3:
            durations = [d.get("avg_ms", 0) or 0 for d in dur_ts]
            if durations:
                avg_of_durations = sum(durations) / len(durations) if durations else 0
                for i, d in enumerate(durations):
                    if d > avg_of_durations * 2 and d > 5000:
                        ts_point = dur_ts[i].get("beginTimeSeconds", "")
                        status_signals.append(
                            f"⚠️ Duration spike detected at {ts_point}"
                        )
                        break

        # Diagnosis.
        if failing_locations and len(failing_locations) == total_locations and total_locations > 0:
            diagnosis = "GLOBAL_FAILURE"
        elif failing_locations and len(failing_locations) < total_locations:
            diagnosis = "REGIONAL_FAILURE"
        elif pass_rate < PASS_RATE_WARN:
            diagnosis = "INTERMITTENT"
        elif avg_duration > DURATION_WARN_MS:
            diagnosis = "DEGRADED_PERFORMANCE"
        else:
            diagnosis = "PASSING"

        duration_ms = int((time.time() - start) * 1000)
        response: dict = {
            "monitor_name": resolved_name,
            "resolved_from": monitor_name if was_fuzzy else None,
            "monitor_type": monitor_type,
            "associated_service": associated_service,
            "since_minutes": since_minutes,
            "status_signals": status_signals,
            "overall": {
                "pass_rate": pass_rate,
                "total_runs": total_runs,
                "avg_duration_ms": avg_duration,
            },
            "by_location": by_location,
            "recent_failures": recent_failures,
            "pass_rate_timeseries": pass_ts,
            "duration_timeseries": dur_ts,
            "diagnosis": diagnosis,
            "duration_ms": duration_ms,
        }

        return json.dumps(response)

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_monitor_status",
            "hint": "Check the monitor name. Use get_synthetic_monitors() to list all.",
            "data_available": False,
        })


async def get_monitor_results(
    monitor_name: str,
    result_filter: str | None = None,
    since_minutes: int = 60,
    limit: int = 50,
) -> str:
    """Get raw run results for a synthetic monitor.

    Useful for digging into specific failures with full error details.

    Args:
        monitor_name: Synthetic monitor name (fuzzy resolved).
        result_filter: Optional filter — 'FAILED', 'SUCCESS', or None for all.
        since_minutes: Time window in minutes.
        limit: Maximum results to return.

    Returns:
        JSON string with raw monitor results and failed requests.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        # Fuzzy resolve.
        try:
            resolved_name, was_fuzzy, confidence = _resolve_monitor(
                monitor_name, intelligence.synthetics.monitor_names
            )
        except MonitorNotFoundError as mnf:
            return json.dumps({
                "error": f"Monitor '{monitor_name}' not found.",
                "tool": "get_monitor_results",
                "closest_matches": mnf.closest_matches,
                "data_available": False,
            })

        safe_name = sanitize_service_name(resolved_name)

        # Build filter clause.
        filter_clause = ""
        if result_filter:
            safe_filter = sanitize_service_name(result_filter).upper()
            if safe_filter in ("FAILED", "SUCCESS"):
                filter_clause = f"AND result = '{safe_filter}' "

        async def _nrql(nrql_str: str) -> list:
            escaped = nrql_str.replace('"', '\\"')
            query = GQL_NRQL_QUERY % (credentials.account_id, escaped)
            result = await client.query(query, timeout_override=20)
            return (
                result.get("data", {})
                .get("actor", {})
                .get("account", {})
                .get("nrql", {})
                .get("results", [])
            )

        # Get check results and request results in parallel.
        checks_nrql = NRQL_MONITOR_RAW_RESULTS % (
            safe_name, filter_clause, since_minutes, min(limit, 200)
        )
        requests_nrql = NRQL_SYNTHETIC_REQUESTS % (
            safe_name, since_minutes, min(limit, 200)
        )

        checks_task = _nrql(checks_nrql)
        requests_task = _nrql(requests_nrql)

        check_results, request_results = await asyncio.gather(
            checks_task, requests_task, return_exceptions=True,
        )

        checks = check_results if not isinstance(check_results, BaseException) else []
        requests = request_results if not isinstance(request_results, BaseException) else []

        # Filter failed requests (responseCode >= 400).
        failed_requests = [
            r for r in requests
            if (r.get("responseCode") or 0) >= 400
        ]

        duration_ms = int((time.time() - start) * 1000)
        return json.dumps({
            "monitor_name": resolved_name,
            "resolved_from": monitor_name if was_fuzzy else None,
            "result_filter": result_filter,
            "total_results": len(checks),
            "runs": checks,
            "failed_requests": failed_requests,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_monitor_results",
            "hint": "Check monitor name.",
            "data_available": False,
        })


async def investigate_synthetic(
    monitor_name: str, since_minutes: int = 60
) -> str:
    """Deep investigation of a specific synthetic monitor.

    Runs monitor status, failure details, correlated APM golden signals,
    and active incidents all in parallel to build a comprehensive diagnosis.

    Args:
        monitor_name: Synthetic monitor name (fuzzy resolved).
        since_minutes: Time window in minutes.

    Returns:
        JSON string with investigation report, findings, and recommendations.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()

        # Fuzzy resolve.
        try:
            resolved_name, was_fuzzy, confidence = _resolve_monitor(
                monitor_name, intelligence.synthetics.monitor_names
            )
        except MonitorNotFoundError as mnf:
            return json.dumps({
                "error": f"Monitor '{monitor_name}' not found.",
                "tool": "investigate_synthetic",
                "closest_matches": mnf.closest_matches,
                "data_available": False,
            })

        monitor_meta = intelligence.synthetics.monitor_map.get(resolved_name)
        associated_service = monitor_meta.associated_service if monitor_meta else None
        monitor_type = monitor_meta.type if monitor_meta else "UNKNOWN"

        # Phase 1: Parallel data gathering.
        from tools.alerts import get_service_incidents
        from tools.golden_signals import get_service_golden_signals

        async def _skipped_result() -> str:
            """Return a placeholder result for skipped data sources."""
            return json.dumps({"skipped": True})

        tasks = [
            asyncio.wait_for(
                get_monitor_status(resolved_name, since_minutes),
                timeout=INVESTIGATION_SOURCE_TIMEOUT_S,
            ),
            asyncio.wait_for(
                get_monitor_results(resolved_name, "FAILED", since_minutes, 20),
                timeout=INVESTIGATION_SOURCE_TIMEOUT_S,
            ),
        ]

        # Correlated APM data if associated service exists.
        if associated_service:
            tasks.append(
                asyncio.wait_for(
                    get_service_golden_signals(associated_service, since_minutes),
                    timeout=INVESTIGATION_SOURCE_TIMEOUT_S,
                )
            )
        else:
            tasks.append(_skipped_result())

        tasks.append(
            asyncio.wait_for(
                get_service_incidents(resolved_name),
                timeout=INVESTIGATION_SOURCE_TIMEOUT_S,
            )
        )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Parse results.
        monitor_status = {}
        if not isinstance(results[0], BaseException):
            try:
                monitor_status = json.loads(results[0])
            except (json.JSONDecodeError, TypeError):
                pass

        failure_details = {}
        if not isinstance(results[1], BaseException):
            try:
                failure_details = json.loads(results[1])
            except (json.JSONDecodeError, TypeError):
                pass

        correlated_service = {}
        if not isinstance(results[2], BaseException):
            try:
                correlated_service = json.loads(results[2])
            except (json.JSONDecodeError, TypeError):
                pass

        active_incidents = {}
        if not isinstance(results[3], BaseException):
            try:
                active_incidents = json.loads(results[3])
            except (json.JSONDecodeError, TypeError):
                pass

        # Phase 2: Analysis.
        diagnosis_code = monitor_status.get("diagnosis", "UNKNOWN")
        status_signals = monitor_status.get("status_signals", [])
        overall = monitor_status.get("overall", {})
        pass_rate = overall.get("pass_rate", 100)
        by_location = monitor_status.get("by_location", [])
        recent_failures = failure_details.get("runs", [])

        # APM health.
        apm_status = correlated_service.get("overall_status", "UNKNOWN")
        apm_error_rate = (
            correlated_service.get("errors", {}).get("error_rate_pct", 0)
        )
        apm_healthy = apm_status == "HEALTHY"

        # Failing locations.
        failing_locations = [
            loc.get("locationLabel", loc.get("facet", "unknown"))
            for loc in by_location
            if loc.get("last_result") == MONITOR_RESULT_FAILED
        ]

        # Recent error messages.
        error_messages = []
        for failure in recent_failures[:5]:
            err = failure.get("error", "")
            if err:
                error_messages.append(err)

        # Phase 3: Build findings.
        findings: list[dict] = []
        recommendations: list[dict] = []

        if diagnosis_code == "GLOBAL_FAILURE":
            findings.append({
                "source": "synthetics",
                "severity": "CRITICAL",
                "finding": f"Monitor '{resolved_name}' failing in ALL locations.",
            })

            if not apm_healthy and associated_service:
                findings.append({
                    "source": "apm_correlation",
                    "severity": "CRITICAL",
                    "finding": (
                        f"APM service '{associated_service}' is also unhealthy "
                        f"(error rate: {apm_error_rate:.1f}%). App-level issue."
                    ),
                })
                recommendations.append({
                    "priority": "P1",
                    "area": "application",
                    "finding": "App issue — both synthetic and APM showing failures.",
                    "action": "Check APM errors, recent deployments, and logs.",
                    "urgency": "IMMEDIATE",
                })
            elif apm_healthy and associated_service:
                findings.append({
                    "source": "apm_correlation",
                    "severity": "WARNING",
                    "finding": (
                        f"APM service '{associated_service}' is HEALTHY. "
                        "Failure is external/auth/UI, not application backend."
                    ),
                })
                recommendations.append({
                    "priority": "P1",
                    "area": "external",
                    "finding": "External/auth issue — APM healthy but synthetic failing.",
                    "action": (
                        "Check auth provider, CDN, DNS, TLS certificates, "
                        "and external API dependencies."
                    ),
                    "urgency": "IMMEDIATE",
                })
            else:
                recommendations.append({
                    "priority": "P1",
                    "area": "investigation",
                    "finding": "Global synthetic failure with no associated service.",
                    "action": "Check the monitored URL, DNS resolution, and TLS.",
                    "urgency": "IMMEDIATE",
                })

        elif diagnosis_code == "REGIONAL_FAILURE":
            findings.append({
                "source": "synthetics",
                "severity": "WARNING",
                "finding": (
                    f"Monitor '{resolved_name}' failing in: "
                    f"{', '.join(failing_locations)}."
                ),
            })
            recommendations.append({
                "priority": "P2",
                "area": "infrastructure",
                "finding": f"Regional failure in {', '.join(failing_locations)}.",
                "action": "Check CDN/DNS routing, load balancer config for those regions.",
                "urgency": "SOON",
            })

        elif diagnosis_code == "INTERMITTENT":
            findings.append({
                "source": "synthetics",
                "severity": "WARNING",
                "finding": (
                    f"Monitor '{resolved_name}' intermittent — "
                    f"{pass_rate:.1f}% pass rate."
                ),
            })
            recommendations.append({
                "priority": "P2",
                "area": "performance",
                "finding": "Intermittent failures may indicate timeout or flapping.",
                "action": (
                    "Check response times, monitor timeout settings, "
                    "and for any rate limiting."
                ),
                "urgency": "SOON",
            })

        elif diagnosis_code == "DEGRADED_PERFORMANCE":
            findings.append({
                "source": "synthetics",
                "severity": "WARNING",
                "finding": (
                    f"Monitor '{resolved_name}' passing but slow — "
                    f"avg {overall.get('avg_duration_ms', 0):.0f}ms."
                ),
            })
            recommendations.append({
                "priority": "P3",
                "area": "performance",
                "finding": "Monitor performance degraded.",
                "action": "Check backend response times and external dependencies.",
                "urgency": "PLANNED",
            })

        # Script-specific recommendation.
        if monitor_type in ("SCRIPT_BROWSER", "SCRIPT_API"):
            for err in error_messages:
                if any(kw in err.lower() for kw in ["element", "selector", "locator", "timeout"]):
                    recommendations.append({
                        "priority": "P1",
                        "area": "script",
                        "finding": "Script error suggests UI changed.",
                        "action": (
                            "Monitor script may need updating — "
                            "check if the UI/API flow changed recently."
                        ),
                        "urgency": "IMMEDIATE",
                    })
                    break

        # Login flow specific.
        if any(kw in resolved_name.lower() for kw in ["login", "auth", "signin", "sign-in"]):
            if diagnosis_code in ("GLOBAL_FAILURE", "INTERMITTENT"):
                recommendations.append({
                    "priority": "P1",
                    "area": "authentication",
                    "finding": "Login/auth flow monitor failing.",
                    "action": (
                        "Check identity provider, session handling, "
                        "MFA service, and auth token refresh."
                    ),
                    "urgency": "IMMEDIATE",
                })

        # Determine failure pattern.
        if diagnosis_code == "GLOBAL_FAILURE":
            failure_pattern = "CONSISTENT"
        elif diagnosis_code == "REGIONAL_FAILURE":
            failure_pattern = "REGIONAL"
        elif diagnosis_code == "INTERMITTENT":
            failure_pattern = "INTERMITTENT"
        else:
            failure_pattern = "NEW" if len(recent_failures) <= 2 else "STABLE"

        # Human-readable diagnosis summary.
        diagnosis_text = _build_diagnosis_text(
            resolved_name, diagnosis_code, associated_service,
            apm_healthy, failing_locations, error_messages, pass_rate,
        )

        duration_ms = int((time.time() - start) * 1000)
        return json.dumps({
            "monitor": {
                "name": resolved_name,
                "type": monitor_type,
                "associated_service": associated_service,
                "status": monitor_meta.status if monitor_meta else "UNKNOWN",
            },
            "overall_status": diagnosis_code,
            "diagnosis": diagnosis_text,
            "failure_pattern": failure_pattern,
            "findings": findings,
            "recommendations": recommendations,
            "correlated_service_health": correlated_service if associated_service else None,
            "active_incidents": active_incidents.get("incidents", []),
            "recent_failure_details": recent_failures[:10],
            "location_breakdown": by_location,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "investigate_synthetic",
            "hint": "Check monitor name.",
            "data_available": False,
        })


def _build_diagnosis_text(
    monitor_name: str,
    diagnosis_code: str,
    associated_service: str | None,
    apm_healthy: bool,
    failing_locations: list[str],
    error_messages: list[str],
    pass_rate: float,
) -> str:
    """Build a human-readable diagnosis paragraph.

    Args:
        monitor_name: The resolved monitor name.
        diagnosis_code: The diagnosis code (e.g. GLOBAL_FAILURE).
        associated_service: APM service correlated with this monitor.
        apm_healthy: Whether the APM service is healthy.
        failing_locations: List of locations where the monitor is failing.
        error_messages: Recent error messages from failures.
        pass_rate: Overall pass rate percentage.

    Returns:
        A human-readable diagnosis paragraph.
    """
    parts: list[str] = [f"Synthetic monitor '{monitor_name}'"]

    if diagnosis_code == "GLOBAL_FAILURE":
        parts.append("is FAILING GLOBALLY across all check locations.")
        if associated_service and not apm_healthy:
            parts.append(
                f"The associated APM service '{associated_service}' is also unhealthy, "
                "indicating an application-level issue."
            )
        elif associated_service and apm_healthy:
            parts.append(
                f"However, the associated APM service '{associated_service}' appears healthy. "
                "This suggests an external dependency, auth, or CDN issue rather than a backend problem."
            )
    elif diagnosis_code == "REGIONAL_FAILURE":
        parts.append(
            f"is failing in {len(failing_locations)} location(s): "
            f"{', '.join(failing_locations)}."
        )
        parts.append("Other locations are passing, suggesting a regional infrastructure issue.")
    elif diagnosis_code == "INTERMITTENT":
        parts.append(
            f"is experiencing intermittent failures with a {pass_rate:.1f}% pass rate."
        )
        parts.append(
            "The flapping pattern suggests timeouts, rate limiting, "
            "or an unstable backend dependency."
        )
    elif diagnosis_code == "DEGRADED_PERFORMANCE":
        parts.append("is passing but with degraded performance (slow response times).")
    else:
        parts.append("is currently passing all checks.")

    if error_messages:
        parts.append(f"Most recent error: {error_messages[0][:200]}")

    return " ".join(parts)
