# Sherlock

Intelligent New Relic investigation MCP for GitHub Copilot and any MCP-compatible AI client.

## Why Sherlock?

Like the detective, Sherlock investigates incidents by gathering clues from every available source — APM, logs, Kubernetes, synthetic monitors, and alerts — then synthesizes them into a clear diagnosis with prioritized recommendations. All from a single natural language prompt.

---

A **production-ready, multi-tenant Model Context Protocol (MCP) server** for New Relic observability. Gives AI coding assistants (GitHub Copilot, Claude, Cursor) **read-only** access to your New Relic telemetry via the NerdGraph GraphQL API.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Prerequisites](#prerequisites)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [Available Tools (20)](#available-tools-20)
7. [Workflows](#workflows)
8. [Security Model](#security-model)
9. [Multi-Tenant Profiles](#multi-tenant-profiles)
10. [Synthetics Deep-Dive](#synthetics-deep-dive)
11. [Developer Guide](#developer-guide)
12. [Troubleshooting](#troubleshooting)
13. [License](#license)

---

## Overview

This MCP server exposes **20 tools** that let an AI assistant query your New Relic account in real time. It learns the shape of your account on connect (APM services, K8s namespaces, synthetic monitors, alert policies, log partitions, infrastructure hosts, browser apps) so every subsequent query is precise and context-aware.

### Key Capabilities

- **Read-only by design** — every NerdGraph mutation is blocked at the client layer
- **Multi-tenant** — switch between accounts/profiles without restarting
- **Fuzzy name resolution** — typos in service or monitor names are auto-corrected
- **Prompt-injection scrubbing** — all tool output is scanned before returning to the LLM
- **Parallel data fetching** — investigation tools fire queries concurrently for speed
- **Credential security** — API keys stored in OS keychain via `keyring`, never in plain text

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    AI Assistant (LLM)                    │
│              (GitHub Copilot / Claude / Cursor)          │
└──────────────────────────┬──────────────────────────────┘
                           │ stdio (MCP protocol)
┌──────────────────────────▼──────────────────────────────┐
│                      main.py                             │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ Tool Router  │  │ Audit Logger │  │ Response Scrub │  │
│  └──────┬──────┘  └──────────────┘  └────────────────┘  │
│         │                                                │
│  ┌──────▼──────────────────────────────────────────────┐│
│  │                   tools/ layer                       ││
│  │  entities │ nrql │ alerts │ apm │ logs │ k8s        ││
│  │  golden_signals │ synthetics │ investigate           ││
│  │  intelligence_tools                                  ││
│  └──────┬──────────────────────────────────────────────┘│
│         │                                                │
│  ┌──────▼──────────────────────────────────────────────┐│
│  │                  core/ layer                         ││
│  │  context │ credentials │ intelligence │ cache        ││
│  │  sanitize │ exceptions                               ││
│  └──────┬──────────────────────────────────────────────┘│
│         │                                                │
│  ┌──────▼──────────────────────────────────────────────┐│
│  │               client/ layer                          ││
│  │  NerdGraphClient (httpx + tenacity retry)            ││
│  │  Read-only enforcement │ Batch queries               ││
│  └─────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────┘
                           │
                    NerdGraph GraphQL API
              US: https://api.newrelic.com/graphql
              EU: https://api.eu.newrelic.com/graphql
```

### Layer Responsibilities

| Layer | Purpose |
|-------|---------|
| **main.py** | MCP server lifecycle, tool registration, audit logging, response scrubbing |
| **tools/** | Individual tool implementations — each file owns one domain |
| **core/** | Shared primitives — credentials, context, intelligence, cache, sanitization |
| **client/** | HTTP transport — NerdGraph client with retry, read-only enforcement, batching |

---

## Prerequisites

| Requirement | Minimum Version |
|-------------|-----------------|
| Python | 3.11+ |
| pip | 23.0+ |
| New Relic User API Key | `NRAK-...` format |
| OS Keychain | macOS Keychain / Windows Credential Locker / Linux Secret Service |

---

## Installation

### macOS (recommended)

```bash
# 1. Install Python 3.11+ via pyenv
brew install pyenv
pyenv install 3.11.9
pyenv local 3.11.9

# 2. Clone the repository
cd ~/Documents
git clone <repo-url> sherlock
cd sherlock

# 3. Create virtual environment
python -m venv .venv
source .venv/bin/activate

# 4. Install in development mode
make install
# or: pip install -e ".[dev]"

# 5. Verify the connection
make connect
# Prompts for Account ID, API Key, Region
# Validates against NerdGraph and saves to OS keychain

# 6. Run the MCP server
make run
```

### Windows

```powershell
# 1. Ensure Python 3.11+ is installed
python --version

# 2. Clone and navigate
cd $env:USERPROFILE\Documents
git clone <repo-url> sherlock
cd sherlock

# 3. Create virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1

# 4. Install
pip install -e ".[dev]"

# 5. Verify connection
python scripts/test_connection.py

# 6. Run
python -m main
```

### VS Code Integration

The server is pre-configured in `.vscode/settings.json`. After installation:

1. Open the `sherlock` folder in VS Code
2. Ensure the Python extension is installed
3. The MCP server will appear under **GitHub Copilot → MCP Servers**
4. Use `@sherlock` in Copilot Chat to interact with your telemetry

---

## Configuration

### Environment Variables

Copy `.env.example` to `.env` and fill in:

```bash
# Required
NEW_RELIC_ACCOUNT_ID=123456
NEW_RELIC_API_KEY=NRAK-xxxxxxxxxxxxxxxxxxxx
NEW_RELIC_REGION=US          # US or EU

# Optional
LOG_LEVEL=INFO               # DEBUG, INFO, WARNING, ERROR
CACHE_TTL_SECONDS=1800       # Intelligence cache TTL (default: 30 min)
```

### Profile-Based Configuration

For multi-tenant setups, use profiles instead of environment variables:

```bash
# Save a profile (interactive)
make connect

# Or programmatically via the CLI
python scripts/cli.py
> connect_account production 123456 NRAK-xxx US
```

---

## Available Tools (20)

### Connection & Intelligence (4 tools)

| # | Tool | Description |
|---|------|-------------|
| 1 | `connect_account` | Connect to a New Relic account by profile name or credentials |
| 2 | `learn_account` | Re-discover account topology (APM, K8s, synthetics, alerts, etc.) |
| 3 | `get_account_summary` | Return a summary of discovered assets |
| 4 | `list_profiles` | List all saved credential profiles |

### Query & Exploration (3 tools)

| # | Tool | Description |
|---|------|-------------|
| 5 | `run_nrql_query` | Execute any read-only NRQL query |
| 6 | `get_nrql_context` | Get NRQL query templates for a specific domain (apm, k8s, synthetics, etc.) |
| 7 | `get_entity_guid` | Look up an entity's GUID by name or domain |

### APM & Performance (3 tools)

| # | Tool | Description |
|---|------|-------------|
| 8 | `get_apm_applications` | List all APM-instrumented applications |
| 9 | `get_app_metrics` | Get key metrics for a specific application |
| 10 | `get_deployments` | List recent deployments for an application |

### Alerts & Incidents (3 tools)

| # | Tool | Description |
|---|------|-------------|
| 11 | `get_alerts` | List alert policies and their conditions |
| 12 | `get_incidents` | List incidents filtered by state (open/closed) |
| 13 | `get_service_incidents` | Get incidents for a specific service (fuzzy name resolution) |

### Infrastructure & Kubernetes (1 tool)

| # | Tool | Description |
|---|------|-------------|
| 14 | `get_k8s_health` | Get K8s cluster health — pods, nodes, containers, events |

### Logs (1 tool)

| # | Tool | Description |
|---|------|-------------|
| 15 | `search_logs` | Search logs by service, severity, keyword, time window |

### Golden Signals (1 tool)

| # | Tool | Description |
|---|------|-------------|
| 16 | `get_service_golden_signals` | Get latency, errors, traffic, saturation for a service |

### Synthetics (4 tools)

| # | Tool | Description |
|---|------|-------------|
| 17 | `get_synthetic_monitors` | List all synthetic monitors with metadata |
| 18 | `get_monitor_status` | Deep health check — per-location success rates, diagnosis codes |
| 19 | `get_monitor_results` | Get recent check results for a monitor |
| 20 | `investigate_synthetic` | Full investigation — monitor health + APM correlation + recommendations |

### Investigation (1 tool)

| # | Tool | Description |
|---|------|-------------|
| — | `investigate_service` | **Mega-tool**: parallel fetch of golden signals, alerts, logs, K8s, synthetics → unified report |

> Note: `investigate_service` uses the tools above internally and is registered as tool #20 in the server.

---

## Workflows

### Quick Health Check

```
User: "How is web-api performing?"
→ Copilot calls: get_service_golden_signals("web-api")
→ Returns: latency p50/p99, error rate, throughput, saturation with threshold alerts
```

### Deep Investigation

```
User: "Investigate the checkout service — it seems slow"
→ Copilot calls: investigate_service("checkout-service")
→ Parallel fetch: golden signals + alerts + logs + K8s + synthetics
→ Returns: unified report with root cause hypothesis and recommendations
```

### Synthetic Monitor Triage

```
User: "Why is the Login Flow monitor failing?"
→ Copilot calls: investigate_synthetic("Login Flow")
→ Fetches: per-location results, APM correlation, recent errors
→ Returns: diagnosis (GLOBAL_FAILURE / REGIONAL_FAILURE / INTERMITTENT) + recommendations
```

### Multi-Account Switching

```
User: "Switch to the staging account"
→ Copilot calls: connect_account("staging")
→ Loads credentials from OS keychain
→ Runs learn_account to discover staging topology
→ All subsequent queries target staging
```

### Custom NRQL

```
User: "Show me the top 10 slowest transactions in the last hour"
→ Copilot calls: run_nrql_query("SELECT average(duration) FROM Transaction FACET name SINCE 1 hour ago LIMIT 10")
→ Returns: raw NRQL results as JSON
```

---

## Security Model

### Read-Only Enforcement

The `NerdGraphClient` blocks **all** mutations at the transport layer. The following operations are explicitly blocked:

- `syntheticscreate`, `syntheticsupdate`, `syntheticsdelete`
- `alertsconditioncreate`, `alertsconditionupdate`, `alertsconditiondelete`
- `dashboardcreate`, `dashboardupdate`, `dashboarddelete`
- `entitycreate`, `entityupdate`, `entitydelete`
- `accountcreate`, `apiAccesscreate`
- `tagTaggingAddTagsToEntity`, `tagTaggingDeleteTagFromEntity`

Any attempt to execute a blocked operation raises `ReadOnlyViolation`, logged as a **SECURITY WARNING** in the audit log.

### Credential Security

- API keys are stored in the **OS keychain** via the `keyring` library
- Keys are never written to disk, environment variables, or logs
- The `redacted_key` property masks all but the last 4 characters
- `model_dump()` excludes the raw API key

### Prompt Injection Defense

All tool responses are scanned by `scrub_tool_response()` before returning to the LLM. Detected patterns include:

- "ignore all previous instructions"
- "you are now" / "act as"
- "system prompt" / "override"
- Markdown/HTML injection attempts

Malicious content is replaced with a safe redaction message.

### Audit Logging

Every tool invocation is logged to `~/.sherlock/logs/audit.log` with:

- Timestamp
- Tool name
- Arguments (API keys redacted)
- Success/failure status
- Execution duration

### How to Revoke Access

If you suspect an API key has been compromised, or you simply want to remove the MCP server's access to a New Relic account:

1. **Rotate the API key in New Relic.** Go to **[one.newrelic.com](https://one.newrelic.com) → User menu → API keys** and delete or regenerate the key used by this server. This immediately invalidates all sessions using the old key.
2. **Delete the local profile.** Run the CLI to remove the stored credential:
   ```bash
   python scripts/cli.py --tool list_profiles      # find the profile name
   # Then delete the keychain entry manually:
   python -c "import keyring; keyring.delete_password('sherlock', '<profile_name>')"
   ```
3. **Clear the intelligence cache** so no stale data remains on disk:
   ```bash
   rm -rf ~/.sherlock/cache/
   ```
4. **Review the audit log** at `~/.sherlock/logs/audit.log` to verify which tools were called and when.

---

## Multi-Tenant Profiles

### Creating Profiles

```bash
# Interactive
make connect

# Programmatic (via CLI)
python scripts/cli.py
> connect_account my-profile 123456 NRAK-xxx US
```

### Profile Storage

```
~/.sherlock/
├── profiles.json          # Profile metadata (no secrets)
├── cache/
│   └── {account_id}.json  # Intelligence cache per account
└── logs/
    ├── sherlock.log       # Application logs (10MB × 5 rotations)
    └── audit.log          # Audit trail (10MB × 10 rotations)
```

### Profile Format

See `profiles/profiles.example.json`:

```json
[
  {
    "name": "production",
    "account_id": "123456",
    "region": "US",
    "created_at": "2025-01-15T10:30:00Z"
  },
  {
    "name": "staging",
    "account_id": "789012",
    "region": "US",
    "created_at": "2025-01-15T10:31:00Z"
  }
]
```

---

## Synthetics Deep-Dive

### Monitor Discovery

During `learn_account`, the server discovers all synthetic monitors and stores metadata:

- Monitor name, GUID, type (SIMPLE, SCRIPT_BROWSER, SCRIPT_API, etc.)
- Enabled/disabled status
- Check locations (AWS regions)
- Check period (EVERY_MINUTE, EVERY_5_MINUTES, etc.)
- Associated APM service (if tagged)

### Diagnosis Codes

`get_monitor_status` returns one of five diagnosis codes:

| Code | Meaning |
|------|---------|
| `PASSING` | All locations succeeding, response times normal |
| `INTERMITTENT` | Some checks failing sporadically across locations |
| `REGIONAL_FAILURE` | Specific locations consistently failing |
| `GLOBAL_FAILURE` | All locations failing — likely a service outage |
| `DEGRADED_PERFORMANCE` | Checks passing but response times elevated |

### APM Correlation

`investigate_synthetic` cross-references monitor failures with APM data:

- **Global failure + APM errors** → Service-side root cause
- **Global failure + APM healthy** → Network/DNS/CDN issue
- **Regional failure** → Regional infrastructure problem
- **Degraded performance + APM latency** → Upstream dependency slowdown

### Fuzzy Monitor Resolution

Monitor names are resolved with a **0.5 threshold** using token overlap matching. This is more lenient than service resolution (0.6) because monitor names tend to be more descriptive:

```
"login flow" → "Login Flow"          ✓ (exact, case-insensitive)
"API Health"  → "API Health Check"    ✓ (token overlap)
"checkout"    → "Checkout Flow"       ✓ (fuzzy match)
"xyz random"  → MonitorNotFoundError  ✗ (suggests closest matches)
```

---

## Developer Guide

### Project Structure

```
sherlock/
├── pyproject.toml              # Build config, dependencies, tool settings
├── Makefile                    # Development commands
├── .env.example                # Environment variable template
├── .gitignore
├── main.py                     # MCP server entry point (20 tools)
├── core/
│   ├── __init__.py
│   ├── exceptions.py           # Custom exceptions
│   ├── sanitize.py             # Input sanitization, fuzzy resolution, scrubbing
│   ├── credentials.py          # Credential management (keyring)
│   ├── cache.py                # Two-layer caching (memory + disk)
│   ├── context.py              # Thread-safe singleton context
│   └── intelligence.py         # Account discovery models + learn_account()
├── client/
│   ├── __init__.py
│   └── newrelic.py             # NerdGraph HTTP client (read-only, retry, batch)
├── tools/
│   ├── __init__.py
│   ├── entities.py             # Entity GUID lookup
│   ├── nrql.py                 # Raw NRQL execution
│   ├── alerts.py               # Alert policies, incidents
│   ├── apm.py                  # APM applications, metrics, deployments
│   ├── logs.py                 # Log search
│   ├── k8s.py                  # Kubernetes health
│   ├── golden_signals.py       # Golden signals (latency, errors, traffic, saturation)
│   ├── synthetics.py           # Synthetic monitoring (status, results, investigation)
│   ├── investigate.py          # Multi-source service investigation
│   └── intelligence_tools.py   # Connection, learning, profiles
├── scripts/
│   ├── test_connection.py      # Interactive connection validator
│   └── cli.py                  # Interactive CLI for all 20 tools
├── tests/
│   ├── conftest.py             # Shared fixtures (mock_credentials, mock_intelligence, etc.)
│   ├── test_credentials.py
│   ├── test_intelligence.py
│   ├── test_sanitize.py
│   ├── test_nrql.py
│   ├── test_synthetics.py      # 12 required test cases
│   ├── test_investigate.py
│   ├── test_k8s.py
│   ├── test_logs.py
│   └── test_context.py
├── profiles/
│   └── profiles.example.json
└── .vscode/
    └── settings.json           # VS Code + MCP server config
```

### Running Tests

```bash
# All tests
make test

# Fast tests (skip slow integration tests)
make test-fast

# Synthetics tests only
make test-synthetics

# With coverage
pytest --cov=. --cov-report=html tests/
```

### Linting & Formatting

```bash
# Lint
make lint

# Format
make format

# Type check
mypy . --strict
```

### Adding a New Tool

1. Create a new file in `tools/` or add to an existing one
2. Implement the async tool function with full type hints and docstring
3. Register it in `main.py` under `handle_list_tools()` and `handle_call_tool()`
4. Add tests in `tests/`
5. Update this README

### Key Design Patterns

- **All tool functions are `async`** — they use `await` for NerdGraph calls
- **Fuzzy name resolution** — use `fuzzy_resolve_service()` or `fuzzy_resolve_monitor()` for user-facing name inputs
- **Context access** — call `AccountContext().get_active()` to get credentials and intelligence
- **Error handling** — raise domain exceptions (`ServiceNotFoundError`, etc.) which are caught in `handle_call_tool()`
- **Response format** — return `json.dumps(...)` from every tool for consistent parsing

---

## Troubleshooting

### Common Issues

| Issue | Solution |
|-------|----------|
| `NotConnectedError` | Run `connect_account` first |
| `CredentialError` | Check API key format (`NRAK-...`) and region (US/EU) |
| `ServiceNotFoundError` | Verify service name with `get_apm_applications` |
| `MonitorNotFoundError` | Verify monitor name with `get_synthetic_monitors` |
| `ReadOnlyViolation` | Mutation attempted — this server is read-only by design |
| `Timeout on investigate` | Increase `INVESTIGATION_TIMEOUT_S` or check network |

### Logs

```bash
# View application logs
make logs

# View audit trail
make audit

# Log locations
ls ~/.sherlock/logs/
```

### Cache Management

```bash
# Intelligence cache is at:
ls ~/.sherlock/cache/

# Force re-learn (clears cache for active account)
# In Copilot Chat: "re-learn the account"
# → calls learn_account tool
```

---

## License

MIT
