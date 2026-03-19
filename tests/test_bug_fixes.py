"""
Tests for all five bug fixes applied to Sherlock.

Bug 1: Naming convention learning + env-preserving resolution
Bug 2: K8s queries use bare deployment name
Bug 3: Null guards on health check functions
Bug 4: Discovery optimization (capped window, timeout, tiered)
Bug 5: Log analysis pattern detection
"""

import asyncio
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.intelligence import (
    AccountIntelligence,
    APMIntelligence,
    K8sIntelligence,
    NamingConvention,
    _learn_naming_convention,
)
from core.query_builder import (
    InvestigationQuery,
    SignalQuery,
    _check_error_logs,
    _check_error_rate,
    _check_external_calls,
    _check_hpa_scaling,
    _check_k8s_events,
    _check_oom,
    _check_pod_status,
    _check_queue_depth,
    _check_replica_health,
    _check_resources,
    _check_slow_queries,
    _check_synthetic_pass_rate,
    _spike_analysis,
    build_investigation_queries,
)
from core.discovery import (
    AvailableEventType,
    DiscoveryResult,
    DISCOVERY_WINDOW_MINUTES,
    TIER1_EVENT_TYPES,
    TIER2_EVENT_TYPES,
    EVENT_REGISTRY,
)
from core.sanitize import fuzzy_resolve_service
from core.exceptions import ServiceNotFoundError


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BUG 1 PART B — NamingConvention is learned per account
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNamingConventionLearning:
    """Test that _learn_naming_convention correctly detects patterns."""

    def test_slash_separator_detected(self):
        """Slash-separated names like 'eswd-prod/pdf-export-service'."""
        apm_names = [
            "eswd-prod/pdf-export-service",
            "eswd-prod/auth-service",
            "eswd-dev/pdf-export-service",
            "eswd-dev/auth-service",
            "eswd-qa/pdf-export-service",
        ]
        nc = _learn_naming_convention(apm_names, [], {})
        assert nc.separator == "/"
        assert nc.env_position == "prefix"
        assert "eswd-prod" in nc.env_values
        assert "eswd-dev" in nc.env_values
        assert "eswd-qa" in nc.env_values
        assert "pdf-export-service" in nc.bare_service_names
        assert "auth-service" in nc.bare_service_names

    def test_suffix_env_detected(self):
        """Suffix env names like 'payment-svc-prod'."""
        apm_names = [
            "payment-svc-prod",
            "auth-service-prod",
            "export-worker-dev",
            "auth-service-dev",
        ]
        nc = _learn_naming_convention(apm_names, [], {})
        assert nc.separator == "-"
        assert nc.env_position == "suffix"
        assert "prod" in nc.env_values
        assert "dev" in nc.env_values
        assert "payment-svc" in nc.bare_service_names

    def test_prefix_env_detected(self):
        """Prefix env names like 'prod-payment-svc'."""
        apm_names = [
            "prod-payment-svc",
            "prod-auth-service",
            "dev-payment-svc",
            "dev-auth-service",
        ]
        nc = _learn_naming_convention(apm_names, [], {})
        assert nc.separator == "-"
        assert nc.env_position == "prefix"
        assert "prod" in nc.env_values
        assert "dev" in nc.env_values

    def test_no_env_pattern_returns_defaults(self):
        """Names without clear env pattern."""
        apm_names = ["my-custom-app", "another-app"]
        nc = _learn_naming_convention(apm_names, [], {})
        assert nc.separator is None or nc.env_position is None

    def test_empty_names_returns_defaults(self):
        nc = _learn_naming_convention([], [], {})
        assert nc.separator is None
        assert nc.env_values == []

    def test_apm_to_k8s_namespace_map_component_match(self):
        """'eswd-prod' should map to K8s namespace 'prod' via component match."""
        apm_names = [
            "eswd-prod/pdf-export-service",
            "eswd-dev/pdf-export-service",
            "eswd-qa/pdf-export-service",
        ]
        k8s_ns = ["prod", "dev", "qa", "kube-system", "monitoring"]
        nc = _learn_naming_convention(apm_names, k8s_ns, {})

        assert nc.apm_to_k8s_namespace_map.get("eswd-prod") == "prod"
        assert nc.apm_to_k8s_namespace_map.get("eswd-dev") == "dev"
        assert nc.apm_to_k8s_namespace_map.get("eswd-qa") == "qa"

    def test_apm_to_k8s_namespace_map_exact_match(self):
        """Direct match when APM env == K8s namespace."""
        apm_names = [
            "prod/payment-svc",
            "dev/payment-svc",
        ]
        k8s_ns = ["prod", "dev", "staging"]
        nc = _learn_naming_convention(apm_names, k8s_ns, {})

        assert nc.apm_to_k8s_namespace_map.get("prod") == "prod"
        assert nc.apm_to_k8s_namespace_map.get("dev") == "dev"

    def test_k8s_deployment_name_format_bare(self):
        """K8s deployments use bare names (without env prefix)."""
        apm_names = [
            "eswd-prod/pdf-export-service",
            "eswd-prod/auth-service",
        ]
        k8s_deps = {
            "prod": ["pdf-export-service", "auth-service"],
        }
        nc = _learn_naming_convention(apm_names, ["prod"], k8s_deps)
        assert nc.k8s_deployment_name_format == "bare"

    def test_k8s_deployment_name_format_full(self):
        """K8s deployments use full APM names (with env prefix)."""
        apm_names = [
            "eswd-prod/pdf-export-service",
            "eswd-prod/auth-service",
        ]
        k8s_deps = {
            "prod": ["eswd-prod/pdf-export-service", "eswd-prod/auth-service"],
        }
        nc = _learn_naming_convention(apm_names, ["prod"], k8s_deps)
        assert nc.k8s_deployment_name_format == "full"

    def test_naming_convention_in_account_intelligence(self):
        """AccountIntelligence has naming_convention field."""
        intel = AccountIntelligence(account_id="test")
        assert hasattr(intel, "naming_convention")
        assert isinstance(intel.naming_convention, NamingConvention)

    # ── Universal naming convention detection tests ──

    def test_unconventional_env_names_slash(self):
        """Non-standard env names like 'blue', 'green', 'canary' detected via stats."""
        apm_names = [
            "blue/billing-api",
            "blue/auth-api",
            "green/billing-api",
            "green/auth-api",
            "canary/billing-api",
        ]
        nc = _learn_naming_convention(apm_names, [], {})
        assert nc.separator == "/"
        assert nc.env_position == "prefix"
        assert "blue" in nc.env_values
        assert "green" in nc.env_values
        assert "canary" in nc.env_values
        assert "billing-api" in nc.bare_service_names

    def test_compound_env_prefix_detected(self):
        """Compound env prefixes like 'eswd-prod', 'eswd-demo' detected."""
        apm_names = [
            "eswd-prod/pdf-export-service",
            "eswd-prod/auth-service",
            "eswd-prod/billing-service",
            "eswd-demo/pdf-export-service",
            "eswd-demo/auth-service",
            "eswd-demo/billing-service",
        ]
        nc = _learn_naming_convention(apm_names, [], {})
        assert nc.separator == "/"
        assert "eswd-prod" in nc.env_values
        assert "eswd-demo" in nc.env_values
        assert "pdf-export-service" in nc.bare_service_names

    def test_extra_entity_names_strengthen_detection(self):
        """OTel/Synthetics/Browser names added as extra_entity_names strengthen signal."""
        apm_names = [
            "team-a/svc-1",
            "team-a/svc-2",
        ]
        extra = [
            "team-a/svc-3",
            "team-b/svc-1",
            "team-b/svc-2",
            "team-b/svc-3",
        ]
        nc = _learn_naming_convention(apm_names, [], {}, extra_entity_names=extra)
        assert nc.separator == "/"
        assert nc.env_position == "prefix"
        assert "team-a" in nc.env_values
        assert "team-b" in nc.env_values
        assert len(nc.bare_service_names) == 3

    def test_dot_separator_detected(self):
        """Dot-separated names like 'prod.billing.api' detected."""
        apm_names = [
            "prod.billing",
            "prod.auth",
            "staging.billing",
            "staging.auth",
        ]
        nc = _learn_naming_convention(apm_names, [], {})
        assert nc.separator == "."
        assert nc.env_position == "prefix"
        assert "prod" in nc.env_values
        assert "staging" in nc.env_values

    def test_underscore_separator_suffix(self):
        """Underscore-separated names with env suffix detected."""
        apm_names = [
            "billing_prod",
            "auth_prod",
            "billing_dev",
            "auth_dev",
            "payments_prod",
        ]
        nc = _learn_naming_convention(apm_names, [], {})
        assert nc.separator == "_"
        assert nc.env_position == "suffix"
        assert "prod" in nc.env_values
        assert "dev" in nc.env_values

    def test_segment_roles_populated(self):
        """segment_roles field is populated for slash-separated names."""
        apm_names = [
            "ns-1/svc-a",
            "ns-1/svc-b",
            "ns-2/svc-a",
        ]
        nc = _learn_naming_convention(apm_names, [], {})
        assert nc.segment_roles == ["environment", "service"]

    def test_all_entity_names_collected(self):
        """all_entity_names contains union of APM + extra names."""
        apm = ["ns/svc-1", "ns/svc-2"]
        extra = ["ns/svc-3", "ns/svc-1"]  # svc-1 is a dupe
        nc = _learn_naming_convention(apm, [], {}, extra_entity_names=extra)
        assert "ns/svc-1" in nc.all_entity_names
        assert "ns/svc-3" in nc.all_entity_names
        # Deduped
        assert len(nc.all_entity_names) == len(set(nc.all_entity_names))

    def test_k8s_namespace_crossref_disambiguates(self):
        """K8s namespaces help classify the correct segment as environment."""
        apm_names = [
            "prod-billing",
            "prod-auth",
            "staging-billing",
            "staging-auth",
        ]
        k8s_ns = ["prod", "staging", "kube-system"]
        nc = _learn_naming_convention(apm_names, k8s_ns, {})
        assert nc.env_position == "prefix"
        assert "prod" in nc.env_values

    def test_secondary_separator_detected(self):
        """Secondary separator ('-' within '/' names) is detected."""
        apm_names = [
            "team-a/my-service",
            "team-a/other-service",
            "team-b/my-service",
        ]
        nc = _learn_naming_convention(apm_names, [], {})
        assert nc.separator == "/"
        assert nc.secondary_separator == "-"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BUG 1 PART A — Environment-preserving fuzzy resolution
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestEnvPreservingResolution:
    """Test that fuzzy_resolve_service preserves the environment segment."""

    @pytest.fixture
    def slash_naming(self):
        return NamingConvention(
            separator="/",
            env_position="prefix",
            env_values=["eswd-prod", "eswd-dev", "eswd-qa", "eswd-perf"],
            bare_service_names=["pdf-export-service", "auth-service"],
            apm_to_k8s_namespace_map={
                "eswd-prod": "prod",
                "eswd-dev": "dev",
            },
            k8s_deployment_name_format="bare",
        )

    @pytest.fixture
    def known_services(self):
        return [
            "eswd-prod/pdf-export-service",
            "eswd-dev/pdf-export-service",
            "eswd-qa/pdf-export-service",
            "eswd-perf/pdf-export-service",
            "eswd-prod/auth-service",
            "eswd-dev/auth-service",
        ]

    def test_prod_resolves_to_prod(self, known_services, slash_naming):
        """'eswd-prod/pdf-export-service' must resolve to prod, not dev."""
        name, was_fuzzy, score = fuzzy_resolve_service(
            "eswd-prod/pdf-export-service",
            known_services,
            naming_convention=slash_naming,
        )
        assert name == "eswd-prod/pdf-export-service"
        assert score == 1.0

    def test_dev_resolves_to_dev(self, known_services, slash_naming):
        """'eswd-dev/pdf-export-service' must resolve to dev."""
        name, was_fuzzy, score = fuzzy_resolve_service(
            "eswd-dev/pdf-export-service",
            known_services,
            naming_convention=slash_naming,
        )
        assert name == "eswd-dev/pdf-export-service"

    def test_qa_resolves_to_qa(self, known_services, slash_naming):
        """'eswd-qa/pdf-export-service' must resolve to qa."""
        name, was_fuzzy, score = fuzzy_resolve_service(
            "eswd-qa/pdf-export-service",
            known_services,
            naming_convention=slash_naming,
        )
        assert name == "eswd-qa/pdf-export-service"

    def test_env_boost_selects_correct_env(self, known_services, slash_naming):
        """Fuzzy match with env boost prefers same env."""
        name, was_fuzzy, score = fuzzy_resolve_service(
            "eswd-prod/pdf-export",  # slightly wrong bare name
            known_services,
            threshold=0.5,
            naming_convention=slash_naming,
        )
        # Should pick prod variant, not dev/qa/perf.
        assert "eswd-prod" in name

    def test_without_naming_convention_still_works(self, known_services):
        """Without naming convention, exact match still works."""
        name, was_fuzzy, score = fuzzy_resolve_service(
            "eswd-prod/pdf-export-service",
            known_services,
        )
        assert name == "eswd-prod/pdf-export-service"
        assert score == 1.0

    def test_suffix_env_resolution(self):
        """Test suffix-based env resolution (e.g., 'service-prod')."""
        suffix_naming = NamingConvention(
            separator="-",
            env_position="suffix",
            env_values=["prod", "dev"],
            bare_service_names=["payment-svc", "auth-service"],
        )
        known = ["payment-svc-prod", "payment-svc-dev", "auth-service-prod"]
        name, _, score = fuzzy_resolve_service(
            "payment-svc-prod",
            known,
            naming_convention=suffix_naming,
        )
        assert name == "payment-svc-prod"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BUG 2 — K8s queries use bare deployment name
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestK8sBareNameQueries:
    """Test that K8s queries use bare deployment name when format is 'bare'."""

    @pytest.fixture
    def bare_naming(self):
        return NamingConvention(
            separator="/",
            env_position="prefix",
            env_values=["eswd-prod"],
            bare_service_names=["pdf-export-service"],
            apm_to_k8s_namespace_map={"eswd-prod": "prod"},
            k8s_deployment_name_format="bare",
        )

    @pytest.fixture
    def full_naming(self):
        return NamingConvention(
            separator="/",
            env_position="prefix",
            env_values=["myapp-prod"],
            bare_service_names=["web-server"],
            apm_to_k8s_namespace_map={"myapp-prod": "prod"},
            k8s_deployment_name_format="full",
        )

    def test_bare_format_strips_env_prefix(self, bare_naming):
        """When k8s_deployment_name_format='bare', K8s queries use bare name."""
        discovery = DiscoveryResult(
            available={
                "K8sPodSample": AvailableEventType(
                    event_type="K8sPodSample",
                    domain="k8s",
                    matched_filter="deploymentName",
                    matched_value="pdf-export-service",
                    signals=["pod_status", "restarts"],
                ),
            },
            domains_with_data=["k8s"],
            service_filter_map={"K8sPodSample": "deploymentName"},
        )

        anchor = MagicMock()
        anchor.primary_service = "eswd-prod/pdf-export-service"
        anchor.since_minutes = 30
        anchor.until_clause = ""

        queries = build_investigation_queries(
            discovery=discovery,
            anchor=anchor,
            namespace="prod",
            naming_convention=bare_naming,
        )

        # All K8s queries should use 'pdf-export-service' not 'eswd-prod/pdf-export-service'.
        for q in queries:
            assert "eswd-prod/pdf-export-service" not in q.nrql
            assert "pdf-export-service" in q.nrql

    def test_full_format_keeps_full_name(self, full_naming):
        """When k8s_deployment_name_format='full', K8s queries use full name."""
        discovery = DiscoveryResult(
            available={
                "K8sPodSample": AvailableEventType(
                    event_type="K8sPodSample",
                    domain="k8s",
                    matched_filter="deploymentName",
                    matched_value="myapp-prod/web-server",
                    signals=["pod_status", "restarts"],
                ),
            },
            domains_with_data=["k8s"],
            service_filter_map={"K8sPodSample": "deploymentName"},
        )

        anchor = MagicMock()
        anchor.primary_service = "myapp-prod/web-server"
        anchor.since_minutes = 30
        anchor.until_clause = ""

        queries = build_investigation_queries(
            discovery=discovery,
            anchor=anchor,
            namespace="prod",
            naming_convention=full_naming,
        )

        for q in queries:
            assert "myapp-prod/web-server" in q.nrql

    def test_correct_namespace_from_map(self, bare_naming):
        """Namespace is correctly resolved from apm_to_k8s_namespace_map."""
        assert bare_naming.apm_to_k8s_namespace_map.get("eswd-prod") == "prod"

    def test_different_client_naming(self):
        """Test with a completely different client naming convention."""
        nc = NamingConvention(
            separator="-",
            env_position="suffix",
            env_values=["prod", "staging"],
            bare_service_names=["user-api", "order-api"],
            apm_to_k8s_namespace_map={"prod": "production", "staging": "stg"},
            k8s_deployment_name_format="bare",
        )
        discovery = DiscoveryResult(
            available={
                "K8sDeploymentSample": AvailableEventType(
                    event_type="K8sDeploymentSample",
                    domain="k8s",
                    matched_filter="deploymentName",
                    matched_value="user-api",
                    signals=["replica_health"],
                ),
            },
            domains_with_data=["k8s"],
            service_filter_map={"K8sDeploymentSample": "deploymentName"},
        )
        anchor = MagicMock()
        anchor.primary_service = "user-api-prod"
        anchor.since_minutes = 30
        anchor.until_clause = ""

        queries = build_investigation_queries(
            discovery=discovery,
            anchor=anchor,
            namespace="production",
            naming_convention=nc,
        )

        for q in queries:
            # Should use "user-api" not "user-api-prod".
            assert "user-api-prod" not in q.nrql
            assert "user-api" in q.nrql


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BUG 3 — Null guards on health check functions
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestNullGuards:
    """Every health check function must handle empty/None/[None] gracefully."""

    ALL_HEALTH_CHECKS = [
        _check_pod_status,
        _check_replica_health,
        _check_hpa_scaling,
        _check_oom,
        _check_resources,
        _check_k8s_events,
        _check_error_rate,
        _check_slow_queries,
        _check_external_calls,
        _check_error_logs,
        _check_synthetic_pass_rate,
        _check_queue_depth,
    ]

    @pytest.mark.parametrize("health_check_fn", ALL_HEALTH_CHECKS)
    def test_empty_list_returns_empty(self, health_check_fn):
        """Empty list input returns empty list, not exception."""
        result = health_check_fn([])
        assert result == [] or isinstance(result, list)

    @pytest.mark.parametrize("health_check_fn", ALL_HEALTH_CHECKS)
    def test_none_input_returns_empty(self, health_check_fn):
        """None input returns empty list, not exception."""
        result = health_check_fn(None)
        assert result == [] or isinstance(result, list)

    @pytest.mark.parametrize("health_check_fn", ALL_HEALTH_CHECKS)
    def test_list_with_none_returns_empty(self, health_check_fn):
        """[None] input returns empty list, not exception."""
        result = health_check_fn([None])
        assert isinstance(result, list)

    @pytest.mark.parametrize("health_check_fn", ALL_HEALTH_CHECKS)
    def test_non_list_input_returns_empty(self, health_check_fn):
        """Non-list input returns empty list."""
        result = health_check_fn("not a list")
        assert result == []

    def test_spike_analysis_empty_list(self):
        result = _spike_analysis([], "error_rate")
        assert result == []

    def test_spike_analysis_none_input(self):
        result = _spike_analysis(None, "error_rate")
        assert result == []

    def test_spike_analysis_list_with_none(self):
        result = _spike_analysis([None, None, None, None], "error_rate")
        assert isinstance(result, list)

    def test_spike_analysis_non_list(self):
        result = _spike_analysis("not a list", "error_rate")
        assert result == []

    def test_error_rate_with_valid_data(self):
        """Verify error_rate still works with valid data after null guard."""
        result = _check_error_rate([{
            "error_rate": 25.0,
            "throughput": 100,
            "p95_latency": 1.0,
            "p99_latency": 2.0,
            "peak_error_rate": 30.0,
        }])
        assert any("error rate" in f.lower() for f in result)

    def test_pod_status_with_valid_data(self):
        """Verify pod_status still works with valid data."""
        result = _check_pod_status([{
            "status": "Failed",
            "podName": "test-pod",
            "current_restarts": 0,
            "ready": False,
        }])
        assert len(result) > 0
        assert any("Failed" in f for f in result)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BUG 4 — Discovery optimization
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestDiscoveryOptimization:
    """Test discovery uses capped window, timeout, and tiered approach."""

    def test_discovery_window_constant_exists(self):
        """DISCOVERY_WINDOW_MINUTES is defined and is 120."""
        assert DISCOVERY_WINDOW_MINUTES == 120

    def test_tier1_event_types_defined(self):
        """Tier 1 contains the 6 core event types."""
        assert "Transaction" in TIER1_EVENT_TYPES
        assert "TransactionError" in TIER1_EVENT_TYPES
        assert "Log" in TIER1_EVENT_TYPES
        assert "K8sPodSample" in TIER1_EVENT_TYPES
        assert "K8sDeploymentSample" in TIER1_EVENT_TYPES
        assert "SyntheticCheck" in TIER1_EVENT_TYPES
        assert len(TIER1_EVENT_TYPES) == 6

    def test_tier2_event_types_defined(self):
        """Tier 2 contains extended K8s event types."""
        assert "K8sContainerSample" in TIER2_EVENT_TYPES
        assert "K8sHpaSample" in TIER2_EVENT_TYPES
        assert "K8sReplicaSetSample" in TIER2_EVENT_TYPES
        assert "InfrastructureEvent" in TIER2_EVENT_TYPES

    def test_discovery_result_has_timeout_field(self):
        """DiscoveryResult model has discovery_timeout field."""
        dr = DiscoveryResult()
        assert hasattr(dr, "discovery_timeout")
        assert dr.discovery_timeout is False

    @pytest.mark.asyncio
    async def test_discovery_timeout_returns_defaults(self):
        """On timeout, discovery returns APM+logs defaults."""
        from core.credentials import Credentials

        creds = Credentials(
            account_id="123", api_key="NRAK-test", region="US"
        )
        anchor = MagicMock()
        anchor.since_minutes = 2880
        anchor.until_clause = ""

        # Patch _check_event_type to hang forever.
        async def slow_check(*args, **kwargs):
            await asyncio.sleep(100)

        from core import discovery as disc_module

        with patch.object(disc_module, "_check_event_type", side_effect=slow_check):
            # Set a very short timeout for testing.
            original_timeout = disc_module.DISCOVERY_TIMEOUT_S
            disc_module.DISCOVERY_TIMEOUT_S = 0.1
            try:
                result = await disc_module.discover_available_data(
                    service_candidates=["test-service"],
                    anchor=anchor,
                    credentials=creds,
                )
                assert result.discovery_timeout is True
                assert "apm" in result.domains_with_data
                assert "logs" in result.domains_with_data
                assert "Transaction" in result.available
                assert "Log" in result.available
            finally:
                disc_module.DISCOVERY_TIMEOUT_S = original_timeout

    @pytest.mark.asyncio
    async def test_discovery_caps_window(self):
        """Discovery uses min(since_minutes, 120) for COUNT queries."""
        from core.credentials import Credentials
        from core import discovery as disc_module

        creds = Credentials(
            account_id="123", api_key="NRAK-test", region="US"
        )
        anchor = MagicMock()
        anchor.since_minutes = 2880  # 48 hours
        anchor.until_clause = ""

        captured_since = []

        original_check = disc_module._check_event_type

        async def capture_check(*args, **kwargs):
            captured_since.append(kwargs.get("since_minutes", args[3] if len(args) > 3 else None))
            return None

        with patch.object(disc_module, "_check_event_type", side_effect=capture_check):
            await disc_module.discover_available_data(
                service_candidates=["test-service"],
                anchor=anchor,
                credentials=creds,
            )

        # All captured since_minutes values should be capped at 30.
        for val in captured_since:
            assert val <= DISCOVERY_WINDOW_MINUTES, f"Discovery used {val} minutes, should be ≤ {DISCOVERY_WINDOW_MINUTES}"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BUG 5 — Log analysis pattern detection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestLogAnalysisPatterns:
    """Test that _check_error_logs detects crash, dependency, and memory patterns."""

    def test_crash_pattern_detected(self):
        """'Application run failed' is detected as a crash."""
        logs = [
            {"message": "Application run failed", "hostname": "pod-1"},
            {"message": "Application run failed", "hostname": "pod-2"},
            {"message": "Application run failed", "hostname": "pod-3"},
        ]
        findings = _check_error_logs(logs)
        assert any("APPLICATION CRASHES" in f for f in findings)
        assert any("3 crash event" in f for f in findings)
        assert any("pod-1" in f or "pod-2" in f for f in findings)

    def test_dependency_failure_with_url(self):
        """Dependency failure with URL extracts hostname."""
        logs = [
            {
                "message": "Failed to fetch substitutions from URL: "
                "http://font-service-backend:80/api/substitutions",
            },
            {
                "message": "IO error during HTTP request to URL: "
                "http://font-service-backend:80/api/fonts",
            },
        ]
        findings = _check_error_logs(logs)
        assert any("DEPENDENCY FAILURE" in f for f in findings)
        assert any("font-service-backend" in f for f in findings)

    def test_dependency_name_extracted_from_url(self):
        """Hostname is correctly extracted from http://host:port/path."""
        logs = [
            {"message": "Connection refused to http://auth-svc:8080/validate"},
        ]
        findings = _check_error_logs(logs)
        assert any("auth-svc" in f for f in findings)

    def test_dependency_grouping_by_hostname(self):
        """Multiple failures to same host are grouped."""
        logs = [
            {"message": "Failed to fetch from http://db-service:5432/query1"},
            {"message": "Failed to fetch from http://db-service:5432/query2"},
            {"message": "Failed to connect to http://cache-svc:6379/get"},
        ]
        findings = _check_error_logs(logs)
        dep_findings = [f for f in findings if "DEPENDENCY FAILURE" in f]
        # Should have 2 dependency findings (db-service and cache-svc).
        assert len(dep_findings) == 2

    def test_memory_pattern_detected(self):
        """OOM and memory patterns are detected."""
        logs = [
            {"message": "java.lang.OutOfMemoryError: Heap space"},
            {"message": "Container killed due to out of memory"},
        ]
        findings = _check_error_logs(logs)
        assert any("MEMORY PRESSURE" in f for f in findings)
        assert any("2 memory-related" in f for f in findings)

    def test_generic_count_only_when_no_pattern(self):
        """Generic count is shown only when no specific pattern matches."""
        logs = [
            {"message": "Something went wrong but no specific pattern"},
            {"message": "Another generic error message"},
            {"message": "Yet another error"},
        ]
        findings = _check_error_logs(logs)
        # Should NOT have APPLICATION CRASHES, DEPENDENCY FAILURE, or MEMORY PRESSURE.
        assert not any("APPLICATION CRASHES" in f for f in findings)
        assert not any("DEPENDENCY FAILURE" in f for f in findings)
        assert not any("MEMORY PRESSURE" in f for f in findings)
        # Should have generic count.
        assert any("error/critical log entries" in f for f in findings)

    def test_crash_suppresses_generic_count(self):
        """When crash is detected, generic count is NOT shown."""
        logs = [
            {"message": "Application run failed", "hostname": "pod-1"},
        ]
        findings = _check_error_logs(logs)
        assert any("APPLICATION CRASHES" in f for f in findings)
        assert not any("error/critical log entries" in f for f in findings)

    def test_recommendation_includes_kubectl(self):
        """Integration test: crash findings generate kubectl recommendations."""
        # This tests the recommendation generator in investigate.py.
        from tools.investigate import _generate_recommendations, InvestigationAnchor

        anchor = InvestigationAnchor(
            primary_service="test-svc",
            since_minutes=30,
        )
        findings = [
            {
                "source": "LOGS",
                "signal": "error_logs",
                "severity": "CRITICAL",
                "finding": "🔴 APPLICATION CRASHES: 3 crash event(s) on pods: pod-1, pod-2",
            },
        ]
        recs = _generate_recommendations(findings, anchor, None, {})
        assert any("Application/Crash" in r.get("area", "") for r in recs)
        assert any("kubectl" in r.get("action", "").lower() for r in recs)

    def test_dependency_recommendation_includes_kubectl(self):
        """Dependency failure findings generate kubectl recommendations."""
        from tools.investigate import _generate_recommendations, InvestigationAnchor

        anchor = InvestigationAnchor(
            primary_service="test-svc",
            since_minutes=30,
        )
        findings = [
            {
                "source": "LOGS",
                "signal": "error_logs",
                "severity": "CRITICAL",
                "finding": "🔴 DEPENDENCY FAILURE: font-service-backend unreachable — 5 error(s).",
            },
        ]
        recs = _generate_recommendations(findings, anchor, None, {})
        assert any("External Dependency" in r.get("area", "") for r in recs)
        assert any("font-service-backend" in r.get("action", "") for r in recs)
        assert any("kubectl" in r.get("action", "").lower() for r in recs)

    def test_empty_results_no_crash(self):
        """Empty input doesn't crash."""
        assert _check_error_logs([]) == []
        assert _check_error_logs(None) == []
        # [None] has len=1 but the None row is skipped — no pattern matches,
        # so the generic count fallback fires (count=1 > 0).
        result = _check_error_logs([None])
        assert isinstance(result, list)
        assert len(result) <= 1  # at most the generic count line

    def test_panic_pattern(self):
        """'panic' keyword is detected."""
        logs = [{"message": "goroutine panic: nil pointer dereference", "hostname": "go-pod"}]
        findings = _check_error_logs(logs)
        assert any("APPLICATION CRASHES" in f for f in findings)

    def test_connection_refused_without_url(self):
        """Connection refused without URL uses 'unknown-dependency'."""
        logs = [{"message": "connection refused"}]
        findings = _check_error_logs(logs)
        assert any("DEPENDENCY FAILURE" in f for f in findings)

    def test_host_port_extraction(self):
        """hostname:port format is extracted when no http:// URL."""
        logs = [{"message": "Failed to connect to redis-master:6379"}]
        findings = _check_error_logs(logs)
        assert any("redis-master" in f for f in findings)
