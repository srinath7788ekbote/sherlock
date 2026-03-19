---
name: log-analysis
description: >
  Deep log analysis for New Relic Logs. Error pattern detection, volume
  spike analysis, severity distribution, exception classification, and
  log-based timeline reconstruction for incident investigation.
---

# Log Analysis — Sherlock Logs Playbook

> Deep log investigation using New Relic Logs via Sherlock MCP tools.
> Use the sherlock-logs agent for execution.

---

## Investigation Steps

### Step 0 — Attribute Discovery (MANDATORY BEFORE NRQL)

```
mcp_sherlock_get_nrql_context(domain="logs")
```

Discover the real service attribute (e.g., `entity.name`, `service.name`, `serviceName`).
Store as `{svc_attr}` and use in ALL subsequent NRQL queries.

### Step 1 — Error Search (MANDATORY FIRST)

```
mcp_sherlock_search_logs(service_name, severity="ERROR", since_minutes=60)
```

**CRITICAL**: Check the `note` field in the response — it tells which attribute found data.
Use that attribute for all subsequent NRQL. If 0 results with ERROR, retry without severity filter.

### Step 2 — Severity Distribution

```sql
SELECT count(*) FROM Log
WHERE `{svc_attr}` LIKE '%{service}%'
FACET level
SINCE 1 hour ago
```

### Step 3 — Error Pattern Classification

```sql
SELECT count(*) FROM Log
WHERE `{svc_attr}` LIKE '%{service}%' AND level = 'ERROR'
FACET message
SINCE 1 hour ago
LIMIT 10
```

### Step 4 — Volume Spike Detection

```sql
SELECT rate(count(*), 1 minute) FROM Log
WHERE `{svc_attr}` LIKE '%{service}%'
TIMESERIES
SINCE 2 hours ago
```

Compare the most recent 15-minute rate against the 2-hour baseline.
A spike is >2x the baseline rate.

### Step 5 — Sample Error Messages

```sql
SELECT message, timestamp FROM Log
WHERE `{svc_attr}` LIKE '%{service}%' AND level = 'ERROR'
SINCE 30 minutes ago
LIMIT 5
```

⚠️ **NEVER hardcode `service_name` in NRQL.** Always use `{svc_attr}` discovered in Step 0.

---

## Log Attribute Awareness

Common log attributes in New Relic vary by agent:

| Attribute | APM Agent | Fluentd/Fluent Bit | Syslog |
|-----------|-----------|-------------------|--------|
| Service | `service_name`, `entity.name` | `service_name`, `app` | `hostname` |
| Severity | `level`, `severity` | `level`, `log_level` | `severity` |
| Message | `message` | `message`, `log` | `message` |

Always call `mcp_sherlock_get_nrql_context(domain="logs")` first to get
the correct attribute names for this account.
