"""
Intelligence management tools for Sherlock.

Provides MCP-facing tools for connecting accounts, learning account
intelligence, viewing summaries, managing profiles, and getting
NRQL query context.
"""

import asyncio
import json
import logging
import time

from core.cache import IntelligenceCache
from core.context import AccountContext
from core.credentials import CredentialManager, Credentials
from core.intelligence import AccountIntelligence, learn_account

logger = logging.getLogger("sherlock.tools.intelligence_tools")

# Module-level singletons.
_credential_manager = CredentialManager()
_cache = IntelligenceCache()


async def _background_refresh(credentials: Credentials, account_id: str) -> None:
    """Background task to refresh stale intelligence cache.

    Runs learn_account and updates both the cache and the active context
    if the refreshed account is still the active one.

    Args:
        credentials: Credentials for the account to refresh.
        account_id: The account ID being refreshed.
    """
    try:
        intelligence = await learn_account(credentials)
        _cache.set(account_id, intelligence.model_dump(mode="json"))
        # Update the live context if this account is still active.
        ctx = AccountContext()
        if ctx.is_connected():
            active_creds, _ = ctx.get_active()
            if active_creds.account_id == account_id:
                ctx.set_active(credentials, intelligence)
        logger.info("Background cache refresh complete for account %s", account_id)
    except Exception as exc:
        logger.warning(
            "Background cache refresh failed for account %s: %s", account_id, exc
        )


async def connect_account(
    account_id: str,
    api_key: str,
    region: str = "US",
    profile_name: str | None = None,
) -> str:
    """Connect to a New Relic account and learn its structure.

    Must be called before any other tool. Validates credentials,
    learns the account, caches intelligence, and sets the active context.

    Args:
        account_id: New Relic account ID.
        api_key: New Relic User API key.
        region: 'US' or 'EU'.
        profile_name: Optional profile name to save for later reuse.

    Returns:
        JSON string with connection status and account summary.
    """
    start = time.time()
    try:
        credentials = Credentials(
            account_id=account_id,
            api_key=api_key,
            region=region.upper(),
        )

        # Validate credentials.
        validation = await _credential_manager.validate_credentials(
            account_id, api_key, region
        )

        if not validation["valid"]:
            return json.dumps({
                "error": f"Credential validation failed: {validation['error']}",
                "tool": "connect_account",
                "hint": "Check your API key and account ID.",
                "data_available": False,
            })

        # Save profile if requested.
        if profile_name:
            _credential_manager.save_profile(profile_name, account_id, api_key, region)

        # Check cache first.
        cached = _cache.get(account_id)
        if cached:
            intelligence = AccountIntelligence(**cached)
            logger.info("Using cached intelligence for account %s", account_id)
        else:
            # Try stale cache for fast startup, then refresh in background.
            stale = _cache.get_stale(account_id)
            if stale:
                intelligence = AccountIntelligence(**stale)
                logger.info(
                    "Using stale intelligence for account %s; "
                    "scheduling background refresh",
                    account_id,
                )
                asyncio.create_task(_background_refresh(credentials, account_id))
            else:
                intelligence = await learn_account(credentials)
                _cache.set(account_id, intelligence.model_dump(mode="json"))

        # Set active context.
        ctx = AccountContext()
        ctx.set_active(credentials, intelligence)

        # Build synthetics summary.
        synth_enabled = [
            name for name, meta in intelligence.synthetics.monitor_map.items()
            if (meta.status or "").upper() != "DISABLED"
        ]
        synthetics_summary = {
            "count": intelligence.synthetics.total_count,
            "enabled": len(synth_enabled),
            "types": intelligence.synthetics.monitor_types,
            "sample_monitors": intelligence.synthetics.monitor_names[:5],
        }

        duration_ms = int((time.time() - start) * 1000)
        return json.dumps({
            "status": "connected",
            "account_id": account_id,
            "account_name": validation["account_name"],
            "user_name": validation["user_name"],
            "region": region.upper(),
            "profile_saved": profile_name is not None,
            "summary": {
                "total_entities": intelligence.entity_counts.total_entities,
                "apm_services": intelligence.account_meta.total_apm_services,
                "sample_services": intelligence.apm.service_names[:5],
                "otel_services": intelligence.otel.service_count,
                "k8s_integrated": intelligence.k8s.integrated,
                "k8s_namespaces": intelligence.k8s.namespaces[:5],
                "k8s_clusters": intelligence.k8s.cluster_count,
                "alert_policies": len(intelligence.alerts.policy_names),
                "logs_enabled": intelligence.logs.enabled,
                "synthetics": synthetics_summary,
                "infra_hosts": intelligence.infra.host_count,
                "containers": intelligence.infra.container_count,
                "browser_apps": len(intelligence.browser.app_names),
                "mobile_apps": intelligence.mobile.app_count,
                "workloads": intelligence.workloads.workload_count,
                "key_transactions": intelligence.entity_counts.key_transaction_count,
                "azure_resources": intelligence.entity_counts.azure_resource_count,
            },
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "connect_account",
            "hint": "Check your account_id, api_key, and region.",
            "data_available": False,
        })


async def learn_account_tool() -> str:
    """Re-learn the active account's structure (force refresh).

    Invalidates the cache and re-discovers all account data.

    Returns:
        JSON string with updated account summary.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, _ = ctx.get_active()

        _cache.invalidate(credentials.account_id)
        intelligence = await learn_account(credentials)
        _cache.set(credentials.account_id, intelligence.model_dump(mode="json"))
        ctx.set_active(credentials, intelligence)

        duration_ms = int((time.time() - start) * 1000)
        return json.dumps({
            "status": "refreshed",
            "account_id": credentials.account_id,
            "total_entities": intelligence.entity_counts.total_entities,
            "apm_services": intelligence.account_meta.total_apm_services,
            "otel_services": intelligence.otel.service_count,
            "k8s_namespaces": len(intelligence.k8s.namespaces),
            "k8s_clusters": intelligence.k8s.cluster_count,
            "alert_policies": len(intelligence.alerts.policy_names),
            "synthetic_monitors": intelligence.synthetics.total_count,
            "infra_hosts": intelligence.infra.host_count,
            "containers": intelligence.infra.container_count,
            "browser_apps": len(intelligence.browser.app_names),
            "mobile_apps": intelligence.mobile.app_count,
            "workloads": intelligence.workloads.workload_count,
            "logs_enabled": intelligence.logs.enabled,
            "duration_ms": duration_ms,
        })

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "learn_account",
            "hint": "Ensure you are connected first.",
            "data_available": False,
        })


async def get_account_summary() -> str:
    """Get full intelligence summary for the active account.

    Returns:
        JSON string with complete account intelligence.
    """
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()

        return json.dumps({
            "account_id": credentials.account_id,
            "account_name": intelligence.account_meta.name,
            "learned_at": intelligence.learned_at.isoformat(),
            "total_entities": intelligence.entity_counts.total_entities,
            "apm": {
                "total_count": intelligence.account_meta.total_apm_services,
                "service_names": intelligence.apm.service_names,
                "service_languages": intelligence.apm.service_languages,
                "naming_pattern": intelligence.apm.naming_pattern,
                "top_error_classes": intelligence.apm.top_error_classes,
                "environments": intelligence.apm.environments,
            },
            "otel": {
                "enabled": intelligence.otel.enabled,
                "service_count": intelligence.otel.service_count,
                "service_names": intelligence.otel.service_names,
            },
            "k8s": {
                "integrated": intelligence.k8s.integrated,
                "clusters": intelligence.k8s.cluster_count,
                "cluster_names": intelligence.k8s.cluster_names,
                "namespaces": intelligence.k8s.namespaces,
                "deployments": intelligence.k8s.deployment_count,
                "pods": intelligence.k8s.pod_count,
                "daemonsets": intelligence.k8s.daemonset_count,
                "statefulsets": intelligence.k8s.statefulset_count,
                "jobs": intelligence.k8s.job_count,
                "cronjobs": intelligence.k8s.cronjob_count,
                "persistent_volumes": intelligence.k8s.pv_count,
                "persistent_volume_claims": intelligence.k8s.pvc_count,
                "naming_pattern": intelligence.k8s.naming_pattern,
            },
            "alerts": {
                "policy_names": intelligence.alerts.policy_names,
                "naming_pattern": intelligence.alerts.naming_pattern,
            },
            "logs": {
                "enabled": intelligence.logs.enabled,
                "service_attribute": intelligence.logs.service_attribute,
                "severity_attribute": intelligence.logs.severity_attribute,
                "top_error_messages": intelligence.logs.top_error_messages,
            },
            "synthetics": {
                "enabled": intelligence.synthetics.enabled,
                "monitor_names": intelligence.synthetics.monitor_names,
                "monitor_map": {
                    name: meta.model_dump()
                    for name, meta in intelligence.synthetics.monitor_map.items()
                },
                "monitor_types": intelligence.synthetics.monitor_types,
                "naming_pattern": intelligence.synthetics.naming_pattern,
                "total_count": intelligence.synthetics.total_count,
            },
            "infra": {
                "cloud_provider": intelligence.infra.cloud_provider,
                "regions": intelligence.infra.regions,
                "host_count": intelligence.infra.host_count,
                "container_count": intelligence.infra.container_count,
            },
            "browser": {
                "enabled": intelligence.browser.enabled,
                "app_names": intelligence.browser.app_names,
            },
            "mobile": {
                "enabled": intelligence.mobile.enabled,
                "app_count": intelligence.mobile.app_count,
                "app_names": intelligence.mobile.app_names,
            },
            "workloads": {
                "enabled": intelligence.workloads.enabled,
                "workload_count": intelligence.workloads.workload_count,
                "workload_names": intelligence.workloads.workload_names,
            },
            "entity_counts": {
                "key_transactions": intelligence.entity_counts.key_transaction_count,
                "service_levels": intelligence.entity_counts.service_level_count,
                "azure_resources": intelligence.entity_counts.azure_resource_count,
                "azure_resource_types": intelligence.entity_counts.azure_resource_types,
            },
        })

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_account_summary",
            "hint": "Ensure you are connected first.",
            "data_available": False,
        })


async def list_profiles() -> str:
    """List all saved credential profiles.

    Returns:
        JSON string with profile metadata.
    """
    try:
        profiles = _credential_manager.list_profiles()
        return json.dumps({
            "total_profiles": len(profiles),
            "profiles": profiles,
        })
    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "list_profiles",
            "hint": "Profile storage may not be initialized.",
            "data_available": False,
        })


async def get_nrql_context(domain: str = "all") -> str:
    """Get NRQL query context for the active account.

    Returns actual service names, attribute names, and sample NRQL patterns
    so the AI can construct correct NRQL queries.

    Args:
        domain: Domain to get context for — 'apm', 'k8s', 'logs',
                'alerts', 'synthetics', or 'all'.

    Returns:
        JSON string with NRQL context including real names and patterns.
    """
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()

        context: dict = {"account_id": credentials.account_id, "domain": domain}

        if domain in ("apm", "all"):
            context["apm"] = {
                "event_types": ["Transaction", "TransactionError", "Deployment"],
                "service_names": intelligence.apm.service_names,
                "key_attributes": [
                    "appName", "name", "duration", "error", "httpResponseCode",
                    "request.uri", "host", "error.class", "error.message",
                ],
                "sample_queries": [
                    f"SELECT average(duration), count(*) FROM Transaction WHERE appName = '{intelligence.apm.service_names[0]}' SINCE 30 minutes ago"
                    if intelligence.apm.service_names else
                    "SELECT average(duration), count(*) FROM Transaction SINCE 30 minutes ago",
                    "SELECT count(*) FROM TransactionError FACET error.class SINCE 1 hour ago LIMIT 10",
                ],
            }

        if domain in ("k8s", "all"):
            context["k8s"] = {
                "event_types": [
                    "K8sPodSample", "K8sContainerSample", "K8sDeploymentSample",
                    "K8sNodeSample", "K8sClusterSample",
                ],
                "namespaces": intelligence.k8s.namespaces,
                "cluster_names": intelligence.k8s.cluster_names,
                "key_attributes": [
                    "namespaceName", "podName", "deploymentName", "clusterName",
                    "status", "isReady", "restartCountDelta", "cpuUsedCoreMilliseconds",
                    "memoryWorkingSetBytes",
                ],
            }

        if domain in ("logs", "all"):
            context["logs"] = {
                "event_type": "Log",
                "service_attribute": intelligence.logs.service_attribute,
                "severity_attribute": intelligence.logs.severity_attribute,
                "key_attributes": [
                    "message", "timestamp",
                    intelligence.logs.service_attribute or "service.name",
                    intelligence.logs.severity_attribute or "level",
                ],
                "sample_queries": [
                    f"SELECT * FROM Log WHERE {intelligence.logs.service_attribute or 'service.name'} = "
                    f"'{intelligence.apm.service_names[0]}' SINCE 1 hour ago LIMIT 100"
                    if intelligence.apm.service_names else
                    "SELECT * FROM Log SINCE 1 hour ago LIMIT 100",
                ],
            }

        if domain in ("alerts", "all"):
            context["alerts"] = {
                "event_types": ["NrAiIncident", "NrAiSignal"],
                "policy_names": intelligence.alerts.policy_names,
                "key_attributes": [
                    "event", "priority", "conditionName", "policyName",
                    "targetName", "openTime", "closeTime",
                ],
            }

        if domain in ("synthetics", "all"):
            context["synthetics"] = {
                "event_types": ["SyntheticCheck", "SyntheticRequest"],
                "monitor_names": intelligence.synthetics.monitor_names,
                "monitor_types": intelligence.synthetics.monitor_types,
                "key_attributes": {
                    "SyntheticCheck": [
                        "monitorName", "result", "duration", "locationLabel",
                        "error", "timestamp", "monitorId",
                    ],
                    "SyntheticRequest": [
                        "monitorName", "URL", "verb", "responseCode",
                        "duration", "timestamp",
                    ],
                },
                "available_locations": list({
                    loc
                    for meta in intelligence.synthetics.monitor_map.values()
                    for loc in meta.locations
                }),
                "sample_queries": [
                    f"SELECT percentage(count(*), WHERE result='SUCCESS') FROM SyntheticCheck WHERE monitorName='{intelligence.synthetics.monitor_names[0]}' SINCE 1 hour ago"
                    if intelligence.synthetics.monitor_names else
                    "SELECT percentage(count(*), WHERE result='SUCCESS') FROM SyntheticCheck SINCE 1 hour ago",
                    "SELECT * FROM SyntheticCheck WHERE result='FAILED' SINCE 1 hour ago ORDER BY timestamp DESC LIMIT 20",
                    "SELECT timestamp, URL, responseCode FROM SyntheticRequest WHERE responseCode >= 400 SINCE 1 hour ago LIMIT 50",
                ],
            }

        return json.dumps(context)

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "get_nrql_context",
            "hint": "Ensure you are connected first.",
            "data_available": False,
        })
