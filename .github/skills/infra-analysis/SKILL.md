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

#### Azure Service Bus
```sql
-- Queue health and dead-letter status
SELECT sum(provider.activeMessages.Average) as active_msgs,
       sum(provider.deadLetteredMessages.Average) as dlq_msgs,
       sum(provider.incomingMessages.Total) as incoming,
       sum(provider.outgoingMessages.Total) as outgoing
FROM AzureServiceBusSample
FACET displayName, entityName
SINCE {since_minutes} minutes ago

-- DLQ spike detection
SELECT sum(provider.deadLetteredMessages.Average) as dlq_msgs
FROM AzureServiceBusSample
TIMESERIES 5 minutes
SINCE {since_minutes} minutes ago
```

**Thresholds:**
- `dlq_msgs > 0` → 🟡 WARNING — messages dead-lettered, manual replay may be needed
- `dlq_msgs > 10` → 🔴 CRITICAL — significant message loss, investigate immediately
- `active_msgs growing + outgoing = 0` → 🔴 CRITICAL — consumer is down

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
