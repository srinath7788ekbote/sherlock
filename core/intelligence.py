"""
Account intelligence learning for Sherlock.

Discovers an account's services, K8s namespaces, alert policies, log
attributes, synthetic monitors, and more — all in parallel. The resulting
AccountIntelligence model enables every tool to work perfectly against
any client's actual data with zero manual configuration.
"""

import asyncio
import base64
import logging
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher

from pydantic import BaseModel, Field

from core.credentials import Credentials
from core.exceptions import IntelligenceError

logger = logging.getLogger("sherlock.intelligence")

# ── GraphQL Queries ──────────────────────────────────────────────────────

GQL_ACCESSIBLE_ACCOUNTS = """
{
  actor {
    accounts {
      id
      name
    }
  }
}
"""

GQL_ACCOUNT_META = """
{
  actor {
    account(id: %s) {
      name
    }
  }
}
"""

# Single query to get entity counts by domain/type for the entire account.
GQL_ENTITY_TYPE_COUNTS = """
{
  actor {
    entitySearch(query: "accountId = %s") {
      count
      types {
        domain
        type
        count
      }
    }
  }
}
"""

# Paginated entity search template — use cursor for pagination.
# %s = account_id, %s = domain/type filter, %s = cursor clause
GQL_ENTITY_SEARCH_PAGINATED = """
{
  actor {
    entitySearch(query: "accountId = %s AND %s") {
      results%s {
        nextCursor
        entities {
          guid
          name
          type
          domain
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

GQL_APM_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'APM' AND type = 'APPLICATION'") {
      count
      results {
        nextCursor
        entities {
          guid
          name
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

GQL_OTEL_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'EXT' AND type = 'SERVICE'") {
      count
      results {
        entities {
          guid
          name
        }
      }
    }
  }
}
"""

GQL_BROWSER_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'BROWSER' AND type = 'APPLICATION'") {
      count
      results {
        entities {
          guid
          name
        }
      }
    }
  }
}
"""

GQL_MOBILE_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'MOBILE' AND type = 'APPLICATION'") {
      count
      results {
        entities {
          guid
          name
        }
      }
    }
  }
}
"""

GQL_INFRA_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'INFRA' AND type = 'HOST'") {
      count
      results {
        entities {
          guid
          name
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

GQL_CONTAINER_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'INFRA' AND type = 'CONTAINER'") {
      count
      results {
        entities {
          guid
          name
        }
      }
    }
  }
}
"""

GQL_WORKLOAD_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'NR1' AND type = 'WORKLOAD'") {
      count
      results {
        entities {
          guid
          name
        }
      }
    }
  }
}
"""

GQL_KEY_TRANSACTION_ENTITIES = """
{
  actor {
    entitySearch(query: "accountId = %s AND type = 'KEY_TRANSACTION'") {
      count
      results {
        entities {
          guid
          name
        }
      }
    }
  }
}
"""

GQL_ALERT_POLICIES = """
{
  actor {
    account(id: %s) {
      alerts {
        policiesSearch {
          policies {
            id
            name
          }
          totalCount
        }
      }
    }
  }
}
"""

GQL_SYNTHETIC_MONITORS = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'SYNTH' AND type = 'MONITOR'") {
      count
      results {
        nextCursor
        entities {
          guid
          name
          alertSeverity
          ... on SyntheticMonitorEntityOutline {
            monitorType
            period
            monitoredUrl
          }
        }
      }
    }
  }
}
"""

GQL_SYNTHETIC_PRIVATE_LOCATIONS = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'SYNTH' AND type = 'PRIVATE_LOCATION'") {
      count
      results {
        entities {
          guid
          name
        }
      }
    }
  }
}
"""

GQL_SYNTHETIC_SECURE_CREDS = """
{
  actor {
    entitySearch(query: "accountId = %s AND domain = 'SYNTH' AND type = 'SECURE_CRED'") {
      count
      results {
        entities {
          guid
          name
        }
      }
    }
  }
}
"""

NRQL_K8S_NAMESPACES = (
    "SELECT uniques(namespaceName) FROM K8sPodSample SINCE 1 day ago LIMIT 200"
)

NRQL_K8S_DEPLOYMENTS = (
    "SELECT uniques(deploymentName) FROM K8sDeploymentSample"
    " FACET namespaceName SINCE 1 day ago LIMIT 200"
)

NRQL_K8S_CLUSTERS = (
    "SELECT uniques(clusterName) FROM K8sPodSample SINCE 1 day ago LIMIT 50"
)

# K8s entity sub-type counts via NRQL
NRQL_K8S_POD_COUNT = (
    "SELECT uniqueCount(entityName) FROM K8sPodSample SINCE 1 day ago"
)

NRQL_K8S_DAEMONSET_COUNT = (
    "SELECT uniqueCount(daemonsetName) FROM K8sDaemonsetSample SINCE 1 day ago"
)

NRQL_K8S_STATEFULSET_COUNT = (
    "SELECT uniqueCount(statefulsetName) FROM K8sStatefulsetSample SINCE 1 day ago"
)

NRQL_K8S_JOB_COUNT = (
    "SELECT uniqueCount(jobName) FROM K8sJobSample SINCE 1 day ago"
)

NRQL_K8S_CRONJOB_COUNT = (
    "SELECT uniqueCount(cronJobName) FROM K8sCronJobSample SINCE 1 day ago"
)

NRQL_K8S_PV_COUNT = (
    "SELECT uniqueCount(volumeName) FROM K8sPersistentVolumeSample SINCE 1 day ago"
)

NRQL_K8S_PVC_COUNT = (
    "SELECT uniqueCount(pvcName) FROM K8sPersistentVolumeClaimSample SINCE 1 day ago"
)

NRQL_LOG_KEYSET = "SELECT keyset() FROM Log SINCE 1 day ago"
NRQL_LOG_COUNT = "SELECT count(*) FROM Log SINCE 1 day ago"
# Probe which service/severity attributes exist in logs (fallback when keyset fails).
NRQL_LOG_ATTR_PROBE = (
    "SELECT "
    + ", ".join(
        f"uniqueCount(`{attr}`) as `has_{attr.replace('.', '_')}`"
        for attr in [
            "service.name", "serviceName", "service", "app.name",
            "application", "appName", "entity.name",
            "level", "severity", "log_severity", "log.level",
            "loglevel", "priority",
        ]
    )
    + " FROM Log SINCE 1 day ago"
)

NRQL_TOP_ERRORS = (
    "SELECT count(*) FROM TransactionError"
    " FACET error.class SINCE 7 days ago LIMIT 20"
)

NRQL_SYNTHETIC_LOCATIONS = (
    "SELECT latest(locationLabel), latest(result), latest(duration), latest(error)"
    " FROM SyntheticCheck WHERE monitorName = '%s'"
    " FACET locationLabel SINCE 1 hour ago LIMIT 20"
)

NRQL_TEMPLATE = """
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

# ── Service attribute heuristics ─────────────────────────────────────────

SERVICE_ATTR_CANDIDATES = [
    "service.name", "entity.name", "serviceName", "service",
    "app.name", "application", "appName",
]

SEVERITY_ATTR_CANDIDATES = [
    "level", "severity", "log_severity", "log.level",
    "loglevel", "priority",
]


# ── Pydantic Models ─────────────────────────────────────────────────────

class APMIntelligence(BaseModel):
    """Intelligence about APM services in the account."""

    service_names: list[str] = Field(default_factory=list)
    service_guids: dict[str, str] = Field(default_factory=dict)
    service_languages: dict[str, str] = Field(default_factory=dict)
    naming_pattern: str = ""
    top_error_classes: list[str] = Field(default_factory=list)
    environments: list[str] = Field(default_factory=list)


class K8sIntelligence(BaseModel):
    """Intelligence about Kubernetes integration in the account."""

    integrated: bool = False
    namespaces: list[str] = Field(default_factory=list)
    deployments: dict[str, list[str]] = Field(default_factory=dict)
    cluster_names: list[str] = Field(default_factory=list)
    naming_pattern: str = ""
    # Entity sub-type counts
    cluster_count: int = 0
    namespace_count: int = 0
    deployment_count: int = 0
    pod_count: int = 0
    daemonset_count: int = 0
    statefulset_count: int = 0
    job_count: int = 0
    cronjob_count: int = 0
    pv_count: int = 0
    pvc_count: int = 0


class AlertsIntelligence(BaseModel):
    """Intelligence about alert policies in the account."""

    policy_names: list[str] = Field(default_factory=list)
    naming_pattern: str = ""


class LogsIntelligence(BaseModel):
    """Intelligence about logging configuration in the account."""

    enabled: bool = False
    service_attribute: str = ""
    severity_attribute: str = ""
    top_error_messages: list[str] = Field(default_factory=list)


class SyntheticMonitorMeta(BaseModel):
    """Metadata for a single synthetic monitor."""

    guid: str = ""
    name: str = ""
    type: str = ""
    status: str = ""
    period: int | str = ""
    locations: list[str] = Field(default_factory=list)
    associated_service: str | None = None


class SyntheticsIntelligence(BaseModel):
    """Intelligence about synthetic monitoring in the account."""

    enabled: bool = False
    monitor_names: list[str] = Field(default_factory=list)
    monitor_map: dict[str, SyntheticMonitorMeta] = Field(default_factory=dict)
    monitor_types: list[str] = Field(default_factory=list)
    naming_pattern: str = ""
    total_count: int = 0


class InfraIntelligence(BaseModel):
    """Intelligence about infrastructure monitoring in the account."""

    cloud_provider: str | None = None
    regions: list[str] = Field(default_factory=list)
    host_count: int = 0
    container_count: int = 0


class BrowserIntelligence(BaseModel):
    """Intelligence about browser monitoring in the account."""

    enabled: bool = False
    app_names: list[str] = Field(default_factory=list)


class MobileIntelligence(BaseModel):
    """Intelligence about mobile monitoring in the account."""

    enabled: bool = False
    app_names: list[str] = Field(default_factory=list)
    app_count: int = 0


class OTelIntelligence(BaseModel):
    """Intelligence about OpenTelemetry services in the account."""

    enabled: bool = False
    service_names: list[str] = Field(default_factory=list)
    service_count: int = 0


class WorkloadIntelligence(BaseModel):
    """Intelligence about workloads in the account."""

    enabled: bool = False
    workload_names: list[str] = Field(default_factory=list)
    workload_count: int = 0


class CrossAccountEntity(BaseModel):
    """An entity whose GUID encodes a different account than the connected one."""

    name: str = ""
    guid: str = ""
    entity_type: str = ""  # APM, EXT, INFRA, SYNTH, etc.
    home_account_id: str = ""  # account the entity actually lives in
    connected_account_id: str = ""  # account we're connected to


class EntityTypeSummary(BaseModel):
    """A single entity type with its domain, type, and count."""

    domain: str = ""
    type: str = ""
    count: int = 0


class EntityCountsSummary(BaseModel):
    """Summary of all entity type counts in the account."""

    total_entities: int = 0
    type_breakdown: list[EntityTypeSummary] = Field(default_factory=list)
    # Convenience aggregates for common categories
    azure_resource_count: int = 0
    azure_resource_types: list[str] = Field(default_factory=list)
    key_transaction_count: int = 0
    service_level_count: int = 0
    issue_count: int = 0


class AccountMeta(BaseModel):
    """High-level account metadata summary."""

    name: str = ""
    total_entities: int = 0
    total_apm_services: int = 0
    otel_services: int = 0
    k8s_integrated: bool = False
    logs_enabled: bool = False
    synthetics_enabled: bool = False
    synthetics_count: int = 0
    container_count: int = 0
    mobile_apps: int = 0
    workload_count: int = 0
    key_transaction_count: int = 0
    azure_resource_count: int = 0


class NamingConvention(BaseModel):
    """Learned naming convention for the account's services.

    Discovered automatically from ALL entity names (APM, K8s, Synthetics,
    Browser, Infra, OTel, etc.) during learn_account(). Uses statistical
    analysis — no hardcoded environment keywords — so it works for ANY
    client's naming convention without manual configuration.

    The learning algorithm:
      1. Detects the primary separator (/, -, _, .) from all entity names.
      2. Splits names into segments by that separator.
      3. Classifies each segment position by *cardinality ratio*:
         - Low cardinality (same values reused across many services)
           → environment/team/namespace segment.
         - High cardinality (unique per service) → service identifier.
      4. Cross-references with K8s namespaces and deployments.

    Used by fuzzy resolution, K8s query building, and other tools.
    """

    separator: str | None = None
    env_position: str | None = None  # "prefix" or "suffix" or None
    env_values: list[str] = Field(default_factory=list)
    bare_service_names: list[str] = Field(default_factory=list)
    apm_to_k8s_namespace_map: dict[str, str] = Field(default_factory=dict)
    k8s_deployment_name_format: str = "bare"  # "bare" or "full"
    # Enhanced fields for universal naming support
    segment_roles: list[str] = Field(default_factory=list)
    """Ordered list of segment roles by position, e.g. ["environment", "service"].
    Possible roles: "environment", "team", "service", "component", "unknown"."""
    team_values: list[str] = Field(default_factory=list)
    """Discovered team/org/namespace values (when separate from environment)."""
    secondary_separator: str | None = None
    """Sub-separator within segments, e.g. '-' within '/'-separated names."""
    all_entity_names: list[str] = Field(default_factory=list)
    """Union of all discovered entity names across domains. Used by
    fuzzy resolver for cross-domain matching."""


class AccountIntelligence(BaseModel):
    """Complete intelligence profile for a New Relic account."""

    account_id: str
    learned_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    apm: APMIntelligence = Field(default_factory=APMIntelligence)
    otel: OTelIntelligence = Field(default_factory=OTelIntelligence)
    k8s: K8sIntelligence = Field(default_factory=K8sIntelligence)
    alerts: AlertsIntelligence = Field(default_factory=AlertsIntelligence)
    logs: LogsIntelligence = Field(default_factory=LogsIntelligence)
    synthetics: SyntheticsIntelligence = Field(default_factory=SyntheticsIntelligence)
    infra: InfraIntelligence = Field(default_factory=InfraIntelligence)
    browser: BrowserIntelligence = Field(default_factory=BrowserIntelligence)
    mobile: MobileIntelligence = Field(default_factory=MobileIntelligence)
    workloads: WorkloadIntelligence = Field(default_factory=WorkloadIntelligence)
    entity_counts: EntityCountsSummary = Field(default_factory=EntityCountsSummary)
    account_meta: AccountMeta = Field(default_factory=AccountMeta)
    naming_convention: NamingConvention = Field(default_factory=NamingConvention)
    cross_account_entities: list[CrossAccountEntity] = Field(default_factory=list)


class AccessibleAccount(BaseModel):
    """An account accessible to the API key."""

    id: str
    name: str
    entity_count: int = 0


async def discover_accounts(credentials: Credentials) -> list[AccessibleAccount]:
    """Discover all accounts accessible to the given API key.

    Makes a single NerdGraph call to list accounts, then optionally
    fetches entity counts for each.

    Args:
        credentials: Credentials (any valid account_id + api_key).

    Returns:
        List of accessible accounts with entity counts.
    """
    import httpx

    endpoint = credentials.endpoint
    headers = {"API-Key": credentials.api_key, "Content-Type": "application/json"}

    async def _gql(query: str) -> dict:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                endpoint, json={"query": query}, headers=headers,
            )
            resp.raise_for_status()
            return resp.json()

    # Get all accessible accounts
    result = await _gql(GQL_ACCESSIBLE_ACCOUNTS)
    raw_accounts = (
        result.get("data", {}).get("actor", {}).get("accounts", [])
    )

    accounts = [
        AccessibleAccount(id=str(a["id"]), name=a.get("name", ""))
        for a in raw_accounts
        if a.get("id")
    ]

    # Fetch entity counts for each account in parallel
    if accounts:
        count_tasks = [
            _gql(GQL_ENTITY_TYPE_COUNTS % a.id) for a in accounts
        ]
        count_results = await asyncio.gather(*count_tasks, return_exceptions=True)
        for i, acct in enumerate(accounts):
            try:
                if not isinstance(count_results[i], BaseException):
                    count = (
                        count_results[i]
                        .get("data", {})
                        .get("actor", {})
                        .get("entitySearch", {})
                        .get("count", 0)
                    )
                    acct.entity_count = count
            except Exception:
                pass

    # Sort by entity count descending
    accounts.sort(key=lambda a: a.entity_count, reverse=True)
    return accounts


# ── Helper functions ─────────────────────────────────────────────────────


def decode_entity_guid(guid: str) -> dict:
    """Decode a New Relic entity GUID to its components.

    Entity GUIDs are base64-encoded strings in the format:
    ``account_id|entity_type|domain|entity_id``

    Args:
        guid: The base64-encoded entity GUID.

    Returns:
        Dict with ``account_id``, ``entity_type``, and ``domain`` keys,
        or empty dict if decoding fails.
    """
    try:
        padded = guid + "=" * (-len(guid) % 4)
        decoded = base64.b64decode(padded.encode()).decode("utf-8")
        parts = decoded.split("|")
        if len(parts) >= 3:
            return {
                "account_id": parts[0],
                "entity_type": parts[1],
                "domain": parts[2],
            }
    except Exception:
        pass
    return {}


def detect_cross_account_entities(
    intelligence: "AccountIntelligence",
) -> list[CrossAccountEntity]:
    """Scan all discovered entity GUIDs for cross-account references.

    Compares the account ID encoded in each entity GUID against the
    connected account ID. Any mismatch indicates the entity lives in a
    different New Relic account.

    Args:
        intelligence: The learned AccountIntelligence to scan.

    Returns:
        List of CrossAccountEntity objects for mismatched entities.
    """
    connected_id = intelligence.account_id
    cross: list[CrossAccountEntity] = []

    # Collect all (name, guid) pairs from every domain.
    guid_pairs: list[tuple[str, str, str]] = []  # (name, guid, source_type)

    for name, guid in intelligence.apm.service_guids.items():
        guid_pairs.append((name, guid, "APM"))

    for name in intelligence.otel.service_names:
        # OTel entities may not have GUIDs stored in a separate map,
        # but they appear in entity search results. Check via name match
        # against APM guids or deduce from the entity search data.
        pass

    for name, meta in intelligence.synthetics.monitor_map.items():
        if meta.guid:
            guid_pairs.append((name, meta.guid, "SYNTH"))

    for name, guid, source_type in guid_pairs:
        decoded = decode_entity_guid(guid)
        if not decoded:
            continue
        home_account = decoded.get("account_id", "")
        if home_account and home_account != connected_id:
            entity_type = decoded.get("entity_type", "UNKNOWN")
            domain = decoded.get("domain", "")
            cross.append(CrossAccountEntity(
                name=name,
                guid=guid,
                entity_type=f"{entity_type}|{domain}" if domain else entity_type,
                home_account_id=home_account,
                connected_account_id=connected_id,
            ))

    return cross


def _infer_naming_pattern(names: list[str]) -> str:
    """Infer a naming convention pattern from a list of names.

    Args:
        names: List of entity names.

    Returns:
        Human-readable description of the naming pattern detected.
    """
    if not names:
        return "unknown"

    patterns: list[str] = []

    # Check for common separators.
    dash_count = sum(1 for n in names if "-" in n)
    underscore_count = sum(1 for n in names if "_" in n)
    dot_count = sum(1 for n in names if "." in n)

    total = len(names)
    if dash_count > total * 0.5:
        patterns.append("kebab-case")
    if underscore_count > total * 0.5:
        patterns.append("snake_case")
    if dot_count > total * 0.5:
        patterns.append("dot.separated")

    # Check for environment suffixes.
    env_suffixes = ["-prod", "-staging", "-dev", "-qa", "-uat", "-test"]
    env_count = sum(1 for n in names if any(n.lower().endswith(s) for s in env_suffixes))
    if env_count > total * 0.3:
        patterns.append("env-suffixed")

    # Check for common prefixes.
    if len(names) >= 2:
        prefix = _common_prefix(names)
        if len(prefix) >= 3:
            patterns.append(f"prefix: '{prefix}'")

    return ", ".join(patterns) if patterns else "mixed"


def _common_prefix(names: list[str]) -> str:
    """Find the common prefix among a list of names.

    Args:
        names: List of strings.

    Returns:
        The longest common prefix.
    """
    if not names:
        return ""
    prefix = names[0]
    for name in names[1:]:
        while not name.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix


def _learn_naming_convention(
    apm_service_names: list[str],
    k8s_namespaces: list[str],
    k8s_deployments: dict[str, list[str]],
    *,
    extra_entity_names: list[str] | None = None,
) -> NamingConvention:
    """Learn the naming convention from the account's actual data.

    Uses **statistical segment analysis** — no hardcoded environment keywords.
    Works for ANY client naming convention:
      - "eswd-prod/pdf-export-service" (slash-separated, compound env prefix)
      - "auth-service-prod" (dash-separated, env suffix)
      - "team-alpha.billing.prod" (dot-separated, multi-level)
      - "my_service_production" (underscore-separated)
      - "blue/green/canary" — even unconventional schemes

    Algorithm:
      1. Detect the primary separator by consistency (most names split into
         the same segment count).
      2. For each segment position, compute *cardinality ratio*:
           cardinality_ratio = unique_values_in_segment / total_names
         Low ratio (< 0.4) + values reused across different remaining
         segments → environment/team.  High ratio → service identifier.
      3. Cross-reference env values with K8s namespaces.
      4. Determine K8s deployment name format (bare vs full).

    Args:
        apm_service_names: All APM service names in the account.
        k8s_namespaces: All K8s namespace names.
        k8s_deployments: Map of namespace to deployment names.
        extra_entity_names: Additional entity names from other domains
            (OTel, Synthetics, Browser, etc.) to strengthen detection.

    Returns:
        Populated NamingConvention.
    """
    # Combine all names for analysis (APM is primary, extras supplement).
    all_names = list(apm_service_names)
    if extra_entity_names:
        for n in extra_entity_names:
            if n and n not in all_names:
                all_names.append(n)

    if not all_names:
        return NamingConvention()

    convention = NamingConvention()
    convention.all_entity_names = sorted(set(all_names))

    # ── Step 1: Detect primary separator ──
    # Rank separators by structural consistency: what fraction of names,
    # when split, produce the same number of segments.
    CANDIDATE_SEPARATORS = ["/", "-", "_", ".", ":"]

    best_sep: str | None = None
    best_sep_score = 0.0
    best_seg_count = 0

    for sep in CANDIDATE_SEPARATORS:
        names_with_sep = [n for n in all_names if sep in n]
        if not names_with_sep or len(names_with_sep) < len(all_names) * 0.25:
            continue

        # Count segment lengths and find the mode.
        seg_counts: dict[int, int] = {}
        for n in names_with_sep:
            sc = n.count(sep) + 1
            seg_counts[sc] = seg_counts.get(sc, 0) + 1

        mode_count = max(seg_counts, key=seg_counts.get)
        mode_fraction = seg_counts[mode_count] / len(names_with_sep)

        # Score = fraction of names with this sep * consistency of segment count
        coverage = len(names_with_sep) / len(all_names)
        score = coverage * mode_fraction

        # Prefer "/" over "-" at equal score (unambiguous namespace separator).
        if sep == "/" and score > 0:
            score += 0.1

        if score > best_sep_score:
            best_sep_score = score
            best_sep = sep
            best_seg_count = mode_count

    if not best_sep:
        # No clear separator — try to detect env from known K8s namespace overlap.
        convention.bare_service_names = sorted(set(apm_service_names))
        return convention

    convention.separator = best_sep

    # Detect secondary separator within segments (e.g., "-" within "/" names).
    if best_sep == "/":
        for sub_sep in ["-", "_", "."]:
            sub_count = sum(
                1 for n in all_names
                if best_sep in n and sub_sep in n.split(best_sep, 1)[-1]
            )
            if sub_count > len(all_names) * 0.3:
                convention.secondary_separator = sub_sep
                break

    # ── Step 2: Segment role detection ──
    # Strategy differs by separator type:
    # - "/" is unambiguous (namespace/resource convention) → position-based
    # - "-", "_", "." are ambiguous (compound names) → first/last cardinality

    if best_sep == "/":
        # ── Slash separator: full segment analysis ──
        # Convention: first segment = namespace/env, last = service name.
        # Validate with cross-segment pairing analysis.

        split_names = []
        for n in all_names:
            segments = n.split(best_sep)
            if len(segments) == best_seg_count:
                split_names.append(segments)

        if not split_names or best_seg_count < 2:
            convention.bare_service_names = sorted(set(apm_service_names))
            return convention

        # Compute per-segment cardinality and pairing stats.
        segment_analysis: list[dict] = []
        for pos in range(best_seg_count):
            values = [segs[pos] for segs in split_names]
            unique_values = set(values)
            cardinality = len(unique_values)

            # Compute average "partner count": for each value of this segment,
            # how many distinct values appear in the OTHER segments?
            from collections import Counter, defaultdict
            partners: dict[str, set[str]] = defaultdict(set)
            for segs in split_names:
                val = segs[pos]
                # Combine all other segment values as the "partner" key.
                other = best_sep.join(
                    segs[j] for j in range(best_seg_count) if j != pos
                )
                partners[val].add(other)
            avg_partners = sum(len(p) for p in partners.values()) / max(cardinality, 1)

            segment_analysis.append({
                "position": pos,
                "unique_values": sorted(unique_values),
                "cardinality": cardinality,
                "avg_partners": round(avg_partners, 2),
            })

        # Classify: for "/" separator, default first=env, last=service.
        # Override only if pairing analysis strongly disagrees.
        roles = ["unknown"] * best_seg_count

        if best_seg_count == 2:
            # 2-segment case (most common): namespace/service
            roles[0] = "environment"
            roles[-1] = "service"
        else:
            # 3+ segments: first is likely team/org, second is env, last is service.
            # Assign by partner-count descending = more shared = "grouping" segment.
            partner_ranked = sorted(
                range(best_seg_count),
                key=lambda p: segment_analysis[p]["avg_partners"],
                reverse=True,
            )
            # Highest partner count → environment (each env contains many services).
            roles[partner_ranked[0]] = "environment"
            # Lowest partner count → will be assigned below.
            # The position with highest cardinality → service.
            svc_pos = max(
                range(best_seg_count),
                key=lambda p: segment_analysis[p]["cardinality"],
            )
            roles[svc_pos] = "service"
            # Any remaining low-cardinality positions → team.
            for p in range(best_seg_count):
                if roles[p] == "unknown":
                    roles[p] = "team"

        convention.segment_roles = roles

        # Extract env/team/service values.
        env_set: set[str] = set()
        team_set: set[str] = set()
        bare_set: set[str] = set()
        for segs in split_names:
            for pos in range(best_seg_count):
                if roles[pos] == "environment":
                    env_set.add(segs[pos])
                elif roles[pos] == "team":
                    team_set.add(segs[pos])
                elif roles[pos] == "service":
                    bare_set.add(segs[pos])

        convention.env_values = sorted(env_set)
        convention.team_values = sorted(team_set)
        convention.bare_service_names = sorted(bare_set)

        # env_position for backward compatibility.
        env_positions = [i for i, r in enumerate(roles) if r == "environment"]
        if env_positions:
            convention.env_position = "prefix" if env_positions[0] == 0 else "suffix"

        # Names that don't follow the modal pattern get added as bare names.
        for n in apm_service_names:
            if n.count(best_sep) + 1 != best_seg_count and n not in bare_set:
                bare_set.add(n)
        convention.bare_service_names = sorted(bare_set)

    else:
        # ── Ambiguous separator (-, _, .): first/last component analysis ──
        # Compound names like "payment-svc-prod" use internal dashes, so we
        # can't split into all segments. Instead, check if the FIRST or LAST
        # component (split by separator) has low cardinality → environment.

        names_with_sep = [n for n in all_names if best_sep in n]
        if not names_with_sep:
            convention.bare_service_names = sorted(set(apm_service_names))
            return convention

        from collections import Counter

        first_components = []
        last_components = []
        for n in names_with_sep:
            parts = n.split(best_sep)
            if len(parts) >= 2:
                first_components.append(parts[0])
                last_components.append(parts[-1])

        first_counter = Counter(first_components)
        last_counter = Counter(last_components)

        n_names = len(names_with_sep)
        first_unique = len(first_counter)
        last_unique = len(last_counter)
        first_ratio = first_unique / max(n_names, 1)
        last_ratio = last_unique / max(n_names, 1)

        # A segment is "env-like" if it has significantly lower cardinality
        # than the other segment, or overlaps with K8s namespaces.
        # Use both absolute threshold and relative comparison.
        # Also: environments come in sets (prod/dev/staging), so require
        # at least 2 distinct values to be considered an environment.
        first_is_env = False
        last_is_env = False

        # Absolute: low cardinality ratio AND at least 2 distinct values.
        if first_ratio <= 0.5 and first_unique >= 2:
            first_is_env = True
        if last_ratio <= 0.5 and last_unique >= 2:
            last_is_env = True

        # Relative: one side has clearly fewer unique values than the other.
        if last_ratio < first_ratio * 0.75 and last_ratio <= 0.6 and last_unique >= 2:
            last_is_env = True
            first_is_env = False
        elif first_ratio < last_ratio * 0.75 and first_ratio <= 0.6 and first_unique >= 2:
            first_is_env = True
            last_is_env = False

        # Cross-reference with K8s namespaces for disambiguation.
        if k8s_namespaces:
            ns_lower = {ns.lower() for ns in k8s_namespaces}
            first_k8s_overlap = sum(
                1 for v in first_counter if v.lower() in ns_lower
            )
            last_k8s_overlap = sum(
                1 for v in last_counter if v.lower() in ns_lower
            )
            if first_k8s_overlap > last_k8s_overlap and first_k8s_overlap > 0:
                first_is_env = True
                last_is_env = False
            elif last_k8s_overlap > first_k8s_overlap and last_k8s_overlap > 0:
                last_is_env = True
                first_is_env = False

        env_set: set[str] = set()
        bare_set: set[str] = set()

        if last_is_env and (not first_is_env or last_ratio < first_ratio):
            # Suffix environment: "payment-svc-prod"
            convention.env_position = "suffix"
            convention.segment_roles = ["service", "environment"]
            for n in all_names:
                parts = n.rsplit(best_sep, 1)
                if len(parts) == 2 and parts[1] in last_counter:
                    env_set.add(parts[1])
                    bare_set.add(parts[0])
                else:
                    bare_set.add(n)
        elif first_is_env:
            # Prefix environment: "prod-payment-svc"
            convention.env_position = "prefix"
            convention.segment_roles = ["environment", "service"]
            for n in all_names:
                parts = n.split(best_sep, 1)
                if len(parts) == 2 and parts[0] in first_counter:
                    env_set.add(parts[0])
                    bare_set.add(parts[1])
                else:
                    bare_set.add(n)
        else:
            # No clear env pattern — names use separator internally only.
            convention.env_position = None
            bare_set = set(apm_service_names)

        convention.env_values = sorted(env_set)
        convention.bare_service_names = sorted(bare_set)

    # ── Step 4: APM env -> K8s namespace mapping ──
    if convention.env_values and k8s_namespaces:
        for env_val in convention.env_values:
            best_ns: str | None = None
            best_score = 0.0
            env_lower = env_val.lower()

            for ns in k8s_namespaces:
                ns_lower = ns.lower()

                # Exact match.
                if env_lower == ns_lower:
                    best_ns = ns
                    best_score = 1.0
                    break

                # Component match: split env on separators, check parts.
                env_parts = re.split(r"[-_.]", env_lower)
                for part in env_parts:
                    if part and part == ns_lower and len(part) > 1:
                        score = 0.9
                        if score > best_score:
                            best_ns = ns
                            best_score = score

                # Substring match.
                if best_score < 0.9:
                    if ns_lower in env_lower and len(ns_lower) > 2:
                        score = len(ns_lower) / max(len(env_lower), 1)
                        if score > best_score:
                            best_ns = ns
                            best_score = score
                    elif env_lower in ns_lower and len(env_lower) > 2:
                        score = len(env_lower) / max(len(ns_lower), 1)
                        if score > best_score:
                            best_ns = ns
                            best_score = score

            if best_ns and best_score > 0.3:
                convention.apm_to_k8s_namespace_map[env_val] = best_ns

    # ── Step 5: K8s deployment name format ──
    if k8s_deployments and convention.bare_service_names:
        all_deps: list[str] = []
        for dep_list in k8s_deployments.values():
            all_deps.extend(dep_list)

        if all_deps:
            bare_set_lower = {b.lower() for b in convention.bare_service_names}
            apm_set_lower = {n.lower() for n in apm_service_names}

            full_match = sum(1 for d in all_deps if d.lower() in apm_set_lower)
            bare_match = sum(1 for d in all_deps if d.lower() in bare_set_lower)

            convention.k8s_deployment_name_format = (
                "full" if full_match > bare_match else "bare"
            )

    logger.info(
        "Naming convention learned: separator=%s, roles=%s, "
        "envs=%s, teams=%s, bare_names=%d, k8s_map=%s, k8s_format=%s",
        convention.separator,
        convention.segment_roles,
        convention.env_values,
        convention.team_values,
        len(convention.bare_service_names),
        convention.apm_to_k8s_namespace_map,
        convention.k8s_deployment_name_format,
    )

    return convention


def _infer_associated_service(
    monitor_name: str, service_names: list[str], threshold: float = 0.5
) -> str | None:
    """Try to match a synthetic monitor name to an APM service name.

    Uses fuzzy matching because monitor names are often descriptive
    (e.g. 'Login Flow - Production') while service names are technical
    (e.g. 'auth-service-prod').

    Args:
        monitor_name: The synthetic monitor name.
        service_names: Known APM service names.
        threshold: Minimum similarity ratio.

    Returns:
        The best matching service name or None.
    """
    if not service_names:
        return None

    monitor_lower = monitor_name.lower()
    monitor_tokens = set(re.split(r"[\s\-_]+", monitor_lower))

    best_match: str | None = None
    best_score: float = 0.0

    for svc in service_names:
        svc_lower = svc.lower()
        svc_tokens = set(re.split(r"[\s\-_]+", svc_lower))

        # Check direct substring.
        if svc_lower in monitor_lower or monitor_lower in svc_lower:
            return svc

        # Token overlap.
        overlap = len(monitor_tokens & svc_tokens)
        total = max(len(monitor_tokens), len(svc_tokens))
        token_score = overlap / total if total > 0 else 0.0

        # Sequence similarity.
        seq_score = SequenceMatcher(None, monitor_lower, svc_lower).ratio()

        score = max(token_score, seq_score)
        if score > best_score:
            best_score = score
            best_match = svc

    return best_match if best_score >= threshold else None


# ── Main learn function ──────────────────────────────────────────────────

async def learn_account(credentials: Credentials) -> AccountIntelligence:
    """Learn everything about a New Relic account in parallel.

    Discovers APM services, OpenTelemetry services, K8s namespaces,
    alert policies, log attributes, synthetic monitors, infrastructure,
    browser apps, mobile apps, containers, workloads, and full entity
    type counts — all concurrently.

    Args:
        credentials: Validated credentials for the target account.

    Returns:
        Complete AccountIntelligence for the account.

    Raises:
        IntelligenceError: If learning fails critically.
    """
    import httpx

    account_id = credentials.account_id
    endpoint = credentials.endpoint
    headers = {"API-Key": credentials.api_key, "Content-Type": "application/json"}

    async def _gql(query: str) -> dict:
        """Execute a NerdGraph query."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                endpoint,
                json={"query": query},
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def _nrql(nrql: str) -> list:
        """Execute an NRQL query via NerdGraph."""
        escaped = nrql.replace('"', '\\"')
        query = NRQL_TEMPLATE % (account_id, escaped)
        result = await _gql(query)
        return (
            result.get("data", {})
            .get("actor", {})
            .get("account", {})
            .get("nrql", {})
            .get("results", [])
        )

    def _extract_entity_search(result: dict) -> tuple[list, int]:
        """Extract entities list and count from entitySearch result."""
        search = (
            result.get("data", {})
            .get("actor", {})
            .get("entitySearch", {})
        )
        count = search.get("count", 0)
        entities = search.get("results", {}).get("entities", [])
        return entities, count

    def _extract_cursor(result: dict) -> str | None:
        """Extract nextCursor from an entitySearch result, if present."""
        try:
            return (
                result.get("data", {})
                .get("actor", {})
                .get("entitySearch", {})
                .get("results", {})
                .get("nextCursor")
            )
        except Exception:
            return None

    async def _paginate_apm_entities(first_page_result: dict) -> list[dict]:
        """Fetch ALL APM entities across pages (first page already fetched).

        New Relic returns ~200 entities per page.  For accounts with >200 APM
        services this ensures we don't silently miss services.
        """
        entities, _ = _extract_entity_search(first_page_result)
        all_entities = list(entities)
        cursor = _extract_cursor(first_page_result)
        max_pages = 20  # safety limit: 20 * 200 = 4000 services

        page = 1
        while cursor and page < max_pages:
            cursor_clause = '(cursor: "%s")' % cursor
            query = GQL_ENTITY_SEARCH_PAGINATED % (
                account_id,
                "domain = 'APM' AND type = 'APPLICATION'",
                cursor_clause,
            )
            try:
                result = await _gql(query)
                page_entities, _ = _extract_entity_search(result)
                if not page_entities:
                    break
                all_entities.extend(page_entities)
                cursor = _extract_cursor(result)
                page += 1
            except Exception:
                logger.debug("APM pagination stopped at page %d", page)
                break

        logger.info(
            "APM entity pagination: loaded %d entities across %d page(s)",
            len(all_entities), page,
        )
        return all_entities

    # ── Launch all discovery queries in parallel ──
    try:
        results = await asyncio.gather(
            _gql(GQL_ACCOUNT_META % account_id),                # 0: account meta
            _gql(GQL_APM_ENTITIES % account_id),                # 1: APM entities
            _nrql(NRQL_K8S_NAMESPACES),                         # 2: K8s namespaces
            _nrql(NRQL_K8S_DEPLOYMENTS),                        # 3: K8s deployments
            _nrql(NRQL_K8S_CLUSTERS),                           # 4: K8s clusters
            _gql(GQL_ALERT_POLICIES % account_id),              # 5: alert policies
            _nrql(NRQL_LOG_KEYSET),                             # 6: log keyset
            _nrql(NRQL_TOP_ERRORS),                             # 7: top errors
            _gql(GQL_INFRA_ENTITIES % account_id),              # 8: infra hosts
            _gql(GQL_BROWSER_ENTITIES % account_id),            # 9: browser apps
            _gql(GQL_SYNTHETIC_MONITORS % account_id),          # 10: synth monitors
            _gql(GQL_ENTITY_TYPE_COUNTS % account_id),          # 11: entity type counts
            _gql(GQL_OTEL_ENTITIES % account_id),               # 12: OTel services
            _gql(GQL_CONTAINER_ENTITIES % account_id),          # 13: containers
            _gql(GQL_MOBILE_ENTITIES % account_id),             # 14: mobile apps
            _gql(GQL_WORKLOAD_ENTITIES % account_id),           # 15: workloads
            _gql(GQL_KEY_TRANSACTION_ENTITIES % account_id),    # 16: key transactions
            _gql(GQL_SYNTHETIC_PRIVATE_LOCATIONS % account_id), # 17: private locations
            _gql(GQL_SYNTHETIC_SECURE_CREDS % account_id),      # 18: secure credentials
            _nrql(NRQL_K8S_POD_COUNT),                          # 19: K8s pod count
            _nrql(NRQL_K8S_DAEMONSET_COUNT),                    # 20: K8s daemonset count
            _nrql(NRQL_K8S_STATEFULSET_COUNT),                  # 21: K8s statefulset count
            _nrql(NRQL_K8S_JOB_COUNT),                          # 22: K8s job count
            _nrql(NRQL_K8S_CRONJOB_COUNT),                      # 23: K8s cronjob count
            _nrql(NRQL_K8S_PV_COUNT),                           # 24: K8s PV count
            _nrql(NRQL_K8S_PVC_COUNT),                          # 25: K8s PVC count
            _nrql(NRQL_LOG_COUNT),                              # 26: log count fallback
            _nrql(NRQL_LOG_ATTR_PROBE),                          # 27: log attribute probe
            return_exceptions=True,
        )
    except Exception as exc:
        raise IntelligenceError(
            f"Failed to learn account {account_id}: {exc}",
            account_id=account_id,
            partial_result=None,
        )

    intel = AccountIntelligence(account_id=account_id)

    # ── 0: Account Meta ──
    try:
        if not isinstance(results[0], BaseException):
            account_data = (
                results[0].get("data", {}).get("actor", {}).get("account", {})
            )
            intel.account_meta.name = account_data.get("name", "")
    except Exception:
        pass

    # ── 11: Entity Type Counts (process early for aggregate view) ──
    try:
        if not isinstance(results[11], BaseException):
            search = (
                results[11].get("data", {}).get("actor", {}).get("entitySearch", {})
            )
            intel.entity_counts.total_entities = search.get("count", 0)
            intel.account_meta.total_entities = intel.entity_counts.total_entities

            azure_count = 0
            azure_types: list[str] = []
            for t in search.get("types", []):
                domain = t.get("domain", "")
                etype = t.get("type", "")
                ecount = t.get("count", 0)
                intel.entity_counts.type_breakdown.append(
                    EntityTypeSummary(domain=domain, type=etype, count=ecount)
                )
                # Identify Azure resources
                if domain == "INFRA" and etype.startswith("AZURE"):
                    azure_count += ecount
                    azure_types.append(etype)
                # Service levels
                if etype == "SERVICE_LEVEL":
                    intel.entity_counts.service_level_count = ecount
                # Issues
                if domain == "AIOPS" or etype == "ISSUE":
                    intel.entity_counts.issue_count += ecount

            intel.entity_counts.azure_resource_count = azure_count
            intel.entity_counts.azure_resource_types = sorted(azure_types)
            intel.account_meta.azure_resource_count = azure_count
    except Exception:
        pass

    # ── 1: APM Services (with pagination) ──
    try:
        if not isinstance(results[1], BaseException):
            _, api_count = _extract_entity_search(results[1])
            # Paginate to fetch ALL APM entities.
            all_apm_entities = await _paginate_apm_entities(results[1])
            intel.account_meta.total_apm_services = (
                api_count if api_count else len(all_apm_entities)
            )
            for ent in all_apm_entities:
                name = ent.get("name", "")
                if name:
                    intel.apm.service_names.append(name)
                    guid = ent.get("guid", "")
                    if guid:
                        intel.apm.service_guids[name] = guid
                tags = {t["key"]: t.get("values", []) for t in ent.get("tags", [])}
                lang = tags.get("language", [""])[0] if tags.get("language") else ""
                if lang:
                    intel.apm.service_languages[name] = lang
                envs = tags.get("environment", [])
                for e in envs:
                    if e and e not in intel.apm.environments:
                        intel.apm.environments.append(e)
            intel.apm.naming_pattern = _infer_naming_pattern(intel.apm.service_names)
    except Exception:
        pass

    # ── 12: OpenTelemetry Services ──
    try:
        if not isinstance(results[12], BaseException):
            entities, count = _extract_entity_search(results[12])
            intel.otel.service_count = count if count else len(entities)
            intel.otel.service_names = [e["name"] for e in entities if e.get("name")]
            intel.otel.enabled = intel.otel.service_count > 0
            intel.account_meta.otel_services = intel.otel.service_count
    except Exception:
        pass

    # ── 2: K8s Namespaces ──
    try:
        if not isinstance(results[2], BaseException) and results[2]:
            ns_list = results[2][0].get("uniques.namespaceName", []) if results[2] else []
            intel.k8s.namespaces = [n for n in ns_list if n]
            intel.k8s.namespace_count = len(intel.k8s.namespaces)
            intel.k8s.integrated = len(intel.k8s.namespaces) > 0
            intel.k8s.naming_pattern = _infer_naming_pattern(intel.k8s.namespaces)
            intel.account_meta.k8s_integrated = intel.k8s.integrated
    except Exception:
        pass

    # ── 3: K8s Deployments ──
    try:
        if not isinstance(results[3], BaseException) and results[3]:
            total_deps = 0
            for row in results[3]:
                ns = row.get("namespaceName", "")
                deps = row.get("uniques.deploymentName", [])
                if ns and deps:
                    dep_list = [d for d in deps if d]
                    intel.k8s.deployments[ns] = dep_list
                    total_deps += len(dep_list)
            intel.k8s.deployment_count = total_deps
    except Exception:
        pass

    # ── 4: K8s Clusters ──
    try:
        if not isinstance(results[4], BaseException) and results[4]:
            clusters = results[4][0].get("uniques.clusterName", []) if results[4] else []
            intel.k8s.cluster_names = [c for c in clusters if c]
            intel.k8s.cluster_count = len(intel.k8s.cluster_names)
    except Exception:
        pass

    # ── 19-25: K8s Sub-type Counts ──
    k8s_count_map = {
        19: "pod_count",
        20: "daemonset_count",
        21: "statefulset_count",
        22: "job_count",
        23: "cronjob_count",
        24: "pv_count",
        25: "pvc_count",
    }
    for idx, attr in k8s_count_map.items():
        try:
            if not isinstance(results[idx], BaseException) and results[idx]:
                # NRQL uniqueCount returns a single result with the count
                row = results[idx][0] if results[idx] else {}
                count_val = 0
                for k, v in row.items():
                    if k.startswith("uniqueCount"):
                        count_val = int(v) if v else 0
                        break
                setattr(intel.k8s, attr, count_val)
        except Exception:
            pass

    # ── 5: Alert Policies ──
    try:
        if not isinstance(results[5], BaseException):
            policy_search = (
                results[5]
                .get("data", {})
                .get("actor", {})
                .get("account", {})
                .get("alerts", {})
                .get("policiesSearch", {})
            )
            policies = policy_search.get("policies", [])
            intel.alerts.policy_names = [p["name"] for p in policies if p.get("name")]
            intel.alerts.naming_pattern = _infer_naming_pattern(intel.alerts.policy_names)
    except Exception:
        pass

    # ── 6: Log Attribute Discovery ──
    try:
        if not isinstance(results[6], BaseException) and results[6]:
            keyset = results[6][0].get("allKeys", []) if results[6] else []
            if not keyset and results[6]:
                keyset = results[6][0].get("uniques.key", []) if results[6] else []
            # Handle key-per-row format: [{"key": "attr1"}, {"key": "attr2"}, ...]
            if not keyset and results[6] and isinstance(results[6], list):
                row_keys = [r.get("key") for r in results[6] if isinstance(r, dict) and "key" in r]
                if row_keys:
                    keyset = row_keys
            if keyset:
                intel.logs.enabled = True
                intel.account_meta.logs_enabled = True
                for candidate in SERVICE_ATTR_CANDIDATES:
                    if candidate in keyset:
                        intel.logs.service_attribute = candidate
                        break
                for candidate in SEVERITY_ATTR_CANDIDATES:
                    if candidate in keyset:
                        intel.logs.severity_attribute = candidate
                        break
    except Exception:
        pass

    # ── 6b: Log Count Fallback ──
    # If keyset() failed to detect logs (e.g. partitioned logs), use count(*)
    # and probe for actual attribute names.
    if not intel.logs.enabled:
        try:
            if not isinstance(results[26], BaseException) and results[26]:
                log_count = results[26][0].get("count", 0) if results[26] else 0
                if log_count > 0:
                    intel.logs.enabled = True
                    intel.account_meta.logs_enabled = True

                    # Try to determine actual attributes from probe query (index 27).
                    probe = {}
                    try:
                        if not isinstance(results[27], BaseException) and results[27]:
                            probe = results[27][0] if results[27] else {}
                    except Exception:
                        pass

                    # Pick the first service attribute with data.
                    for candidate in SERVICE_ATTR_CANDIDATES:
                        key = f"has_{candidate.replace('.', '_')}"
                        if probe.get(key, 0) > 0:
                            intel.logs.service_attribute = candidate
                            break
                    if not intel.logs.service_attribute:
                        intel.logs.service_attribute = "service.name"

                    # Pick the first severity attribute with data.
                    for candidate in SEVERITY_ATTR_CANDIDATES:
                        key = f"has_{candidate.replace('.', '_')}"
                        if probe.get(key, 0) > 0:
                            intel.logs.severity_attribute = candidate
                            break
                    if not intel.logs.severity_attribute:
                        intel.logs.severity_attribute = "level"

                    logger.info(
                        "Log keyset() returned empty but %d logs found via count; "
                        "enabled with service_attr=%s, severity_attr=%s",
                        log_count,
                        intel.logs.service_attribute,
                        intel.logs.severity_attribute,
                    )
        except Exception:
            pass

    # ── 7: Top Error Classes ──
    try:
        if not isinstance(results[7], BaseException) and results[7]:
            intel.apm.top_error_classes = [
                r.get("error.class", r.get("facet", ""))
                for r in results[7]
                if r.get("error.class") or r.get("facet")
            ][:20]
    except Exception:
        pass

    # ── 8: Infrastructure ──
    try:
        if not isinstance(results[8], BaseException):
            entities, count = _extract_entity_search(results[8])
            intel.infra.host_count = count if count else len(entities)
            providers = set()
            regions = set()
            for host in entities:
                tags = {t["key"]: t.get("values", []) for t in host.get("tags", [])}
                if "aws" in str(tags).lower():
                    providers.add("AWS")
                elif "azure" in str(tags).lower():
                    providers.add("Azure")
                elif "gcp" in str(tags).lower() or "google" in str(tags).lower():
                    providers.add("GCP")
                for r in tags.get("region", []):
                    if r:
                        regions.add(r)

            intel.infra.cloud_provider = ", ".join(sorted(providers)) if providers else None
            intel.infra.regions = sorted(regions)
    except Exception:
        pass

    # ── 13: Containers ──
    try:
        if not isinstance(results[13], BaseException):
            _, count = _extract_entity_search(results[13])
            intel.infra.container_count = count
            intel.account_meta.container_count = count
    except Exception:
        pass

    # ── 9: Browser Apps ──
    try:
        if not isinstance(results[9], BaseException):
            entities, count = _extract_entity_search(results[9])
            intel.browser.app_names = [e["name"] for e in entities if e.get("name")]
            intel.browser.enabled = (count or len(intel.browser.app_names)) > 0
    except Exception:
        pass

    # ── 14: Mobile Apps ──
    try:
        if not isinstance(results[14], BaseException):
            entities, count = _extract_entity_search(results[14])
            intel.mobile.app_names = [e["name"] for e in entities if e.get("name")]
            intel.mobile.app_count = count if count else len(intel.mobile.app_names)
            intel.mobile.enabled = intel.mobile.app_count > 0
            intel.account_meta.mobile_apps = intel.mobile.app_count
    except Exception:
        pass

    # ── 15: Workloads ──
    try:
        if not isinstance(results[15], BaseException):
            entities, count = _extract_entity_search(results[15])
            intel.workloads.workload_names = [e["name"] for e in entities if e.get("name")]
            intel.workloads.workload_count = count if count else len(intel.workloads.workload_names)
            intel.workloads.enabled = intel.workloads.workload_count > 0
            intel.account_meta.workload_count = intel.workloads.workload_count
    except Exception:
        pass

    # ── 16: Key Transactions ──
    try:
        if not isinstance(results[16], BaseException):
            _, count = _extract_entity_search(results[16])
            intel.entity_counts.key_transaction_count = count
            intel.account_meta.key_transaction_count = count
    except Exception:
        pass

    # ── 17: Synthetic Private Locations ──
    synth_private_location_count = 0
    try:
        if not isinstance(results[17], BaseException):
            _, count = _extract_entity_search(results[17])
            synth_private_location_count = count
    except Exception:
        pass

    # ── 18: Synthetic Secure Credentials ──
    synth_secure_cred_count = 0
    try:
        if not isinstance(results[18], BaseException):
            _, count = _extract_entity_search(results[18])
            synth_secure_cred_count = count
    except Exception:
        pass

    # ── 10: Synthetic Monitors ──
    try:
        if not isinstance(results[10], BaseException):
            search = (
                results[10].get("data", {}).get("actor", {}).get("entitySearch", {})
            )
            synth_count = search.get("count", 0)
            synth_entities = search.get("results", {}).get("entities", [])

            monitor_types_seen: set[str] = set()
            for ent in synth_entities:
                name = ent.get("name", "")
                if not name:
                    continue
                mon_type = ent.get("monitorType", "UNKNOWN")
                monitor_types_seen.add(mon_type)

                meta = SyntheticMonitorMeta(
                    guid=ent.get("guid", ""),
                    name=name,
                    type=mon_type,
                    status=ent.get("monitoredUrl") or "ENABLED",
                    period=ent.get("period") or "",
                    locations=[],
                    associated_service=_infer_associated_service(
                        name, intel.apm.service_names
                    ),
                )
                intel.synthetics.monitor_names.append(name)
                intel.synthetics.monitor_map[name] = meta

            intel.synthetics.monitor_types = sorted(monitor_types_seen)
            intel.synthetics.total_count = synth_count if synth_count else len(intel.synthetics.monitor_names)
            intel.synthetics.enabled = intel.synthetics.total_count > 0
            intel.synthetics.naming_pattern = _infer_naming_pattern(
                intel.synthetics.monitor_names
            )
            intel.account_meta.synthetics_enabled = intel.synthetics.enabled
            intel.account_meta.synthetics_count = intel.synthetics.total_count

            # Fetch location data for each monitor (parallel, up to 50).
            if intel.synthetics.monitor_names:
                location_tasks = []
                for mon_name in intel.synthetics.monitor_names[:50]:
                    location_tasks.append(_nrql(NRQL_SYNTHETIC_LOCATIONS % mon_name))

                location_results = await asyncio.gather(
                    *location_tasks, return_exceptions=True
                )
                for i, mon_name in enumerate(intel.synthetics.monitor_names[:50]):
                    try:
                        if not isinstance(location_results[i], BaseException):
                            locations = []
                            for row in location_results[i]:
                                loc = row.get("locationLabel", row.get("facet", ""))
                                if loc:
                                    locations.append(loc)
                            if mon_name in intel.synthetics.monitor_map:
                                intel.synthetics.monitor_map[mon_name].locations = locations
                    except Exception:
                        pass
    except Exception as synth_exc:
        logger.warning("Synthetics processing failed: %s", synth_exc)

    # ── Learn Naming Convention (feed ALL entity names) ──
    try:
        # Collect entity names from every domain for stronger signal.
        extra_names: list[str] = []
        extra_names.extend(intel.otel.service_names)
        extra_names.extend(intel.synthetics.monitor_names)
        extra_names.extend(intel.browser.app_names)
        extra_names.extend(intel.mobile.app_names)
        extra_names.extend(intel.workloads.workload_names)

        intel.naming_convention = _learn_naming_convention(
            apm_service_names=intel.apm.service_names,
            k8s_namespaces=intel.k8s.namespaces,
            k8s_deployments=intel.k8s.deployments,
            extra_entity_names=extra_names,
        )
    except Exception as nc_exc:
        logger.warning("Naming convention learning failed: %s", nc_exc)

    logger.info(
        "Account %s learned: %d total entities, %d APM services, %d OTel services, "
        "%d K8s namespaces, %d alert policies, %d synthetic monitors, "
        "%d hosts, %d containers, %d browser apps, %d mobile apps, "
        "%d workloads, logs=%s",
        account_id,
        intel.entity_counts.total_entities,
        intel.account_meta.total_apm_services,
        intel.otel.service_count,
        len(intel.k8s.namespaces),
        len(intel.alerts.policy_names),
        intel.synthetics.total_count,
        intel.infra.host_count,
        intel.infra.container_count,
        len(intel.browser.app_names),
        intel.mobile.app_count,
        intel.workloads.workload_count,
        intel.logs.enabled,
    )

    # ── Cross-Account Entity Detection ──
    try:
        intel.cross_account_entities = detect_cross_account_entities(intel)
        if intel.cross_account_entities:
            logger.warning(
                "Cross-account entities detected in account %s: %s",
                account_id,
                [
                    f"{e.name} (home={e.home_account_id})"
                    for e in intel.cross_account_entities
                ],
            )
    except Exception as xacc_exc:
        logger.warning("Cross-account detection failed: %s", xacc_exc)

    return intel
