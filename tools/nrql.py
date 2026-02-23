"""
NRQL query execution tool for Sherlock.

Provides a raw NRQL query tool that validates, sanitizes, and executes
NRQL queries against the active account via NerdGraph.
"""

import json
import logging
import time

from client.newrelic import get_client
from core.context import AccountContext
from core.sanitize import sanitize_nrql_string

logger = logging.getLogger("sherlock.tools.nrql")

# NerdGraph template for NRQL execution.
GQL_NRQL = """
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

# Maximum NRQL query length.
MAX_NRQL_LENGTH = 4000


async def run_nrql_query(nrql: str) -> str:
    """Execute a raw NRQL query against the active New Relic account.

    Sanitizes the query, executes via NerdGraph, and returns structured results.

    Args:
        nrql: The NRQL query string to execute.

    Returns:
        JSON string with query results or error information.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        # Basic validation.
        if not nrql or not nrql.strip():
            return json.dumps({
                "error": "Empty NRQL query.",
                "tool": "run_nrql_query",
                "hint": "Provide a valid NRQL query. Use get_nrql_context() first.",
                "data_available": False,
            })

        if len(nrql) > MAX_NRQL_LENGTH:
            return json.dumps({
                "error": f"NRQL query too long ({len(nrql)} chars, max {MAX_NRQL_LENGTH}).",
                "tool": "run_nrql_query",
                "hint": "Simplify your query.",
                "data_available": False,
            })

        # Escape double quotes for embedding in GraphQL string.
        escaped_nrql = nrql.replace("\\", "\\\\").replace('"', '\\"')
        query = GQL_NRQL % (credentials.account_id, escaped_nrql)
        result = await client.query(query)

        nrql_results = (
            result.get("data", {})
            .get("actor", {})
            .get("account", {})
            .get("nrql", {})
            .get("results", [])
        )

        errors = result.get("errors", [])
        warnings = [e.get("message", "") for e in errors] if errors else []

        duration_ms = int((time.time() - start) * 1000)
        logger.info("NRQL executed in %dms, %d results", duration_ms, len(nrql_results))

        return json.dumps({
            "nrql": nrql,
            "account_id": credentials.account_id,
            "result_count": len(nrql_results),
            "results": nrql_results,
            "warnings": warnings,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("NRQL query failed: %s", exc)
        return json.dumps({
            "error": str(exc),
            "tool": "run_nrql_query",
            "hint": "Check NRQL syntax. Use get_nrql_context() for valid attribute names.",
            "data_available": False,
            "duration_ms": duration_ms,
        })
