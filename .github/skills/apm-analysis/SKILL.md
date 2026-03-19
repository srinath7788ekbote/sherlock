---
name: apm-analysis
description: >
  Deep APM analysis for New Relic APM. Golden signals deep-dive, transaction
  performance analysis, error classification, deployment correlation, and
  SLA/Apdex assessment.
---

# APM Analysis — Sherlock APM Playbook

> Deep APM investigation using New Relic APM via Sherlock MCP tools.
> Use the sherlock-apm agent for execution.

---

## Investigation Steps

### Step 1 — Golden Signals

```
mcp_sherlock_get_service_golden_signals(service_name, since_minutes)
```

### Step 2 — Deployment Correlation

```
mcp_sherlock_get_deployments(app_name)
```

Compare deployment timestamps with signal degradation. If degradation started
within 30 minutes of a deploy, flag deployment as potential cause.

### Step 3 — Error Breakdown

```sql
SELECT count(*) FROM TransactionError
WHERE appName = '{service}'
FACET error.class, error.message
SINCE 1 hour ago
LIMIT 20
```

### Step 4 — Slow Transaction Analysis

```sql
SELECT average(duration), percentile(duration, 95, 99)
FROM Transaction
WHERE appName = '{service}'
FACET name
SINCE 1 hour ago
ORDER BY average(duration) DESC
LIMIT 10
```

### Step 5 — Throughput Trend

```sql
SELECT rate(count(*), 1 minute)
FROM Transaction
WHERE appName = '{service}'
TIMESERIES
SINCE 2 hours ago
```

### Step 6 — External Service Impact

```sql
SELECT average(duration), count(*)
FROM Transaction
WHERE appName = '{service}'
FACET externalCallName
SINCE 1 hour ago
ORDER BY average(duration) DESC
```

---

## Thresholds

| Metric | HEALTHY | WARNING | CRITICAL |
|--------|---------|---------|----------|
| Error Rate | <1% | 1-5% | >5% |
| P95 Latency | <1s | 1-5s | >5s |
| P99 Latency | <2s | 2-10s | >10s |
| Throughput change | Stable | -30% | -50% |
| Apdex | >0.9 | 0.7-0.9 | <0.7 |
