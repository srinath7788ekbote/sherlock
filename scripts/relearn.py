"""
Force re-learn intelligence for one or all saved profiles.

Usage:
    python scripts/relearn.py                     # Re-learn ALL profiles
    python scripts/relearn.py --profile DFIN_AD   # Re-learn one profile
    python scripts/relearn.py --list              # List available profiles
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# Add project root to path.
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.credentials import CredentialManager
from tools.intelligence_tools import connect_account, learn_account_tool


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Force re-learn intelligence for Sherlock profiles.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--profile", "-p",
        help="Profile name to re-learn (omit to re-learn all)",
    )
    group.add_argument(
        "--list", "-l",
        action="store_true",
        help="List available profiles and exit",
    )
    return parser.parse_args()


async def _connect_profile(name: str, manager: CredentialManager) -> bool:
    """Connect to a profile. Returns True on success."""
    try:
        creds = manager.load_profile(name)
    except Exception as exc:
        print(f"  [SKIP] Cannot load profile '{name}': {exc}")
        return False

    result = json.loads(await connect_account(
        account_id=creds.account_id,
        api_key=creds.api_key,
        region=creds.region,
    ))
    if result.get("error"):
        print(f"  [FAIL] Connect failed: {result['error']}")
        return False
    return True


async def _relearn_profile(name: str, manager: CredentialManager) -> dict:
    """Force re-learn a single profile. Returns result summary."""
    print(f"\n{'='*50}")
    print(f"  Profile: {name}")
    print(f"{'='*50}")

    if not await _connect_profile(name, manager):
        return {"profile": name, "status": "connect_failed"}

    print("  Connecting... OK")
    print("  Force re-learning (this may take 10-30s)...")

    start = time.time()
    result = json.loads(await learn_account_tool(force=True))
    elapsed = time.time() - start

    if result.get("error"):
        print(f"  [FAIL] {result['error']}")
        return {"profile": name, "status": "learn_failed", "error": result["error"]}

    status = result.get("status", "unknown")
    entities = result.get("total_entities", "?")
    apm = result.get("apm_services", "?")
    otel = result.get("otel_services", "?")
    k8s_ns = result.get("k8s_namespaces", "?")

    print(f"  Status:     {status}")
    print(f"  Entities:   {entities}")
    print(f"  APM:        {apm} services")
    print(f"  OTel:       {otel} services")
    print(f"  K8s:        {k8s_ns} namespaces")
    print(f"  Duration:   {elapsed:.1f}s")

    return {
        "profile": name,
        "status": status,
        "total_entities": entities,
        "duration_s": round(elapsed, 1),
    }


async def main() -> None:
    args = _parse_args()
    manager = CredentialManager()
    profiles = manager.list_profiles()

    if not profiles:
        print("No profiles found. Save profiles via connect_account first.")
        sys.exit(1)

    profile_names = [p["name"] for p in profiles]

    if args.list:
        print("Available profiles:")
        for p in profiles:
            print(f"  - {p['name']}  (account: {p['account_id']}, region: {p.get('region', '?')})")
        sys.exit(0)

    if args.profile:
        if args.profile not in profile_names:
            print(f"Profile '{args.profile}' not found.")
            print(f"Available: {', '.join(profile_names)}")
            sys.exit(1)
        targets = [args.profile]
    else:
        targets = profile_names

    print(f"Sherlock — Force Re-learn")
    print(f"Profiles to re-learn: {len(targets)}")

    results = []
    for name in targets:
        r = await _relearn_profile(name, manager)
        results.append(r)

    # Summary
    print(f"\n{'='*50}")
    print(f"  Summary")
    print(f"{'='*50}")
    ok = [r for r in results if r["status"] == "refreshed"]
    failed = [r for r in results if r["status"] != "refreshed"]
    print(f"  Succeeded: {len(ok)}/{len(results)}")
    if failed:
        print(f"  Failed:    {', '.join(r['profile'] for r in failed)}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
