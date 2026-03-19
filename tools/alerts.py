"""
Alert and incident tools for Sherlock.

Provides tools to query alert policies, active incidents, and
service-specific incident information.
"""

import json
import logging
import time

from client.newrelic import get_client
from core.context import AccountContext
from core.deeplinks import get_builder as _get_deeplink_builder
from core.sanitize import fuzzy_resolve_service, sanitize_service_name

logger = logging.getLogger("sherlock.tools.alerts")

# GraphQL queries for alert data.
GQL_ALERT_POLICIES = """
{
  actor {
    account(id: %s) {
      alerts {
        policiesSearch {
          policies {
            id
            name
            incidentPreference
          }
        }
      }
    }
  }
}
"""

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

NRQL_OPEN_INCIDENTS = (
    "SELECT latest(event), latest(priority), latest(conditionName), "
    "latest(policyName), latest(targetName), latest(openTime) "
    "FROM NrAiIncident WHERE event = 'open' "
    "SINCE 1 day ago FACET incidentId LIMIT 100"
)

NRQL_INCIDENTS_BY_STATE = (
    "SELECT latest(event), latest(priority), latest(conditionName), "
    "latest(policyName), latest(targetName), latest(openTime), latest(closeTime) "
    "FROM NrAiIncident WHERE event = '%s' "
    "SINCE 7 days ago FACET incidentId LIMIT 100"
)

NRQL_SERVICE_INCIDENTS = (
    "SELECT latest(event), latest(priority), latest(conditionName), "
    "latest(policyName), latest(openTime) "
    "FROM NrAiIncident WHERE targetName LIKE '%%%s%%' "
    "SINCE 7 days ago FACET incidentId LIMIT 50"
)


async def get_alerts() -> str:
    """Get all alert policies for the active account.

    Returns:
        JSON string with alert policies or error information.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        query = GQL_ALERT_POLICIES % credentials.account_id
        result = await client.query(query)

        policies = (
            result.get("data", {})
            .get("actor", {})
            .get("account", {})
            .get("alerts", {})
            .get("policiesSearch", {})
            .get("policies", [])
        )

        duration_ms = int((time.time() - start) * 1000)
        return json.dumps({
            "account_id": credentials.account_id,
            "total_policies": len(policies),
            "policies": policies,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_alerts",
            "hint": "Ensure you are connected.",
            "data_available": False,
        })


async def get_incidents(state: str = "open") -> str:
    """Get incidents filtered by state.

    Args:
        state: Incident state filter — 'open' or 'closed'.

    Returns:
        JSON string with incidents or error information.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        if state.lower() == "open":
            nrql = NRQL_OPEN_INCIDENTS
        else:
            nrql = NRQL_INCIDENTS_BY_STATE % state.lower()

        escaped_nrql = nrql.replace('"', '\\"')
        query = GQL_NRQL_QUERY % (credentials.account_id, escaped_nrql)
        result = await client.query(query)

        incidents = (
            result.get("data", {})
            .get("actor", {})
            .get("account", {})
            .get("nrql", {})
            .get("results", [])
        )

        duration_ms = int((time.time() - start) * 1000)

        # Deep links — only for open/activated incidents.
        if state.lower() == "open":
            try:
                _builder = _get_deeplink_builder()
                if _builder:
                    for inc in incidents:
                        inc_id = inc.get("incidentId", inc.get("facet", ""))
                        if inc_id:
                            inc["deep_link"] = _builder.alert_incident(str(inc_id))
            except Exception:
                pass

        return json.dumps({
            "account_id": credentials.account_id,
            "state_filter": state,
            "total_incidents": len(incidents),
            "incidents": incidents,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_incidents",
            "hint": "Check state parameter: 'open' or 'closed'.",
            "data_available": False,
        })


async def get_service_incidents(service_name: str) -> str:
    """Get incidents related to a specific service or monitor.

    Fuzzy-resolves the service name against known APM services and monitors.

    Args:
        service_name: Service or monitor name to search for.

    Returns:
        JSON string with service-specific incidents or error information.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        safe_name = sanitize_service_name(service_name)

        # Try fuzzy resolve against APM services (but don't fail if not found).
        resolved_name = safe_name
        was_fuzzy = False
        try:
            resolved_name, was_fuzzy, confidence = fuzzy_resolve_service(
                safe_name, intelligence.apm.service_names, threshold=0.5,
                naming_convention=intelligence.naming_convention,
            )
        except Exception:
            resolved_name = safe_name

        escaped_nrql = (NRQL_SERVICE_INCIDENTS % resolved_name).replace('"', '\\"')
        query = GQL_NRQL_QUERY % (credentials.account_id, escaped_nrql)
        result = await client.query(query)

        incidents = (
            result.get("data", {})
            .get("actor", {})
            .get("account", {})
            .get("nrql", {})
            .get("results", [])
        )

        duration_ms = int((time.time() - start) * 1000)

        # Deep links for each incident.
        try:
            _builder = _get_deeplink_builder()
            if _builder:
                for inc in incidents:
                    inc_id = inc.get("incidentId", inc.get("facet", ""))
                    if inc_id:
                        inc["deep_link"] = _builder.alert_incident(str(inc_id))
        except Exception:
            pass

        response: dict = {
            "service_name": resolved_name,
            "total_incidents": len(incidents),
            "incidents": incidents,
            "duration_ms": duration_ms,
        }
        if was_fuzzy:
            response["resolved_from"] = service_name
            response["note"] = f"Fuzzy matched '{service_name}' → '{resolved_name}'"

        return json.dumps(response)

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_service_incidents",
            "hint": "Check the service name.",
            "data_available": False,
        })
