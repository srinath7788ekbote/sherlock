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
