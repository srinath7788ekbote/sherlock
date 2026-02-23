"""
Credential validation script for Sherlock.

Run this before first use to verify your New Relic API key works.
Usage:
    python scripts/test_connection.py
    python scripts/test_connection.py --account-id 123456 --api-key NRAK-xxx
    python scripts/test_connection.py --account-id 123456 --api-key NRAK-xxx --region EU
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Add project root to path.
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from core.credentials import CredentialManager


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments with fallback to interactive prompts."""
    parser = argparse.ArgumentParser(
        description="Validate New Relic credentials and preview account intelligence.",
    )
    parser.add_argument(
        "--account-id",
        help="New Relic Account ID (prompted interactively if omitted)",
    )
    parser.add_argument(
        "--api-key",
        help="New Relic User API Key (prompted interactively if omitted)",
    )
    parser.add_argument(
        "--region",
        choices=["US", "EU"],
        default="US",
        help="Data center region (default: US)",
    )
    return parser.parse_args()


async def main() -> None:
    """Credential validation flow with argparse and learn_account preview."""
    args = _parse_args()

    print("=" * 60)
    print("  Sherlock — Connection Test")
    print("=" * 60)
    print()

    # Use CLI args if provided, then .env, then fall back to interactive prompts.
    account_id = args.account_id or os.getenv("NEW_RELIC_ACCOUNT_ID")
    if not account_id:
        account_id = input("Enter your New Relic Account ID: ").strip()
        if not account_id:
            print("Error: Account ID is required.")
            sys.exit(1)

    api_key = args.api_key or os.getenv("NEW_RELIC_API_KEY")
    if not api_key:
        api_key = input("Enter your New Relic User API Key: ").strip()
        if not api_key:
            print("Error: API key is required.")
            sys.exit(1)

    region = args.region or os.getenv("NEW_RELIC_REGION", "US")
    if not args.account_id and not args.api_key and not os.getenv("NEW_RELIC_ACCOUNT_ID"):
        # Interactive mode — also prompt for region.
        region_input = input("Region (US/EU) [US]: ").strip().upper() or "US"
        if region_input in ("US", "EU"):
            region = region_input
        else:
            print(f"Warning: Invalid region '{region_input}', defaulting to US.")
            region = "US"

    print()
    print(f"Testing connection to New Relic ({region})...")
    print(f"Account ID: {account_id}")
    print(f"API Key: {api_key[:4]}***{api_key[-4:]}")
    print()

    manager = CredentialManager()
    result = await manager.validate_credentials(account_id, api_key, region)

    if result["valid"]:
        print("✅ Connection successful!")
        print(f"   User: {result['user_name']}")
        print(f"   Account: {result['account_name']}")
        print()

        # Discover all accessible accounts.
        from core.credentials import Credentials
        from core.intelligence import discover_accounts, learn_account

        credentials = Credentials(
            account_id=account_id,
            api_key=api_key,
            region=region,
        )

        print("Discovering accessible accounts...")
        try:
            accessible = await discover_accounts(credentials)
            if accessible:
                print(f"\n   Your API key has access to {len(accessible)} accounts:")
                total_all = sum(a.entity_count for a in accessible)
                for i, acct in enumerate(accessible, 1):
                    marker = " ◀ (selected)" if acct.id == account_id else ""
                    print(f"   {i:>3}. [{acct.id}] {acct.name:<40} {acct.entity_count:>6,} entities{marker}")
                print(f"   {'':>4}  {'':>8} {'Total':>40} {total_all:>6,} entities")
                print()

                # Allow user to switch accounts
                switch = input(
                    f"Enter account number to explore (1-{len(accessible)}) "
                    f"or press Enter to keep [{account_id}]: "
                ).strip()
                if switch.isdigit() and 1 <= int(switch) <= len(accessible):
                    chosen = accessible[int(switch) - 1]
                    account_id = chosen.id
                    credentials = Credentials(
                        account_id=account_id,
                        api_key=api_key,
                        region=region,
                    )
                    print(f"   Switched to: [{account_id}] {chosen.name}")
                print()
        except Exception as exc:
            print(f"   ⚠️  Account discovery failed: {exc}")
            print()

        # Preview learn_account to show what the server will discover.
        preview = input("Preview account intelligence? (y/N): ").strip().lower()
        if preview == "y":
            print()
            print(f"Learning account {account_id} structure (this may take 10-30 seconds)...")
            try:
                intelligence = await learn_account(credentials)
                print()
                print("Account Intelligence Preview:")
                print(f"   Total Entities:     {intelligence.entity_counts.total_entities:,}")
                print()

                # ── APM ──
                print(f"   Services - APM:     {intelligence.account_meta.total_apm_services}")
                if intelligence.apm.service_names:
                    for svc in intelligence.apm.service_names[:5]:
                        print(f"      • {svc}")
                    if len(intelligence.apm.service_names) > 5:
                        print(f"      ... and {len(intelligence.apm.service_names) - 5} more")

                # ── OpenTelemetry ──
                print(f"   Services - OTel:    {intelligence.otel.service_count}")
                if intelligence.otel.service_names:
                    for svc in intelligence.otel.service_names[:5]:
                        print(f"      • {svc}")
                    if len(intelligence.otel.service_names) > 5:
                        print(f"      ... and {len(intelligence.otel.service_names) - 5} more")

                # ── Infrastructure ──
                print(f"   Infra Hosts:        {intelligence.infra.host_count}")
                print(f"   Containers:         {intelligence.infra.container_count}")

                # ── Browser & Mobile ──
                print(f"   Browser Apps:       {len(intelligence.browser.app_names)}")
                print(f"   Mobile Apps:        {intelligence.mobile.app_count}")

                # ── Synthetics ──
                print(f"   Synthetic Monitors: {intelligence.synthetics.total_count}")
                if intelligence.synthetics.monitor_names:
                    for mon in intelligence.synthetics.monitor_names[:5]:
                        print(f"      • {mon}")
                    if len(intelligence.synthetics.monitor_names) > 5:
                        print(f"      ... and {len(intelligence.synthetics.monitor_names) - 5} more")

                # ── Workloads ──
                print(f"   Workloads:          {intelligence.workloads.workload_count}")

                # ── Kubernetes ──
                print(f"   K8s Integrated:     {intelligence.k8s.integrated}")
                if intelligence.k8s.integrated:
                    print(f"      Clusters:           {intelligence.k8s.cluster_count}")
                    if intelligence.k8s.cluster_names:
                        for cl in intelligence.k8s.cluster_names[:5]:
                            print(f"         • {cl}")
                    print(f"      Namespaces:         {intelligence.k8s.namespace_count}")
                    print(f"      Deployments:        {intelligence.k8s.deployment_count}")
                    print(f"      Pods:               {intelligence.k8s.pod_count}")
                    print(f"      DaemonSets:         {intelligence.k8s.daemonset_count}")
                    print(f"      StatefulSets:       {intelligence.k8s.statefulset_count}")
                    print(f"      Jobs:               {intelligence.k8s.job_count}")
                    print(f"      CronJobs:           {intelligence.k8s.cronjob_count}")
                    print(f"      PersistentVolumes:  {intelligence.k8s.pv_count}")
                    print(f"      PersistentVolumeClaims: {intelligence.k8s.pvc_count}")

                # ── Alerts & Logs ──
                print(f"   Alert Policies:     {len(intelligence.alerts.policy_names)}")
                print(f"   Logs Enabled:       {intelligence.logs.enabled}")
                print(f"   Key Transactions:   {intelligence.entity_counts.key_transaction_count}")
                print(f"   Service Levels:     {intelligence.entity_counts.service_level_count}")

                # ── Azure Resources ──
                if intelligence.entity_counts.azure_resource_count > 0:
                    print(f"   Azure Resources:    {intelligence.entity_counts.azure_resource_count}")
                    for atype in intelligence.entity_counts.azure_resource_types[:10]:
                        # Find count from type_breakdown
                        for tb in intelligence.entity_counts.type_breakdown:
                            if tb.type == atype:
                                print(f"      • {atype}: {tb.count}")
                                break
                    if len(intelligence.entity_counts.azure_resource_types) > 10:
                        print(f"      ... and {len(intelligence.entity_counts.azure_resource_types) - 10} more types")

                # ── Full Entity Type Breakdown (summary) ──
                if intelligence.entity_counts.type_breakdown:
                    print()
                    print("   Entity Type Breakdown:")
                    sorted_types = sorted(
                        intelligence.entity_counts.type_breakdown,
                        key=lambda x: x.count,
                        reverse=True,
                    )
                    for et in sorted_types[:25]:
                        print(f"      {et.domain}/{et.type}: {et.count:,}")
                    if len(sorted_types) > 25:
                        print(f"      ... and {len(sorted_types) - 25} more types")
            except Exception as exc:
                print(f"⚠️  learn_account preview failed: {exc}")
                print("   (Connection still works — learning may succeed at runtime.)")

        print()
        save = input("Save as profile? (y/N): ").strip().lower()
        if save == "y":
            profile_name = input("Profile name: ").strip()
            if profile_name:
                manager.save_profile(profile_name, account_id, api_key, region)
                print(f"✅ Profile '{profile_name}' saved.")
            else:
                print("Skipped — no profile name provided.")
    else:
        print(f"❌ Connection failed: {result['error']}")
        sys.exit(1)

    print()
    print("You can now start the MCP server with: python main.py")
    print("Or use the CLI: python scripts/cli.py")


if __name__ == "__main__":
    asyncio.run(main())
