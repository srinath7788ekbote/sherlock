"""
Tests for account intelligence learning.
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.intelligence import (
    AccountIntelligence,
    APMIntelligence,
    K8sIntelligence,
    AlertsIntelligence,
    LogsIntelligence,
    SyntheticsIntelligence,
    SyntheticMonitorMeta,
    InfraIntelligence,
    BrowserIntelligence,
    MobileIntelligence,
    OTelIntelligence,
    WorkloadIntelligence,
    EntityCountsSummary,
    AccountMeta,
    learn_account,
)


class TestAccountIntelligence:
    """Tests for the AccountIntelligence model."""

    def test_default_intelligence_is_empty(self):
        """New intelligence instance has empty collections."""
        intel = AccountIntelligence(account_id="test")
        assert intel.apm.service_names == []
        assert intel.k8s.namespaces == []
        assert intel.synthetics.monitor_names == []

    def test_synthetics_monitor_meta(self):
        """SyntheticMonitorMeta stores monitor information."""
        meta = SyntheticMonitorMeta(
            name="Checkout Flow",
            guid="ABC123",
            type="SCRIPT_BROWSER",
            status="ENABLED",
            locations=["AWS_US_EAST_1", "AWS_EU_WEST_1"],
            period="EVERY_5_MINUTES",
            associated_service="checkout-service",
        )
        assert meta.name == "Checkout Flow"
        assert len(meta.locations) == 2
        assert meta.associated_service == "checkout-service"

    def test_intelligence_serialization(self, mock_intelligence):
        """Intelligence model can be serialized to dict and back."""
        dumped = mock_intelligence.model_dump()
        restored = AccountIntelligence(**dumped)
        assert restored.apm.service_names == mock_intelligence.apm.service_names
        assert len(restored.synthetics.monitor_names) == len(mock_intelligence.synthetics.monitor_names)


class TestLearnAccount:
    """Tests for the learn_account function."""

    @respx.mock
    @pytest.mark.asyncio
    async def test_learn_account_discovers_services(self, mock_credentials):
        """learn_account populates APM service names from NerdGraph."""

        def _handler(request):
            body = json.loads(request.content)
            query = body.get("query", "")
            # APM entity search
            if "domain = 'APM'" in query and "APPLICATION" in query:
                return httpx.Response(200, json={
                    "data": {"actor": {"entitySearch": {
                        "count": 2,
                        "results": {
                            "nextCursor": None,
                            "entities": [
                                {"guid": "G1", "name": "web-api", "tags": []},
                                {"guid": "G2", "name": "worker", "tags": []},
                            ],
                        },
                    }}}
                })
            # Other entitySearch queries
            if "entitySearch" in query:
                return httpx.Response(200, json={
                    "data": {"actor": {"entitySearch": {
                        "count": 0, "results": {"entities": []}, "types": [],
                    }}}
                })
            # Alert policies
            if "policiesSearch" in query:
                return httpx.Response(200, json={
                    "data": {"actor": {"account": {"alerts": {
                        "policiesSearch": {"policies": [], "totalCount": 0}
                    }}}}
                })
            # NRQL + account meta queries
            return httpx.Response(200, json={
                "data": {"actor": {"account": {"name": "Test", "nrql": {"results": []}}}}
            })

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_handler)

        intel = await learn_account(mock_credentials)
        assert isinstance(intel, AccountIntelligence)
        assert "web-api" in intel.apm.service_names
        assert "worker" in intel.apm.service_names

    @respx.mock
    @pytest.mark.asyncio
    async def test_learn_account_discovers_monitors(self, mock_credentials):
        """learn_account populates synthetic monitor metadata."""

        def _handler(request):
            body = json.loads(request.content)
            query = body.get("query", "")
            # Synthetic monitors
            if "SYNTH" in query and "MONITOR" in query:
                return httpx.Response(200, json={
                    "data": {"actor": {"entitySearch": {
                        "count": 2,
                        "results": {
                            "nextCursor": None,
                            "entities": [
                                {
                                    "guid": "GUID1", "name": "Login Flow",
                                    "monitorType": "SCRIPT_BROWSER",
                                    "period": "EVERY_5_MINUTES",
                                    "alertSeverity": None,
                                },
                                {
                                    "guid": "GUID2", "name": "API Health",
                                    "monitorType": "SCRIPT_API",
                                    "period": "EVERY_MINUTE",
                                    "alertSeverity": None,
                                },
                            ],
                        },
                    }}}
                })
            # Other entitySearch queries
            if "entitySearch" in query:
                return httpx.Response(200, json={
                    "data": {"actor": {"entitySearch": {
                        "count": 0, "results": {"entities": []}, "types": [],
                    }}}
                })
            if "policiesSearch" in query:
                return httpx.Response(200, json={
                    "data": {"actor": {"account": {"alerts": {
                        "policiesSearch": {"policies": [], "totalCount": 0}
                    }}}}
                })
            return httpx.Response(200, json={
                "data": {"actor": {"account": {"name": "Test", "nrql": {"results": []}}}}
            })

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_handler)

        intel = await learn_account(mock_credentials)
        assert len(intel.synthetics.monitor_names) >= 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_learn_account_handles_errors_gracefully(self, mock_credentials):
        """learn_account returns partial data even when some queries fail."""

        def _handler(request):
            body = json.loads(request.content)
            query = body.get("query", "")
            # APM entities succeed
            if "domain = 'APM'" in query and "APPLICATION" in query:
                return httpx.Response(200, json={
                    "data": {"actor": {"entitySearch": {
                        "count": 1,
                        "results": {
                            "nextCursor": None,
                            "entities": [
                                {"guid": "G1", "name": "survivor-app", "tags": []},
                            ],
                        },
                    }}}
                })
            # Synthetic and browser queries fail
            if "SYNTH" in query or "BROWSER" in query:
                return httpx.Response(500)
            # Other entitySearch queries
            if "entitySearch" in query:
                return httpx.Response(200, json={
                    "data": {"actor": {"entitySearch": {
                        "count": 0, "results": {"entities": []}, "types": [],
                    }}}
                })
            if "policiesSearch" in query:
                return httpx.Response(200, json={
                    "data": {"actor": {"account": {"alerts": {
                        "policiesSearch": {"policies": [], "totalCount": 0}
                    }}}}
                })
            return httpx.Response(200, json={
                "data": {"actor": {"account": {"name": "Test", "nrql": {"results": []}}}}
            })

        respx.post("https://api.newrelic.com/graphql").mock(side_effect=_handler)

        intel = await learn_account(mock_credentials)
        assert "survivor-app" in intel.apm.service_names
