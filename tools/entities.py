"""
Entity GUID resolution tool for Sherlock.

Resolves human-readable entity names to New Relic entity GUIDs
using NerdGraph entitySearch. Used both internally by other tools
and exposed as an MCP-facing tool.
"""

import json
import logging
from typing import Any

from client.newrelic import get_client
from core.context import AccountContext
from core.sanitize import sanitize_service_name

logger = logging.getLogger("sherlock.tools.entities")

# GraphQL query to search for entities by name and optional domain.
GQL_ENTITY_SEARCH = """
{
  actor {
    entitySearch(query: "accountId = %s AND name = '%s'%s") {
      results {
        entities {
          guid
          name
          type
          domain
          alertSeverity
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


async def get_entity_guid(
    entity_name: str,
    domain: str | None = None,
    entity_type: str | None = None,
) -> str:
    """Resolve an entity name to its New Relic GUID.

    Internal helper and MCP-facing tool. Searches NerdGraph entitySearch
    for matching entities.

    Args:
        entity_name: The entity name to search for.
        domain: Optional domain filter (e.g. 'APM', 'INFRA', 'SYNTH').
        entity_type: Optional entity type filter (e.g. 'APPLICATION', 'HOST').

    Returns:
        JSON string with the entity GUID(s) and metadata.
    """
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        safe_name = sanitize_service_name(entity_name)
        domain_filter = ""
        if domain:
            domain_filter += f" AND domain = '{domain}'"
        if entity_type:
            domain_filter += f" AND type = '{entity_type}'"

        query = GQL_ENTITY_SEARCH % (credentials.account_id, safe_name, domain_filter)
        result = await client.query(query)

        entities = (
            result.get("data", {})
            .get("actor", {})
            .get("entitySearch", {})
            .get("results", {})
            .get("entities", [])
        )

        if not entities:
            return json.dumps({
                "error": f"No entity found matching '{entity_name}'",
                "tool": "get_entity_guid",
                "hint": "Check the entity name or try a different domain filter.",
                "data_available": False,
            })

        results: list[dict[str, Any]] = []
        for ent in entities:
            results.append({
                "guid": ent.get("guid", ""),
                "name": ent.get("name", ""),
                "type": ent.get("type", ""),
                "domain": ent.get("domain", ""),
                "alert_severity": ent.get("alertSeverity", ""),
            })

        return json.dumps({
            "entity_name": entity_name,
            "matches": len(results),
            "entities": results,
        })

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_entity_guid",
            "hint": "Ensure you are connected to an account.",
            "data_available": False,
        })
