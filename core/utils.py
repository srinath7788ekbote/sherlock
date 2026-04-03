"""
Shared utility functions for Sherlock tools.

Extracted from tools/investigate.py to remove coupling between
domain-specific tools and the monolith investigation engine.
"""

import re as _re

from pydantic import BaseModel, Field
from datetime import datetime, timezone


def safe_extract_results(body: dict) -> list[dict]:
    """Safely navigate ``data.actor.account.nrql.results`` even when
    intermediate values are *None* rather than missing."""
    d = body if isinstance(body, dict) else {}
    for key in ("data", "actor", "account", "nrql", "results"):
        d = d.get(key) if isinstance(d, dict) else None
        if d is None:
            return []
    return d if isinstance(d, list) else []


def strip_null_timeseries(results: list[dict]) -> list[dict]:
    """Remove timeseries buckets where all metric values are null.

    NRQL TIMESERIES queries return a bucket for every time window even when
    there is no data, filling metric columns with ``None``.  For sparse
    services this produces hundreds of useless rows.  Strip them so the
    output stays compact and readable.
    """
    if not results or not isinstance(results, list):
        return results

    # Detect timeseries rows by the presence of begin/endTimeSeconds.
    ts_keys = {"beginTimeSeconds", "endTimeSeconds"}
    filtered: list[dict] = []
    for row in results:
        if not isinstance(row, dict):
            filtered.append(row)
            continue
        if not ts_keys.issubset(row.keys()):
            # Not a timeseries row — keep as-is.
            filtered.append(row)
            continue
        # Check whether every non-timestamp value is None / null.
        metric_vals = [v for k, v in row.items() if k not in ts_keys]
        if any(v is not None for v in metric_vals):
            filtered.append(row)

    # If the filtered timeseries is still very long and all metric values are
    # identical (e.g. hundreds of buckets with error_rate=0.0), compact it
    # into a short summary to keep output readable.
    if len(filtered) > 20 and all(
        isinstance(r, dict) and ts_keys.issubset(r.keys()) for r in filtered
    ):
        metric_keys = [
            k for k in filtered[0] if k not in ts_keys
        ]
        # Collect unique metric value tuples.
        unique_vals = {
            tuple(r.get(k) for k in metric_keys) for r in filtered
        }
        if len(unique_vals) == 1:
            # Every single bucket has the same metrics — summarize.
            vals = next(iter(unique_vals))
            summary: dict = {
                "summary": "constant_value",
                "bucket_count": len(filtered),
                "first_bucket": filtered[0]["beginTimeSeconds"],
                "last_bucket": filtered[-1]["endTimeSeconds"],
            }
            for k, v in zip(metric_keys, vals):
                summary[k] = v
            return [summary]

    return filtered


class IncidentPattern(BaseModel):
    """Pattern analysis across recent incidents for the same service."""

    occurrence_count: int = 0
    first_occurrence: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_occurrence: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_recurring: bool = False
    recurrence_interval_hours: float | None = None
    consistent_cause: str | None = None
    pattern_summary: str = ""


class InvestigationAnchor(BaseModel):
    """Anchors an investigation to the correct time window and service."""

    primary_service: str = ""
    all_candidates: list[str] = Field(default_factory=list)
    active_incident: dict | None = None
    recent_incidents: list[dict] = Field(default_factory=list)
    window_start: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    window_end: datetime | None = None
    since_minutes: int = 60
    until_clause: str = ""
    window_source: str = "default"
    incident_pattern: IncidentPattern | None = None


# ── Frustration / Retry Detection ────────────────────────────────────────
# Adapted from Claude Code's userPromptKeywords.ts.
# Focused on SRE/incident-response frustration language.

_FRUSTRATION_PATTERN = _re.compile(
    r'\b('
    r'wtf|wth|ffs|omfg|'
    r'still (broken|not working|failing|down)|'
    r'why is it still|why is this still|'
    r'same (issue|problem|error|thing) (again|still)|'
    r'nothing works|not working|'
    r'checked (\d+ )?times|investigated (\d+ )?times|'
    r'what the (hell|heck)|'
    r'so frustrating|this sucks|'
    r'no (data|results|output|response) (again|still)|'
    r'still (showing|returning|getting) nothing|'
    r'why (again|still|always)|'
    r'keeps (failing|breaking|returning null)|'
    r'broken again|down again|failing again'
    r')\b',
    _re.IGNORECASE,
)


def detect_frustration_signals(
    prompt: str,
    account_id: str,
    service_name: str = "",
    retry_threshold: int = 2,
    retry_window_minutes: int = 20,
) -> dict:
    """
    Detect if an engineer is in a frustration/retry loop.

    Combines two signals:
    1. Language signal: frustration keywords in the prompt (fast regex)
    2. Retry signal: same service investigated multiple times recently

    Returns a dict with:
        frustrated: bool — True if either signal fired
        language_signal: bool — frustration words detected in prompt
        retry_signal: bool — same service investigated multiple times
        retry_count: int — how many times this service was investigated recently
        prior_severities: list[str] — severities from prior investigations
        recommendation: str — what escalation mode should focus on
    """
    from core.session_memory import SessionMemory

    language_signal = bool(_FRUSTRATION_PATTERN.search(prompt)) if prompt else False

    # Retry signal from session memory
    retry_count = 0
    prior_severities: list[str] = []
    retry_signal = False

    try:
        mem = SessionMemory()
        recent = mem.get_recent(
            account_id,
            limit=10,
            max_age_minutes=retry_window_minutes,
        )
        if service_name:
            matching = [
                s for s in recent
                if (
                    service_name.lower() in s.service_name.lower()
                    or service_name.lower() in s.bare_name.lower()
                    or s.bare_name.lower() in service_name.lower()
                )
            ]
        else:
            # If no specific service, check if the LAST service was investigated
            # multiple times (engineer is repeating without naming the service)
            if recent:
                last_service = recent[0].service_name
                matching = [s for s in recent if s.service_name == last_service]
            else:
                matching = []

        retry_count = len(matching)
        prior_severities = [s.severity for s in matching]
        retry_signal = retry_count >= retry_threshold

    except Exception:
        pass  # Never let frustration detection break anything

    frustrated = language_signal or retry_signal

    # Build recommendation for escalation mode
    recommendation = ""
    if frustrated:
        hints: list[str] = []
        if retry_count >= 2:
            hints.append(f"investigated {retry_count}x recently — skip repeated queries")
        if all(s == "CRITICAL" for s in prior_severities) and len(prior_severities) >= 2:
            hints.append("consistently CRITICAL — root cause not yet found")
        if retry_count == 0 and language_signal:
            hints.append("first investigation — check cross-account entities and OTel fallback")
        recommendation = "; ".join(hints) if hints else "escalation mode activated"

    return {
        "frustrated": frustrated,
        "language_signal": language_signal,
        "retry_signal": retry_signal,
        "retry_count": retry_count,
        "prior_severities": prior_severities,
        "recommendation": recommendation,
    }
