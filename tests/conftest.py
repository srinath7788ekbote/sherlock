"""
Shared test fixtures for the Sherlock test suite.

Provides mock credentials, intelligence, context, and NerdGraph
response interceptors for all test modules.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from core.context import AccountContext
from core.credentials import Credentials
from core.dependency_graph import (
    DependencyGraph,
    DependencyNode,
    ServiceDependency,
)
from core.intelligence import (
    AccountIntelligence,
    AccountMeta,
    AlertsIntelligence,
    APMIntelligence,
    BrowserIntelligence,
    EntityCountsSummary,
    InfraIntelligence,
    K8sIntelligence,
    LogsIntelligence,
    MobileIntelligence,
    NamingConvention,
    OTelIntelligence,
    SyntheticMonitorMeta,
    SyntheticsIntelligence,
    WorkloadIntelligence,
)


@pytest.fixture
def mock_credentials() -> Credentials:
    """Provide test New Relic credentials."""
    return Credentials(
        account_id="123456",
        api_key="NRAK-test123456789abcdef",
        region="US",
    )


@pytest.fixture
def mock_intelligence() -> AccountIntelligence:
    """Provide a fully populated AccountIntelligence for testing."""
    return AccountIntelligence(
        account_id="123456",
        learned_at=datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
        apm=APMIntelligence(
            service_names=[
                "payment-svc-prod",
                "auth-service-prod",
                "export-worker-prod",
            ],
            service_guids={
                "payment-svc-prod": "MTIzNDU2fEFQTXxBUFBMSUNBVElPTnwx",
                "auth-service-prod": "MTIzNDU2fEFQTXxBUFBMSUNBVElPTnwy",
                "export-worker-prod": "MTIzNDU2fEFQTXxBUFBMSUNBVElPTnwz",
            },
            service_languages={
                "payment-svc-prod": "java",
                "auth-service-prod": "python",
                "export-worker-prod": "nodejs",
            },
            naming_pattern="kebab-case, env-suffixed",
            top_error_classes=[
                "java.lang.NullPointerException",
                "TimeoutError",
                "ConnectionRefusedError",
            ],
            environments=["prod", "staging"],
        ),
        k8s=K8sIntelligence(
            integrated=True,
            namespaces=["payments-prod", "auth-prod", "data-prod"],
            deployments={
                "payments-prod": ["payment-svc-prod", "payment-worker-prod"],
                "auth-prod": ["auth-service-prod"],
                "data-prod": ["export-worker-prod"],
            },
            cluster_names=["main-cluster-prod"],
            naming_pattern="kebab-case, env-suffixed",
        ),
        alerts=AlertsIntelligence(
            policy_names=[
                "Payment Service - Critical",
                "Auth Service - Warning",
                "Export Worker - Critical",
            ],
            naming_pattern="env-suffixed",
        ),
        logs=LogsIntelligence(
            enabled=True,
            service_attribute="service.name",
            severity_attribute="level",
            top_error_messages=["Connection refused", "Timeout exceeded"],
        ),
        synthetics=SyntheticsIntelligence(
            enabled=True,
            monitor_names=[
                "Login Flow - Production",
                "Payment Checkout - Prod",
                "Export API Health Check",
                "Auth Token Refresh - Prod",
            ],
            monitor_map={
                "Login Flow - Production": SyntheticMonitorMeta(
                    guid="SYNTH-GUID-001",
                    name="Login Flow - Production",
                    type="SCRIPT_BROWSER",
                    status="ENABLED",
                    period="EVERY_5_MINUTES",
                    locations=["AWS_US_EAST_1", "AWS_EU_WEST_1", "AWS_AP_SOUTHEAST_1"],
                    associated_service="auth-service-prod",
                ),
                "Payment Checkout - Prod": SyntheticMonitorMeta(
                    guid="SYNTH-GUID-002",
                    name="Payment Checkout - Prod",
                    type="SCRIPT_BROWSER",
                    status="ENABLED",
                    period="EVERY_5_MINUTES",
                    locations=["AWS_US_EAST_1", "AWS_EU_WEST_1"],
                    associated_service="payment-svc-prod",
                ),
                "Export API Health Check": SyntheticMonitorMeta(
                    guid="SYNTH-GUID-003",
                    name="Export API Health Check",
                    type="SCRIPT_API",
                    status="ENABLED",
                    period="EVERY_MINUTE",
                    locations=["AWS_US_EAST_1"],
                    associated_service="export-worker-prod",
                ),
                "Auth Token Refresh - Prod": SyntheticMonitorMeta(
                    guid="SYNTH-GUID-004",
                    name="Auth Token Refresh - Prod",
                    type="SCRIPT_API",
                    status="ENABLED",
                    period="EVERY_5_MINUTES",
                    locations=["AWS_US_EAST_1", "AWS_EU_WEST_1"],
                    associated_service="auth-service-prod",
                ),
            },
            monitor_types=["SCRIPT_API", "SCRIPT_BROWSER"],
            naming_pattern="env-suffixed",
            total_count=4,
        ),
        infra=InfraIntelligence(
            cloud_provider="AWS",
            regions=["us-east-1", "eu-west-1"],
            host_count=12,
        ),
        browser=BrowserIntelligence(
            enabled=True,
            app_names=["Payment Portal"],
        ),
        account_meta=AccountMeta(
            name="Acme Corp Production",
            total_apm_services=3,
            k8s_integrated=True,
            logs_enabled=True,
            synthetics_enabled=True,
            synthetics_count=4,
        ),
    )


@pytest.fixture
def mock_context(mock_credentials, mock_intelligence):
    """Set up active account context with mock data."""
    AccountContext.reset_singleton()
    ctx = AccountContext()
    ctx.set_active(mock_credentials, mock_intelligence)
    yield ctx
    ctx.clear()
    AccountContext.reset_singleton()


@pytest.fixture
def mock_nerdgraph(mock_credentials):
    """Set up respx to intercept NerdGraph API calls."""
    with respx.mock(assert_all_called=False) as router:
        route = router.post("https://api.newrelic.com/graphql")
        route.mock(return_value=httpx.Response(
            200,
            json={"data": {"actor": {"account": {"nrql": {"results": []}}}}},
        ))
        yield router


@pytest.fixture
def mock_synthetic_check_passing():
    """Provide a mock SyntheticCheck response with all locations passing."""
    return {
        "data": {
            "actor": {
                "account": {
                    "nrql": {
                        "results": [
                            {
                                "pass_rate": 100.0,
                                "total_runs": 120,
                                "avg_duration_ms": 2500.0,
                            }
                        ]
                    }
                }
            }
        }
    }


@pytest.fixture
def mock_synthetic_check_global_failure():
    """Provide a mock SyntheticCheck response with all locations failing."""
    return {
        "data": {
            "actor": {
                "account": {
                    "nrql": {
                        "results": [
                            {
                                "pass_rate": 0.0,
                                "total_runs": 120,
                                "avg_duration_ms": 15000.0,
                            }
                        ]
                    }
                }
            }
        }
    }


@pytest.fixture
def mock_synthetic_location_results_passing():
    """Provide mock by-location data with all locations passing."""
    return {
        "data": {
            "actor": {
                "account": {
                    "nrql": {
                        "results": [
                            {
                                "facet": "AWS_US_EAST_1",
                                "locationLabel": "AWS_US_EAST_1",
                                "last_result": "SUCCESS",
                                "pass_rate": 100.0,
                                "last_duration_ms": 2400,
                                "last_error": None,
                            },
                            {
                                "facet": "AWS_EU_WEST_1",
                                "locationLabel": "AWS_EU_WEST_1",
                                "last_result": "SUCCESS",
                                "pass_rate": 100.0,
                                "last_duration_ms": 2600,
                                "last_error": None,
                            },
                        ]
                    }
                }
            }
        }
    }


@pytest.fixture
def mock_synthetic_location_results_regional():
    """Provide mock by-location data with regional failure."""
    return {
        "data": {
            "actor": {
                "account": {
                    "nrql": {
                        "results": [
                            {
                                "facet": "AWS_US_EAST_1",
                                "locationLabel": "AWS_US_EAST_1",
                                "last_result": "SUCCESS",
                                "pass_rate": 100.0,
                                "last_duration_ms": 2400,
                                "last_error": None,
                            },
                            {
                                "facet": "AWS_EU_WEST_1",
                                "locationLabel": "AWS_EU_WEST_1",
                                "last_result": "FAILED",
                                "pass_rate": 30.0,
                                "last_duration_ms": 15000,
                                "last_error": "Timeout waiting for element",
                            },
                        ]
                    }
                }
            }
        }
    }


@pytest.fixture
def mock_synthetic_location_results_global():
    """Provide mock by-location data with global failure."""
    return {
        "data": {
            "actor": {
                "account": {
                    "nrql": {
                        "results": [
                            {
                                "facet": "AWS_US_EAST_1",
                                "locationLabel": "AWS_US_EAST_1",
                                "last_result": "FAILED",
                                "pass_rate": 0.0,
                                "last_duration_ms": 15000,
                                "last_error": "Connection refused",
                            },
                            {
                                "facet": "AWS_EU_WEST_1",
                                "locationLabel": "AWS_EU_WEST_1",
                                "last_result": "FAILED",
                                "pass_rate": 0.0,
                                "last_duration_ms": 15000,
                                "last_error": "Connection refused",
                            },
                        ]
                    }
                }
            }
        }
    }


@pytest.fixture
def mock_golden_signals_healthy():
    """Provide mock golden signals data for a healthy service."""
    return json.dumps({
        "service_name": "auth-service-prod",
        "overall_status": "HEALTHY",
        "health_signals": [],
        "latency": {"avg_duration_s": 0.15, "p99": 0.5},
        "throughput": {"rpm": 1200},
        "errors": {"error_rate_pct": 0.5, "total_transactions": 36000},
        "saturation": {"avg_cpu_pct": 45},
    })


@pytest.fixture
def mock_golden_signals_critical():
    """Provide mock golden signals data for a critical service."""
    return json.dumps({
        "service_name": "auth-service-prod",
        "overall_status": "CRITICAL",
        "health_signals": [
            "🔴 CRITICAL error rate: 45.2%",
            "🔴 ZERO throughput — service may be down",
        ],
        "latency": {"avg_duration_s": 5.0, "p99": 12.0},
        "throughput": {"rpm": 0},
        "errors": {"error_rate_pct": 45.2, "total_transactions": 100},
        "saturation": {"avg_cpu_pct": 95},
    })


@pytest.fixture
def mock_dependency_graph() -> DependencyGraph:
    """Provide a mock DependencyGraph for testing.

    Graph topology:
      payment-svc-prod → auth-service-prod → export-worker-prod
      payment-svc-prod → export-worker-prod (direct)
    """
    payment_to_auth = ServiceDependency(
        caller="payment-svc-prod",
        callee="auth-service-prod",
        call_count=5000,
        error_rate=2.5,
        avg_latency_ms=150.0,
        source="span",
        confidence=1.0,
    )
    auth_to_export = ServiceDependency(
        caller="auth-service-prod",
        callee="export-worker-prod",
        call_count=1200,
        error_rate=15.0,
        avg_latency_ms=8000.0,
        source="span",
        confidence=1.0,
    )
    payment_to_export = ServiceDependency(
        caller="payment-svc-prod",
        callee="export-worker-prod",
        call_count=800,
        error_rate=0.5,
        avg_latency_ms=200.0,
        source="span",
        confidence=1.0,
    )

    payment_node = DependencyNode(
        service_name="payment-svc-prod",
        direct_dependencies=["auth-service-prod", "export-worker-prod"],
        direct_dependents=[],
        transitive_dependencies=["auth-service-prod", "export-worker-prod"],
        dependency_details={
            "auth-service-prod": payment_to_auth,
            "export-worker-prod": payment_to_export,
        },
    )
    auth_node = DependencyNode(
        service_name="auth-service-prod",
        direct_dependencies=["export-worker-prod"],
        direct_dependents=["payment-svc-prod"],
        transitive_dependencies=["export-worker-prod"],
        dependency_details={
            "export-worker-prod": auth_to_export,
        },
    )
    export_node = DependencyNode(
        service_name="export-worker-prod",
        direct_dependencies=[],
        direct_dependents=["payment-svc-prod", "auth-service-prod"],
        transitive_dependencies=[],
        dependency_details={},
    )

    return DependencyGraph(
        account_id="123456",
        total_services=3,
        total_edges=3,
        nodes={
            "payment-svc-prod": payment_node,
            "auth-service-prod": auth_node,
            "export-worker-prod": export_node,
        },
        build_source="span",
        coverage_pct=100.0,
        external_dependencies={
            "payment-svc-prod": ["stripe-api.com", "cdn.example.com"],
        },
    )
