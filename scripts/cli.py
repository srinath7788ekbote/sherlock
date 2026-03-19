"""
CLI mode for Sherlock.

Allows testing tools interactively without an MCP client like Copilot.
Usage:
    python scripts/cli.py                              # Interactive mode
    python scripts/cli.py --list-tools                 # List all tools
    python scripts/cli.py --tool get_synthetic_monitors
    python scripts/cli.py --tool get_monitor_status --args '{"monitor_name": "Login Flow"}'
    python scripts/cli.py --profile production --tool get_account_summary
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add project root to path.
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.alerts import get_alerts, get_incidents, get_service_incidents
from tools.apm import get_apm_applications, get_app_metrics, get_deployments
from tools.dependencies import get_service_dependencies
from tools.golden_signals import get_service_golden_signals
from tools.intelligence_tools import (
    connect_account,
    get_account_summary,
    get_nrql_context,
    learn_account_tool,
    list_profiles,
)
from tools.investigate import investigate_service  # LEGACY
from tools.k8s import get_k8s_health
from tools.logs import search_logs
from tools.nrql import run_nrql_query
from tools.synthetics import (
    get_monitor_results,
    get_monitor_status,
    get_synthetic_monitors,
    investigate_synthetic,
)

TOOL_MAP = {
    "connect_account": connect_account,
    "list_profiles": list_profiles,
    "learn_account": learn_account_tool,
    "get_account_summary": get_account_summary,
    "get_nrql_context": get_nrql_context,
    "investigate_service": investigate_service,  # LEGACY — prefer agent-team
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
    "get_service_dependencies": get_service_dependencies,
    "run_nrql_query": run_nrql_query,
}


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments for single-shot or interactive mode."""
    parser = argparse.ArgumentParser(
        description="Sherlock — CLI Mode",
    )
    parser.add_argument(
        "--tool",
        choices=list(TOOL_MAP.keys()),
        help="Tool to execute (single-shot mode)",
    )
    parser.add_argument(
        "--args",
        default="{}",
        help="JSON arguments for the tool (default: {})",
    )
    parser.add_argument(
        "--profile",
        help="Connect to a saved profile before running the tool",
    )
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="List all available tools and exit",
    )
    return parser.parse_args()


def print_json(data: str) -> None:
    """Pretty-print a JSON string.

    Args:
        data: JSON string to format and print.
    """
    try:
        parsed = json.loads(data)
        print(json.dumps(parsed, indent=2))
    except (json.JSONDecodeError, TypeError):
        print(data)


async def interactive_loop() -> None:
    """Run the interactive CLI loop."""
    print("=" * 60)
    print("  Sherlock — CLI Mode")
    print("=" * 60)
    print()
    print("Available tools:")
    for i, name in enumerate(TOOL_MAP.keys(), 1):
        print(f"  {i:2d}. {name}")
    print()
    print("Type 'help' for usage, 'quit' to exit.")
    print()

    while True:
        try:
            command = input("mcp> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not command:
            continue

        if command.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            break

        if command.lower() == "help":
            print("\nUsage:")
            print("  <tool_name> [json_args]")
            print("  Example: connect_account {\"account_id\": \"123\", \"api_key\": \"NRAK-xxx\"}")
            print("  Example: get_synthetic_monitors")
            print("  Example: get_monitor_status {\"monitor_name\": \"Login Flow\"}")
            print()
            continue

        if command.lower() == "tools":
            for i, name in enumerate(TOOL_MAP.keys(), 1):
                print(f"  {i:2d}. {name}")
            continue

        # Parse command.
        parts = command.split(None, 1)
        tool_name = parts[0]
        args_str = parts[1] if len(parts) > 1 else "{}"

        if tool_name not in TOOL_MAP:
            print(f"Unknown tool: {tool_name}")
            print("Type 'tools' to see available tools.")
            continue

        try:
            args = json.loads(args_str)
        except json.JSONDecodeError:
            print(f"Invalid JSON arguments: {args_str}")
            continue

        print(f"\nRunning {tool_name}...")
        try:
            result = await TOOL_MAP[tool_name](**args)
            print_json(result)
        except Exception as exc:
            print(f"Error: {exc}")

        print()


def main() -> None:
    """Entry point for CLI mode — supports single-shot and interactive."""
    args = _parse_args()

    # --list-tools: print tool names and exit.
    if args.list_tools:
        print("Available tools:")
        for i, name in enumerate(TOOL_MAP.keys(), 1):
            print(f"  {i:2d}. {name}")
        sys.exit(0)

    # --tool: single-shot execution.
    if args.tool:
        asyncio.run(_single_shot(args))
    else:
        asyncio.run(interactive_loop())


async def _single_shot(args: argparse.Namespace) -> None:
    """Execute a single tool and exit.

    Optionally connects a profile first if --profile is provided.

    Args:
        args: Parsed CLI arguments with tool, args, and optional profile.
    """
    # Connect profile if requested.
    if args.profile:
        from core.credentials import CredentialManager

        manager = CredentialManager()
        try:
            creds = manager.load_profile(args.profile)
            print(f"Connecting profile '{args.profile}'...")
            result = await connect_account(
                account_id=creds.account_id,
                api_key=creds.api_key,
                region=creds.region,
            )
            parsed = json.loads(result)
            if parsed.get("error"):
                print(f"Connection failed: {parsed['error']}")
                sys.exit(1)
            print(f"Connected to account {creds.account_id}.\n")
        except Exception as exc:
            print(f"Failed to load profile '{args.profile}': {exc}")
            sys.exit(1)

    # Parse tool arguments.
    try:
        tool_args = json.loads(args.args)
    except json.JSONDecodeError:
        print(f"Invalid JSON arguments: {args.args}")
        sys.exit(1)

    # Execute the tool.
    print(f"Running {args.tool}...")
    try:
        result = await TOOL_MAP[args.tool](**tool_args)
        print_json(result)
    except Exception as exc:
        print(f"Error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
