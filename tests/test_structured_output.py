"""
Tests for the structured output schema and helpers.

Covers InvestigationReport, DomainResult, Recommendation dataclasses
and their factory/builder functions.
"""

import json

from core.structured_output import (
    DomainResult,
    InvestigationReport,
    Recommendation,
    build_domain_result,
    build_recommendation,
    empty_report,
)


class TestEmptyReport:
    """Tests for the empty_report factory function."""

    def test_sets_identity_fields_with_slash(self):
        """Slash-separated name is split into namespace and bare_name."""
        r = empty_report("eswd-prod/client-service", "3007677", window_minutes=60)
        assert r.service_name == "eswd-prod/client-service"
        assert r.bare_name == "client-service"
        assert r.namespace == "eswd-prod"
        assert r.account_id == "3007677"
        assert r.window_minutes == 60

    def test_no_namespace_without_slash(self):
        """Service name without a slash has empty namespace."""
        r = empty_report("payment-svc", "123456")
        assert r.bare_name == "payment-svc"
        assert r.namespace == ""
        assert r.service_name == "payment-svc"

    def test_defaults_are_sane(self):
        """Verify default values on the report."""
        r = empty_report("svc", "1")
        assert r.severity == "UNKNOWN"
        assert r.confidence == "LOW"
        assert r.causal_pattern == "NONE"
        assert r.error_rate is None
        assert r.latency_p95_ms is None
        assert r.throughput_rpm is None
        assert r.is_otel is False
        assert r.domains == {}
        assert r.recommendations == []
        assert r.open_incident_ids == []
        assert r.chronic_flag is False
        assert r.stale_signal_flag is False
        assert r.retry_count == 0
        assert r.cross_account_entities == []
        assert r.escalation_mode is False


class TestSerialization:
    """Tests for JSON serialization."""

    def test_to_json_produces_valid_json(self):
        """to_json() returns a parseable JSON string."""
        r = empty_report("ns/svc", "42")
        result = json.loads(r.to_json())
        assert isinstance(result, dict)

    def test_full_json_contains_all_top_level_keys(self):
        """Serialized JSON contains every field from the dataclass."""
        r = empty_report("ns/svc", "42")
        r.severity = "CRITICAL"
        r.root_cause = "DB cascade"
        r.causal_pattern = "DB_CASCADE"
        r.error_rate = 12.4
        r.domains = {
            "apm": build_domain_result("CRITICAL", "High error rate", "12.4%"),
        }
        r.recommendations = [
            build_recommendation("P1", "Fix DB pool", "Pool exhausted"),
        ]

        parsed = json.loads(r.to_json())

        expected_keys = {
            "service_name", "bare_name", "namespace", "account_id",
            "account_name", "timestamp", "window_minutes",
            "investigation_duration_seconds", "severity", "confidence",
            "is_victim", "origin_service", "root_cause", "causal_chain",
            "causal_pattern", "error_rate", "latency_p95_ms",
            "throughput_rpm", "is_otel", "domains", "recommendations",
            "open_incident_ids", "chronic_flag", "stale_signal_flag",
            "retry_count", "cross_account_entities", "escalation_mode",
        }
        assert expected_keys.issubset(parsed.keys())

    def test_serialized_values_match(self):
        """Key values survive the JSON round-trip."""
        r = empty_report("eswd-prod/sifi-adapter", "3007677")
        r.severity = "WARNING"
        r.error_rate = 5.5
        r.is_otel = True

        parsed = json.loads(r.to_json())
        assert parsed["severity"] == "WARNING"
        assert parsed["error_rate"] == 5.5
        assert parsed["is_otel"] is True
        assert parsed["bare_name"] == "sifi-adapter"


class TestSessionSnapshotFields:
    """Tests for to_session_snapshot_fields()."""

    def test_returns_required_keys(self):
        """Snapshot fields contain the keys SessionMemory expects."""
        r = empty_report("ns/svc", "1")
        r.severity = "HEALTHY"
        r.root_cause = "none"
        r.causal_pattern = "NONE"
        r.error_rate = 0.01
        r.is_otel = False

        fields = r.to_session_snapshot_fields()
        required = {
            "severity", "root_cause", "causal_chain", "causal_pattern",
            "error_rate", "is_otel", "open_incident_ids", "chronic_flag",
            "stale_signal_flag", "cross_account_entities",
        }
        assert required == set(fields.keys())

    def test_values_propagated(self):
        """Values set on the report appear in extracted snapshot fields."""
        r = empty_report("ns/svc", "1")
        r.severity = "CRITICAL"
        r.chronic_flag = True
        r.open_incident_ids = ["INC-1", "INC-2"]

        fields = r.to_session_snapshot_fields()
        assert fields["severity"] == "CRITICAL"
        assert fields["chronic_flag"] is True
        assert fields["open_incident_ids"] == ["INC-1", "INC-2"]


class TestBuildDomainResult:
    """Tests for build_domain_result() helper."""

    def test_produces_correct_dict(self):
        """Returns a dict with all DomainResult fields."""
        d = build_domain_result("WARNING", "Elevated errors", "5.2%", "https://nr.link")
        assert d == {
            "status": "WARNING",
            "finding": "Elevated errors",
            "key_metric": "5.2%",
            "deep_link": "https://nr.link",
        }

    def test_defaults_for_optional_fields(self):
        """key_metric and deep_link default to empty string."""
        d = build_domain_result("HEALTHY", "All good")
        assert d["key_metric"] == ""
        assert d["deep_link"] == ""


class TestBuildRecommendation:
    """Tests for build_recommendation() helper."""

    def test_produces_correct_dict(self):
        """Returns a dict with all Recommendation fields."""
        r = build_recommendation("P1", "Restart the pod", "OOMKilled 3 times")
        assert r == {
            "priority": "P1",
            "action": "Restart the pod",
            "why": "OOMKilled 3 times",
        }
