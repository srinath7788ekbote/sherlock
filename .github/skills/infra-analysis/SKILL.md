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
