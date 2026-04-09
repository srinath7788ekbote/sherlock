---
name: infra-analysis
description: >
  Deep infrastructure and dependency analysis for New Relic. Service dependency
  mapping, host health metrics, upstream/downstream health analysis, browser
  performance, messaging system analysis, and blast radius assessment.
---

# Infrastructure Analysis — Sherlock Infra Playbook

> Deep infrastructure and dependency investigation using New Relic via
> Sherlock MCP tools. Use the sherlock-infra agent for execution.

---

## Investigation Steps

### Step 1 — Dependency Mapping

```
mcp_sherlock_get_service_dependencies(service_name, direction="both")
```

Map upstream callers and downstream dependencies. Flag any unhealthy dependencies.

### Step 2 — Host Health (NRQL)

```sql
-- Host CPU
SELECT average(cpuPercent), max(cpuPercent)
FROM SystemSample
WHERE hostname LIKE '%{service}%'
TIMESERIES
SINCE 1 hour ago

-- Host Memory
SELECT average(memoryUsedPercent), max(memoryUsedPercent)
FROM SystemSample
WHERE hostname LIKE '%{service}%'
TIMESERIES
SINCE 1 hour ago

-- Disk Usage
SELECT average(diskUsedPercent)
FROM SystemSample
WHERE hostname LIKE '%{service}%'
FACET hostname
SINCE 30 minutes ago
```

### Step 2b — Azure Cloud Integration Metrics

Run these ONLY if the service connects to Azure managed services.
These event types come from New Relic's Azure cloud integration, NOT the NR agent.

#### Azure PostgreSQL Flexible Server
```sql
-- DB availability (1 = alive, 0 = completely down — CRITICAL if 0)
SELECT latest(provider.databaseAvailability.Average) as availability,
       average(provider.cpuPercent.Average) as cpu_pct,
       average(provider.memoryPercent.Average) as mem_pct,
       sum(provider.connectionsFailed.Total) as failed_connections,
       sum(provider.activeConnections.Average) as active_connections
FROM AzurePostgreSqlFlexibleServerSample
FACET displayName, resourceGroupName
SINCE {since_minutes} minutes ago

-- Availability timeseries — shows WHEN it dropped
SELECT latest(provider.databaseAvailability.Average) as availability,
       sum(provider.connectionsFailed.Total) as failed
FROM AzurePostgreSqlFlexibleServerSample
TIMESERIES 1 minute
SINCE {since_minutes} minutes ago

-- Storage and throughput spike detection
SELECT average(provider.storageLimitMb.Average) as storage_limit_mb,
       average(provider.storageUsedMb.Average) as storage_used_mb,
       sum(provider.networkBytesIngress.Total)/1e6 as ingress_mb,
       sum(provider.networkBytesEgress.Total)/1e6 as egress_mb
FROM AzurePostgreSqlFlexibleServerSample
SINCE {since_minutes} minutes ago TIMESERIES 5 minutes
```

**Thresholds:**
- `availability = 0` → 🔴 CRITICAL — database completely unavailable
- `availability < 1` → 🟡 WARNING — intermittent availability issues
- `failed_connections > 0` → 🟡 WARNING — investigate pg_hba.conf and SSL config
- `active_connections > (max_connections * 0.8)` → 🟡 WARNING — connection saturation

#### Azure Service Bus — Discovery-First Queries

**ALWAYS use namespaces and queue names from `AccountIntelligence.azure_service_bus`.
Never hardcode namespace or entity names — they vary per client and environment.**

If `AccountIntelligence` is not available, discover first:
```sql
-- Discover all ASB namespaces in this account
SELECT uniques(namespace, 30), uniques(entityName, 100)
FROM AzureServiceBusQueueSample
SINCE 1 hour ago LIMIT 1
```

Use the returned values in all subsequent queries.

**Correct event types and attribute names:**

| What to query | Event type | Key attributes |
|---|---|---|
| Queue metrics | `AzureServiceBusQueueSample` | `activeMessages.Average`, `deadLetterMessages`, `incomingMessages.Total`, `messages` |
| Topic metrics | `AzureServiceBusTopicSample` | `incomingMessages.Total`, `entityName`, `namespace` |
| Namespace metrics | `AzureServiceBusNamespaceSample` | `memoryUsagePercent.Maximum`, `entityName` |

**Filter fields:** Use `namespace = '{name}'` and `entityName LIKE '%{name}%'` —
NOT `displayName` (that field does not exist in these event types).

**DLQ thresholds:**
- `deadLetterMessages > 0` → 🟡 ALWAYS report
- `deadLetterMessages > 10` → 🔴 CRITICAL
- Growing `activeMessages` with zero `incomingMessages` → consumer is DOWN

#### Azure Redis Cache
```sql
SELECT average(provider.cacheHitsPerSecond.Average) as hits,
       average(provider.cacheMissesPerSecond.Average) as misses,
       average(provider.connectedClients.Average) as clients,
       average(provider.usedMemoryPercentage.Average) as mem_pct
FROM AzureRedisCacheSample
SINCE {since_minutes} minutes ago TIMESERIES 5 minutes
```

#### Azure Key Vault
```sql
SELECT sum(provider.serviceApiResult.Total) as api_calls,
       filter(sum(provider.serviceApiResult.Total),
              WHERE provider.statusCode LIKE '4%' OR provider.statusCode LIKE '5%')
              as errors
FROM AzureKeyVaultSample
SINCE {since_minutes} minutes ago
```

**Important:** These event types will return NO_DATA if Azure integration is not
configured for the account. That is fine — report `⚪ NO_DATA: Azure integration
not configured` and continue. Never fail silently.

### Step 3 — External Service Calls

```sql
-- External call performance
SELECT average(duration), count(*)
FROM Transaction
WHERE appName = '{service}'
FACET externalCallName
SINCE 1 hour ago
ORDER BY average(duration) DESC

-- Database call performance
SELECT average(databaseDuration), count(*)
FROM Transaction
WHERE appName = '{service}' AND databaseDuration > 0
FACET databaseCallName
SINCE 1 hour ago
```

### Step 4 — Browser Performance (if applicable)

```sql
-- Page load times
SELECT average(duration), count(*)
FROM PageView
WHERE appName LIKE '%{service}%'
TIMESERIES
SINCE 1 hour ago

-- JavaScript errors
SELECT count(*) FROM JavaScriptError
WHERE appName LIKE '%{service}%'
FACET errorMessage
SINCE 1 hour ago
LIMIT 10
```

### Step 5 — Messaging / Queue Health

```sql
-- Queue metrics
SELECT count(*), average(duration)
FROM QueueSample
WHERE queue LIKE '%{service}%'
SINCE 30 minutes ago

-- Kafka consumer lag (if applicable)
SELECT latest(consumer.lag), latest(consumer.offsetBehind)
FROM KafkaConsumerSample
WHERE consumerGroup LIKE '%{service}%'
FACET topic
SINCE 30 minutes ago
```

### Step 6 — Blast Radius Assessment

Count upstream services (who is affected if this service fails).
Check health of each downstream dependency individually.

---

## Thresholds

| Metric | HEALTHY | WARNING | CRITICAL |
|--------|---------|---------|----------|
| Unhealthy deps | 0 | 1 | 2+ |
| Host CPU | <70% | 70-90% | >90% |
| Host Memory | <80% | 80-95% | >95% |
| Disk usage | <75% | 75-90% | >90% |
| Upstream blast | <3 services | 3-10 services | >10 services |
| External call P95 | <1s | 1-5s | >5s |

---

## Upstream Cascade Detection (Layer 3 Analysis)

When any 5xx errors, connection failures, or upstream health issues are found,
ALWAYS run Layer 3 analysis to find what caused the upstream to become unhealthy.
Do NOT stop at the symptom (e.g., "Envoy UH flag detected"). Find the cause.

### Mandatory DB Connection Error Scan

Run this query whenever investigating 5xx errors, upstream failures, or
services reporting connection/timeout errors:

```sql
SELECT count(*) FROM Log
WHERE cluster_name IN ('{cluster_name}')
AND (
  message LIKE '%FATAL: terminating connection%'
  OR message LIKE '%administrator command%'
  OR message LIKE '%HikariPool%'
  OR message LIKE '%SQLSTATE%'
  OR message LIKE '%Connection refused%'
  OR message LIKE '%Unable to acquire%'
  OR message LIKE '%pool is empty%'
  OR message LIKE '%Connection reset%'
  OR message LIKE '%Broken pipe%'
)
SINCE {since_minutes} minutes ago
TIMESERIES 5 minutes
FACET entity.name
LIMIT 20
```

**Why this matters:** Database connection errors (PostgreSQL, SQL Server, MySQL)
manifest as upstream 5xx errors in Istio/nginx. The Envoy response flag `UH`
(No Healthy Upstream) is frequently caused by all pods failing health checks
because their DB connections were terminated — not by Istio misconfiguration.

**Blast radius:** Always use `FACET entity.name` — a single DB restart typically
cascades to ALL services that share that database. Report ALL affected services,
not just the alerted one.

**SQLSTATE codes to know:**
| Code | Meaning | Cause |
|------|---------|-------|
| `57P01` | Admin shutdown | Azure/GCP/AWS maintenance restart |
| `08006` | Connection failure | Network interruption |
| `08001` | Unable to connect | DB down or unreachable |
| `57014` | Query cancelled | Statement timeout |
| `40P01` | Deadlock | Query contention |

### Azure Infrastructure Event Correlation

When DB connection errors are found, correlate with Azure maintenance:

```sql
SELECT latest(provider.availabilityPercent.Average),
       latest(provider.cpuPercent.Average),
       latest(provider.activeConnections.Average),
       latest(provider.connectionsFailed.Count)
FROM AzurePostgreSqlFlexibleServerSample
SINCE {since_minutes} minutes ago
TIMESERIES 5 minutes
FACET displayName
LIMIT 10
```

Also check Redis, Service Bus, and other shared infrastructure:
```sql
SELECT latest(provider.serverLoad.Average),
       latest(provider.connectedClients.Average)
FROM AzureRedisCacheSample
SINCE {since_minutes} minutes ago
FACET displayName
```

**If AzurePostgreSqlFlexibleServerSample returns zero rows:**
The Azure integration may not be configured. Fall back to:
```sql
SELECT count(*) FROM Log
WHERE message LIKE '%FATAL%'
AND (message LIKE '%postgres%' OR message LIKE '%pg%' OR message LIKE '%sql%')
SINCE {since_minutes} minutes ago
FACET entity.name
```

### Istio/Envoy Data Source Priority

**Priority order for Istio telemetry:**

1. **Log-based (try first):**
```sql
SELECT count(*) FROM Log
WHERE container_name = 'istio-proxy'
AND status > 499
SINCE {since_minutes} minutes ago
TIMESERIES 5 minutes
FACET vhost, status, upstream_cluster
LIMIT 20
```

2. **Metric-based (try if Log returns zero):**
```sql
SELECT sum(istio_requests_total) FROM Metric
WHERE response_code LIKE '5%'
SINCE {since_minutes} minutes ago
FACET response_flags, destination_service, response_code
```

3. **Prometheus via OTel (try if both above return zero):**
```sql
SELECT rate(sum(istio_requests_total), 1 minute) FROM Metric
SINCE {since_minutes} minutes ago
TIMESERIES 5 minutes
FACET destination_service
```

**Never report "no Istio data" after only querying one of these sources.**

### Envoy Response Flag Interpretation

When Envoy response flags are found, ALWAYS ask what caused them — never stop
at the flag itself:

| Flag | Meaning | What to check next |
|------|---------|-------------------|
| `UH` | No healthy upstream | → Check pod health, then DB connections, then mTLS config |
| `UC` | Upstream connection terminated | → Check if DB was restarted, check pod restarts |
| `UF` | Upstream connection failure | → Check service port naming, mTLS mode |
| `URX` | Upstream retry limit exceeded | → Check why the upstream is slow (DB queries, OOM) |
| `NR` | No route match | → Check VirtualService routing rules |
| `DC` | Downstream disconnect | → Usually client-side, check if cascaded from upstream |

**Rule:** `UH` flag + connection pool FATAL logs in the same time window = DB restart,
not Istio misconfiguration. Do NOT recommend DestinationRule changes if DB errors
are present.

### Layer 3 Summary Template

When upstream cascade is detected, add this to the investigation report:

```markdown
### ⚡ Layer 3 — Upstream Cascade Root Cause

**Primary cause:** {DB restart / Redis eviction / ASB throttle / other}
**Trigger:** {Azure maintenance / OOM / admin command / network / other}
**Timestamp:** {exact UTC time from log}
**Blast radius:** {N} services affected:
  - {service1}: {N} connection errors
  - {service2}: {N} connection errors
  ...

**Why Envoy showed UH/UC flags:** Pods failed health checks because their
DB connections were terminated — NOT because of Istio misconfiguration.

**Evidence:**
- `FATAL: terminating connection due to administrator command` at {time}
- Connection pool timeout across {N} services
- Azure PostgreSQL `provider.connectionsFailed.Count` spike at {time}
```
