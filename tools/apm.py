"""
APM (Application Performance Monitoring) tools for Sherlock.

Provides tools to list APM applications, get application metrics,
and retrieve deployment history. Supports both APM-agent (Transaction)
and OTel-instrumented (Span) services.
"""

import asyncio
import json
import logging
import time

from client.newrelic import get_client
from core.context import AccountContext
from core.sanitize import fuzzy_resolve_service, sanitize_service_name

logger = logging.getLogger("sherlock.tools.apm")

# GraphQL query for APM entities.
GQL_APM_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'APM' AND type = 'APPLICATION'") {
      results {
        entities {
          guid
          name
          alertSeverity
          reporting
          tags {
            key
            values
          }
        }
      }
    }
  }
}
"""

# NRQL templates for APM metrics.
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

NRQL_APP_METRICS = (
    "SELECT average(duration) as avg_response_time, "
    "count(*) as throughput, "
    "percentage(count(*), WHERE error IS true) as error_rate "
    "FROM Transaction WHERE appName = '%s' "
    "SINCE %d minutes ago"
)

NRQL_DEPLOYMENTS = (
    "SELECT latest(timestamp), latest(description), latest(revision), "
    "latest(changelog), latest(user) "
    "FROM Deployment WHERE appName = '%s' "
    "SINCE 30 days ago LIMIT %d"
)

# ── OTel variants ────────────────────────────────────────────────────────

NRQL_OTEL_CHECK_SPANS = (
    "SELECT count(*) as event_count FROM Span "
    "WHERE entity.name = '%s' SINCE 15 minutes ago"
)

NRQL_OTEL_CHECK_TXNS = (
    "SELECT count(*) as event_count FROM Transaction "
    "WHERE appName = '%s' SINCE 15 minutes ago"
)

NRQL_OTEL_APP_METRICS = (
    "SELECT average(duration) as avg_response_time, "
    "rate(count(*), 1 minute) as throughput, "
    "percentage(count(*), WHERE otel.status_code = 'ERROR') as error_rate "
    "FROM Span "
    "WHERE entity.name = '%s' AND span.kind = 'SERVER' "
    "SINCE %d minutes ago"
)

NRQL_OTEL_DEPLOYMENTS = (
    "SELECT latest(service.version) as version, "
    "latest(deployment.environment) as environment "
    "FROM Span WHERE entity.name = '%s' SINCE 24 hours ago"
)


async def _is_otel_service_apm(
    service_name: str,
    account_id: str,
    client,
) -> bool:
    """Detect OTel service for APM tools (Span present, Transaction absent)."""
    try:
        async def _count(template: str) -> int:
            nrql = template % service_name
            escaped = nrql.replace('"', '\\"')
            query = GQL_NRQL_QUERY % (account_id, escaped)
            result = await client.query(query, timeout_override=10)
            rows = (
                result.get("data", {})
                .get("actor", {})
                .get("account", {})
                .get("nrql", {})
                .get("results", [])
            )
            if rows and isinstance(rows[0], dict):
                return rows[0].get("event_count", 0) or 0
            return 0

        span_count, txn_count = await asyncio.gather(
            _count(NRQL_OTEL_CHECK_SPANS),
            _count(NRQL_OTEL_CHECK_TXNS),
        )
        return span_count > 0 and txn_count == 0
    except Exception:
        return False


async def get_apm_applications() -> str:
    """Get all APM applications for the active account.

    Returns:
        JSON string with APM applications list.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        query = GQL_APM_ENTITIES % credentials.account_id
        result = await client.query(query)

        entities = (
            result.get("data", {})
            .get("actor", {})
            .get("entitySearch", {})
            .get("results", {})
            .get("entities", [])
        )

        apps = []
        for ent in entities:
            tags = {t["key"]: t.get("values", []) for t in ent.get("tags", [])}
            apps.append({
                "name": ent.get("name", ""),
                "guid": ent.get("guid", ""),
                "alert_severity": ent.get("alertSeverity", ""),
                "reporting": ent.get("reporting", False),
                "language": tags.get("language", [""])[0] if tags.get("language") else "",
                "environment": tags.get("environment", [""])[0] if tags.get("environment") else "",
            })

        duration_ms = int((time.time() - start) * 1000)
        return json.dumps({
            "account_id": credentials.account_id,
            "total_applications": len(apps),
            "applications": apps,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_apm_applications",
            "hint": "Ensure you are connected.",
            "data_available": False,
        })


async def get_app_metrics(app_name: str, since_minutes: int = 30) -> str:
    """Get key metrics for a specific APM application.

    Fuzzy-resolves the app name against known APM services.

    Args:
        app_name: Application name to get metrics for.
        since_minutes: Time window in minutes.

    Returns:
        JSON string with application metrics.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        safe_name = sanitize_service_name(app_name)
        resolved_name, was_fuzzy, confidence = fuzzy_resolve_service(
            safe_name, intelligence.apm.service_names,
            naming_convention=intelligence.naming_convention,
        )

        # Detect OTel vs APM agent instrumentation.
        is_otel = False
        try:
            is_otel = await _is_otel_service_apm(
                resolved_name, credentials.account_id, client,
            )
        except Exception:
            pass

        if is_otel:
            nrql = NRQL_OTEL_APP_METRICS % (resolved_name, since_minutes)
        else:
            nrql = NRQL_APP_METRICS % (resolved_name, since_minutes)
        escaped_nrql = nrql.replace('"', '\\"')
        query = GQL_NRQL_QUERY % (credentials.account_id, escaped_nrql)
        result = await client.query(query)

        metrics = (
            result.get("data", {})
            .get("actor", {})
            .get("account", {})
            .get("nrql", {})
            .get("results", [])
        )

        duration_ms = int((time.time() - start) * 1000)
        response: dict = {
            "app_name": resolved_name,
            "since_minutes": since_minutes,
            "instrumentation": "otel" if is_otel else "apm",
            "metrics": metrics[0] if metrics else {},
            "duration_ms": duration_ms,
        }
        if was_fuzzy:
            response["resolved_from"] = app_name
            response["note"] = f"Fuzzy matched '{app_name}' → '{resolved_name}'"
        if is_otel:
            response.setdefault("warnings", []).append(
                "⚠️ OTel service detected — using Span events instead of Transaction"
            )

        return json.dumps(response)

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_app_metrics",
            "hint": "Check the application name. Use get_apm_applications() to list all.",
            "data_available": False,
        })


async def get_deployments(app_name: str, limit: int = 10) -> str:
    """Get recent deployments for an APM application.

    Args:
        app_name: Application name.
        limit: Maximum number of deployments to return.

    Returns:
        JSON string with deployment history.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        safe_name = sanitize_service_name(app_name)
        resolved_name, was_fuzzy, confidence = fuzzy_resolve_service(
            safe_name, intelligence.apm.service_names,
            naming_convention=intelligence.naming_convention,
        )

        # Detect OTel vs APM agent instrumentation.
        is_otel = False
        try:
            is_otel = await _is_otel_service_apm(
                resolved_name, credentials.account_id, client,
            )
        except Exception:
            pass

        if is_otel:
            nrql = NRQL_OTEL_DEPLOYMENTS % resolved_name
        else:
            nrql = NRQL_DEPLOYMENTS % (resolved_name, limit)
        escaped_nrql = nrql.replace('"', '\\"')
        query = GQL_NRQL_QUERY % (credentials.account_id, escaped_nrql)
        result = await client.query(query)

        deployments = (
            result.get("data", {})
            .get("actor", {})
            .get("account", {})
            .get("nrql", {})
            .get("results", [])
        )

        duration_ms = int((time.time() - start) * 1000)
        response: dict = {
            "app_name": resolved_name,
            "instrumentation": "otel" if is_otel else "apm",
            "total_deployments": len(deployments),
            "deployments": deployments,
            "duration_ms": duration_ms,
        }
        if was_fuzzy:
            response["resolved_from"] = app_name
        if is_otel:
            response["note"] = (
                "OTel service — deployment info extracted from "
                "service.version and deployment.environment Span attributes"
            )

        return json.dumps(response)

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_deployments",
            "hint": "Check the application name.",
            "data_available": False,
        })
