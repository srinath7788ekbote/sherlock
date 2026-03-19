"""
Shared utility functions for Sherlock tools.

Extracted from tools/investigate.py to remove coupling between
domain-specific tools and the monolith investigation engine.
"""

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
