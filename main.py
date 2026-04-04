"""
Sherlock — main entry point.

Registers all 24 MCP tools, configures logging, and starts the
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

# Load .env relative to this file's directory (not cwd) so credentials
# are found regardless of how the MCP server is launched.
load_dotenv(Path(__file__).resolve().parent / ".env")

# ── Directory setup ──────────────────────────────────────────────────────

CONFIG_DIR = Path(__file__).resolve().parent / ".sherlock"
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
from tools.dependencies import get_service_dependencies
from tools.entities import get_entity_guid
from tools.golden_signals import get_service_golden_signals
from tools.intelligence_tools import (
    connect_account,
    get_account_summary,
    get_frustration_context_tool,
    get_nrql_context,
    get_session_context_tool,
    get_structured_report_tool,
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
            "and caches intelligence. Provide EITHER a saved profile_name (which "
            "loads credentials from keychain) OR explicit account_id + api_key."
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
                    "description": (
                        "Saved profile name to connect from (loads credentials "
                        "from keychain). When provided, account_id and api_key "
                        "are not required."
                    ),
                },
            },
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
            "Discover ALL entity names, types, and relationships in the active account. "
            "MUST be called before any investigation to get real service names, K8s "
            "deployment names, synthetic monitor names, and entity relationships. "
            "Results are cached — subsequent calls refresh the cache."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    # 4. get_account_summary
    Tool(
        name="get_account_summary",
        description="Get complete intelligence summary for the active account.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # 5. get_session_context
    Tool(
        name="get_session_context",
        description=(
            "Return investigation history from the current session. "
            "Use this to answer follow-up questions like 'is it still degraded?', "
            "'why did that happen again?', or 'what was the last service we checked?' "
            "without running a full new investigation. "
            "Optionally filter by service_name. "
            "Session history is lost when VS Code restarts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": "Optional. Filter to a specific service name.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of recent investigations to return (1-10).",
                    "default": 5,
                },
            },
            "required": [],
        },
    ),
    # 6. get_frustration_context
    Tool(
        name="get_frustration_context",
        description=(
            "Detect if the engineer is in a frustration or retry loop. "
            "Combines language signals (frustration keywords in the prompt) "
            "with retry signals (same service investigated multiple times recently). "
            "Returns mode=ESCALATION when frustrated, triggering a different "
            "investigation strategy that avoids repeating failed queries and "
            "broadens the investigation scope. "
            "Call at the start of any investigation where frustration is suspected."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The engineer's message text for language analysis.",
                },
                "service_name": {
                    "type": "string",
                    "description": "Optional. Service name to check retry count for.",
                },
            },
            "required": [],
        },
    ),
    # 7. get_structured_report
    Tool(
        name="get_structured_report",
        description=(
            "Return the most recent investigation as machine-readable structured JSON. "
            "This is the machine-readable parallel to the human markdown report. "
            "Use this to feed MTTR dashboards, Slack/Teams notifications, "
            "ticketing systems, or programmatic comparisons between investigations. "
            "Supports three formats: "
            "'full' (all investigation fields), "
            "'summary' (verdict + root cause only), "
            "'metrics' (numeric values only). "
            "Run an investigation first, then call this."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": "Optional. Service to get report for. Uses last investigated if empty.",
                },
                "format": {
                    "type": "string",
                    "enum": ["full", "summary", "metrics"],
                    "description": "'full' | 'summary' | 'metrics'. Default: full.",
                    "default": "full",
                },
            },
            "required": [],
        },
    ),
    # 8. get_nrql_context
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
    # 8. investigate_service
    Tool(
        name="investigate_service",
        description=(
            "[LEGACY] Quick automated check across all domains for a service. "
            "Returns findings with domain-level status and recommendations. "
            "For comprehensive investigation, use the agent-team pattern "
            "(sherlock-team-lead dispatching to all 6 domain agents) instead. "
            "Use this tool ONLY for a fast summary or when agents are not available."
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
    # 9. investigate_synthetic
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
    # 10. get_service_golden_signals
    Tool(
        name="get_service_golden_signals",
        description=(
            "Get the four golden signals (latency, traffic, errors, saturation) "
            "for an APM service with trend data. Essential for APM domain analysis."
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
    # 11. get_k8s_health
    Tool(
        name="get_k8s_health",
        description=(
            "Get Kubernetes health data (pods, restarts, resource usage, deployments) "
            "for a service or namespace. Use the BARE deployment name (after '/') "
            "and namespace (before '/') for best results."
        ),
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
    # 12. search_logs
    Tool(
        name="search_logs",
        description=(
            "Search logs with filters for service, severity, and keywords. "
            "Essential for log domain analysis and error pattern detection."
        ),
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
    # 13. get_synthetic_monitors
    Tool(
        name="get_synthetic_monitors",
        description=(
            "List all synthetic monitors for the active account with status summary."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    # 14. get_monitor_status
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
    # 15. get_monitor_results
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
    # 16. get_apm_applications
    Tool(
        name="get_apm_applications",
        description="List all APM applications for the active account.",
        inputSchema={"type": "object", "properties": {}},
    ),
    # 17. get_app_metrics
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
    # 18. get_deployments
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
    # 19. get_alerts
    Tool(
        name="get_alerts",
        description=(
            "Get all alert policies for the active account. Takes no parameters. "
            "Returns account-wide policies, not service-specific."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    # 20. get_incidents
    Tool(
        name="get_incidents",
        description=(
            "Get incidents filtered by state (open or closed). Returns account-wide "
            "incidents — does NOT accept a service name filter. Use "
            "get_service_incidents for service-specific incidents."
        ),
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
    # 21. get_service_incidents
    Tool(
        name="get_service_incidents",
        description=(
            "Get incidents related to a specific service or monitor. "
            "Use this instead of get_incidents when filtering by service name."
        ),
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
    # 22. run_nrql_query
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
    # 23. get_service_dependencies
    Tool(
        name="get_service_dependencies",
        description=(
            "Get upstream and downstream service dependencies for an APM service. "
            "Shows which services call this service and which services it calls, "
            "with health warnings for unhealthy dependencies."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {
                    "type": "string",
                    "description": "APM service name to look up dependencies for",
                },
                "direction": {
                    "type": "string",
                    "enum": ["downstream", "upstream", "both"],
                    "default": "both",
                    "description": "Which direction of dependencies to return",
                },
                "include_external": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include external (non-NR) endpoint dependencies",
                },
                "max_depth": {
                    "type": "integer",
                    "default": 2,
                    "description": "Maximum dependency depth (1-5)",
                },
            },
            "required": ["service_name"],
        },
    ),
]

# ── Tool dispatch map ────────────────────────────────────────────────────

TOOL_HANDLERS = {
    "connect_account": connect_account,
    "list_profiles": list_profiles,
    "learn_account": learn_account_tool,
    "get_account_summary": get_account_summary,
    "get_session_context": get_session_context_tool,
    "get_frustration_context": get_frustration_context_tool,
    "get_structured_report": get_structured_report_tool,
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
    "get_service_dependencies": get_service_dependencies,
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
        if result_data.get("status") == "connected":
            logger.info(
                "Auto-connect successful for account %s (%s)",
                account_id,
                result_data.get("account_name", ""),
            )
        else:
            logger.warning(
                "Auto-connect failed for account %s: %s",
                account_id,
                result_data.get("error", "unknown error"),
            )
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
