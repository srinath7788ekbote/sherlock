---
name: alerts-analysis
description: >
  Deep alert and incident analysis for New Relic Alerts. Active incident
  detection, alert policy evaluation, violation history, incident pattern
  recognition, and cross-service incident correlation.
---

# Alerts Analysis — Sherlock Alerts Playbook

> Deep alert and incident investigation using New Relic Alerts via Sherlock MCP tools.
> Use the sherlock-alerts agent for execution.

---

## Investigation Steps

### Step 1 — Service-Specific Incidents

```
mcp_sherlock_get_service_incidents(service_name)
```

Check for active and recently-closed incidents related to this service.

### Step 2 — Account-Wide Incidents

```
mcp_sherlock_get_incidents(state="open")
```

Look for related incidents across other services (correlated failures).

### Step 3 — Alert Policies

```
mcp_sherlock_get_alerts()
```

Identify which policies and conditions cover this service.

### Step 4 — Incident Pattern Analysis (NRQL)

```sql
-- Incident frequency over 7 days
SELECT count(*) FROM NrAiIncident
WHERE title LIKE '%{service}%'
FACET priority
SINCE 7 days ago

-- Incident timeline
SELECT count(*) FROM NrAiIncident
WHERE title LIKE '%{service}%'
TIMESERIES
SINCE 7 days ago

-- Current condition evaluation
SELECT count(*) FROM NrAiIncident
WHERE title LIKE '%{service}%' AND event = 'open'
SINCE 24 hours ago
```

### Step 5 — Alert Condition Reproduction

If an alert is firing, reproduce the NRQL condition from the alert policy
to verify the current value against the threshold:

```sql
-- Example: error rate condition
SELECT percentage(count(*), WHERE error IS true) FROM Transaction
WHERE appName = '{service}'
SINCE 5 minutes ago
```

---

## Thresholds

| Metric | HEALTHY | WARNING | CRITICAL |
|--------|---------|---------|----------|
| Active incidents | 0 | 1 (warning priority) | 1+ (critical priority) |
| Incidents (7d) | 0-1 | 2-5 | >5 (recurring pattern) |
| Open duration | - | <30 min | >30 min |
| Correlated incidents | 0 | 1 other service | 2+ services |

---

## Stale Alert Detection

When an incident has been open for >30 minutes AND the underlying metric appears
to have stopped emitting data, check for the stale signal trap.

### Step 1 — Verify metric is still emitting

For a metric-based alert condition, check if new data points have arrived recently:

```sql
-- Replace with the actual metric name from the alert condition NRQL
SELECT count(*) FROM Metric
WHERE metricTimesliceName = '{metric_name}'
SINCE 30 minutes ago
TIMESERIES 5 minutes
```

If this returns zero data points → the signal has gone silent.

### Step 2 — Check alert condition configuration

Query the alert condition settings (via NR API or from the incident NRQL):

| Setting | Stale-trap value | Safe value |
|---------|-----------------|------------|
| `fillOption` | `last_value` | `none` |
| `expirationDuration` | `0` (disabled) | `600-900` (10-15 min) |
| `closeViolationsOnExpiration` | `false` | `true` |
| `violationTimeLimitSeconds` | `259200` (72h) | `3600-86400` |

### Step 3 — Diagnose and report

If `fillOption: last_value` AND `expirationDuration: 0` AND no new metric data:

```
🔒 STALE ALERT TRAP DETECTED

The alert is stuck open because:
- The metric '{metric_name}' has not emitted new data for {N} minutes
- Alert condition uses fillOption: last_value with no signal expiration
- The evaluator is replaying the last known value ({last_value}) indefinitely
- The incident will NOT auto-close until violationTimeLimitSeconds expires
  (in {hours_remaining} hours from now)

IMMEDIATE ACTIONS:
1. Manually close this incident — it is not reflecting real current state
   [Close incident](https://aiops.service.newrelic.com/accounts/{accountId}/incidents/{id}/redirect)

LONG-TERM FIX (update alert condition configuration):
- fillOption: none
- expirationDuration: 900
- closeViolationsOnExpiration: true

Closing note to paste:
"Metric silent — no new data emitting. Alert stuck open due to fillOption: last_value
with no signal expiration. Manually closing. Follow-up: set closeViolationsOnExpiration: true +
expirationDuration: 900 to prevent recurrence."
```
