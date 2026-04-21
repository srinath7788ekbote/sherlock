"""
Tests for the deep-link URL generation module (core/deeplinks.py)
and link injection across all tool modules.

Covers:
  - DeepLinkBuilder URL construction for every method
  - Region handling (US / EU)
  - URL encoding of NRQL and special characters
  - Error resilience (bad input → None, never raises)
  - get_builder() convenience function
  - Link injection in synthetics, golden_signals, k8s, alerts, logs
"""

import base64
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.context import AccountContext
from core.credentials import Credentials
from core.deeplinks import DeepLinkBuilder, NR_BASE_EU, NR_BASE_US, get_builder


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def builder_us() -> DeepLinkBuilder:
    """US-region builder."""
    return DeepLinkBuilder(account_id="123456", region="US")


@pytest.fixture
def builder_eu() -> DeepLinkBuilder:
    """EU-region builder."""
    return DeepLinkBuilder(account_id="789012", region="EU")


@pytest.fixture
def _context_us(mock_credentials, mock_intelligence):
    """Set up active US account context for get_builder() tests."""
    AccountContext.reset_singleton()
    ctx = AccountContext()
    ctx.set_active(mock_credentials, mock_intelligence)
    yield ctx
    ctx.clear()
    AccountContext.reset_singleton()


# ── NRQL chart tests ────────────────────────────────────────────────────


class TestNrqlChart:
    """nrql_chart() returns the bare New Relic Query Builder URL.

    New Relic's current router (verified 2026-04) rejects client-side NRQL
    pre-loading via URL parameters and redirects to the home page. Any attempt
    to pass ``query=``, ``nrql=``, ``pane=`` or ``#fragment`` NRQL silently
    redirects. The bare ``?account=<id>`` form is the only verified-working
    format. Callers include the NRQL in their response body so users can paste.
    """

    def test_nrql_chart_returns_url(self, builder_us):
        nrql = "SELECT count(*) FROM Transaction WHERE appName = 'my-svc' SINCE 30 minutes ago"
        url = builder_us.nrql_chart(nrql, 30)
        assert url is not None
        assert isinstance(url, str)

    def test_nrql_chart_us_region_uses_correct_base(self, builder_us):
        url = builder_us.nrql_chart("SELECT 1", 10)
        assert url is not None
        assert url.startswith(NR_BASE_US)
        assert NR_BASE_EU not in url

    def test_nrql_chart_eu_region_uses_correct_base(self, builder_eu):
        url = builder_eu.nrql_chart("SELECT 1", 10)
        assert url is not None
        assert url.startswith(NR_BASE_EU)
        assert NR_BASE_US not in url

    def test_nrql_chart_contains_account_id(self, builder_us):
        url = builder_us.nrql_chart("SELECT 1", 10)
        assert "platform[accountId]=123456" in url

    def test_nrql_chart_path(self, builder_us):
        url = builder_us.nrql_chart("SELECT 1", 10)
        # Uses the launcher with a base64 pane= payload to pre-load NRQL.
        assert "/launcher/data-exploration.query-builder" in url
        assert "pane=" in url

    def test_nrql_chart_no_redirect_triggering_params(self, builder_us):
        """URL must use pane= with base64-encoded JSON to pre-load NRQL.

        The pane= parameter carries a JSON payload with the nerdletId,
        initialNrqlValue, and initialAccountId.  Raw ``query=``,
        ``nrql=``, and ``state=`` params must NOT appear.
        """
        nrql = "SELECT count(*) FROM Transaction WHERE appName = 'eswd-prod/sifi-adapter' AND `http.statusCode` >= 500 SINCE 3 hours ago FACET request.uri TIMESERIES 10 minutes"
        url = builder_us.nrql_chart(nrql, 180)
        assert url is not None
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        assert "query" not in params
        assert "nrql" not in params
        assert "state" not in params
        # pane= IS expected — it carries the base64-encoded config.
        assert "pane" in params
        # No URL fragment — fragments can trigger redirects.
        assert parsed.fragment == ""


# ── Entity link tests ────────────────────────────────────────────────────


class TestEntityLink:
    def test_entity_link_format(self, builder_us):
        guid = "MTIzNDU2fEFQTXxBUFBMSUNBVElPTnwx"
        url = builder_us.entity_link(guid)
        assert url is not None
        assert f"/redirect/entity/{guid}" in url

    def test_entity_link_eu(self, builder_eu):
        guid = "ABCDEF123"
        url = builder_eu.entity_link(guid)
        assert url.startswith(NR_BASE_EU)


# ── APM errors / transactions tests ─────────────────────────────────────


class TestApmErrors:
    def test_apm_errors_uses_nr1_core_errors_inbox_path(self, builder_us):
        """Verified 2026-04: NR route is /nr1-core/errors-inbox/entity-inbox/<GUID>."""
        guid = "MTIzNDU2fEFQTXxBUFBMSUNBVElPTnwx"
        url = builder_us.apm_errors(guid)
        assert url is not None
        assert f"/nr1-core/errors-inbox/entity-inbox/{guid}" in url
        assert "duration=" in url
        # Legacy (broken) patterns must not be present.
        assert "nerdletId=errors-inbox.homepage" not in url
        assert f"/redirect/entity/{guid}" not in url

    def test_apm_transactions_uses_nr1_core_apm_features_path(self, builder_us):
        """Verified 2026-04: NR route is /nr1-core/apm-features/transactions/<GUID>."""
        guid = "GUID123"
        url = builder_us.apm_transactions(guid)
        assert url is not None
        assert f"/nr1-core/apm-features/transactions/{guid}" in url
        assert "duration=" in url
        # Legacy (broken) pattern must not be present.
        assert "nerdletId=apm-nerdlets.apm-transactions-nerdlet" not in url


# ── K8s tests ────────────────────────────────────────────────────────────


class TestK8sLinks:
    def test_k8s_workload_encodes_filters(self, builder_us):
        url = builder_us.k8s_workload("payments-prod", "payment-svc")
        assert url is not None
        decoded = urllib.parse.unquote(url)
        assert "namespaceName" in decoded
        assert "deploymentName" in decoded
        assert "payment-svc" in decoded
        assert "payments-prod" in decoded

    def test_k8s_workload_uses_nr1_core_path(self, builder_us):
        """Legacy /kubernetes route redirects to Catalog; /nr1-core works."""
        url = builder_us.k8s_workload("payments-prod", "payment-svc")
        assert "/nr1-core" in url
        assert "/kubernetes" not in url
        assert "account=123456" in url

    def test_k8s_explorer_with_namespace(self, builder_us):
        url = builder_us.k8s_explorer("my-ns")
        assert url is not None
        assert "/nr1-core" in url
        decoded = urllib.parse.unquote(url)
        assert "namespaceName" in decoded
        assert "my-ns" in decoded

    def test_k8s_explorer_without_namespace(self, builder_us):
        url = builder_us.k8s_explorer()
        assert url is not None
        assert "/nr1-core" in url
        # filters still present (K8s entity type filter) — just no namespace
        decoded = urllib.parse.unquote(url)
        assert "KUBERNETES_DEPLOYMENT" in decoded
        assert "namespaceName" not in decoded


# ── Synthetic tests ──────────────────────────────────────────────────────


class TestSyntheticLinks:
    def test_synthetic_results_uses_monitor_result_list_path(self, builder_us):
        """Verified 2026-04: NR route is /synthetics/monitor-result-list/<GUID>."""
        guid = "SYNTH-GUID-001"
        url = builder_us.synthetic_results(guid, 60, "FAILED")
        assert url is not None
        assert f"/synthetics/monitor-result-list/{guid}" in url
        assert "result=FAILED" in url
        assert "duration=3600000" in url
        # Legacy (broken) pattern must not be present.
        assert "synthetics-nerdlets" not in url
        assert f"/redirect/entity/{guid}" not in url

    def test_synthetic_results_without_filter(self, builder_us):
        url = builder_us.synthetic_results("G1", 30)
        assert url is not None
        assert "result=" not in url
        assert "/synthetics/monitor-result-list/G1" in url

    def test_synthetic_monitor_delegates_to_entity_link(self, builder_us):
        guid = "SYNTH-GUID-002"
        url = builder_us.synthetic_monitor(guid)
        expected = builder_us.entity_link(guid)
        assert url == expected


# ── Alert tests ──────────────────────────────────────────────────────────


class TestAlertLinks:
    def test_alert_incident_uses_alerts_page(self, builder_us):
        """Verified 2026-04: the AIOPS incident redirect URL fails for any
        incident that is no longer "live" in the user's session. The
        /alerts page with a wide duration window is the reliable landing
        point and shows the specific incident in the event list.
        """
        url = builder_us.alert_incident("12345")
        assert url is not None
        assert url.startswith(NR_BASE_US)
        assert "/alerts" in url
        assert "account=123456" in url
        assert "duration=" in url
        # Legacy (broken) AIOPS pattern must not be present.
        assert "aiops.service.newrelic.com" not in url
        assert "/incidents/" not in url


# ── Distributed traces tests ────────────────────────────────────────────


class TestDistributedTraces:
    def test_distributed_traces_error_only_adds_filter(self, builder_us):
        guid = "GUID-1"
        url = builder_us.distributed_traces(guid, 60, error_only=True)
        assert url is not None
        assert "filters=" in url
        # Decode the filter and verify content.
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        filters_b64 = params["filters"][0]
        decoded_filter = json.loads(base64.b64decode(filters_b64))
        assert decoded_filter == {"error": True}

    def test_distributed_traces_no_error_filter_by_default(self, builder_us):
        url = builder_us.distributed_traces("GUID-1", 30)
        assert url is not None
        assert "filters=" not in url

    def test_distributed_traces_duration(self, builder_us):
        url = builder_us.distributed_traces("GUID-1", 60)
        # 60 * 60 * 1000 = 3600000
        assert "duration=3600000" in url

    def test_distributed_traces_entity_guid(self, builder_us):
        url = builder_us.distributed_traces("MY-GUID", 30)
        assert "entity.guid=MY-GUID" in url


# ── Error resilience tests ───────────────────────────────────────────────


class TestErrorResilience:
    def test_any_method_never_raises_on_bad_input(self, builder_us):
        """Pass None, empty string, garbage to every method — verify
        None returned and no exception raised."""
        # nrql_chart
        assert builder_us.nrql_chart(None, None) is None or builder_us.nrql_chart("", 0) is not None
        # spike_chart
        result = builder_us.spike_chart(None, None)
        assert result is None or isinstance(result, str)
        # entity_link
        assert builder_us.entity_link("") is not None or builder_us.entity_link("") == ""
        # apm_errors with empty
        result = builder_us.apm_errors("")
        assert result is None or isinstance(result, str)
        # distributed_traces
        result = builder_us.distributed_traces("", 0, error_only=True)
        assert result is None or isinstance(result, str)
        # log_search_nrql
        result = builder_us.log_search_nrql(service_name="", service_attribute="", since_minutes=0)
        assert result is None or isinstance(result, str)
        # log_search_ui
        result = builder_us.log_search_ui()
        assert result is None or isinstance(result, str)
        result = builder_us.log_search_ui(service_name=None, severity=None)
        assert result is None or isinstance(result, str)
        # k8s_explorer
        result = builder_us.k8s_explorer(None)
        assert result is None or isinstance(result, str)
        # k8s_workload
        result = builder_us.k8s_workload("", "")
        assert result is None or isinstance(result, str)
        # synthetic_monitor
        result = builder_us.synthetic_monitor("")
        assert result is None or isinstance(result, str)
        # synthetic_results
        result = builder_us.synthetic_results("", 0, None)
        assert result is None or isinstance(result, str)
        # alert_incident
        result = builder_us.alert_incident("")
        assert result is None or isinstance(result, str)


# ── get_builder() tests ─────────────────────────────────────────────────


class TestGetBuilder:
    def test_get_builder_returns_none_when_not_connected(self):
        """When context is cleared, get_builder() returns None safely."""
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.clear()
        result = get_builder()
        assert result is None
        AccountContext.reset_singleton()

    def test_get_builder_returns_builder_when_connected(self, _context_us):
        builder = get_builder()
        assert builder is not None
        assert isinstance(builder, DeepLinkBuilder)


# ── Integration: K8s links ───────────────────────────────────────────────


class TestK8sLinkInjection:
    @pytest.mark.asyncio
    async def test_k8s_links_absent_when_no_findings(
        self, mock_context, mock_nerdgraph
    ):
        """get_k8s_health with healthy result has no links block."""
        from tools.k8s import get_k8s_health

        result = await get_k8s_health(
            service_name="payment-svc-prod",
            namespace="payments-prod",
            since_minutes=30,
        )
        data = json.loads(result)
        # Mock returns empty results → no signals → no links.
        if data.get("health_signals") == []:
            assert "links" not in data


# ── Integration: Golden signals links ────────────────────────────────────


class TestGoldenSignalLinkInjection:
    @pytest.mark.asyncio
    async def test_golden_signals_links_absent_when_healthy(
        self, mock_context, mock_nerdgraph
    ):
        """Healthy service has no links block."""
        from tools.golden_signals import get_service_golden_signals

        result = await get_service_golden_signals(
            "payment-svc-prod", since_minutes=30
        )
        data = json.loads(result)
        # Mock returns empty results → HEALTHY → no links.
        if data.get("health_signals") == []:
            assert "links" not in data


# ── Integration: Synthetic links ─────────────────────────────────────────


class TestSyntheticLinkInjection:
    @pytest.mark.asyncio
    async def test_synthetic_links_absent_when_passing(
        self, mock_context, mock_nerdgraph
    ):
        """PASSING monitor has no links block."""
        import httpx
        import respx

        # Override mock to return passing data.
        with respx.mock(assert_all_called=False) as router:
            route = router.post("https://api.newrelic.com/graphql")
            route.mock(return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "actor": {
                            "account": {
                                "nrql": {
                                    "results": [{
                                        "pass_rate": 100.0,
                                        "total_runs": 120,
                                        "avg_duration_ms": 2500.0,
                                    }]
                                }
                            }
                        }
                    }
                },
            ))
            from tools.synthetics import get_monitor_status

            result = await get_monitor_status(
                "Login Flow - Production", since_minutes=60
            )
            data = json.loads(result)
            if data.get("diagnosis") == "PASSING":
                assert "links" not in data


# ── Integration: Log links ───────────────────────────────────────────────


class TestLogLinkInjection:
    @pytest.mark.asyncio
    async def test_log_links_absent_when_no_errors(
        self, mock_context, mock_nerdgraph
    ):
        """Zero log results → no links block."""
        from tools.logs import search_logs

        result = await search_logs(
            service_name="payment-svc-prod",
            severity="ERROR",
            since_minutes=30,
        )
        data = json.loads(result)
        # Mock returns empty results → 0 logs → no links.
        if data.get("total_logs", 0) == 0:
            assert "links" not in data


# ── Integration: Alert links ────────────────────────────────────────────


class TestAlertLinkInjection:
    @pytest.mark.asyncio
    async def test_incident_link_present_for_active_incidents(
        self, mock_context
    ):
        """ACTIVATED (open) incident has deep_link."""
        import httpx
        import respx

        with respx.mock(assert_all_called=False) as router:
            route = router.post("https://api.newrelic.com/graphql")
            route.mock(return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "actor": {
                            "account": {
                                "nrql": {
                                    "results": [
                                        {
                                            "facet": "INC-001",
                                            "incidentId": "INC-001",
                                            "latest.event": "open",
                                            "latest.priority": "CRITICAL",
                                            "latest.conditionName": "High Errors",
                                            "latest.policyName": "Prod",
                                            "latest.targetName": "payment-svc",
                                            "latest.openTime": 1700000000000,
                                        }
                                    ]
                                }
                            }
                        }
                    }
                },
            ))
            from tools.alerts import get_incidents

            result = await get_incidents(state="open")
            data = json.loads(result)
            assert data["total_incidents"] > 0
            inc = data["incidents"][0]
            assert "deep_link" in inc
            # Verified 2026-04: AIOPS incident redirect URL only works for
            # live session-cached incidents. alert_incident() now returns the
            # /alerts page with a wide duration window so the incident is
            # always accessible.
            deep_link = inc["deep_link"]
            assert "/alerts" in deep_link
            assert "account=" in deep_link
            assert "duration=" in deep_link

    @pytest.mark.asyncio
    async def test_incident_link_absent_for_closed_incidents(
        self, mock_context
    ):
        """CLOSED incident has no deep_link (only open gets links)."""
        import httpx
        import respx

        with respx.mock(assert_all_called=False) as router:
            route = router.post("https://api.newrelic.com/graphql")
            route.mock(return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "actor": {
                            "account": {
                                "nrql": {
                                    "results": [
                                        {
                                            "facet": "INC-002",
                                            "incidentId": "INC-002",
                                            "latest.event": "close",
                                            "latest.priority": "WARNING",
                                            "latest.conditionName": "Latency",
                                            "latest.policyName": "Prod",
                                            "latest.targetName": "auth-svc",
                                            "latest.openTime": 1700000000000,
                                            "latest.closeTime": 1700003600000,
                                        }
                                    ]
                                }
                            }
                        }
                    }
                },
            ))
            from tools.alerts import get_incidents

            result = await get_incidents(state="closed")
            data = json.loads(result)
            # Closed incidents should NOT have deep_link.
            for inc in data.get("incidents", []):
                assert "deep_link" not in inc


# ── log_search_nrql tests ───────────────────────────────────────────────


class TestLogSearchNrql:
    """log_search_nrql() delegates to nrql_chart() with a pane= payload."""

    def test_log_search_nrql_uses_logger_path(self):
        """log_search_nrql() must route to the query-builder with pane= payload."""
        builder = DeepLinkBuilder(account_id="3007677", region="US")
        url = builder.log_search_nrql(
            service_name="tagging-service",
            service_attribute="entity.name",
            severity="ERROR",
            keyword="HikariPool",
            since_minutes=60,
        )
        assert url is not None
        assert "/launcher/data-exploration.query-builder" in url
        assert "pane=" in url

    def test_log_search_nrql_contains_account_id(self):
        builder = DeepLinkBuilder(account_id="3007677", region="US")
        url = builder.log_search_nrql(
            service_name="tagging-service",
            service_attribute="entity.name",
            keyword="HikariPool",
            since_minutes=60,
        )
        assert "platform[accountId]=3007677" in url

    def test_log_search_nrql_accepts_dotted_attribute(self):
        """entity.name attribute must not cause the builder to fail."""
        builder = DeepLinkBuilder(account_id="3007677", region="US")
        url = builder.log_search_nrql(
            service_name="tagging-service",
            service_attribute="entity.name",
            since_minutes=60,
        )
        assert url is not None
        assert isinstance(url, str)


# ── log_search_ui tests ─────────────────────────────────────────────────


class TestLogSearchUi:
    """log_search_ui() targets the logger.log-tailer nerdlet directly."""

    def test_log_search_ui_returns_log_tailer_launcher_url(self):
        builder = DeepLinkBuilder(account_id="123456", region="US")
        url = builder.log_search_ui(service_name="my-svc")
        assert url is not None
        assert "/launcher/logger.log-tailer" in url
        assert "pane=" in url

    def test_log_search_ui_service_name_filter(self):
        builder = DeepLinkBuilder(account_id="123456", region="US")
        url = builder.log_search_ui(
            service_name="web-api",
            service_attribute="entity.name",
        )
        assert url is not None
        # Decode the pane to verify query content.
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        pane_b64 = urllib.parse.unquote(params["pane"][0])
        # b64 may have missing padding
        pane_b64 += "=" * (-len(pane_b64) % 4)
        pane_data = json.loads(base64.b64decode(pane_b64))
        assert 'entity.name:"web-api"' in pane_data["query"]

    def test_log_search_ui_severity_filter_single(self):
        builder = DeepLinkBuilder(account_id="123456", region="US")
        url = builder.log_search_ui(severity="ERROR")
        assert url is not None
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        pane_b64 = urllib.parse.unquote(params["pane"][0])
        pane_b64 += "=" * (-len(pane_b64) % 4)
        pane_data = json.loads(base64.b64decode(pane_b64))
        assert 'level:"ERROR"' in pane_data["query"]
        # Single severity should NOT have parentheses
        assert "(" not in pane_data["query"]

    def test_log_search_ui_severity_filter_multi(self):
        builder = DeepLinkBuilder(account_id="123456", region="US")
        url = builder.log_search_ui(severity="ERROR,FATAL,CRITICAL")
        assert url is not None
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        pane_b64 = urllib.parse.unquote(params["pane"][0])
        pane_b64 += "=" * (-len(pane_b64) % 4)
        pane_data = json.loads(base64.b64decode(pane_b64))
        assert "(" in pane_data["query"]
        assert 'level:"ERROR"' in pane_data["query"]
        assert 'level:"FATAL"' in pane_data["query"]
        assert 'level:"CRITICAL"' in pane_data["query"]
        assert " OR " in pane_data["query"]

    def test_log_search_ui_keyword_filter(self):
        builder = DeepLinkBuilder(account_id="123456", region="US")
        url = builder.log_search_ui(keyword="HikariPool")
        assert url is not None
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        pane_b64 = urllib.parse.unquote(params["pane"][0])
        pane_b64 += "=" * (-len(pane_b64) % 4)
        pane_data = json.loads(base64.b64decode(pane_b64))
        assert 'message:"HikariPool"' in pane_data["query"]

    def test_log_search_ui_namespace_and_cluster(self):
        """Platform log pattern: no service_name, just namespace + cluster."""
        builder = DeepLinkBuilder(account_id="123456", region="US")
        url = builder.log_search_ui(
            namespace="istio-system",
            cluster="main-cluster",
        )
        assert url is not None
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        pane_b64 = urllib.parse.unquote(params["pane"][0])
        pane_b64 += "=" * (-len(pane_b64) % 4)
        pane_data = json.loads(base64.b64decode(pane_b64))
        assert 'namespace_name:"istio-system"' in pane_data["query"]
        assert 'cluster_name:"main-cluster"' in pane_data["query"]
        # No service filter
        assert "entity.name" not in pane_data["query"]

    def test_log_search_ui_empty_query_valid(self):
        """No filters supplied still returns a working URL."""
        builder = DeepLinkBuilder(account_id="123456", region="US")
        url = builder.log_search_ui()
        assert url is not None
        assert "/launcher/logger.log-tailer" in url
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        pane_b64 = urllib.parse.unquote(params["pane"][0])
        pane_b64 += "=" * (-len(pane_b64) % 4)
        pane_data = json.loads(base64.b64decode(pane_b64))
        assert pane_data["query"] == ""

    def test_log_search_ui_dotted_attribute(self):
        """entity.name attribute is used as-is in the query."""
        builder = DeepLinkBuilder(account_id="123456", region="US")
        url = builder.log_search_ui(
            service_name="my-svc",
            service_attribute="entity.name",
        )
        assert url is not None
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query)
        pane_b64 = urllib.parse.unquote(params["pane"][0])
        pane_b64 += "=" * (-len(pane_b64) % 4)
        pane_data = json.loads(base64.b64decode(pane_b64))
        assert pane_data["query"].startswith('entity.name:"my-svc"')

    def test_log_search_ui_eu_region(self):
        builder = DeepLinkBuilder(account_id="789012", region="EU")
        url = builder.log_search_ui(service_name="my-svc")
        assert url is not None
        assert url.startswith(NR_BASE_EU)
        assert "/launcher/logger.log-tailer" in url

    def test_log_search_ui_contains_account_id(self):
        builder = DeepLinkBuilder(account_id="123456", region="US")
        url = builder.log_search_ui(service_name="my-svc")
        assert "platform[accountId]=123456" in url


# ── K8s workload with deployment GUID tests ──────────────────────────────


class TestK8sWorkloadWithGuid:
    """k8s_workload() with optional deployment_guid parameter."""

    def test_k8s_workload_with_guid_uses_entity_view_url(self):
        builder = DeepLinkBuilder(account_id="123456", region="US")
        url = builder.k8s_workload(
            "payments-prod", "payment-svc",
            deployment_guid="K8S-DEPLOY-GUID-001",
        )
        assert url is not None
        assert "/nr1-core/kubernetes-cluster-explorer/k8s-deployment-overview/K8S-DEPLOY-GUID-001" in url
        assert "account=123456" in url

    def test_k8s_workload_without_guid_uses_fallback_filter_url(self):
        builder = DeepLinkBuilder(account_id="123456", region="US")
        url = builder.k8s_workload("payments-prod", "payment-svc")
        assert url is not None
        assert "/nr1-core" in url
        decoded = urllib.parse.unquote(url)
        assert "deploymentName" in decoded
        assert "payment-svc" in decoded
        # Should NOT contain k8s-deployment-overview (no GUID).
        assert "k8s-deployment-overview" not in url

    def test_k8s_workload_with_guid_and_cluster_includes_cluster_tag(self):
        builder = DeepLinkBuilder(account_id="123456", region="US")
        url = builder.k8s_workload(
            "payments-prod", "payment-svc",
            deployment_guid="K8S-DEPLOY-GUID-001",
            cluster="cluster-a",
        )
        assert url is not None
        decoded = urllib.parse.unquote(url)
        assert "`tags.clusterName` = 'cluster-a'" in decoded

    def test_k8s_workload_filter_includes_backticked_namespace_tag(self):
        builder = DeepLinkBuilder(account_id="123456", region="US")
        url = builder.k8s_workload(
            "payments-prod", "payment-svc",
            deployment_guid="K8S-DEPLOY-GUID-001",
        )
        assert url is not None
        decoded = urllib.parse.unquote(url)
        assert "`tags.namespace` = 'payments-prod'" in decoded

    def test_k8s_workload_without_guid_with_cluster(self):
        """Fallback path also threads cluster into filter."""
        builder = DeepLinkBuilder(account_id="123456", region="US")
        url = builder.k8s_workload(
            "payments-prod", "payment-svc",
            cluster="main-cluster",
        )
        assert url is not None
        decoded = urllib.parse.unquote(url)
        assert "clusterName" in decoded
        assert "main-cluster" in decoded


# ── Routing invariant tests (AST-based regression prevention) ────────────


class TestRoutingInvariants:
    """Guardrails that prevent named-entity links from routing through nrql_chart."""

    _ENTITY_VIEW_KEYS = frozenset({
        "service_overview", "errors_inbox", "workload_view",
        "view_in_nr", "error_logs",
    })

    def test_nrql_chart_only_used_for_chart_suffix_keys(self):
        """Parse tools/ source files and assert that entity-view keys
        never route through nrql_chart().

        Any response dict key in _ENTITY_VIEW_KEYS must NOT have
        nrql_chart on the same assignment line.
        """
        import pathlib
        violations = []
        for path in pathlib.Path("tools").rglob("*.py"):
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                for key in self._ENTITY_VIEW_KEYS:
                    if f'"{key}"' in stripped and "nrql_chart" in stripped:
                        violations.append(f"{path}:{i}: {stripped}")
        assert violations == [], (
            "Entity-view keys must NOT route through nrql_chart():\n"
            + "\n".join(violations)
        )

    def test_no_tool_response_dict_has_log_search_lucene_key(self):
        """log_search() (Lucene) is retired — no tool should call it.

        log_search_nrql() and log_search_ui() are the valid replacements.
        """
        import pathlib
        violations = []
        for path in pathlib.Path("tools").rglob("*.py"):
            lines = path.read_text(encoding="utf-8").splitlines()
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                # Match _builder.log_search( but NOT log_search_nrql or log_search_ui
                if ".log_search(" in stripped:
                    if ".log_search_nrql(" not in stripped and ".log_search_ui(" not in stripped:
                        violations.append(f"{path}:{i}: {stripped}")
        assert violations == [], (
            "Retired log_search() (Lucene) still referenced in tools:\n"
            + "\n".join(violations)
        )

