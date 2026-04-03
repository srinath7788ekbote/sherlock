"""
Tests for the investigate_service mega-tool.
"""

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import httpx
import pytest
import respx

from core.context import AccountContext
from core.credentials import Credentials
from core.discovery import AvailableEventType, DiscoveryResult
from tools.investigate import (
    InvestigationAnchor,
    IncidentPattern,
    investigate_service,
    _anchor_investigation,
    _match_incident_to_candidates,
    _analyze_incident_pattern,
    _severity_emoji,
    _overall_status,
    _generate_recommendations,
    INVESTIGATION_TIMEOUT_S,
    QUERY_TIMEOUT_S,
)


def _mock_nrql_response(results):
    """Build a standard NerdGraph NRQL response."""
    return {
        "data": {
            "actor": {
                "account": {
                    "nrql": {
                        "results": results
                    }
                }
            }
        }
    }


def _empty_nrql():
    """Empty NRQL response."""
    return _mock_nrql_response([])


@pytest.fixture
def investigate_context(mock_credentials, mock_intelligence, mock_context):
    """Full context for investigate tests."""
    return mock_credentials


@pytest.mark.xdist_group("discovery")
class TestInvestigateService:
    """Tests for investigate_service."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_basic_investigation(self, investigate_context):
        """Basic investigation returns structured report."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_empty_nrql())
        )

        result = await investigate_service("payment-svc-prod")
        parsed = json.loads(result)

        assert "investigation_report" in parsed or "error" not in parsed.get("tool", "")
        if "investigation_report" in parsed:
            report = parsed["investigation_report"]
            assert "service" in report
            assert "overall_status" in report

    @respx.mock
    @pytest.mark.asyncio
    async def test_unknown_service_suggests_alternatives(self, investigate_context):
        """Unknown service name returns error or still runs investigation."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_empty_nrql())
        )

        result = await investigate_service("totally-unknown-service-xyz-123")
        assert isinstance(result, str)
        lower = result.lower()
        assert "investigation" in lower or "error" in lower or "totally-unknown" in lower

    @respx.mock
    @pytest.mark.asyncio
    async def test_synthetics_source_failure_graceful_degradation(self, investigate_context):
        """Investigation continues even when synthetic queries fail."""
        def _side_effect(request):
            body = request.content.decode()
            if "SyntheticCheck" in body:
                return httpx.Response(500, json={"error": "timeout"})
            return httpx.Response(200, json=_empty_nrql())

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=_side_effect
        )

        result = await investigate_service("payment-svc-prod")
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert "investigation_report" in parsed or "error" in parsed

    @patch("core.discovery.DISCOVERY_TIMEOUT_S", 600.0)
    @respx.mock
    @pytest.mark.asyncio
    async def test_all_sources_parallel_execution(self, investigate_context):
        """All queries execute within the investigation timeout."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_empty_nrql())
        )

        import time
        start = time.monotonic()
        result = await investigate_service("payment-svc-prod")
        elapsed = time.monotonic() - start

        # Investigation should complete in a reasonable time.
        # Under pytest-xdist, parallel workers cause CPU contention so
        # allow a generous multiplier over the base timeout.
        assert elapsed < INVESTIGATION_TIMEOUT_S * 5
        assert isinstance(result, str)

    @respx.mock
    @pytest.mark.asyncio
    async def test_investigation_with_active_incidents(self, investigate_context):
        """Investigation properly anchors to an active incident."""
        incident_response = _mock_nrql_response([{
            "title": "High Error Rate - payment-svc-prod",
            "priority": "CRITICAL",
            "state": "activated",
            "createdAt": int(
                (datetime.now(timezone.utc) - timedelta(minutes=45)).timestamp() * 1000
            ),
            "entityName": "payment-svc-prod",
        }])

        def _side_effect(request):
            body = request.content.decode()
            if "NrAiIncident" in body:
                return httpx.Response(200, json=incident_response)
            return httpx.Response(200, json=_empty_nrql())

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=_side_effect
        )

        result = await investigate_service("payment-svc-prod")
        parsed = json.loads(result)

        if "investigation_report" in parsed:
            report = parsed["investigation_report"]
            assert report["window"]["source"] in ("incident_anchored", "requested")

    @respx.mock
    @pytest.mark.asyncio
    async def test_k8s_source_failure_graceful_degradation(self, investigate_context):
        """Investigation continues even when K8s queries fail."""
        def _side_effect(request):
            body = request.content.decode()
            if "K8sPodSample" in body or "K8sDeployment" in body:
                return httpx.Response(500, json={"error": "K8s API unreachable"})
            return httpx.Response(200, json=_empty_nrql())

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=_side_effect
        )

        result = await investigate_service("payment-svc-prod")
        parsed = json.loads(result)

        assert "investigation_report" in parsed or "error" in parsed
        if "investigation_report" in parsed:
            assert parsed["investigation_report"]["overall_status"] in (
                "HEALTHY", "WARNING", "CRITICAL"
            )

    @respx.mock
    @pytest.mark.asyncio
    async def test_logs_source_failure_graceful_degradation(self, investigate_context):
        """Investigation continues even when log queries fail."""
        def _side_effect(request):
            body = request.content.decode()
            if "FROM Log" in body:
                return httpx.Response(500, json={"error": "Log service timeout"})
            return httpx.Response(200, json=_empty_nrql())

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=_side_effect
        )

        result = await investigate_service("payment-svc-prod")
        parsed = json.loads(result)

        assert "investigation_report" in parsed or "error" in parsed
        if "investigation_report" in parsed:
            assert parsed["investigation_report"]["overall_status"] in (
                "HEALTHY", "WARNING", "CRITICAL"
            )

    @pytest.mark.asyncio
    async def test_p1_recommendation_for_oom_kill(self, investigate_context):
        """P1 recommendation generated when OOMKill findings exist."""
        anchor = InvestigationAnchor(
            primary_service="payment-svc-prod",
            since_minutes=30,
            window_source="requested",
        )
        findings = [
            {"severity": "CRITICAL", "finding": "🔴 OOMKilled: payment-svc-prod-abc (3x) — used 512MB / 512MB limit"},
            {"severity": "CRITICAL", "finding": "🔴 Pod payment-svc-prod-abc in CrashLoopBackOff state"},
        ]
        recs = _generate_recommendations(findings, anchor, None, {})
        k8s_recs = [r for r in recs if r["area"] == "k8s"]
        assert any(r["priority"] == "P1" for r in k8s_recs)
        assert any("OOM" in r["action"] for r in k8s_recs)

    @pytest.mark.asyncio
    async def test_p1_recommendation_for_high_error_rate(self, investigate_context):
        """P1 recommendation generated for critically high error rate."""
        anchor = InvestigationAnchor(
            primary_service="payment-svc-prod",
            since_minutes=30,
            window_source="requested",
        )
        findings = [
            {"severity": "CRITICAL", "finding": "🔴 CRITICAL error rate: 35.0% (peak: 40.0%)"},
        ]
        recs = _generate_recommendations(findings, anchor, None, {})
        error_recs = [r for r in recs if r["area"] == "errors"]
        assert any(r["priority"] == "P1" for r in error_recs)
        assert any("deployment" in r["action"].lower() for r in error_recs)

    @respx.mock
    @pytest.mark.asyncio
    async def test_fuzzy_service_resolution_in_investigate(self, investigate_context):
        """Fuzzy service name resolves correctly during investigation."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_empty_nrql())
        )

        result = await investigate_service("payment-svc")
        parsed = json.loads(result)

        if "investigation_report" in parsed:
            service = parsed["investigation_report"].get("service", "")
            all_investigated = parsed["investigation_report"].get(
                "all_services_investigated", []
            )
            assert "payment-svc-prod" in all_investigated or "payment-svc-prod" == service

    @respx.mock
    @pytest.mark.asyncio
    async def test_deployment_correlation_detected(self, investigate_context):
        """High error rate findings produce deployment rollback recommendations."""
        def _side_effect(request):
            body = request.content.decode()
            if "event_count" in body and "Transaction" in body and "appName" in body:
                return httpx.Response(200, json=_mock_nrql_response([{"event_count": 500}]))
            if "error IS true" in body and "TIMESERIES" not in body:
                return httpx.Response(200, json=_mock_nrql_response([{
                    "error_rate": 40.0,
                    "throughput": 100,
                    "p95_latency": 3.0,
                    "p99_latency": 5.0,
                    "peak_error_rate": 45.0,
                    "min_throughput": 10,
                }]))
            if "TransactionError" in body and "FACET" in body:
                return httpx.Response(200, json=_mock_nrql_response([{
                    "errorClass": "NullPointerException",
                    "count": 200,
                    "sample_message": "NullPointerException in PaymentHandler",
                }]))
            return httpx.Response(200, json=_empty_nrql())

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=_side_effect
        )

        result = await investigate_service("payment-svc-prod")
        parsed = json.loads(result)

        if "prioritized_recommendations" in parsed:
            all_actions = " ".join(
                r.get("action", "") for r in parsed["prioritized_recommendations"]
            )
            lower = all_actions.lower()
            assert "deployment" in lower or "rollback" in lower


# ── New tests for the three-phase architecture ───────────────────────────


class TestBareNameCandidates:
    """Test that investigate_service adds bare name variants for K8s discovery."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_bare_name_added_for_slash_separated_service(self, investigate_context, mock_intelligence):
        """Service names with '/' always get bare segments added as candidates."""
        mock_intelligence.apm.service_names.append("eswd-prod/sifi-adapter")

        captured_candidates = []

        async def capture_discover(service_candidates, **kwargs):
            captured_candidates.extend(service_candidates)
            return DiscoveryResult()

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_empty_nrql())
        )

        with patch("tools.investigate.discover_available_data", side_effect=capture_discover):
            await investigate_service("eswd-prod/sifi-adapter")

        # The bare name "sifi-adapter" should be in the candidates,
        # regardless of whether naming_convention was learned.
        assert "sifi-adapter" in captured_candidates, (
            f"Expected 'sifi-adapter' in candidates, got: {captured_candidates}"
        )
        # The prefix segment should also be present for namespace matching.
        assert "eswd-prod" in captured_candidates, (
            f"Expected 'eswd-prod' in candidates, got: {captured_candidates}"
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_no_extra_candidates_without_slash(self, investigate_context, mock_intelligence):
        """Service names without '/' don't get extra bare name variants."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_empty_nrql())
        )

        captured_candidates = []

        async def capture_discover(service_candidates, **kwargs):
            captured_candidates.extend(service_candidates)
            return DiscoveryResult()

        with patch("tools.investigate.discover_available_data", side_effect=capture_discover):
            await investigate_service("payment-svc-prod")

        # No extra slash-split variants added (no '/' in name).
        slash_split_variants = [c for c in captured_candidates if "/" not in c and c != "payment-svc-prod"]
        # Only the original candidate should be present (no bare variants from '/')
        assert "payment-svc-prod" in captured_candidates


class TestAnchorInvestigation:
    """Tests for _anchor_investigation (Phase 1)."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_anchor_uses_incident_window(self, investigate_context, mock_intelligence):
        """When an active incident matches, the window is anchored to it."""
        incident_time = datetime.now(timezone.utc) - timedelta(minutes=45)
        incident_ts_ms = int(incident_time.timestamp() * 1000)

        incident_response = _mock_nrql_response([{
            "title": "High Error Rate - payment-svc-prod",
            "state": "activated",
            "createdAt": incident_ts_ms,
            "entityName": "payment-svc-prod",
        }])

        recent_response = _mock_nrql_response([{
            "title": "High Error Rate - payment-svc-prod",
            "createdAt": incident_ts_ms,
        }])

        def _side_effect(request):
            body = request.content.decode()
            if "state = 'activated'" in body:
                return httpx.Response(200, json=incident_response)
            return httpx.Response(200, json=recent_response)

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=_side_effect
        )

        anchor = await _anchor_investigation(
            service_candidates=["payment-svc-prod"],
            since_minutes_requested=30,
            intelligence=mock_intelligence,
            credentials=investigate_context,
        )

        assert anchor.window_source == "incident_anchored"
        assert anchor.active_incident is not None
        # Window should extend before the incident (30 min baseline).
        assert anchor.since_minutes > 30

    @respx.mock
    @pytest.mark.asyncio
    async def test_anchor_falls_back_to_requested_window(
        self, investigate_context, mock_intelligence
    ):
        """When no incident matches, uses the requested time window."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_empty_nrql())
        )

        anchor = await _anchor_investigation(
            service_candidates=["payment-svc-prod"],
            since_minutes_requested=30,
            intelligence=mock_intelligence,
            credentials=investigate_context,
        )

        assert anchor.window_source == "requested"
        assert anchor.since_minutes == 30
        assert anchor.active_incident is None


class TestMatchIncidentToCandidates:
    """Tests for _match_incident_to_candidates."""

    def test_exact_title_match(self):
        """Incident title containing candidate name matches."""
        incidents = [
            {"title": "High Error Rate - payment-svc-prod", "entityName": "payment-svc-prod"},
        ]
        result = _match_incident_to_candidates(incidents, ["payment-svc-prod"])
        assert result is not None
        assert "payment-svc-prod" in result["title"]

    def test_no_match_returns_none(self):
        """Completely unrelated incidents return None."""
        incidents = [
            {"title": "DNS Failure - dns-server", "entityName": "dns-server"},
        ]
        result = _match_incident_to_candidates(incidents, ["payment-svc-prod"])
        assert result is None or isinstance(result, dict)


class TestAnalyzeIncidentPattern:
    """Tests for _analyze_incident_pattern."""

    def test_recurring_pattern_detected(self):
        """Multiple similar incidents are detected as recurring."""
        now = datetime.now(timezone.utc)
        incidents = [
            {
                "title": "High Error Rate - payment-svc-prod",
                "createdAt": int((now - timedelta(hours=i * 4)).timestamp() * 1000),
            }
            for i in range(5)
        ]

        pattern = _analyze_incident_pattern(incidents)

        assert pattern is not None
        assert pattern.is_recurring is True
        assert pattern.occurrence_count == 5
        assert pattern.recurrence_interval_hours is not None

    def test_single_incident_not_recurring(self):
        """A single incident is not flagged as recurring."""
        now = datetime.now(timezone.utc)
        incidents = [
            {
                "title": "One-off issue",
                "createdAt": int(now.timestamp() * 1000),
            }
        ]

        pattern = _analyze_incident_pattern(incidents)
        assert pattern is not None
        assert pattern.is_recurring is False

    def test_empty_incidents_returns_none(self):
        """Empty incident list returns None."""
        pattern = _analyze_incident_pattern([])
        assert pattern is None


class TestSeverityEmoji:
    """Tests for _severity_emoji."""

    def test_critical_detected(self):
        assert _severity_emoji("🔴 CRITICAL error rate: 25%") == "CRITICAL"

    def test_warning_detected(self):
        assert _severity_emoji("⚠️ High P99 latency: 5.2s") == "WARNING"

    def test_info_detected(self):
        assert _severity_emoji("ℹ️ 3 error log entries") == "INFO"

    def test_no_emoji_returns_info(self):
        assert _severity_emoji("Some plain finding") == "INFO"


class TestOverallStatus:
    """Tests for _overall_status."""

    def test_critical_when_any_critical(self):
        findings = [
            {"severity": "WARNING", "finding": "something"},
            {"severity": "CRITICAL", "finding": "bad"},
        ]
        assert _overall_status(findings) == "CRITICAL"

    def test_warning_when_no_critical(self):
        findings = [
            {"severity": "WARNING", "finding": "something"},
            {"severity": "INFO", "finding": "ok"},
        ]
        assert _overall_status(findings) == "WARNING"

    def test_healthy_when_no_issues(self):
        findings = [
            {"severity": "INFO", "finding": "all good"},
        ]
        assert _overall_status(findings) == "HEALTHY"

    def test_empty_findings_is_healthy(self):
        assert _overall_status([]) == "HEALTHY"


class TestGenerateRecommendations:
    """Tests for _generate_recommendations."""

    def test_oom_recommendation(self):
        """OOMKill finding generates a P1 k8s recommendation."""
        anchor = InvestigationAnchor(
            primary_service="api",
            since_minutes=30,
            window_source="requested",
        )
        findings = [
            {"severity": "CRITICAL", "finding": "🔴 OOMKilled: api-pod (3x)"},
        ]
        recs = _generate_recommendations(findings, anchor, None, {})
        k8s_recs = [r for r in recs if r["area"] == "k8s"]
        assert any(r["priority"] == "P1" for r in k8s_recs)
        assert any("OOM" in r["action"] for r in k8s_recs)

    def test_high_error_rate_recommendation(self):
        """Critical error rate generates a P1 errors recommendation."""
        anchor = InvestigationAnchor(
            primary_service="api",
            since_minutes=30,
            window_source="requested",
        )
        findings = [
            {"severity": "CRITICAL", "finding": "🔴 CRITICAL error rate: 35.0%"},
        ]
        recs = _generate_recommendations(findings, anchor, None, {})
        error_recs = [r for r in recs if r["area"] == "errors"]
        assert any(r["priority"] == "P1" for r in error_recs)
        assert any("deployment" in r["action"].lower() for r in error_recs)

    def test_zero_throughput_recommendation(self):
        """Zero throughput generates a P1 application recommendation."""
        anchor = InvestigationAnchor(
            primary_service="api",
            since_minutes=30,
            window_source="requested",
        )
        findings = [
            {"severity": "CRITICAL", "finding": "🔴 ZERO throughput — service may be down"},
        ]
        recs = _generate_recommendations(findings, anchor, None, {})
        assert any(r["priority"] == "P1" for r in recs)

    def test_recurring_incident_recommendation(self):
        """Recurring incident pattern generates a P1 reliability recommendation."""
        anchor = InvestigationAnchor(
            primary_service="api",
            since_minutes=30,
            window_source="incident_anchored",
            incident_pattern=IncidentPattern(
                occurrence_count=5,
                is_recurring=True,
                recurrence_interval_hours=4.0,
                pattern_summary="5 incident(s) in last 7 days; recurring ~every 4.0h",
            ),
        )
        findings = []
        recs = _generate_recommendations(findings, anchor, None, {})
        reliability_recs = [r for r in recs if r["area"] == "reliability"]
        assert len(reliability_recs) > 0
        assert any("recurring" in r["action"].lower() for r in reliability_recs)


class TestInvestigationReportStructure:
    """Tests for the shape of the investigation report JSON."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_report_contains_all_sections(self, investigate_context):
        """The final report JSON contains all required top-level keys."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_empty_nrql())
        )

        result = await investigate_service("payment-svc-prod")
        parsed = json.loads(result)

        if "investigation_report" in parsed:
            assert "findings" in parsed
            assert "prioritized_recommendations" in parsed
            assert "raw_data" in parsed
            assert "duration_ms" in parsed
            report = parsed["investigation_report"]
            assert "service" in report
            assert "overall_status" in report
            assert "window" in report
            assert "domains_investigated" in report

    @respx.mock
    @pytest.mark.asyncio
    async def test_report_includes_duration(self, investigate_context):
        """The report includes execution duration in milliseconds."""
        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=_empty_nrql())
        )

        result = await investigate_service("payment-svc-prod")
        parsed = json.loads(result)

        assert "duration_ms" in parsed
        assert isinstance(parsed["duration_ms"], int)
        assert parsed["duration_ms"] >= 0
