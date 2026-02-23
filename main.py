"""
Sherlock — main entry point.

Registers all 20 MCP tools, configures logging, and starts the
stdio-based MCP server. All tool responses are scrubbed for prompt
injection before being returned to the client.
"""

import asyncio
import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from core.context import AccountContext
from core.exceptions import ReadOnlyViolation
from core.sanitize import scrub_tool_response

# ── Load environment ─────────────────────────────────────────────────────

load_dotenv()

# ── Directory setup ──────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".sherlock"
LOG_DIR = CONFIG_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging setup (NEVER write to stdout — reserved for MCP protocol) ───

def _setup_logging() -> None:
    """Configure structured JSON logging to file only."""
    root_logger = logging.getLogger("sherlock")
    root_logger.setLevel(logging.DEBUG)

    # Main log — rotating 10MB, 5 backups.
    main_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "sherlock.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    main_handler.setLevel(logging.DEBUG)
    main_handler.setFormatter(logging.Formatter(
        '{"time":"%(asctime)s","level":"%(levelname)s",'
        '"logger":"%(name)s","message":"%(message)s"}'
    ))
    root_logger.addHandler(main_handler)

    # Audit log — rotating 10MB, 10 backups.
    audit_logger = logging.getLogger("sherlock.audit")
    audit_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "audit.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    audit_handler.setLevel(logging.INFO)
    audit_handler.setFormatter(logging.Formatter(
        '{"time":"%(asctime)s","level":"%(levelname)s",'
        '"logger":"%(name)s","message":"%(message)s"}'
    ))
    audit_logger.addHandler(audit_handler)

    # Suppress logging to stdout/stderr from other libraries.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


_setup_logging()

logger = logging.getLogger("sherlock.main")
audit_logger = logging.getLogger("sherlock.audit")

# ── Tool imports ─────────────────────────────────────────────────────────

from tools.alerts import get_alerts, get_incidents, get_service_incidents
from tools.apm import get_apm_applications, get_app_metrics, get_deployments
from tools.entities import get_entity_guid
from tools.golden_signals import get_service_golden_signals
from tools.intelligence_tools import (
    connect_account,
    get_account_summary,
    get_nrql_context,
    learn_account_tool,
    list_profiles,
)
from tools.investigate import investigate_service
from tools.k8s import get_k8s_health
from tools.logs import search_logs
from tools.nrql import run_nrql_query
from tools.synthetics import (
    get_monitor_results,
    get_monitor_status,
    get_synthetic_monitors,
    investigate_synthetic,
)

# ── Tool definitions ─────────────────────────────────────────────────────

TOOLS: list[Tool] = [
    # 1. connect_account
    Tool(
        name="connect_account",
        description=(
            "Connect to a New Relic account. Call this first — required before "
            "all other tools. Validates credentials, learns the account structure, "
            "and caches intelligence."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "New Relic account ID"},
                "api_key": {"type": "string", "description": "New Relic User API key"},
                "region": {
                    "type": "string", "enum": ["US", "EU"], "default": "US",
                    "description": "Data center region",
                },
                "profile_name": {
                    "type": "string",
                    "description": "Optional profile name to save for later reuse",
                },
            },
            "required": ["account_id", "api_key"],
        },
    ),
    # 2. list_profiles
    Tool(
        name="list_profiles",
        description="List all saved New Relic credential profiles.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # 3. learn_account
    Tool(
        name="learn_account",
        description=(
            "Re-learn the active account's structure. Forces a refresh of "
            "services, namespaces, monitors, and all other intelligence."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    # 4. get_account_summary
    Tool(
        name="get_account_summary",
        description="Get complete intelligence summary for the active account.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # 5. get_nrql_context
    Tool(
        name="get_nrql_context",
        description=(
            "Call before constructing any NRQL to get real service names, "
            "monitor names, and attribute names for this account."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "enum": ["apm", "k8s", "logs", "alerts", "synthetics", "all"],
                    "default": "all",
                    "description": "Domain to get context for",
                },
            },
        },
    ),
    # 6. investigate_service
    Tool(
        name="investigate_service",
        description=(
            "Use when a service is alerting, down, slow, broken, or having issues. "
            "Runs full parallel investigation across APM, logs, K8s, synthetics, "
            "and alerts — returns findings and fix recommendations."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": "APM service name to investigate",
                },
                "namespace": {
                    "type": "string",
                    "description": "Optional K8s namespace",
                },
                "since_minutes": {
                    "type": "integer", "default": 30,
                    "description": "Time window in minutes",
                },
            },
            "required": ["service_name"],
        },
    ),
    # 7. investigate_synthetic
    Tool(
        name="investigate_synthetic",
        description=(
            "Use when a synthetic monitor is failing or a login flow / "
            "health check is broken. Deep investigation with APM correlation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "monitor_name": {
                    "type": "string",
                    "description": "Synthetic monitor name to investigate",
                },
                "since_minutes": {
                    "type": "integer", "default": 60,
                    "description": "Time window in minutes",
                },
            },
            "required": ["monitor_name"],
        },
    ),
    # 8. get_service_golden_signals
    Tool(
        name="get_service_golden_signals",
        description=(
            "Get the four golden signals (latency, traffic, errors, saturation) "
            "for an APM service with trend data."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "APM service name"},
                "since_minutes": {
                    "type": "integer", "default": 30,
                    "description": "Time window in minutes",
                },
            },
            "required": ["service_name"],
        },
    ),
    # 9. get_k8s_health
    Tool(
        name="get_k8s_health",
        description="Get Kubernetes health data for a service or namespace.",
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service name"},
                "namespace": {"type": "string", "description": "K8s namespace"},
                "since_minutes": {
                    "type": "integer", "default": 30,
                    "description": "Time window in minutes",
                },
            },
        },
    ),
    # 10. search_logs
    Tool(
        name="search_logs",
        description="Search logs with filters for service, severity, and keywords.",
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {"type": "string", "description": "Service name filter"},
                "severity": {
                    "type": "string",
                    "description": "Severity filter (e.g. 'ERROR', 'WARN', 'ERROR,WARN')",
                },
                "keyword": {"type": "string", "description": "Keyword to search in messages"},
                "since_minutes": {
                    "type": "integer", "default": 60,
                    "description": "Time window in minutes",
                },
                "limit": {
                    "type": "integer", "default": 100,
                    "description": "Max results",
                },
            },
        },
    ),
    # 11. get_synthetic_monitors
    Tool(
        name="get_synthetic_monitors",
        description=(
            "List all synthetic monitors for the active account with status summary."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    # 12. get_monitor_status
    Tool(
        name="get_monitor_status",
        description=(
            "Use to check if a specific synthetic monitor is passing or failing "
            "and in which locations."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "monitor_name": {
                    "type": "string",
                    "description": "Synthetic monitor name",
                },
                "since_minutes": {
                    "type": "integer", "default": 60,
                    "description": "Time window in minutes",
                },
            },
            "required": ["monitor_name"],
        },
    ),
    # 13. get_monitor_results
    Tool(
        name="get_monitor_results",
        description="Get raw run results for a synthetic monitor, useful for digging into failures.",
        inputSchema={
            "type": "object",
            "properties": {
                "monitor_name": {"type": "string", "description": "Synthetic monitor name"},
                "result_filter": {
                    "type": "string", "enum": ["FAILED", "SUCCESS"],
                    "description": "Filter by result type",
                },
                "since_minutes": {
                    "type": "integer", "default": 60,
                    "description": "Time window in minutes",
                },
                "limit": {
                    "type": "integer", "default": 50,
                    "description": "Max results",
                },
            },
            "required": ["monitor_name"],
        },
    ),
    # 14. get_apm_applications
    Tool(
        name="get_apm_applications",
        description="List all APM applications for the active account.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # 15. get_app_metrics
    Tool(
        name="get_app_metrics",
        description="Get key performance metrics for an APM application.",
        inputSchema={
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "APM application name"},
                "since_minutes": {
                    "type": "integer", "default": 30,
                    "description": "Time window in minutes",
                },
            },
            "required": ["app_name"],
        },
    ),
    # 16. get_deployments
    Tool(
        name="get_deployments",
        description="Get recent deployment history for an APM application.",
        inputSchema={
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "APM application name"},
                "limit": {
                    "type": "integer", "default": 10,
                    "description": "Max deployments to return",
                },
            },
            "required": ["app_name"],
        },
    ),
    # 17. get_alerts
    Tool(
        name="get_alerts",
        description="Get all alert policies for the active account.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # 18. get_incidents
    Tool(
        name="get_incidents",
        description="Get incidents filtered by state (open or closed).",
        inputSchema={
            "type": "object",
            "properties": {
                "state": {
                    "type": "string", "enum": ["open", "closed"], "default": "open",
                    "description": "Incident state filter",
                },
            },
        },
    ),
    # 19. get_service_incidents
    Tool(
        name="get_service_incidents",
        description="Get incidents related to a specific service or monitor.",
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": "Service or monitor name to search for",
                },
            },
            "required": ["service_name"],
        },
    ),
    # 20. run_nrql_query
    Tool(
        name="run_nrql_query",
        description="Execute a raw NRQL query. Use get_nrql_context first to get valid names.",
        inputSchema={
            "type": "object",
            "properties": {
                "nrql": {"type": "string", "description": "NRQL query to execute"},
            },
            "required": ["nrql"],
        },
    ),
]

# ── Tool dispatch map ────────────────────────────────────────────────────

TOOL_HANDLERS = {
    "connect_account": connect_account,
    "list_profiles": list_profiles,
    "learn_account": learn_account_tool,
    "get_account_summary": get_account_summary,
    "get_nrql_context": get_nrql_context,
    "investigate_service": investigate_service,
    "investigate_synthetic": investigate_synthetic,
    "get_service_golden_signals": get_service_golden_signals,
    "get_k8s_health": get_k8s_health,
    "search_logs": search_logs,
    "get_synthetic_monitors": get_synthetic_monitors,
    "get_monitor_status": get_monitor_status,
    "get_monitor_results": get_monitor_results,
    "get_apm_applications": get_apm_applications,
    "get_app_metrics": get_app_metrics,
    "get_deployments": get_deployments,
    "get_alerts": get_alerts,
    "get_incidents": get_incidents,
    "get_service_incidents": get_service_incidents,
    "run_nrql_query": run_nrql_query,
}

# ── MCP Server ───────────────────────────────────────────────────────────

app = Server("sherlock")


@app.list_tools()
async def handle_list_tools() -> list[Tool]:
    """Return the list of all available tools."""
    return TOOLS


@app.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Dispatch a tool call, scrub the response, and log to audit.

    Args:
        name: The tool name to call.
        arguments: The tool arguments dict.

    Returns:
        List containing a single TextContent with the JSON response.
    """
    start = time.time()
    account_id = ""

    try:
        ctx = AccountContext()
        if ctx.is_connected():
            creds, _ = ctx.get_active()
            account_id = creds.account_id
    except Exception:
        pass

    try:
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            result = json.dumps({
                "error": f"Unknown tool: {name}",
                "available_tools": list(TOOL_HANDLERS.keys()),
            })
        else:
            result = await handler(**arguments)

        # Scrub response for prompt injection.
        try:
            parsed = json.loads(result)
            scrubbed = scrub_tool_response(parsed, account_id=account_id, tool=name)
            result = json.dumps(scrubbed)
        except (json.JSONDecodeError, TypeError):
            result = str(scrub_tool_response(result, account_id=account_id, tool=name))

        duration_ms = int((time.time() - start) * 1000)
        audit_logger.info(
            json.dumps({
                "event": "tool_call",
                "tool": name,
                "account_id": account_id,
                "duration_ms": duration_ms,
                "success": True,
            })
        )

        return [TextContent(type="text", text=result)]

    except ReadOnlyViolation as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.warning(
            "SECURITY WARNING: ReadOnlyViolation in tool '%s': %s (keyword: %s)",
            name, exc.message, exc.blocked_keyword,
        )
        audit_logger.warning(
            json.dumps({
                "event": "SECURITY_WARNING",
                "tool": name,
                "account_id": account_id,
                "violation": exc.message,
                "blocked_keyword": exc.blocked_keyword,
                "duration_ms": duration_ms,
            })
        )
        error_result = json.dumps({
            "error": "Operation blocked: this server is read-only.",
            "tool": name,
            "hint": "Only read/query operations are allowed.",
            "data_available": False,
        })
        return [TextContent(type="text", text=error_result)]

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.error("Tool '%s' failed: %s", name, exc)
        audit_logger.info(
            json.dumps({
                "event": "tool_call",
                "tool": name,
                "account_id": account_id,
                "duration_ms": duration_ms,
                "success": False,
                "error": str(exc),
            })
        )
        error_result = json.dumps({
            "error": str(exc),
            "tool": name,
            "data_available": False,
        })
        return [TextContent(type="text", text=error_result)]


# ── Entry points ─────────────────────────────────────────────────────────

async def _auto_connect_from_env() -> None:
    """Auto-connect using .env credentials if present and no account is active."""
    ctx = AccountContext()
    if ctx.is_connected():
        return

    account_id = os.getenv("NEW_RELIC_ACCOUNT_ID", "").strip()
    api_key = os.getenv("NEW_RELIC_API_KEY", "").strip()
    region = os.getenv("NEW_RELIC_REGION", "US").strip().upper()

    if not account_id or not api_key:
        logger.debug("No .env credentials found — skipping auto-connect")
        return

    logger.info("Auto-connecting to account %s from .env", account_id)
    try:
        result = await connect_account(account_id, api_key, region)
        result_data = json.loads(result)
        if result_data.get("data_available"):
            logger.info("Auto-connect successful for account %s", account_id)
        else:
            logger.warning("Auto-connect returned: %s", result)
    except Exception as exc:
        logger.warning("Auto-connect failed: %s", exc)


async def main() -> None:
    """Start the MCP server on stdio transport."""
    logger.info("Sherlock server starting (stdio transport)")
    await _auto_connect_from_env()
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main_sync() -> None:
    """Synchronous entry point for the MCP server."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
