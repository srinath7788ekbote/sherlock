---
name: synthetic-debug
description: >
  Deep synthetic monitor investigation. Monitor health analysis, failure
  location diagnosis, scripted test debugging, availability trending,
  and APM backend correlation for New Relic Synthetics.
---

# Synthetic Debug — Sherlock Synthetics Playbook

> Deep synthetic monitor investigation using New Relic Synthetics.
> Use the sherlock-synthetics agent for execution.

---

## Investigation Steps

### Step 1 — List Monitors

```
mcp_sherlock_get_synthetic_monitors()
```

Find monitors related to the service/endpoint.

### Step 2 — Monitor Status

```
mcp_sherlock_get_monitor_status(monitor_name, since_minutes)
```

Check success rate by location.

### Step 3 — Failure Details

```
mcp_sherlock_get_monitor_results(monitor_name, result_filter="FAILED", since_minutes)
```

Examine specific failure messages, HTTP codes, and locations.

### Step 4 — Deep Investigation (with APM correlation)

```
mcp_sherlock_investigate_synthetic(monitor_name, since_minutes)
```

Full automated analysis including:
- Success rate trending
- Failure pattern by location
- APM backend correlation (is the backend slow/erroring?)
- Response time analysis

### Step 5 — Custom Availability Trending

```sql
SELECT percentage(count(*), WHERE result = 'SUCCESS')
FROM SyntheticCheck
WHERE monitorName = '{monitor}'
TIMESERIES
SINCE 24 hours ago
```

### Step 6 — Response Time Analysis

```sql
SELECT average(duration), percentile(duration, 95)
FROM SyntheticCheck
WHERE monitorName = '{monitor}'
FACET locationLabel
SINCE 24 hours ago
```

---

## Thresholds

| Metric | HEALTHY | WARNING | CRITICAL |
|--------|---------|---------|----------|
| Success Rate | >99% | 95-99% | <95% |
| Failed Locations | 0 | 1-2 | 3+ or all |
| Response Time | <2s | 2-5s | >5s |
| Duration (scripted) | <10s | 10-30s | >30s |
