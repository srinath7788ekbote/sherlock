"""Quick test to verify deep link base64 encoding."""
import base64
import json
import urllib.parse

nrql = "SELECT count(*) FROM Span WHERE appName = 'eswd-prod/edgar-service' AND span.kind = 'client' AND name = 'External/data.sec.gov/Spring-WebClient/exchange' SINCE 3 hours ago FACET http.statusCode TIMESERIES 10 minutes"
account_id = 3007677

pane = json.dumps(
    {
        "nerdletId": "data-exploration.query-builder",
        "initialActiveInterface": "nrqlEditor",
        "initialNrqlValue": nrql,
        "initialAccountId": account_id,
    },
    separators=(",", ":"),
)

b64_raw = base64.b64encode(pane.encode()).decode()

print("=== Raw base64 ===")
print(f"Has '+': {'+' in b64_raw}")
print(f"Has '/': {'/' in b64_raw}")
print(f"Has '=': {b64_raw.endswith('=')}")
print(f"Sample: ...{b64_raw[-80:]}")
print()

# Current (broken) URL
broken_url = (
    f"https://one.newrelic.com/launcher/data-exploration.query-builder"
    f"?pane={b64_raw}"
    f"&platform[accountId]={account_id}"
)

# Fixed: URL-encode the base64 pane value
b64_encoded = urllib.parse.quote(b64_raw, safe="")
fixed_url = (
    f"https://one.newrelic.com/launcher/data-exploration.query-builder"
    f"?pane={b64_encoded}"
    f"&platform[accountId]={account_id}"
)

print("=== Broken URL (current) ===")
print(broken_url[:200])
print()
print("=== Fixed URL (URL-encoded pane) ===")
print(fixed_url[:200])
print()

# Verify round-trip
decoded_back = urllib.parse.unquote(b64_encoded)
assert decoded_back == b64_raw, "Round-trip failed!"
print("Round-trip verification: PASSED")

# Test many different NRQLs to see if any produce + or /
import random
test_nrqls = [
    "SELECT count(*) FROM TransactionError WHERE appName = 'eswd-prod/edgar-service' SINCE 2 hours ago FACET request.uri LIMIT 20",
    "SELECT latest(reason), latest(message) FROM K8sPodSample WHERE nodeName LIKE '%vmss0003u6%' AND status = 'Failed' SINCE 6 hours ago FACET podName LIMIT 20",
    "SELECT max(`activeMessages.Average`) as peak_active, max(deadLetterMessages) as peak_dlq FROM AzureServiceBusQueueSample WHERE entityName LIKE 'prod-export%' SINCE 2 hours ago FACET entityName",
    "SELECT latest(`cpuPercent.Average`) as cpu_pct, latest(`memoryPercent.Average`) as mem_pct FROM AzurePostgreSqlFlexibleServerSample WHERE displayName LIKE '%prd%tngo%' SINCE 2 hours ago FACET displayName",
    "SELECT percentage(count(*), WHERE result = 'SUCCESS') as 'Success Rate' FROM SyntheticCheck SINCE 24 hours ago FACET monitorName LIMIT 50",
]
print("\n=== Testing multiple NRQLs for + or / in base64 ===")
bad_count = 0
for i, test_nrql in enumerate(test_nrqls):
    p = json.dumps({"nerdletId":"data-exploration.query-builder","initialActiveInterface":"nrqlEditor","initialNrqlValue":test_nrql,"initialAccountId":3007677},separators=(",",":"))
    b = base64.b64encode(p.encode()).decode()
    has_bad = '+' in b or '/' in b
    if has_bad:
        bad_count += 1
    print(f"  NRQL #{i+1}: has_bad_chars={has_bad}  (+={'+' in b}, /={'/' in b}, =={b.endswith('=')})")
print(f"\n{bad_count}/{len(test_nrqls)} NRQLs produce unsafe base64 chars")
