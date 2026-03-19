---
name: k8s-debug
description: >
  Deep Kubernetes investigation for services monitored by New Relic K8s
  integration. Pod lifecycle analysis, container resource diagnosis,
  node health, OOMKill detection, restart loop analysis, and HPA scaling
  assessment. Second-level deep-dive invoked after incident-triage
  identifies K8s as the problem domain.
---

# Kubernetes Deep-Dive Debug — Sherlock K8s Playbook

> This skill provides deep K8s investigation using New Relic's K8s integration
> data. Use the sherlock-k8s agent for execution.

---

## Decision Tree

Start here when a K8s issue is identified:

```
Is there an active K8s alert/incident?
├── YES → Check alert condition NRQL → is it pod, node, or HPA?
│   ├── Pod → Pod Investigation Layer
│   ├── Node → Node Investigation Layer
│   └── HPA → Scaling Investigation Layer
└── NO → What symptom was reported?
    ├── CrashLoopBackOff → Pod Investigation Layer
    ├── OOMKilled → Container Investigation Layer
    ├── Pending → Scheduling Investigation Layer
    ├── High CPU/Memory → Container Investigation Layer
    └── Replica mismatch → Deployment Investigation Layer
```

---

## Pod Investigation Layer

### NRQL Queries

```sql
-- Pod status overview
SELECT latest(status), latest(isReady), sum(restartCount)
FROM K8sPodSample
WHERE deploymentName LIKE '%{service}%'
FACET podName
SINCE 1 hour ago

-- Recent restarts with reasons
SELECT latest(reason), latest(status), sum(restartCount)
FROM K8sPodSample
WHERE deploymentName LIKE '%{service}%' AND restartCount > 0
FACET podName
SINCE 2 hours ago

-- K8s events for this service
SELECT *
FROM InfrastructureEvent
WHERE category = 'kubernetes'
AND (involvedObjectName LIKE '%{service}%' OR involvedObjectKind = 'Pod')
SINCE 1 hour ago
LIMIT 50
```

### What to Look For

| Signal | Meaning | Next Step |
|--------|---------|-----------|
| restartCount > 0 | Pod crashed and restarted | Check container logs and reason |
| status = 'Pending' | Pod can't be scheduled | Check node capacity and taints |
| isReady = false | Pod running but not serving | Check probe configuration |
| K8s Warning events | Scheduling, resource, or image issues | Parse event messages |

---

## Container Investigation Layer

### NRQL Queries

```sql
-- Container CPU/memory usage vs limits
SELECT average(cpuUsedCores), average(cpuLimitCores),
       average(memoryUsedBytes/1e6) as memMB, average(memoryLimitBytes/1e6) as limitMB
FROM K8sContainerSample
WHERE deploymentName LIKE '%{service}%'
FACET containerName, podName
SINCE 1 hour ago

-- OOMKill detection
SELECT count(*)
FROM K8sContainerSample
WHERE deploymentName LIKE '%{service}%' AND reason = 'OOMKilled'
FACET podName
SINCE 24 hours ago

-- CPU throttling detection
SELECT average(cpuCfsThrottledPeriodsDelta)
FROM K8sContainerSample
WHERE deploymentName LIKE '%{service}%'
FACET podName
TIMESERIES SINCE 1 hour ago
```

### What to Look For

| Signal | Meaning | Next Step |
|--------|---------|-----------|
| memory > 90% of limit | OOMKill risk | Increase memory limit or fix leak |
| CPU throttled > 0 | CPU limit hit | Check CPU limits vs actual usage |
| OOMKilled count > 0 | Container killed for memory | Memory leak analysis needed |

---

## Node Investigation Layer

### NRQL Queries

```sql
-- Node health overview
SELECT latest(cpuUsedCores/cpuRequestedCores*100) as cpuPct,
       latest(memoryUsedBytes/memoryCapacityBytes*100) as memPct
FROM K8sNodeSample
FACET nodeName
SINCE 30 minutes ago

-- Pods per node (capacity check)
SELECT uniqueCount(podName)
FROM K8sPodSample
FACET nodeName
SINCE 30 minutes ago
```

---

## Deployment Investigation Layer

### NRQL Queries

```sql
-- Deployment replica health
SELECT latest(podsDesired), latest(podsReady), latest(podsAvailable)
FROM K8sDeploymentSample
WHERE deploymentName LIKE '%{service}%'
TIMESERIES SINCE 2 hours ago

-- ReplicaSet health
SELECT latest(podsDesired), latest(podsReady)
FROM K8sReplicaSetSample
WHERE replicaSetName LIKE '%{service}%'
SINCE 1 hour ago
```

---

## K8s Naming Convention

K8s entities in New Relic use different names than APM:

| APM Name | K8s Attribute | Example |
|----------|---------------|---------|
| `eswd-prod/sifi-adapter` | `deploymentName` | `sifi-adapter` |
| `eswd-prod/sifi-adapter` | `namespaceName` | `eswd-prod` |
| `eswd-prod/sifi-adapter` | `label.app` | `sifi-adapter` |

**Always search with the bare service name (after /) plus the full name.**
