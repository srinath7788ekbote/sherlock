---
name: zero-result-fallback
description: >
  Universal zero-result fallback protocol for all Sherlock agents. Defines the
  exact fallback ladder every agent must follow when any query returns NO_DATA,
  zero results, or null. Prevents false NO_DATA reports caused by wrong event
  types, wrong accounts, wrong time windows, or wrong name formats.
---

# Zero-Result Fallback Protocol

When any query returns NO_DATA, zero results, or null — do NOT report NO_DATA
immediately. Follow this fallback ladder in order. Only declare NO_DATA after
ALL applicable fallbacks are exhausted.

## Fallback Ladder

### Level 1 — Wrong Event Type

**APM services:**
| If this returns zero | Try this instead |
|---------------------|-----------------|
| `FROM Transaction WHERE appName = '{service}'` | `FROM Span WHERE entity.name = '{service}'` (OTel) |
| `FROM TransactionError WHERE appName = '{service}'` | `FROM Span WHERE entity.name = '{service}' AND otel.status_code = 'ERROR'` |
| `FROM Metric WHERE metricName = 'istio_requests_total'` | `FROM Log WHERE container_name = 'istio-proxy' AND status > 499` |
| `FROM Metric WHERE metricName LIKE '%istio%'` | `FROM Log WHERE container_name = 'istio-proxy'` |

**Log queries:**
| If this returns zero | Try this instead |
|---------------------|-----------------|
| `FROM Log WHERE entity.name = '{service}'` | `FROM Log WHERE service.name = '{service}'` |
| `FROM Log WHERE service.name = '{service}'` | `FROM Log WHERE message LIKE '%{bare_name}%'` |
| `FROM Log WHERE appName = '{service}'` | `FROM Log WHERE entity.name = '{service}'` |

**Infrastructure:**
| If this returns zero | Try this instead |
|---------------------|-----------------|
| `FROM AzurePostgreSqlFlexibleServerSample` (zero rows) | `FROM Log WHERE message LIKE '%FATAL%' OR '%postgresql%'` |
| `FROM K8sPodSample WHERE deploymentName = '{service}'` | `FROM K8sPodSample WHERE podName LIKE '%{bare_name}%'` |
| `FROM K8sContainerSample WHERE containerName = '{service}'` | `FROM K8sPodSample WHERE namespaceName = '{namespace}'` |
| `FROM AzureServiceBusSample` returns zero | Try `FROM AzureServiceBusQueueSample` with `FACET entityName, namespace` |
| `FROM AzureServiceBusQueueSample` with `provider.` prefix attributes returns zero | Remove `provider.` prefix — use `activeMessages.Average` not `provider.activeMessages.Average` |
| `WHERE displayName LIKE '%service%'` on ASB returns zero | Try `WHERE entityName LIKE '%service%'` — ASB uses `entityName` not `displayName` |
| `FROM AzureServiceBusQueueSample` returns zero entirely | Try `FROM AzureServiceBusTopicSample` or check `AccountIntelligence.azure_service_bus.configured` |
| Any ASB query returns zero and `configured = False` | ASB is not in this account — skip all ASB queries, report `⚪ Not configured` |

### Level 2 — Wrong Account

If Level 1 also returns zero:

1. Check `AccountIntelligence.cross_account_entities` from the learn_account response
2. If the service appears in cross-account entities:
   ```
   ⚠️ {service} data is in account {home_account_id} — not the currently connected account.
   Connect to that account profile to query its data.
   ```
3. Pass `CROSS_ACCOUNT_FLAG` to Team Lead with the home account ID

### Level 3 — Wrong Time Window

If Level 2 also returns zero or data is too sparse:

- Expand from 60 min → 3 hours: `SINCE 3 hours ago`
- If still sparse, expand to 24 hours: `SINCE 24 hours ago`
- Report: "⚠️ Sparse data — expanded to {N} hour window"

### Level 4 — Wrong Name Format

If Level 3 also returns nothing:

1. Try bare name: `{namespace}/{service}` → `{service}` (strip the namespace prefix)
2. Try wildcard: `WHERE entity.name LIKE '%{bare_name}%'`
3. Try GUID-based lookup:
   ```nrql
   SELECT uniques(entity.guid), uniques(entity.name) FROM Span
   WHERE entity.name LIKE '%{bare_name}%'
   SINCE 3 hours ago LIMIT 20
   ```

### Level 5 — Declare NO_DATA with Evidence

Only after Levels 1-4 are exhausted, declare NO_DATA — but always include:
```
NO_DATA: {
  domain: "{domain name}",
  tried: [
    "FROM Transaction WHERE appName = '{service}' (0 results)",
    "FROM Span WHERE entity.name = '{service}' (0 results)",
    "expanded to 3hr window (0 results)",
    "wildcard search '%{bare_name}%' (0 results)"
  ],
  cross_account_suspected: true/false,
  likely_account_id: "{if known from learn_account}",
  recommendation: "Enable {data type} or check account {id}"
}
```

## Which Agents Use This Skill

ALL agents must follow this protocol. Never return NO_DATA after a single failed query.

## Quick Reference Card

```
Query returns zero?
  ├── Try alternative event type (Metric→Log, Transaction→Span, appName→entity.name)
  ├── Check cross-account entity list
  ├── Expand time window (60min→3hr→24hr)
  ├── Try bare name / wildcard / GUID
  └── Only then: declare NO_DATA with full evidence trail
```
