"""
Regression tests for account intelligence — naming-convention learning,
env-preserving fuzzy resolution.

(K8s bare-name, null-guard, discovery, and log-pattern tests were removed
alongside the legacy monolith investigation engine.)
"""

import pytest

from core.intelligence import (
    AccountIntelligence,
    NamingConvention,
    _learn_naming_convention,
)
from core.sanitize import fuzzy_resolve_service


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
