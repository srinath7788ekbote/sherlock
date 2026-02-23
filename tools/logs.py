"""
Log search tool for Sherlock.

Provides log searching against the active account using NRQL queries
on the Log event type. Resolves service names and log attributes
from account intelligence.
"""

import json
import logging
import time

from client.newrelic import get_client
from core.context import AccountContext
from core.sanitize import fuzzy_resolve_service, sanitize_nrql_string, sanitize_service_name

logger = logging.getLogger("sherlock.tools.logs")

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

# NRQL log search template — dynamically assembled.
NRQL_LOG_BASE = "SELECT timestamp, message, %s, %s FROM Log WHERE 1=1"
NRQL_LOG_SERVICE_FILTER = " AND %s = '%s'"
NRQL_LOG_SEVERITY_FILTER = " AND %s IN (%s)"
NRQL_LOG_KEYWORD_FILTER = " AND message LIKE '%%%s%%'"
NRQL_LOG_SINCE = " SINCE %d minutes ago"
NRQL_LOG_ORDER = " ORDER BY timestamp DESC"
NRQL_LOG_LIMIT = " LIMIT %d"


async def search_logs(
    service_name: str | None = None,
    severity: str | None = None,
    keyword: str | None = None,
    since_minutes: int = 60,
    limit: int = 100,
) -> str:
    """Search logs for the active account.

    Dynamically builds NRQL using the account's discovered log attributes.
    Fuzzy-resolves service names against known APM services.

    Args:
        service_name: Optional service name to filter by.
        severity: Optional severity filter (e.g. 'ERROR', 'WARN', 'ERROR,WARN').
        keyword: Optional keyword to search in log messages.
        since_minutes: Time window in minutes.
        limit: Maximum log entries to return.

    Returns:
        JSON string with log search results.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        if not intelligence.logs.enabled:
            return json.dumps({
                "error": "Logging is not enabled for this account.",
                "tool": "search_logs",
                "hint": "Enable New Relic logging or check your account setup.",
                "data_available": False,
            })

        svc_attr = intelligence.logs.service_attribute or "service.name"
        sev_attr = intelligence.logs.severity_attribute or "level"

        # Build NRQL.
        nrql = NRQL_LOG_BASE % (svc_attr, sev_attr)

        resolved_name = None
        was_fuzzy = False
        if service_name:
            safe_name = sanitize_service_name(service_name)
            try:
                resolved_name, was_fuzzy, confidence = fuzzy_resolve_service(
                    safe_name, intelligence.apm.service_names, threshold=0.5,
                    naming_convention=intelligence.naming_convention,
                )
            except Exception:
                resolved_name = safe_name
            nrql += NRQL_LOG_SERVICE_FILTER % (svc_attr, resolved_name)

        if severity:
            safe_severity = sanitize_nrql_string(severity)
            levels = [f"'{s.strip()}'" for s in safe_severity.split(",")]
            nrql += NRQL_LOG_SEVERITY_FILTER % (sev_attr, ", ".join(levels))

        if keyword:
            safe_keyword = sanitize_nrql_string(keyword)
            nrql += NRQL_LOG_KEYWORD_FILTER % safe_keyword

        nrql += NRQL_LOG_SINCE % since_minutes
        nrql += NRQL_LOG_ORDER
        nrql += NRQL_LOG_LIMIT % min(limit, 500)

        escaped_nrql = nrql.replace('"', '\\"')
        query = GQL_NRQL_QUERY % (credentials.account_id, escaped_nrql)
        result = await client.query(query)

        logs = (
            result.get("data", {})
            .get("actor", {})
            .get("account", {})
            .get("nrql", {})
            .get("results", [])
        )

        duration_ms = int((time.time() - start) * 1000)
        response: dict = {
            "account_id": credentials.account_id,
            "service_name": resolved_name,
            "severity_filter": severity,
            "keyword": keyword,
            "since_minutes": since_minutes,
            "total_logs": len(logs),
            "logs": logs,
            "nrql_used": nrql,
            "duration_ms": duration_ms,
        }
        if was_fuzzy and resolved_name:
            response["resolved_from"] = service_name
            response["note"] = f"Fuzzy matched '{service_name}' → '{resolved_name}'"

        return json.dumps(response)

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "search_logs",
            "hint": "Check parameters. Use get_nrql_context('logs') for attribute names.",
            "data_available": False,
        })
