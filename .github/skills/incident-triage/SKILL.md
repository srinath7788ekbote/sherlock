---
name: incident-triage
description: >
  Full-cycle incident investigation and triage for SRE operations. Combines
  Sherlock live observability (New Relic APM, alerts, logs, K8s health,
  golden signals) with optional HiveMind KB (Helm, Terraform, pipelines,
  secrets) for root-cause analysis and remediation plans.
---

# Incident Triage — Sherlock SRE Playbook

> This skill governs how Sherlock investigates production incidents.
> Activated automatically when incident-related keywords appear.
> Follow every phase in order. Skip nothing.

---

## ⛔ PRIME CONSTRAINTS

| # | Rule |
|---|------|
| T-1 | **ALWAYS connect first** — `mcp_sherlock_connect_account` before anything |
| T-2 | **ALWAYS learn the account** — `mcp_sherlock_learn_account` to discover real entity names |
| T-3 | **ALL six domains MUST be checked** — APM, K8s, Logs, Alerts, Synthetics, Infra |
| T-4 | **NEVER skip a domain** — NO_DATA is valuable signal, not a reason to skip |
| T-5 | **ALWAYS correlate timing** across domains (deploy → error spike → K8s restarts) |
| T-6 | **ALWAYS check dependencies** — is this service the origin or a downstream victim? |
| T-7 | **ALWAYS provide remediation** — prioritized fix recommendations for every finding |
| T-8 | **NEVER answer from training data** when Sherlock tools have results |
| T-9 | **NEVER use `investigate_service` for full triage** — spawn ALL 6 agents with domain-specific tools |
| T-10 | **ALWAYS parse K8s names** — bare name (after `/`) for deploymentName, prefix (before `/`) for namespace |

---

## Phase 0 — MANDATORY PRE-FLIGHT

Before any domain investigation, complete these steps in order:

```
STEP 0.1: mcp_sherlock_connect_account()
          → Connect to the New Relic account. REQUIRED.

STEP 0.2: mcp_sherlock_learn_account()
          → Discovers ALL entity names, types, and relationships.
          → This tells you the REAL service names, K8s deployment names, etc.
          → Use these real names in all subsequent queries.

STEP 0.3: Parse the service name:
          → "eswd-prod/sifi-adapter" → full="eswd-prod/sifi-adapter",
            bare="sifi-adapter", namespace="eswd-prod"
          → "my-service" → full="my-service", bare="my-service", namespace=None

STEP 0.4: mcp_sherlock_get_nrql_context(domain="all")
          → Get real attribute names for NRQL queries.
          → Pass to agents so they use correct WHERE clauses.
```

**NEVER skip Steps 0.1–0.3.**

---

## Phase 1 — SIGNAL EXTRACTION

When investigating any service, extract these signals immediately:

| Signal | How |
|--------|-----|
| Service name | From user input, parse alert target variants |
| Time window | From user or incident start time |
| Namespace | From K8s context or intelligence |
| Error type | From alert/log content |

---

## Phase 2 — PARALLEL DOMAIN INVESTIGATION

Dispatch ALL domain specialist agents simultaneously:

### 2.1 APM Domain (sherlock-apm)

```
STEP 1: mcp_sherlock_get_service_golden_signals(service_name, since_minutes)
STEP 2: mcp_sherlock_get_app_metrics(app_name, since_minutes)
STEP 3: mcp_sherlock_get_deployments(app_name)
STEP 4: NRQL for error breakdown: SELECT count(*) FROM TransactionError FACET error.class
```

### 2.2 K8s Domain (sherlock-k8s)

**⛔ CRITICAL: Parse the APM service name first.**
Given `eswd-prod/sifi-adapter` → bare_name=`sifi-adapter`, namespace=`eswd-prod`.
Pass the BARE name to K8s queries, NOT the full APM name.

```
STEP 1: mcp_sherlock_get_k8s_health(service_name="{bare_name}", namespace="{namespace}", since_minutes=60)
STEP 2: If no data → NRQL fallback:
        SELECT latest(status), sum(restartCount) FROM K8sPodSample
        WHERE deploymentName LIKE '%{bare_name}%' FACET podName SINCE {window} minutes ago
STEP 3: If no data → Try podName:
        SELECT latest(status) FROM K8sPodSample
        WHERE podName LIKE '%{bare_name}%' FACET podName SINCE {window} minutes ago
STEP 4: If no data → Try label.app:
        SELECT latest(status) FROM K8sPodSample
        WHERE `label.app` LIKE '%{bare_name}%' FACET podName SINCE {window} minutes ago
STEP 5: NRQL K8s events:
        SELECT * FROM InfrastructureEvent WHERE category = 'kubernetes'
        AND (involvedObjectName LIKE '%{bare_name}%' OR involvedObjectNamespace = '{namespace}')
        SINCE {window} minutes ago LIMIT 50
```

**NEVER report K8s NO_DATA until Steps 1-4 are all exhausted.**

### 2.3 Logs Domain (sherlock-logs)

```
STEP 1: mcp_sherlock_search_logs(service_name, severity="ERROR", since_minutes=60)
STEP 2: NRQL: SELECT count(*) FROM Log WHERE service_name LIKE '%service%' FACET level
STEP 3: NRQL: SELECT count(*) FROM Log WHERE service_name LIKE '%service%' AND level = 'ERROR' FACET message LIMIT 10
```

### 2.4 Alerts Domain (sherlock-alerts)

```
STEP 1: mcp_sherlock_get_service_incidents(service_name)
STEP 2: mcp_sherlock_get_incidents(state="open")
STEP 3: NRQL: SELECT count(*) FROM NrAiIncident WHERE title LIKE '%service%' SINCE 7 days ago
```

### 2.5 Synthetics Domain (sherlock-synthetics)

```
STEP 1: mcp_sherlock_get_synthetic_monitors()
STEP 2: mcp_sherlock_get_monitor_status(monitor_name, since_minutes) — for each related monitor
STEP 3: mcp_sherlock_get_monitor_results(monitor_name, result_filter="FAILED") — if failing
```

### 2.6 Infra Domain (sherlock-infra)

```
STEP 1: mcp_sherlock_get_service_dependencies(service_name, direction="both")
STEP 2: NRQL: SELECT average(cpuPercent), average(memoryUsedPercent) FROM SystemSample WHERE hostname LIKE '%service%'
STEP 3: NRQL: SELECT count(*) FROM PageView WHERE appName LIKE '%service%' — if browser app
```

---

## Phase 3 — CROSS-DOMAIN CORRELATION

After all 6 domain agents report back, the Team Lead MUST:

### 3.1 Timeline Alignment

Order ALL findings by timestamp to build a causal chain:

```
14:18 — Deployment detected (APM agent: get_deployments)
14:20 — Error rate spike to 5.2% (APM agent: get_service_golden_signals)
14:22 — K8s pod restart count +3 (K8s agent: get_k8s_health)
14:25 — OOMKill event (K8s agent: K8sContainerSample NRQL)
14:28 — Downstream service latency increase (Infra agent: get_service_dependencies)
```

### 3.2 Origin vs Victim Analysis (MANDATORY)

For every investigation, explicitly determine:

| Question | How to Answer |
|----------|---------------|
| Is this service the **origin** of the problem? | Check dependencies: are downstream services healthy? If YES → this service is the origin. |
| Is this service a **victim** of an upstream failure? | Check dependencies: are upstream services unhealthy? If YES → trace further upstream. |
| Is this a **cascading failure**? | Check if multiple services have correlated timing. |

Use the dependency map from `sherlock-infra` to trace the causal chain.
**NEVER conclude "origin" without checking downstream health.**
**NEVER conclude "victim" without checking upstream health.**

### 3.3 Root Cause Classification

Based on cross-domain evidence, classify the root cause:

| Pattern | Evidence | Root Cause |
|---------|----------|------------|
| Deploy → error spike → restarts | APM deploy + APM errors + K8s restarts | **Bad deployment** |
| Memory climbing → OOMKill → restart → 502s | K8s mem trend + K8s OOMKill + APM errors | **Memory leak / underspecified limits** |
| Upstream timeout → this service errors | Infra deps unhealthy + APM errors | **Upstream dependency failure** |
| No deploy, no infra change, errors start | APM errors + Logs show exception | **Application bug / data issue** |
| Secret expired → connection refused | Logs: auth error + Infra: dep unhealthy | **Secret/credential rotation failure** |

### 3.4 Conflict Resolution

If two domains give conflicting signals, flag it explicitly:

```
⚠️ CONFLICT: APM reports 0.2% error rate (HEALTHY) but K8s shows 5 restarts (WARNING).
Possible explanation: restarts are recovering fast enough that APM sees low error rate.
Recommendation: investigate WHY pods are restarting despite low error rate.
```

---

## Phase 4 — SYNTHESIS

Produce the final report using the **concise format** below. Only domains with findings get detail sections. NO_DATA/HEALTHY domains get one line in the status table only.

**Deep Link Rule**: Every finding MUST include a clickable `[View in New Relic](url)` link extracted from tool response `links` or `deep_link` fields.

```markdown
# 🔍 {service_name} — {CRITICAL|WARNING|HEALTHY}

**Window:** {N} min | **Account:** {account} | **Confidence:** {HIGH|MEDIUM|LOW}

> {Root cause in 1-2 sentences. Cite the causal chain.}

## Domain Status
| Domain | Status | Finding |
|--------|--------|---------|
| APM | 🟡 | 391 errors, spike at 09:00 UTC — [View](url) |
| K8s | 🟢 | 2/2 pods, 0 restarts — [View](url) |
| Logs | ⚪ | No log forwarding configured |
| Alerts | ⚪ | No policies configured |
| Synthetics | ⚪ | No monitors configured |
| Infra | 🟢 | All deps healthy — [View](url) |

## Findings
{ONLY domains with WARNING or CRITICAL get detail sections.}

## Recommendations
| # | Action | Why | Link |
|---|--------|-----|------|
| 1 | ... | ... | [View](url) |
```

**Report Size Rules**: HEALTHY ≈15 lines, WARNING ≈30-40 lines, CRITICAL ≈50-60 lines. NEVER exceed 200 lines.

---

## Phase 5 — HiveMind ENRICHMENT (when available)

If the HiveMind MCP server is connected:

1. `hivemind_get_active_client` — establish client context
2. `hivemind_query_memory` — search for Helm values, Terraform, pipeline configs
3. `hivemind_impact_analysis` — blast radius from infrastructure perspective  
4. `hivemind_get_secret_flow` — if secret/credential issues are suspected
5. `hivemind_get_pipeline` — if deployment correlation found

Merge HiveMind infrastructure context with Sherlock live telemetry for the complete picture.
