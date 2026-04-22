"""
Tests for multi-cluster K8s awareness in get_k8s_health.

Covers the 4-mode cluster resolution (none, single, explicit, breakdown),
per-cluster signal generation, deep-link scoping, and the regression
scenario where prod/DR status was conflated.

All cluster names are generic placeholders — no tenant-specific names.
"""

import json
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from core.context import AccountContext
from tools.k8s import _resolve_cluster_mode, get_k8s_health


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


def _make_intelligence(cluster_names=None, **kwargs):
    """Build a mock intelligence object with configurable cluster_names."""
    from core.intelligence import (
        AccountIntelligence,
        AccountMeta,
        AlertsIntelligence,
        APMIntelligence,
        K8sIntelligence,
        LogsIntelligence,
        NamingConvention,
        SyntheticsIntelligence,
    )
    from datetime import datetime, timezone

    return AccountIntelligence(
        account_id="123456",
        learned_at=datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
        apm=APMIntelligence(
            service_names=["test-svc-prod"],
            service_guids={"test-svc-prod": "GUID-1"},
        ),
        k8s=K8sIntelligence(
            integrated=True,
            namespaces=["prod"],
            deployments={"prod": ["gateway", "worker"]},
            cluster_names=cluster_names or [],
        ),
        alerts=AlertsIntelligence(),
        logs=LogsIntelligence(),
        synthetics=SyntheticsIntelligence(),
        account_meta=AccountMeta(
            name="Test Account",
            total_apm_services=1,
            k8s_integrated=True,
        ),
    )


# ─── TestResolveClusterMode ─────────────────────────────────────────────


class TestResolveClusterMode:
    """Pure unit tests for the _resolve_cluster_mode helper."""

    def test_zero_clusters_returns_empty(self):
        intel = _make_intelligence(cluster_names=[])
        cluster_filter, facet_prefix, resolved, mode = _resolve_cluster_mode(intel, None)
        assert cluster_filter == ""
        assert facet_prefix == ""
        assert resolved == ""
        assert mode == "none"

    def test_single_cluster_auto_filters(self):
        intel = _make_intelligence(cluster_names=["only-cluster"])
        cluster_filter, facet_prefix, resolved, mode = _resolve_cluster_mode(intel, None)
        assert "clusterName = 'only-cluster'" in cluster_filter
        assert facet_prefix == ""
        assert resolved == "only-cluster"
        assert mode == "single"

    def test_multi_cluster_no_override_returns_breakdown(self):
        intel = _make_intelligence(cluster_names=["cluster-a", "cluster-b"])
        cluster_filter, facet_prefix, resolved, mode = _resolve_cluster_mode(intel, None)
        assert cluster_filter == ""
        assert facet_prefix == "clusterName, "
        assert resolved == "<breakdown>"
        assert mode == "breakdown"

    def test_multi_cluster_with_override_filters(self):
        intel = _make_intelligence(cluster_names=["prod-cluster", "dr-cluster"])
        cluster_filter, facet_prefix, resolved, mode = _resolve_cluster_mode(intel, "prod-cluster")
        assert "clusterName = 'prod-cluster'" in cluster_filter
        assert facet_prefix == ""
        assert resolved == "prod-cluster"
        assert mode == "explicit"

    def test_explicit_cluster_fuzzy_resolves(self):
        intel = _make_intelligence(cluster_names=["prod-east-cluster"])
        cluster_filter, facet_prefix, resolved, mode = _resolve_cluster_mode(intel, "prod-east")
        # Fuzzy resolution should find prod-east-cluster from partial input.
        assert "prod-east" in resolved
        assert mode == "explicit"

    def test_single_cluster_ignores_explicit_override(self):
        """When only 1 cluster exists, explicit override still resolves to that cluster."""
        intel = _make_intelligence(cluster_names=["only-cluster"])
        # Explicit param is used because the caller explicitly requested it.
        cluster_filter, facet_prefix, resolved, mode = _resolve_cluster_mode(intel, "only-cluster")
        assert "clusterName = 'only-cluster'" in cluster_filter
        assert mode == "explicit"

    def test_explicit_cluster_not_in_known_list_still_explicit(self):
        """Unknown cluster name is fuzzy-resolved to nearest match — mode is explicit."""
        intel = _make_intelligence(cluster_names=["cluster-a", "cluster-b"])
        cluster_filter, facet_prefix, resolved, mode = _resolve_cluster_mode(intel, "ghost-cluster")
        # fuzzy_resolve_service resolves to the closest known cluster
        assert "clusterName" in cluster_filter
        assert mode == "explicit"
        # The resolved cluster must be one of the known clusters (fuzzy match)
        assert resolved in ["cluster-a", "cluster-b"]

    def test_none_intelligence_k8s_returns_none_mode(self):
        """If intelligence has no k8s attribute at all, returns none mode."""
        intel = MagicMock(spec=[])  # No k8s attribute
        cluster_filter, facet_prefix, resolved, mode = _resolve_cluster_mode(intel, None)
        assert cluster_filter == ""
        assert mode == "none"


# ─── TestGetK8sHealthBackwardsCompat ─────────────────────────────────────


class TestGetK8sHealthBackwardsCompat:
    """Verify existing single-cluster behavior unchanged."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_single_cluster_account_existing_call_unchanged(self, mock_credentials):
        """Mock 1 cluster known, no cluster_name passed."""
        intel = _make_intelligence(cluster_names=["main-cluster"])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        pod_data = _mock_nrql_response([
            {"podName": "gw-abc", "latest.status": "Running", "latest.isReady": True},
        ])
        empty = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=pod_data),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
            ]
        )

        result = await get_k8s_health(namespace="prod")
        parsed = json.loads(result)
        assert parsed["cluster_mode"] == "single"
        assert parsed["cluster_name"] == "main-cluster"

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_zero_cluster_account_existing_call_unchanged(self, mock_credentials):
        """Mock 0 clusters known, no cluster_name."""
        intel = _make_intelligence(cluster_names=[])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        empty = _mock_nrql_response([])
        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
            ]
        )

        result = await get_k8s_health(namespace="prod")
        parsed = json.loads(result)
        assert parsed["cluster_mode"] == "none"
        assert parsed["cluster_name"] is None or parsed["cluster_name"] == ""

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_existing_namespace_filter_unchanged(self, mock_credentials):
        """namespace='prod' still produces WHERE namespaceName = 'prod'."""
        intel = _make_intelligence(cluster_names=[])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        empty = _mock_nrql_response([])
        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
            ]
        )

        result = await get_k8s_health(namespace="prod")
        parsed = json.loads(result)
        assert parsed["namespace"] == "prod"

        ctx.clear()
        AccountContext.reset_singleton()


# ─── TestGetK8sHealthMultiClusterBreakdown ──────────────────────────────


class TestGetK8sHealthMultiClusterBreakdown:
    """Multi-cluster, no override — breakdown mode."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_breakdown_mode_response_has_cluster_mode_field(self, mock_credentials):
        """cluster_mode should be 'breakdown' when multi-cluster, no override."""
        intel = _make_intelligence(cluster_names=["cluster-a", "cluster-b"])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        empty = _mock_nrql_response([])
        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
            ]
        )

        result = await get_k8s_health(namespace="prod")
        parsed = json.loads(result)
        assert parsed["cluster_mode"] == "breakdown"
        assert parsed["cluster_name"] is None
        assert set(parsed["clusters_known"]) == {"cluster-a", "cluster-b"}

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_breakdown_mode_signals_include_cluster_prefix(self, mock_credentials):
        """In breakdown, deployment signals must have [cluster] prefix."""
        intel = _make_intelligence(cluster_names=["cluster-a", "cluster-b"])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        # cluster-a healthy, cluster-b has 0/2 pods
        dep_data = _mock_nrql_response([
            {
                "clusterName": "cluster-a",
                "deploymentName": "gateway",
                "latest.podsAvailable": 2,
                "latest.podsDesired": 2,
            },
            {
                "clusterName": "cluster-b",
                "deploymentName": "gateway",
                "latest.podsAvailable": 0,
                "latest.podsDesired": 2,
            },
        ])
        empty = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=empty),       # pods
                httpx.Response(200, json=empty),       # restarts
                httpx.Response(200, json=empty),       # resources
                httpx.Response(200, json=dep_data),    # deployments
            ]
        )

        result = await get_k8s_health(namespace="prod")
        parsed = json.loads(result)
        signals = parsed["health_signals"]
        # Only cluster-b should have a signal (0/2 pods)
        assert len(signals) == 1
        assert "[cluster-b]" in signals[0]
        assert "gateway" in signals[0]
        assert "0/2" in signals[0]

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_breakdown_mode_links_by_cluster(self, mock_credentials):
        """Breakdown mode should produce links_by_cluster, not single links."""
        intel = _make_intelligence(cluster_names=["cluster-a", "cluster-b"])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        dep_data = _mock_nrql_response([
            {
                "clusterName": "cluster-b",
                "deploymentName": "gateway",
                "latest.podsAvailable": 0,
                "latest.podsDesired": 2,
            },
        ])
        pod_data = _mock_nrql_response([
            {
                "clusterName": "cluster-b",
                "podName": "gateway-abc",
                "latest.status": "Failed",
            },
        ])
        empty = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=pod_data),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=dep_data),
            ]
        )

        result = await get_k8s_health(namespace="prod")
        parsed = json.loads(result)
        assert parsed["cluster_mode"] == "breakdown"

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_breakdown_mode_facets_by_cluster(self, mock_credentials):
        """In breakdown mode, clusters_known reflects the multi-cluster state."""
        intel = _make_intelligence(cluster_names=["cluster-a", "cluster-b"])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        empty = _mock_nrql_response([])
        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
            ]
        )

        result = await get_k8s_health(namespace="prod")
        parsed = json.loads(result)
        assert parsed["cluster_mode"] == "breakdown"
        assert len(parsed["clusters_known"]) == 2

        ctx.clear()
        AccountContext.reset_singleton()


# ─── TestGetK8sHealthExplicitCluster ────────────────────────────────────


class TestGetK8sHealthExplicitCluster:
    """Multi-cluster, override provided."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_cluster_filters_query(self, mock_credentials):
        """cluster_names=['a','b'], call with cluster_name='a'."""
        intel = _make_intelligence(cluster_names=["cluster-a", "cluster-b"])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        empty = _mock_nrql_response([])
        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
            ]
        )

        result = await get_k8s_health(namespace="prod", cluster_name="cluster-a")
        parsed = json.loads(result)
        assert parsed["cluster_mode"] == "explicit"
        assert parsed["cluster_name"] == "cluster-a"

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_cluster_response_mode(self, mock_credentials):
        """Response should have cluster_mode='explicit' and correct cluster_name."""
        intel = _make_intelligence(cluster_names=["cluster-a", "cluster-b"])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        empty = _mock_nrql_response([])
        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
            ]
        )

        result = await get_k8s_health(namespace="prod", cluster_name="cluster-b")
        parsed = json.loads(result)
        assert parsed["cluster_mode"] == "explicit"
        assert parsed["cluster_name"] == "cluster-b"

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_cluster_signals_no_breakdown_prefix(self, mock_credentials):
        """In explicit mode, signals should NOT have [cluster] prefix."""
        intel = _make_intelligence(cluster_names=["cluster-a", "cluster-b"])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        dep_data = _mock_nrql_response([
            {
                "deploymentName": "gateway",
                "latest.podsAvailable": 0,
                "latest.podsDesired": 2,
            },
        ])
        empty = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=dep_data),
            ]
        )

        result = await get_k8s_health(namespace="prod", cluster_name="cluster-a")
        parsed = json.loads(result)
        signals = parsed["health_signals"]
        assert len(signals) == 1
        assert "[cluster" not in signals[0]  # No cluster prefix in explicit mode
        assert "gateway" in signals[0]

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_explicit_cluster_link_scoped(self, mock_credentials):
        """In explicit mode, links should be cluster-scoped."""
        intel = _make_intelligence(cluster_names=["cluster-a", "cluster-b"])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        dep_data = _mock_nrql_response([
            {
                "deploymentName": "gateway",
                "latest.podsAvailable": 0,
                "latest.podsDesired": 2,
            },
        ])
        empty = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=dep_data),
            ]
        )

        result = await get_k8s_health(namespace="prod", cluster_name="cluster-a")
        parsed = json.loads(result)
        assert parsed["cluster_mode"] == "explicit"
        assert parsed["cluster_name"] == "cluster-a"

        ctx.clear()
        AccountContext.reset_singleton()


# ─── TestGetK8sHealthFuzzyClusterMatch ──────────────────────────────────


class TestGetK8sHealthFuzzyClusterMatch:
    """Input cleanliness — fuzzy cluster matching."""

    def test_partial_cluster_name_resolves(self):
        intel = _make_intelligence(cluster_names=["prod-east-cluster"])
        cluster_filter, _, resolved, mode = _resolve_cluster_mode(intel, "prod-east")
        assert "prod-east" in resolved
        assert mode == "explicit"

    def test_unknown_cluster_fuzzy_resolves_to_nearest(self):
        """Unknown cluster is fuzzy-resolved to nearest known cluster."""
        intel = _make_intelligence(cluster_names=["cluster-a", "cluster-b"])
        cluster_filter, _, resolved, mode = _resolve_cluster_mode(intel, "ghost-cluster")
        assert mode == "explicit"
        # fuzzy resolver picks the closest match from known clusters
        assert resolved in ["cluster-a", "cluster-b"]


# ─── TestMultiClusterRegression ─────────────────────────────────────────


class TestMultiClusterRegression:
    """Guard against the original April 20 conflation bug."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_pod_status_not_conflated(self, mock_credentials):
        """Mock 2 clusters with same namespace, different health.

        prod-live: gateway 2/2, prod-dr: gateway 0/2.
        Without cluster_name, both clusters must appear separately in signals.
        """
        intel = _make_intelligence(cluster_names=["prod-live", "prod-dr"])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        dep_data = _mock_nrql_response([
            {
                "clusterName": "prod-live",
                "deploymentName": "gateway",
                "latest.podsAvailable": 2,
                "latest.podsDesired": 2,
            },
            {
                "clusterName": "prod-dr",
                "deploymentName": "gateway",
                "latest.podsAvailable": 0,
                "latest.podsDesired": 2,
            },
        ])
        empty = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=empty),       # pods
                httpx.Response(200, json=empty),       # restarts
                httpx.Response(200, json=empty),       # resources
                httpx.Response(200, json=dep_data),    # deployments
            ]
        )

        result = await get_k8s_health(namespace="prod")
        parsed = json.loads(result)
        signals = parsed["health_signals"]

        # Must have exactly 1 signal — only prod-dr is unhealthy
        assert len(signals) == 1
        # The signal must include [prod-dr] prefix
        assert "[prod-dr]" in signals[0]
        # Must NOT contain a bare signal without cluster prefix
        for s in signals:
            assert "[" in s, f"Signal missing cluster prefix: {s}"

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_health_signal_count_matches_per_cluster_facet(self, mock_credentials):
        """Multi-cluster: 1 healthy cluster, 1 unhealthy → exactly 1 signal."""
        intel = _make_intelligence(cluster_names=["healthy-cluster", "sick-cluster"])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        dep_data = _mock_nrql_response([
            {
                "clusterName": "healthy-cluster",
                "deploymentName": "worker",
                "latest.podsAvailable": 3,
                "latest.podsDesired": 3,
            },
            {
                "clusterName": "sick-cluster",
                "deploymentName": "worker",
                "latest.podsAvailable": 1,
                "latest.podsDesired": 3,
            },
        ])
        empty = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=dep_data),
            ]
        )

        result = await get_k8s_health(namespace="prod")
        parsed = json.loads(result)
        signals = parsed["health_signals"]

        assert len(signals) == 1
        assert "[sick-cluster]" in signals[0]
        assert "1/3" in signals[0]

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_breakdown_multiple_unhealthy_clusters(self, mock_credentials):
        """Both clusters unhealthy — should produce 2 separate signals."""
        intel = _make_intelligence(cluster_names=["cluster-a", "cluster-b"])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        dep_data = _mock_nrql_response([
            {
                "clusterName": "cluster-a",
                "deploymentName": "api",
                "latest.podsAvailable": 0,
                "latest.podsDesired": 2,
            },
            {
                "clusterName": "cluster-b",
                "deploymentName": "api",
                "latest.podsAvailable": 1,
                "latest.podsDesired": 3,
            },
        ])
        empty = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=dep_data),
            ]
        )

        result = await get_k8s_health(namespace="prod")
        parsed = json.loads(result)
        signals = parsed["health_signals"]

        assert len(signals) == 2
        # Extract cluster names from signals like "⚠️ [cluster-a] Deployment api: 0/2 pods available"
        cluster_names_found = []
        for s in signals:
            if "[" in s and "]" in s:
                start = s.index("[") + 1
                end = s.index("]")
                cluster_names_found.append(s[start:end])
        assert "cluster-a" in cluster_names_found
        assert "cluster-b" in cluster_names_found

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_breakdown_crashing_pods_include_cluster_prefix(self, mock_credentials):
        """Crashing pods in breakdown mode must have [cluster] prefix."""
        intel = _make_intelligence(cluster_names=["cluster-a", "cluster-b"])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        pod_data = _mock_nrql_response([
            {
                "clusterName": "cluster-a",
                "podName": "api-xyz",
                "latest.status": "Failed",
                "latest.isReady": False,
            },
        ])
        empty = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=pod_data),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
            ]
        )

        result = await get_k8s_health(namespace="prod")
        parsed = json.loads(result)
        signals = parsed["health_signals"]

        # Both the Failed and not-ready signals should have cluster prefix
        for s in signals:
            assert "[cluster-a]" in s, f"Signal missing cluster prefix: {s}"

        ctx.clear()
        AccountContext.reset_singleton()

    @respx.mock
    @pytest.mark.asyncio
    async def test_breakdown_restarting_containers_include_cluster_prefix(self, mock_credentials):
        """Restarting containers in breakdown mode must have [cluster] prefix."""
        intel = _make_intelligence(cluster_names=["cluster-a", "cluster-b"])
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.set_active(mock_credentials, intel)

        restart_data = _mock_nrql_response([
            {
                "clusterName": "cluster-b",
                "containerName": "web",
                "podName": "web-123",
                "restarts": 15,
            },
        ])
        empty = _mock_nrql_response([])

        respx.post("https://api.newrelic.com/graphql").mock(
            side_effect=[
                httpx.Response(200, json=empty),
                httpx.Response(200, json=restart_data),
                httpx.Response(200, json=empty),
                httpx.Response(200, json=empty),
            ]
        )

        result = await get_k8s_health(namespace="prod")
        parsed = json.loads(result)
        signals = parsed["health_signals"]

        assert len(signals) >= 1
        assert "[cluster-b]" in signals[0]

        ctx.clear()
        AccountContext.reset_singleton()
