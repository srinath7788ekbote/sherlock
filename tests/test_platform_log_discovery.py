"""
Tests for platform log discovery (Step 0c) in learn_account and search_logs.

Verifies that:
- LogsIntelligence model has the 4 new platform log fields
- KNOWN_PLATFORM_NAMESPACES is a frozenset with canonical namespaces
- _parse_log_namespace_intelligence picks the correct attribute spelling
- learn_account populates platform log fields via gather tasks 31+32
- search_logs Step 0c triggers for platform components and skips for regular APM services
- Multi-tenant isolation: AccountContext singleton doesn't leak values between tenants
"""
import json

import httpx
import pytest
import respx

from core.context import AccountContext
from core.intelligence import (
    KNOWN_PLATFORM_NAMESPACES,
    AccountIntelligence,
    APMIntelligence,
    K8sIntelligence,
    LogsIntelligence,
    _parse_log_namespace_intelligence,
)
from tools.logs import search_logs

# ── Helpers ──────────────────────────────────────────────────────────────

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


# ══════════════════════════════════════════════════════════════════════════
# Model Tests
# ══════════════════════════════════════════════════════════════════════════


class TestLogsIntelligenceModel:
    def test_logs_intelligence_has_new_fields(self):
        """Verify default values for all 4 new fields."""
        intel = LogsIntelligence()
        assert intel.namespace_attribute == ""
        assert intel.cluster_attribute == ""
        assert intel.platform_namespaces == []
        assert intel.all_discovered_namespaces == []

    def test_account_intelligence_logs_has_new_fields(self):
        """Verify new fields accessible from AccountIntelligence."""
        intel = AccountIntelligence(account_id="12345")
        assert intel.logs.namespace_attribute == ""
        assert intel.logs.cluster_attribute == ""
        assert intel.logs.platform_namespaces == []
        assert intel.logs.all_discovered_namespaces == []


class TestKnownPlatformNamespaces:
    def test_known_platform_namespaces_is_frozenset(self):
        assert isinstance(KNOWN_PLATFORM_NAMESPACES, frozenset)

    def test_known_platform_namespaces_contains_core_meshes(self):
        for ns in ["istio-system", "linkerd", "ingress-nginx", "kube-system"]:
            assert ns in KNOWN_PLATFORM_NAMESPACES, f"{ns} not in KNOWN_PLATFORM_NAMESPACES"

    def test_known_platform_namespaces_has_minimum_count(self):
        assert len(KNOWN_PLATFORM_NAMESPACES) >= 25


# ══════════════════════════════════════════════════════════════════════════
# Intelligence Parsing Tests (_parse_log_namespace_intelligence)
# ══════════════════════════════════════════════════════════════════════════


class TestParseLogNamespaceIntelligence:
    def test_parse_selects_plain_attribute_when_fluent_bit(self):
        """Probe row with has_plain > 0 → picks namespace_name / cluster_name."""
        probe = {"has_plain": 500, "has_k8s_prefix": 0, "has_kubernetes_prefix": 0}
        enum = {"plain_ns": ["istio-system", "payments-prod", "kube-system"]}
        ns_attr, cl_attr, platform_ns, all_ns = _parse_log_namespace_intelligence(probe, enum)
        assert ns_attr == "namespace_name"
        assert cl_attr == "cluster_name"
        assert "istio-system" in platform_ns
        assert "kube-system" in platform_ns
        assert "payments-prod" not in platform_ns
        assert "payments-prod" in all_ns

    def test_parse_selects_k8s_prefix_when_otel(self):
        """Probe row with has_k8s_prefix > 0 → picks k8s.namespace.name."""
        probe = {"has_plain": 0, "has_k8s_prefix": 300, "has_kubernetes_prefix": 0}
        enum = {"k8s_ns": ["istio-system", "monitoring"]}
        ns_attr, cl_attr, platform_ns, all_ns = _parse_log_namespace_intelligence(probe, enum)
        assert ns_attr == "k8s.namespace.name"
        assert cl_attr == "k8s.cluster.name"
        assert "istio-system" in platform_ns
        assert "monitoring" in platform_ns

    def test_parse_selects_kubernetes_prefix_when_legacy_fluentd(self):
        """Probe row with has_kubernetes_prefix > 0 → picks kubernetes.namespace_name."""
        probe = {"has_plain": 0, "has_k8s_prefix": 0, "has_kubernetes_prefix": 200}
        enum = {"kubernetes_ns": ["kube-system", "linkerd"]}
        ns_attr, cl_attr, platform_ns, all_ns = _parse_log_namespace_intelligence(probe, enum)
        assert ns_attr == "kubernetes.namespace_name"
        assert cl_attr == "kubernetes.cluster_name"
        assert "kube-system" in platform_ns
        assert "linkerd" in platform_ns

    def test_parse_picks_highest_count_when_mixed(self):
        """Mixed tenant: picks whichever attribute has more rows."""
        probe = {"has_plain": 100, "has_k8s_prefix": 400, "has_kubernetes_prefix": 50}
        enum = {"k8s_ns": ["istio-system"]}
        ns_attr, cl_attr, _, _ = _parse_log_namespace_intelligence(probe, enum)
        assert ns_attr == "k8s.namespace.name"
        assert cl_attr == "k8s.cluster.name"

    def test_parse_empty_probe_yields_empty_attributes(self):
        """All zeros → empty strings, no crash."""
        probe = {"has_plain": 0, "has_k8s_prefix": 0, "has_kubernetes_prefix": 0}
        enum = {}
        ns_attr, cl_attr, platform_ns, all_ns = _parse_log_namespace_intelligence(probe, enum)
        assert ns_attr == ""
        assert cl_attr == ""
        assert platform_ns == []
        assert all_ns == []

    def test_enumeration_filters_to_platform_allowlist(self):
        """Discovered list includes mixed namespaces → only platform ones in platform_namespaces."""
        probe = {"has_plain": 500, "has_k8s_prefix": 0, "has_kubernetes_prefix": 0}
        enum = {"plain_ns": [
            "istio-system", "app-prod", "payments-team",
            "kube-system", "custom-ns", "ingress-nginx",
        ]}
        _, _, platform_ns, all_ns = _parse_log_namespace_intelligence(probe, enum)
        assert sorted(platform_ns) == ["ingress-nginx", "istio-system", "kube-system"]
        assert len(all_ns) == 6

    def test_enumeration_caps_all_discovered_at_50(self):
        """Feed 100 namespaces → all_discovered_namespaces has length 50."""
        probe = {"has_plain": 100, "has_k8s_prefix": 0, "has_kubernetes_prefix": 0}
        enum = {"plain_ns": [f"ns-{i}" for i in range(100)]}
        _, _, _, all_ns = _parse_log_namespace_intelligence(probe, enum)
        assert len(all_ns) == 50

    def test_enumeration_handles_none_values(self):
        """Discovered list with None entries → filtered out, no crash."""
        probe = {"has_plain": 100, "has_k8s_prefix": 0, "has_kubernetes_prefix": 0}
        enum = {"plain_ns": ["istio-system", None, "", "kube-system", None]}
        _, _, platform_ns, all_ns = _parse_log_namespace_intelligence(probe, enum)
        assert None not in all_ns
        assert "" not in all_ns
        assert "istio-system" in platform_ns

    def test_enumeration_deduplicates(self):
        """Duplicate namespaces in enumeration row → deduplicated."""
        probe = {"has_plain": 100, "has_k8s_prefix": 0, "has_kubernetes_prefix": 0}
        enum = {"plain_ns": ["istio-system", "kube-system", "istio-system", "kube-system"]}
        _, _, _, all_ns = _parse_log_namespace_intelligence(probe, enum)
        assert all_ns == ["istio-system", "kube-system"]


# ══════════════════════════════════════════════════════════════════════════
# learn_account Integration Tests (respx)
# ══════════════════════════════════════════════════════════════════════════


class TestLearnAccountPlatformLogFields:
    """Tests that learn_account populates the platform log fields correctly."""

    @staticmethod
    def _build_gather_mock_responses(task_31_row, task_32_row):
        """Build a side_effect callable that returns canned data for tasks 31 and 32.

        For all other gather tasks, returns minimal valid data so the gather
        doesn't abort.
        """
        # Standard empty NRQL result
        empty_nrql = {"data": {"actor": {"account": {"nrql": {"results": []}}}}}
        # Standard empty entity search
        empty_entity_search = {
            "data": {"actor": {"entitySearch": {"count": 0, "results": {"entities": []}}}}
        }
        # Account meta
        account_meta = {"data": {"actor": {"account": {"name": "Test Account"}}}}
        # Alert policies
        alert_policies = {
            "data": {"actor": {"account": {"alerts": {"policiesSearch": {
                "policies": [], "totalCount": 0,
            }}}}}
        }

        def side_effect(request, route):
            body = json.loads(request.content)
            query_text = body.get("query", "")

            # Task 31: log namespace probe
            if (
                "has_plain" in query_text
                or "NRQL_LOG_NAMESPACE_PROBE" in query_text
                or "filter(count(*)" in query_text
            ):
                return httpx.Response(200, json={
                    "data": {"actor": {"account": {"nrql": {"results": [task_31_row]}}}}
                })
            # Task 32: log namespace enumeration
            if (
                "plain_ns" in query_text
                or "uniques(namespace_name" in query_text
                or "uniques(`k8s.namespace.name`" in query_text
            ):
                return httpx.Response(200, json={
                    "data": {"actor": {"account": {"nrql": {"results": [task_32_row]}}}}
                })

            # GQL queries (entitySearch pattern)
            if "entitySearch" in query_text:
                return httpx.Response(200, json=empty_entity_search)
            # Alert policies
            if "policiesSearch" in query_text:
                return httpx.Response(200, json=alert_policies)
            # Account meta
            if "account(id" in query_text and "nrql" not in query_text:
                return httpx.Response(200, json=account_meta)

            # Default: empty NRQL
            return httpx.Response(200, json=empty_nrql)

        return side_effect

    @respx.mock
    @pytest.mark.asyncio
    async def test_learn_account_populates_platform_log_fields_for_istio_tenant(self):
        """Fluent Bit tenant with istio-system and kube-system namespaces."""
        from core.credentials import Credentials
        from core.intelligence import learn_account

        creds = Credentials(account_id="99999", api_key="NRAK-test999", region="US")
        task_31 = {"has_plain": 500, "has_k8s_prefix": 0, "has_kubernetes_prefix": 0, "total": 500}
        task_32 = {
            "plain_ns": ["istio-system", "app-prod", "kube-system"],
            "k8s_ns": [], "kubernetes_ns": [],
        }

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=self._build_gather_mock_responses(task_31, task_32)
        )

        intel = await learn_account(creds)
        assert intel.logs.namespace_attribute == "namespace_name"
        assert intel.logs.cluster_attribute == "cluster_name"
        assert sorted(intel.logs.platform_namespaces) == ["istio-system", "kube-system"]
        assert "app-prod" in intel.logs.all_discovered_namespaces
        assert "app-prod" not in intel.logs.platform_namespaces

    @respx.mock
    @pytest.mark.asyncio
    async def test_learn_account_populates_for_otel_tenant(self):
        """OTel tenant with k8s.namespace.name attribute."""
        from core.credentials import Credentials
        from core.intelligence import learn_account

        creds = Credentials(account_id="88888", api_key="NRAK-test888", region="US")
        task_31 = {"has_plain": 0, "has_k8s_prefix": 500, "has_kubernetes_prefix": 0, "total": 500}
        task_32 = {"plain_ns": [], "k8s_ns": ["istio-system", "monitoring"], "kubernetes_ns": []}

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=self._build_gather_mock_responses(task_31, task_32)
        )

        intel = await learn_account(creds)
        assert intel.logs.namespace_attribute == "k8s.namespace.name"
        assert intel.logs.cluster_attribute == "k8s.cluster.name"
        assert "istio-system" in intel.logs.platform_namespaces
        assert "monitoring" in intel.logs.platform_namespaces

    @respx.mock
    @pytest.mark.asyncio
    async def test_learn_account_tenant_without_k8s_logs_gets_empty_fields(self):
        """Non-K8s tenant: all zeros → empty fields, no crash."""
        from core.credentials import Credentials
        from core.intelligence import learn_account

        creds = Credentials(account_id="77777", api_key="NRAK-test777", region="US")
        task_31 = {"has_plain": 0, "has_k8s_prefix": 0, "has_kubernetes_prefix": 0, "total": 0}
        task_32 = {"plain_ns": [], "k8s_ns": [], "kubernetes_ns": []}

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=self._build_gather_mock_responses(task_31, task_32)
        )

        intel = await learn_account(creds)
        assert intel.logs.namespace_attribute == ""
        assert intel.logs.cluster_attribute == ""
        assert intel.logs.platform_namespaces == []
        assert intel.logs.all_discovered_namespaces == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_learn_account_task_31_failure_does_not_abort_gather(self):
        """Simulated failure on task 31 → other tasks still populate."""
        from core.credentials import Credentials
        from core.intelligence import learn_account

        creds = Credentials(account_id="66666", api_key="NRAK-test666", region="US")

        def side_effect(request, route):
            body = json.loads(request.content)
            query_text = body.get("query", "")

            # Fail on task 31 and 32 queries
            if "filter(count(*)" in query_text or "uniques(namespace_name" in query_text:
                return httpx.Response(500, json={"error": "Internal Server Error"})

            # entitySearch
            if "entitySearch" in query_text:
                return httpx.Response(200, json={
                    "data": {"actor": {"entitySearch": {"count": 0, "results": {"entities": []}}}}
                })
            # policiesSearch
            if "policiesSearch" in query_text:
                return httpx.Response(200, json={
                    "data": {"actor": {"account": {"alerts": {
                        "policiesSearch": {"policies": [], "totalCount": 0},
                    }}}}
                })
            # account meta
            if "account(id" in query_text and "nrql" not in query_text:
                return httpx.Response(200, json={
                    "data": {"actor": {"account": {"name": "Fallback Test"}}}
                })

            return httpx.Response(200, json={
                "data": {"actor": {"account": {"nrql": {"results": []}}}}
            })

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=side_effect)

        # Should NOT raise — gather continues despite task 31/32 failures.
        intel = await learn_account(creds)
        assert intel.account_id == "66666"
        # Platform fields get defaults (empty) since tasks failed.
        assert intel.logs.namespace_attribute == ""
        assert intel.logs.platform_namespaces == []


# ══════════════════════════════════════════════════════════════════════════
# search_logs Step 0c Tests
# ══════════════════════════════════════════════════════════════════════════


def _make_platform_intelligence(**overrides):
    """Build an AccountIntelligence with platform log fields populated."""
    defaults = dict(
        account_id="123456",
        apm=APMIntelligence(
            service_names=["web-api", "payment-svc-prod"],
            service_guids={
                "web-api": "MTIzNDU2fEFQTXxBUFBMSUNBVElPTnwx",
                "payment-svc-prod": "MTIzNDU2fEFQTXxBUFBMSUNBVElPTnwy",
            },
        ),
        k8s=K8sIntelligence(
            integrated=True,
            cluster_names=["aks-test"],
            namespaces=["istio-system", "app-prod"],
        ),
        logs=LogsIntelligence(
            enabled=True,
            service_attribute="service.name",
            severity_attribute="level",
            namespace_attribute="namespace_name",
            cluster_attribute="cluster_name",
            platform_namespaces=["istio-system", "kube-system"],
            all_discovered_namespaces=["istio-system", "app-prod", "kube-system"],
        ),
    )
    defaults.update(overrides)
    return AccountIntelligence(**defaults)


@pytest.fixture
def platform_context(mock_credentials):
    """Set up context with platform log discovery fields populated."""
    AccountContext.reset_singleton()
    ctx = AccountContext()
    intel = _make_platform_intelligence()
    from core.credentials import Credentials
    creds = Credentials(account_id="123456", api_key="NRAK-test123456789abcdef", region="US")
    ctx.set_active(creds, intel)
    yield ctx
    ctx.clear()
    AccountContext.reset_singleton()


class TestSearchLogsStep0c:
    """Tests for Step 0c platform log fallback in search_logs."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_triggers_step_0c_when_service_has_no_apm_entity(
        self, platform_context
    ):
        """Service 'istio-gateway' not in apm.service_names → Step 0c triggers."""
        empty = _mock_nrql_response([])
        platform_logs = _mock_nrql_response([
            {
                "timestamp": 1700000000000,
                "message": "upstream connect error",
                "namespace_name": "istio-system",
                "cluster_name": "aks-test",
                "level": "ERROR",
                "status": 503,
                "response_flags": "UH",
            },
        ])

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = body.get("query", "")
            # Step 0c query uses namespace_name IN
            if "namespace_name" in query and "IN (" in query:
                return httpx.Response(200, json=platform_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("istio-gateway")
        parsed = json.loads(result)
        assert parsed["total_logs"] == 1
        assert parsed.get("platform_log_source") is True
        assert "namespace_name" in parsed.get("note", "")

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_triggers_step_0c_for_platform_keyword_match(
        self, platform_context
    ):
        """Service 'kube-proxy' triggers via keyword 'kube-'."""
        empty = _mock_nrql_response([])
        platform_logs = _mock_nrql_response([
            {"timestamp": 1700000000000, "message": "kube-proxy event", "level": "INFO"},
        ])

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = body.get("query", "")
            if "namespace_name" in query and "IN (" in query:
                return httpx.Response(200, json=platform_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("kube-proxy")
        parsed = json.loads(result)
        assert parsed["total_logs"] == 1
        assert parsed.get("platform_log_source") is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_skips_step_0c_when_platform_namespaces_empty(
        self, mock_credentials
    ):
        """Non-K8s tenant (empty platform_namespaces) → Step 0c never fires."""
        AccountContext.reset_singleton()
        ctx = AccountContext()
        intel = AccountIntelligence(
            account_id="123456",
            apm=APMIntelligence(service_names=[]),
            logs=LogsIntelligence(
                enabled=True,
                service_attribute="service.name",
                severity_attribute="level",
                # No platform discovery
                namespace_attribute="",
                cluster_attribute="",
                platform_namespaces=[],
            ),
        )
        ctx.set_active(mock_credentials, intel)

        empty = _mock_nrql_response([])
        call_count = {"n": 0}

        def _side_effect(request, route):
            call_count["n"] += 1
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("istio-gateway")
        parsed = json.loads(result)
        assert parsed["total_logs"] == 0
        assert "platform_log_source" not in parsed

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_skips_step_0c_when_step_0b_found_logs(
        self, platform_context
    ):
        """Step 0b returns logs → Step 0c is NEVER invoked."""
        service_logs = _mock_nrql_response([
            {"timestamp": 1700000000000, "message": "Found via service attr", "level": "INFO"},
        ])
        call_count = {"n": 0}

        def _side_effect(request, route):
            call_count["n"] += 1
            return httpx.Response(200, json=service_logs)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("web-api")
        parsed = json.loads(result)
        assert parsed["total_logs"] == 1
        assert "platform_log_source" not in parsed
        # Only 1 call (primary query succeeded) — Step 0c never runs.
        assert call_count["n"] == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_skips_step_0c_for_regular_apm_service(
        self, platform_context
    ):
        """Service 'web-api' IS in apm.service_names → Step 0c skipped."""
        empty = _mock_nrql_response([])

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = body.get("query", "")
            # Verify Step 0c NRQL is NEVER generated for an APM service.
            assert "namespace_name" not in query or "IN (" not in query, (
                "Step 0c should not be triggered for regular APM service"
            )
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("web-api")
        parsed = json.loads(result)
        assert parsed["total_logs"] == 0
        assert "platform_log_source" not in parsed

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_platform_log_source_flag_set_on_platform_logs(
        self, platform_context
    ):
        """Step 0c returns data → response contains platform_log_source: True."""
        empty = _mock_nrql_response([])
        platform_logs = _mock_nrql_response([
            {"timestamp": 1700000000000, "message": "envoy access log", "level": "INFO"},
        ])

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = body.get("query", "")
            if "namespace_name" in query and "IN (" in query:
                return httpx.Response(200, json=platform_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("envoy-proxy")
        parsed = json.loads(result)
        assert parsed.get("platform_log_source") is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_platform_log_source_flag_absent_on_service_logs(
        self, platform_context
    ):
        """Step 1 returns data → platform_log_source is absent."""
        service_logs = _mock_nrql_response([
            {"timestamp": 1700000000000, "message": "Normal log", "level": "INFO"},
        ])

        respx.post("https://api.newrelic.com/graphql").mock(
            return_value=httpx.Response(200, json=service_logs)
        )

        result = await search_logs("web-api")
        parsed = json.loads(result)
        assert parsed["total_logs"] == 1
        assert "platform_log_source" not in parsed

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_step_0c_uses_k8s_prefix_for_otel_tenant(
        self, mock_credentials
    ):
        """OTel tenant uses backtick-quoted k8s.namespace.name in NRQL."""
        AccountContext.reset_singleton()
        ctx = AccountContext()
        intel = _make_platform_intelligence(
            logs=LogsIntelligence(
                enabled=True,
                service_attribute="service.name",
                severity_attribute="level",
                namespace_attribute="k8s.namespace.name",
                cluster_attribute="k8s.cluster.name",
                platform_namespaces=["istio-system"],
                all_discovered_namespaces=["istio-system"],
            ),
        )
        ctx.set_active(mock_credentials, intel)

        empty = _mock_nrql_response([])
        platform_logs = _mock_nrql_response([
            {"timestamp": 1700000000000, "message": "otel log entry"},
        ])

        captured_nrql = {}

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = body.get("query", "")
            if "`k8s.namespace.name`" in query and "IN (" in query:
                captured_nrql["query"] = query
                return httpx.Response(200, json=platform_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("istio-gateway")
        parsed = json.loads(result)
        assert parsed["total_logs"] == 1
        assert "query" in captured_nrql, "Step 0c should have used k8s.namespace.name"
        assert "`k8s.namespace.name`" in captured_nrql["query"]

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_step_0c_includes_cluster_filter_when_k8s_clusters_known(
        self, platform_context
    ):
        """When k8s.cluster_names has entries, NRQL includes cluster filter."""
        empty = _mock_nrql_response([])
        platform_logs = _mock_nrql_response([
            {"timestamp": 1700000000000, "message": "test"},
        ])

        captured_nrql = {}

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = body.get("query", "")
            if "namespace_name" in query and "IN (" in query:
                captured_nrql["query"] = query
                return httpx.Response(200, json=platform_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        await search_logs("nginx-ingress")
        assert "aks-test" in captured_nrql.get("query", ""), (
            "Expected cluster filter with 'aks-test' in NRQL"
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_step_0c_omits_cluster_filter_when_no_clusters(
        self, mock_credentials
    ):
        """When k8s.cluster_names is empty, NRQL does NOT include cluster filter."""
        AccountContext.reset_singleton()
        ctx = AccountContext()
        intel = _make_platform_intelligence(
            k8s=K8sIntelligence(integrated=True, cluster_names=[], namespaces=[]),
        )
        ctx.set_active(mock_credentials, intel)

        empty = _mock_nrql_response([])
        platform_logs = _mock_nrql_response([
            {"timestamp": 1700000000000, "message": "test"},
        ])

        captured_nrql = {}

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = body.get("query", "")
            if "namespace_name" in query and "IN (" in query:
                captured_nrql["query"] = query
                return httpx.Response(200, json=platform_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        await search_logs("istio-gateway")
        assert "LIKE '%" not in captured_nrql.get("query", ""), (
            "Cluster filter should be omitted when no clusters known"
        )

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_step_0c_returns_istio_fields_when_present(
        self, platform_context
    ):
        """Mock response includes response_flags, vhost, etc. → passed through."""
        empty = _mock_nrql_response([])
        platform_logs = _mock_nrql_response([
            {
                "timestamp": 1700000000000,
                "message": "envoy access log",
                "namespace_name": "istio-system",
                "level": "ERROR",
                "status": 503,
                "response_flags": "UH",
                "vhost": "api.example.com",
                "upstream_cluster": "web-api.app-prod|http|80",
                "response_code_details": "upstream_reset_before_response_started",
            },
        ])

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = body.get("query", "")
            if "namespace_name" in query and "IN (" in query:
                return httpx.Response(200, json=platform_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("istio-gateway")
        parsed = json.loads(result)
        assert parsed["total_logs"] == 1
        log_entry = parsed["logs"][0]
        assert log_entry["response_flags"] == "UH"
        assert log_entry["vhost"] == "api.example.com"
        assert log_entry["status"] == 503

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_step_0c_works_when_istio_fields_absent(
        self, platform_context
    ):
        """Mock response has only basic fields → no KeyError."""
        empty = _mock_nrql_response([])
        platform_logs = _mock_nrql_response([
            {
                "timestamp": 1700000000000,
                "message": "basic log message",
                "level": "INFO",
            },
        ])

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = body.get("query", "")
            if "namespace_name" in query and "IN (" in query:
                return httpx.Response(200, json=platform_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("istio-gateway")
        parsed = json.loads(result)
        assert parsed["total_logs"] == 1
        # response_flags may be null/absent — no crash
        assert parsed.get("platform_log_source") is True

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_step_0c_failure_falls_through_silently(
        self, platform_context
    ):
        """Step 0c query raises → falls through silently to zero-result response."""
        empty = _mock_nrql_response([])

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = body.get("query", "")
            # Simulate Step 0c failure
            if "namespace_name" in query and "IN (" in query:
                return httpx.Response(500, json={"error": "server error"})
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        result = await search_logs("istio-gateway")
        parsed = json.loads(result)
        # No crash, just zero logs returned
        assert parsed["total_logs"] == 0
        assert "platform_log_source" not in parsed

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_step_0c_respects_severity_filter(
        self, platform_context
    ):
        """Severity filter 'ERROR' → NRQL includes severity clause."""
        empty = _mock_nrql_response([])
        platform_logs = _mock_nrql_response([
            {"timestamp": 1700000000000, "message": "error log", "level": "ERROR"},
        ])

        captured_nrql = {}

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = body.get("query", "")
            if "namespace_name" in query and "IN (" in query:
                captured_nrql["query"] = query
                return httpx.Response(200, json=platform_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        await search_logs("istio-gateway", severity="ERROR")
        assert "'ERROR'" in captured_nrql.get("query", ""), (
            "Expected severity filter in Step 0c NRQL"
        )

    @respx.mock
    @pytest.mark.asyncio
    async def test_search_logs_step_0c_respects_keyword_filter(
        self, platform_context
    ):
        """Keyword 'HikariPool' → NRQL includes LIKE clause."""
        empty = _mock_nrql_response([])
        platform_logs = _mock_nrql_response([
            {"timestamp": 1700000000000, "message": "HikariPool timeout"},
        ])

        captured_nrql = {}

        def _side_effect(request, route):
            body = json.loads(request.content)
            query = body.get("query", "")
            if "namespace_name" in query and "IN (" in query:
                captured_nrql["query"] = query
                return httpx.Response(200, json=platform_logs)
            return httpx.Response(200, json=empty)

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_side_effect)

        await search_logs("istio-gateway", keyword="HikariPool")
        assert "HikariPool" in captured_nrql.get("query", ""), (
            "Expected keyword in Step 0c NRQL"
        )


# ══════════════════════════════════════════════════════════════════════════
# Multi-Tenant Regression Tests
# ══════════════════════════════════════════════════════════════════════════


class TestMultiTenantIsolation:
    def test_tenant_a_fluent_bit_tenant_b_otel_both_work(self):
        """Verify AccountContext singleton doesn't leak values between tenants."""
        from core.credentials import Credentials

        AccountContext.reset_singleton()
        ctx = AccountContext()

        # Tenant A: Fluent Bit (namespace_name)
        creds_a = Credentials(account_id="AAAA", api_key="NRAK-aaa", region="US")
        intel_a = _make_platform_intelligence(
            account_id="AAAA",
            logs=LogsIntelligence(
                enabled=True,
                service_attribute="service.name",
                severity_attribute="level",
                namespace_attribute="namespace_name",
                cluster_attribute="cluster_name",
                platform_namespaces=["istio-system"],
                all_discovered_namespaces=["istio-system", "tenant-a-ns"],
            ),
        )
        ctx.set_active(creds_a, intel_a)
        active_creds, active_intel = ctx.get_active()
        assert active_intel.logs.namespace_attribute == "namespace_name"
        assert active_intel.logs.platform_namespaces == ["istio-system"]

        # Tenant B: OTel (k8s.namespace.name) — overwrite the singleton
        creds_b = Credentials(account_id="BBBB", api_key="NRAK-bbb", region="US")
        intel_b = _make_platform_intelligence(
            account_id="BBBB",
            logs=LogsIntelligence(
                enabled=True,
                service_attribute="entity.name",
                severity_attribute="severity",
                namespace_attribute="k8s.namespace.name",
                cluster_attribute="k8s.cluster.name",
                platform_namespaces=["linkerd", "kube-system"],
                all_discovered_namespaces=["linkerd", "kube-system", "tenant-b-ns"],
            ),
        )
        ctx.set_active(creds_b, intel_b)
        active_creds, active_intel = ctx.get_active()
        # Must be tenant B's values, not tenant A's
        assert active_intel.logs.namespace_attribute == "k8s.namespace.name"
        assert sorted(active_intel.logs.platform_namespaces) == ["kube-system", "linkerd"]
        assert "tenant-a-ns" not in active_intel.logs.all_discovered_namespaces

        ctx.clear()
        AccountContext.reset_singleton()

    def test_tenant_with_custom_mesh_namespace_not_in_allowlist_ignored(self):
        """Custom namespace not in KNOWN_PLATFORM_NAMESPACES → excluded from platform_namespaces."""
        probe = {"has_plain": 500, "has_k8s_prefix": 0, "has_kubernetes_prefix": 0}
        enum = {"plain_ns": ["my-custom-mesh-system", "istio-system", "app-ns"]}
        _, _, platform_ns, all_ns = _parse_log_namespace_intelligence(probe, enum)
        assert "my-custom-mesh-system" not in platform_ns
        assert "istio-system" in platform_ns
        assert "my-custom-mesh-system" in all_ns
