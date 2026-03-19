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
5. [Sharing with Teammates (Private Repository)](#sharing-with-teammates-private-repository)
6. [Configuration](#configuration)
7. [Available Tools (21)](#available-tools-21)
8. [Workflows](#workflows)
9. [Security Model](#security-model)
10. [Multi-Tenant Profiles](#multi-tenant-profiles)
11. [Synthetics Deep-Dive](#synthetics-deep-dive)
12. [Service Dependencies](#service-dependencies)
13. [Developer Guide](#developer-guide)
14. [Troubleshooting](#troubleshooting)
15. [License](#license)

---

## Overview

This MCP server exposes **21 tools** that let an AI assistant query your New Relic account in real time. It learns the shape of your account on connect (APM services, OpenTelemetry services, K8s namespaces, synthetic monitors, alert policies, log partitions, infrastructure hosts, browser apps, mobile apps, workloads) so every subsequent query is precise and context-aware.

### Key Capabilities

- **Read-only by design** — every NerdGraph mutation is blocked at the client layer
- **Agent-team architecture** — 7 specialized agents + 7 skills for comprehensive investigation
- **Multi-tenant** — switch between accounts/profiles without restarting
- **Fuzzy name resolution** — typos in service or monitor names are auto-corrected
- **Prompt-injection scrubbing** — all tool output is scanned before returning to the LLM
- **Parallel data fetching** — domain agents operate concurrently for speed
- **Credential security** — API keys stored in OS keychain via `keyring`, never in plain text
- **Deep links** — every finding includes a clickable URL to the exact New Relic UI view
- **Service dependency mapping** — automatic dependency graph built from spans, logs, and naming patterns

---

## Architecture

Sherlock uses a **multi-agent team** architecture for comprehensive investigations.
A Team Lead orchestrates 6 specialist agents, each calling domain-specific MCP tools directly.

```
┌─────────────────────────────────────────────────────────────┐
│                    AI Assistant (LLM)                        │
│              (GitHub Copilot / Claude / Cursor)              │
└───────────────────────────┬─────────────────────────────────┘
                            │ stdio (MCP protocol)
┌───────────────────────────▼─────────────────────────────────┐
│                       main.py                                │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐      │
│  │ Tool Router  │  │ Audit Logger │  │ Response Scrub │      │
│  └──────┬──────┘  └──────────────┘  └────────────────┘      │
│         │                                                    │
│  ┌──────▼──────────────────────────────────────────────────┐ │
│  │                   tools/ layer                          │ │
│  │  entities │ nrql │ alerts │ apm │ logs │ k8s           │ │
│  │  golden_signals │ synthetics │ dependencies             │ │
│  │  intelligence_tools │ investigate [LEGACY]              │ │
│  └──────┬──────────────────────────────────────────────────┘ │
│         │                                                    │
│  ┌──────▼──────────────────────────────────────────────────┐ │
│  │                  core/ layer                            │ │
│  │  context │ credentials │ intelligence │ cache           │ │
│  │  sanitize │ exceptions │ deeplinks │ utils              │ │
│  │  dependency_graph │ graph_builder                       │ │
│  │  discovery [DEPRECATED] │ query_builder [DEPRECATED]    │ │
│  └──────┬──────────────────────────────────────────────────┘ │
│         │                                                    │
│  ┌──────▼──────────────────────────────────────────────────┐ │
│  │               client/ layer                             │ │
│  │  NerdGraphClient (httpx + tenacity retry)               │ │
│  │  Read-only enforcement │ Batch queries                  │ │
│  └─────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
                            │
                     NerdGraph GraphQL API
               US: https://api.newrelic.com/graphql
               EU: https://api.eu.newrelic.com/graphql
```

### Agent-Team Investigation Flow

For comprehensive "investigate service X" requests, the AI assistant uses
the agent-team pattern defined in `.github/agents/` and `.github/skills/`:

```
User: "investigate service X"
  │
  ▼
sherlock-team-lead (orchestrator)
  ├── connect_account (if needed)
  ├── learn_account (discover entity names)
  ├── Parse name: eswd-prod/sifi-adapter → bare=sifi-adapter, ns=eswd-prod
  │
  ├── PARALLEL DISPATCH ──────────────────────────────────────
  │   ├── sherlock-apm ──────→ get_service_golden_signals, get_app_metrics, get_deployments
  │   ├── sherlock-k8s ──────→ get_k8s_health, NRQL fallbacks
  │   ├── sherlock-logs ─────→ search_logs, NRQL severity distribution
  │   ├── sherlock-alerts ───→ get_service_incidents, get_incidents
  │   ├── sherlock-synthetics → get_synthetic_monitors, investigate_synthetic
  │   └── sherlock-infra ────→ get_service_dependencies, NRQL infrastructure
  │
  ▼
sherlock-team-lead (synthesize ALL results)
  │
  ▼
Unified Investigation Report (all 6 domains)
```

| Agent | Specialty | Primary Tools |
|-------|-----------|---------------|
| `sherlock-team-lead` | Orchestrator, synthesizer | `connect_account`, `learn_account`, `get_nrql_context` |
| `sherlock-apm` | Golden signals, transactions, errors, deployments | `get_service_golden_signals`, `get_app_metrics`, `get_deployments` |
| `sherlock-k8s` | Pods, containers, nodes, K8s events | `get_k8s_health`, `run_nrql_query` |
| `sherlock-logs` | Error patterns, log volume, exception analysis | `search_logs`, `run_nrql_query` |
| `sherlock-alerts` | Active incidents, alert policies, violations | `get_service_incidents`, `get_incidents`, `get_alerts` |
| `sherlock-synthetics` | Monitor health, failure locations, availability | `get_synthetic_monitors`, `get_monitor_status`, `investigate_synthetic` |
| `sherlock-infra` | Dependencies, host health, browser, messaging | `get_service_dependencies`, `run_nrql_query` |

### Layer Responsibilities

| Layer | Purpose |
|-------|---------|
| **main.py** | MCP server lifecycle, tool registration, audit logging, response scrubbing |
| **tools/** | Individual tool implementations — each file owns one domain |
| **core/** | Shared primitives — credentials, context, intelligence, cache, sanitization, deep links, utils, dependency graph |
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

#### Quick Setup (automated)

1. Clone the repository:

```powershell
cd $env:USERPROFILE\Documents
git clone <repo-url> sherlock
cd sherlock
```

2. Right-click **`setup.bat`** and select **Run as administrator**.

The script automatically installs Chocolatey, Make, Python (latest), creates the virtual environment, and installs all dependencies. It skips anything already installed.

3. Open a **new terminal** (regular, not admin), then:

```powershell
cd $env:USERPROFILE\Documents\sherlock
.venv\Scripts\activate.bat
make connect
```

This will prompt you for:
- **Account ID** — your New Relic account ID (numeric)
- **API Key** — a User API key in `NRAK-...` format ([create one here](https://one.newrelic.com/api-keys))
- **Region** — `US` or `EU`

Credentials are saved securely in Windows Credential Locker.

4. Configure MCP in your AI client (see [MCP Configuration](#mcp-configuration) below).

#### Manual Setup (step-by-step)

If you prefer to run each step yourself, or if the batch file fails:

<details>
<summary>Click to expand manual steps</summary>

**Install Chocolatey** — open PowerShell as Administrator:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
```

**Install Make and Python** — close and reopen PowerShell, then:

```powershell
choco install make -y
choco install python -y
```

**Clone, create venv, install** — close and reopen PowerShell:

```powershell
cd $env:USERPROFILE\Documents
git clone <repo-url> sherlock
cd sherlock
python -m venv .venv
.venv\Scripts\Activate.ps1
make install
```

> **Note:** If you get an execution-policy error on `Activate.ps1`, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` first.

**Connect and save profile:**

```powershell
make connect
```

</details>

#### MCP Configuration

After setup, add the Sherlock MCP server to your AI client's configuration.

**VS Code / GitHub Copilot** — add to your `settings.json` (or use the pre-configured `.vscode/settings.json` in this repo):

```json
{
  "github.copilot.chat.mcpServers": {
    "sherlock": {
      "command": "python",
      "args": ["main.py"],
      "cwd": "C:\\Users\\<your-username>\\Documents\\sherlock"
    }
  }
}
```

**Claude Desktop** — add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sherlock": {
      "command": "C:\\Users\\<your-username>\\Documents\\sherlock\\.venv\\Scripts\\python.exe",
      "args": ["main.py"],
      "cwd": "C:\\Users\\<your-username>\\Documents\\sherlock"
    }
  }
}
```

**Cursor** — add to `.cursor/mcp.json` in your project root (or global config):

```json
{
  "mcpServers": {
    "sherlock": {
      "command": "C:\\Users\\<your-username>\\Documents\\sherlock\\.venv\\Scripts\\python.exe",
      "args": ["main.py"],
      "cwd": "C:\\Users\\<your-username>\\Documents\\sherlock"
    }
  }
}
```

> **Important:** Replace `<your-username>` with your actual Windows username. Use the full path to the `.venv` Python executable so the MCP client uses the correct virtual environment.

#### Verify everything works

```powershell
# Run the MCP server directly to test
make run
# or: python main.py
```

Then open your AI client and try:

```
@sherlock list all profiles
@sherlock how is my-service performing?
```

### VS Code Integration

The server is pre-configured in `.vscode/settings.json`. After installation:

1. Open the `sherlock` folder in VS Code
2. Ensure the Python extension is installed
3. The MCP server will appear under **GitHub Copilot → MCP Servers**
4. Use `@sherlock` in Copilot Chat to interact with your telemetry

---

## Sharing with Teammates (Private Repository)

You do **not** need to make this repository public for your team to use it. GitHub supports several access-control options for private repositories.

### Option 1 — GitHub Collaborators (personal repositories)

Invite teammates directly from the repository settings:

1. Go to **Settings → Collaborators** on GitHub
2. Click **Add people** and enter each teammate's GitHub username or email
3. Each teammate accepts the invitation and can then clone the private repository:

```bash
# HTTPS (no SSH setup required)
git clone https://github.com/<your-username>/sherlock.git sherlock

# SSH (requires SSH key configured on the teammate's GitHub account)
git clone git@github.com:<your-username>/sherlock.git sherlock
```

### Option 2 — GitHub Teams (organization repositories)

If the repository is owned by a GitHub organization:

1. Go to **Settings → Collaborators and teams**
2. Add an existing team (or create one) with at least **Read** access
3. All members of that team can clone and install Sherlock without the repository being public

### Option 3 — Install from a private repository using a PAT

Teammates can install Sherlock without cloning the repo by using a [GitHub Personal Access Token (PAT)](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) with `repo` scope:

```bash
# Set the token as an environment variable to avoid storing it in shell history
export GITHUB_PAT=<PAT>
pip install "git+https://${GITHUB_PAT}@github.com/<your-username>/sherlock.git"
```

Replace `<PAT>` with the token and `<your-username>` with the repository owner.

### Option 4 — Distribute a pre-built wheel

Build a wheel and share it directly (e.g., via Slack, email, or an internal artifact store):

```bash
# Build the wheel (run once by the maintainer)
pip install build
python -m build --wheel

# Share the generated file, e.g.:
#   dist/sherlock-1.0.0-py3-none-any.whl

# Teammates install it with (run from the directory containing the .whl file):
pip install sherlock-1.0.0-py3-none-any.whl
```

> **Tip**: use Option 1 or Option 2 if you want teammates to receive future updates automatically via `git pull`. Use Option 3 or Option 4 if you prefer zero-friction installation without requiring GitHub access.

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

## Available Tools (21)

### Connection & Intelligence (5 tools)

| # | Tool | Description |
|---|------|-------------|
| 1 | `connect_account` | Connect to a New Relic account by profile name or credentials |
| 2 | `list_profiles` | List all saved credential profiles |
| 3 | `learn_account` | Re-discover account topology (APM, OTel, K8s, synthetics, alerts, etc.) |
| 4 | `get_account_summary` | Return a summary of discovered assets (APM, OTel, infra, browser, mobile, workloads) |
| 5 | `get_nrql_context` | Get NRQL query templates for a specific domain (apm, k8s, synthetics, etc.) |

### Query & Exploration (2 tools)

| # | Tool | Description |
|---|------|-------------|
| 6 | `run_nrql_query` | Execute any read-only NRQL query (includes deep link to Query Builder) |
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
| 12 | `get_incidents` | List incidents filtered by state (open/closed), includes deep links |
| 13 | `get_service_incidents` | Get incidents for a specific service (fuzzy name resolution) |

### Infrastructure & Kubernetes (1 tool)

| # | Tool | Description |
|---|------|-------------|
| 14 | `get_k8s_health` | Get K8s cluster health — pods, nodes, containers, events (with deep links) |

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
| 21 | `investigate_service` | **[LEGACY]** Quick automated check across all domains. For comprehensive investigation, use the agent-team pattern (sherlock-team-lead dispatching to all 6 domain agents) instead |

### Service Dependencies (1 tool — included in the 21 above)

| # | Tool | Description |
|---|------|-------------|
| — | `get_service_dependencies` | Get upstream and downstream service dependencies with call counts, error rates, latency, confidence scores, and health warnings |

---

## Workflows

### Quick Health Check

```
User: "How is web-api performing?"
→ Copilot calls: get_service_golden_signals("web-api")
→ Returns: latency p50/p99, error rate, throughput, saturation with threshold alerts
```

### Deep Investigation (Agent-Team)

```
User: "Investigate the checkout service — it seems slow"
→ sherlock-team-lead dispatches ALL 6 domain agents in parallel:
  → sherlock-apm: get_service_golden_signals, get_app_metrics, get_deployments
  → sherlock-k8s: get_k8s_health with namespace + deployment resolution
  → sherlock-logs: search_logs, NRQL severity distribution
  → sherlock-alerts: get_service_incidents, get_incidents
  → sherlock-synthetics: get_synthetic_monitors, get_monitor_status
  → sherlock-infra: get_service_dependencies
→ Team Lead synthesizes: unified report with findings, deep links, root cause, recommendations
```

### Service Dependency Mapping

```
User: "What does the payment service depend on?"
→ Copilot calls: get_service_dependencies("payment-service")
→ Returns: upstream callers, downstream callees, call counts, error rates, latency, health warnings
```

### Dependency Chain Investigation

```
User: "Show me what calls the auth service and what it calls"
→ Copilot calls: get_service_dependencies("auth-service", direction="both", max_depth=3)
→ Returns: full upstream/downstream dependency tree with transitive dependencies
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
├── graphs/
│   └── {account_id}.json  # Dependency graph per account
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

## Service Dependencies

### Dependency Graph

Sherlock automatically builds a service dependency graph during `connect_account`. The graph uses three discovery strategies, merged in priority order:

| Strategy | Confidence | Source |
|----------|------------|--------|
| **Span-Based** | 1.0 (highest) | `Span` event data — `peer.service.name`, `http.url` |
| **Log-Based** | 0.7 | Log error messages containing service references |
| **Inferred** | 0.4 (lowest) | Shared naming segments between services |

Higher-confidence edges override lower ones when the same dependency is detected by multiple strategies.

### Graph Persistence

The dependency graph is saved to `~/.sherlock/graphs/{account_id}.json` and reloaded on subsequent connections. The graph has a **24-hour staleness TTL** — stale graphs are still usable but flagged as stale in responses.

### Using the Dependencies Tool

```
User: "What services does checkout depend on?"
→ Copilot calls: get_service_dependencies("checkout", direction="downstream")
→ Returns: list of downstream services with call counts, error rates, latency, confidence

User: "What is calling the auth service?"
→ Copilot calls: get_service_dependencies("auth-service", direction="upstream")
→ Returns: list of upstream callers with health warnings for unhealthy dependencies
```

The `sherlock-infra` agent automatically includes dependency analysis in its investigation reports.

---

## Developer Guide

### Project Structure

```
sherlock/
├── pyproject.toml              # Build config, dependencies, tool settings
├── Makefile                    # Development commands
├── .env.example                # Environment variable template
├── .gitignore
├── main.py                     # MCP server entry point (21 tools)
├── .github/
│   ├── copilot-instructions.md # Agent-team rules, report format, anti-hallucination
│   ├── agents/                 # 7 agent definitions
│   │   ├── sherlock-team-lead.agent.md
│   │   ├── sherlock-apm.agent.md
│   │   ├── sherlock-k8s.agent.md
│   │   ├── sherlock-logs.agent.md
│   │   ├── sherlock-alerts.agent.md
│   │   ├── sherlock-synthetics.agent.md
│   │   └── sherlock-infra.agent.md
│   └── skills/                 # 7 domain skill definitions
│       ├── apm-analysis/SKILL.md
│       ├── k8s-debug/SKILL.md
│       ├── log-analysis/SKILL.md
│       ├── alerts-analysis/SKILL.md
│       ├── synthetic-debug/SKILL.md
│       ├── infra-analysis/SKILL.md
│       └── incident-triage/SKILL.md
├── core/
│   ├── __init__.py
│   ├── exceptions.py           # Custom exceptions
│   ├── sanitize.py             # Input sanitization, fuzzy resolution, scrubbing
│   ├── credentials.py          # Credential management (keyring)
│   ├── cache.py                # Two-layer caching (memory + disk)
│   ├── context.py              # Thread-safe singleton context
│   ├── intelligence.py         # Account discovery models + learn_account()
│   ├── utils.py                # Shared utilities (safe_extract_results, strip_null_timeseries)
│   ├── deeplinks.py            # New Relic deep link URL builder
│   ├── dependency_graph.py     # Dependency graph data model and persistence
│   ├── graph_builder.py        # 3-strategy dependency graph builder (spans, logs, naming)
│   ├── discovery.py            # [DEPRECATED] Data discovery engine
│   └── query_builder.py        # [DEPRECATED] Dynamic NRQL query generation
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
│   ├── k8s.py                  # Kubernetes health (direct NRQL queries)
│   ├── golden_signals.py       # Golden signals (direct NRQL queries)
│   ├── synthetics.py           # Synthetic monitoring (status, results, investigation)
│   ├── investigate.py          # [LEGACY] Monolith investigation engine
│   ├── intelligence_tools.py   # Connection, learning, profiles
│   └── dependencies.py         # Service dependency mapping
├── scripts/
│   ├── test_connection.py      # Interactive connection validator
│   └── cli.py                  # Interactive CLI for all 21 tools
├── tests/
│   ├── conftest.py             # Shared fixtures
│   ├── test_alerts.py
│   ├── test_apm.py
│   ├── test_bug_fixes.py
│   ├── test_context.py
│   ├── test_credentials.py
│   ├── test_deeplinks.py
│   ├── test_dependencies_tool.py
│   ├── test_dependency_graph.py
│   ├── test_discovery.py
│   ├── test_entities.py
│   ├── test_golden_signals.py
│   ├── test_graph_builder.py
│   ├── test_intelligence.py
│   ├── test_intelligence_tools.py
│   ├── test_investigate.py
│   ├── test_k8s.py
│   ├── test_logs.py
│   ├── test_nrql.py
│   ├── test_query_builder.py
│   ├── test_sanitize.py
│   └── test_synthetics.py
├── profiles/
│   └── profiles.example.json
└── .vscode/
    └── settings.json           # VS Code + MCP server config
```

### Running Tests

```bash
# All tests
make test

# Fast tests (stop on first failure)
make test-fast

# Synthetics tests only
make test-synthetics

# Investigation tests only
make test-investigate

# Dependency graph tests only
make test-dependencies

# Discovery engine tests only
make test-discovery

# Deep links tests only
make test-deeplinks

# With coverage report
make test-cov
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
