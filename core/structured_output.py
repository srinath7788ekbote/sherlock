"""
Structured output schema for Sherlock investigation reports.

Inspired by Claude Code's SyntheticOutputTool — produces typed, machine-readable
investigation results alongside the human-readable markdown prose.

This enables downstream consumers (MTTR dashboard, Slack/Teams, ticketing)
to parse investigation results without fragile markdown parsing.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class DomainResult:
    """Result from a single domain agent."""
    status: str           # CRITICAL | WARNING | HEALTHY | NO_DATA
    finding: str          # 1-2 sentence summary
    key_metric: str = ""  # most important number
    deep_link: str = ""   # NR deep link URL


@dataclass
class Recommendation:
    """A single actionable recommendation."""
    priority: str   # P1 | P2 | P3
    action: str     # what to do
    why: str        # why it matters


@dataclass
class InvestigationReport:
    """
    Machine-readable investigation report. Parallel to the human-readable
    markdown prose — same data, structured for downstream consumption.
    """
    # Identity
    service_name: str
    bare_name: str
    namespace: str
    account_id: str
    account_name: str = ""

    # Timing
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    window_minutes: int = 60
    investigation_duration_seconds: float = 0.0

    # Verdict
    severity: str = "UNKNOWN"       # CRITICAL | WARNING | HEALTHY | UNKNOWN
    confidence: str = "LOW"         # HIGH | MEDIUM | LOW
    is_victim: bool = False
    origin_service: str = ""

    # Root cause
    root_cause: str = ""
    causal_chain: str = ""
    causal_pattern: str = "NONE"

    # Key metrics
    error_rate: Optional[float] = None
    latency_p95_ms: Optional[float] = None
    throughput_rpm: Optional[float] = None
    is_otel: bool = False

    # Domain results
    domains: dict = field(default_factory=dict)

    # Recommendations
    recommendations: list = field(default_factory=list)

    # Alerts
    open_incident_ids: list = field(default_factory=list)
    chronic_flag: bool = False
    stale_signal_flag: bool = False

    # Session context
    retry_count: int = 0
    cross_account_entities: list = field(default_factory=list)
    escalation_mode: bool = False

    def to_json(self, indent: int = 2) -> str:
        """Serialize to JSON string."""
        return json.dumps(asdict(self), indent=indent, default=str)

    def to_session_snapshot_fields(self) -> dict:
        """
        Extract fields needed to create a SessionMemory InvestigationSnapshot.
        Used to auto-save after investigation.
        """
        return {
            "severity": self.severity,
            "root_cause": self.root_cause,
            "causal_chain": self.causal_chain,
            "causal_pattern": self.causal_pattern,
            "error_rate": self.error_rate,
            "is_otel": self.is_otel,
            "open_incident_ids": self.open_incident_ids,
            "chronic_flag": self.chronic_flag,
            "stale_signal_flag": self.stale_signal_flag,
            "cross_account_entities": self.cross_account_entities,
        }


def build_domain_result(
    status: str,
    finding: str,
    key_metric: str = "",
    deep_link: str = "",
) -> dict:
    """Helper to build a domain result dict."""
    return asdict(DomainResult(
        status=status,
        finding=finding,
        key_metric=key_metric,
        deep_link=deep_link,
    ))


def build_recommendation(priority: str, action: str, why: str) -> dict:
    """Helper to build a recommendation dict."""
    return asdict(Recommendation(priority=priority, action=action, why=why))


def empty_report(
    service_name: str,
    account_id: str,
    window_minutes: int = 60,
) -> InvestigationReport:
    """Create an empty report with identity fields set."""
    parts = service_name.split("/", 1)
    namespace = parts[0] if len(parts) == 2 else ""
    bare_name = parts[1] if len(parts) == 2 else service_name

    return InvestigationReport(
        service_name=service_name,
        bare_name=bare_name,
        namespace=namespace,
        account_id=account_id,
        window_minutes=window_minutes,
    )
