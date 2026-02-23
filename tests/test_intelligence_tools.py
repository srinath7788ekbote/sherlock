"""
Tests for intelligence management tools.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.context import AccountContext
from core.intelligence import AccountIntelligence
from tools.intelligence_tools import (
    connect_account,
    get_account_summary,
    get_nrql_context,
    learn_account_tool,
    list_profiles,
)


class TestConnectAccount:
    """Tests for connect_account."""

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools._credential_manager")
    @patch("tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    @patch("tools.intelligence_tools._cache")
    async def test_successful_connection(
        self, mock_cache, mock_learn, mock_cred_mgr, mock_intelligence
    ):
        """Successful connection returns status=connected."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(return_value={
            "valid": True,
            "account_name": "Acme Corp",
            "user_name": "test-user",
        })
        mock_cache.get.return_value = None
        mock_cache.get_stale.return_value = None
        mock_learn.return_value = mock_intelligence

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert parsed["status"] == "connected"
        assert parsed["account_id"] == "123456"
        assert parsed["account_name"] == "Acme Corp"
        assert "summary" in parsed
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools._credential_manager")
    async def test_invalid_credentials(self, mock_cred_mgr):
        """Invalid credentials return error."""
        mock_cred_mgr.validate_credentials = AsyncMock(return_value={
            "valid": False,
            "error": "Invalid API key",
        })

        result = await connect_account("123456", "bad-key", "US")
        parsed = json.loads(result)

        assert "error" in parsed
        assert "Credential validation failed" in parsed["error"]
        assert parsed["data_available"] is False

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools._credential_manager")
    @patch("tools.intelligence_tools._cache")
    async def test_uses_cached_intelligence(
        self, mock_cache, mock_cred_mgr, mock_intelligence
    ):
        """Uses cached intelligence when available."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(return_value={
            "valid": True,
            "account_name": "Acme",
            "user_name": "user",
        })
        mock_cache.get.return_value = mock_intelligence.model_dump(mode="json")

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert parsed["status"] == "connected"
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools._credential_manager")
    @patch("tools.intelligence_tools._cache")
    @patch("tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    async def test_saves_profile_when_requested(
        self, mock_learn, mock_cache, mock_cred_mgr, mock_intelligence
    ):
        """Profile is saved when profile_name is provided."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(return_value={
            "valid": True,
            "account_name": "Acme",
            "user_name": "user",
        })
        mock_cache.get.return_value = None
        mock_cache.get_stale.return_value = None
        mock_learn.return_value = mock_intelligence

        result = await connect_account("123456", "NRAK-key", "US", profile_name="prod")
        parsed = json.loads(result)

        assert parsed["profile_saved"] is True
        mock_cred_mgr.save_profile.assert_called_once_with("prod", "123456", "NRAK-key", "US")
        AccountContext.reset_singleton()


class TestLearnAccountTool:
    """Tests for learn_account_tool."""

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    @patch("tools.intelligence_tools._cache")
    async def test_successful_refresh(
        self, mock_cache, mock_learn, mock_context, mock_intelligence
    ):
        """Re-learn returns refreshed status."""
        mock_learn.return_value = mock_intelligence

        result = await learn_account_tool()
        parsed = json.loads(result)

        assert parsed["status"] == "refreshed"
        assert parsed["account_id"] == "123456"
        mock_cache.invalidate.assert_called_once_with("123456")

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        """Error when no active context."""
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.clear()

        result = await learn_account_tool()
        parsed = json.loads(result)

        assert "error" in parsed
        AccountContext.reset_singleton()


class TestGetAccountSummary:
    """Tests for get_account_summary."""

    @pytest.mark.asyncio
    async def test_returns_full_summary(self, mock_context):
        """Returns complete intelligence summary."""
        result = await get_account_summary()
        parsed = json.loads(result)

        assert parsed["account_id"] == "123456"
        assert parsed["account_name"] == "Acme Corp Production"
        assert "apm" in parsed
        assert "k8s" in parsed
        assert "alerts" in parsed
        assert "logs" in parsed
        assert "synthetics" in parsed
        assert "infra" in parsed
        assert "browser" in parsed
        assert parsed["apm"]["service_names"] == [
            "payment-svc-prod",
            "auth-service-prod",
            "export-worker-prod",
        ]

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        """Error when no active context."""
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.clear()

        result = await get_account_summary()
        parsed = json.loads(result)

        assert "error" in parsed
        AccountContext.reset_singleton()


class TestListProfiles:
    """Tests for list_profiles."""

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools._credential_manager")
    async def test_returns_profiles(self, mock_cred_mgr):
        """Returns profile list."""
        mock_cred_mgr.list_profiles.return_value = [
            {"name": "prod", "account_id": "123456"},
            {"name": "staging", "account_id": "789012"},
        ]

        result = await list_profiles()
        parsed = json.loads(result)

        assert parsed["total_profiles"] == 2
        assert len(parsed["profiles"]) == 2

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools._credential_manager")
    async def test_empty_profiles(self, mock_cred_mgr):
        """No profiles returns empty list."""
        mock_cred_mgr.list_profiles.return_value = []

        result = await list_profiles()
        parsed = json.loads(result)

        assert parsed["total_profiles"] == 0
        assert parsed["profiles"] == []


class TestGetNrqlContext:
    """Tests for get_nrql_context."""

    @pytest.mark.asyncio
    async def test_all_domains(self, mock_context):
        """Domain='all' returns all sections."""
        result = await get_nrql_context("all")
        parsed = json.loads(result)

        assert parsed["domain"] == "all"
        assert "apm" in parsed
        assert "k8s" in parsed
        assert "logs" in parsed
        assert "alerts" in parsed
        assert "synthetics" in parsed

    @pytest.mark.asyncio
    async def test_apm_domain_only(self, mock_context):
        """Domain='apm' returns only apm section."""
        result = await get_nrql_context("apm")
        parsed = json.loads(result)

        assert parsed["domain"] == "apm"
        assert "apm" in parsed
        assert "k8s" not in parsed
        assert "payment-svc-prod" in parsed["apm"]["service_names"]

    @pytest.mark.asyncio
    async def test_k8s_domain(self, mock_context):
        """Domain='k8s' returns k8s context."""
        result = await get_nrql_context("k8s")
        parsed = json.loads(result)

        assert "k8s" in parsed
        assert "payments-prod" in parsed["k8s"]["namespaces"]
        assert "main-cluster-prod" in parsed["k8s"]["cluster_names"]

    @pytest.mark.asyncio
    async def test_synthetics_domain(self, mock_context):
        """Domain='synthetics' returns monitor context."""
        result = await get_nrql_context("synthetics")
        parsed = json.loads(result)

        assert "synthetics" in parsed
        assert "Login Flow - Production" in parsed["synthetics"]["monitor_names"]
        assert "SCRIPT_BROWSER" in parsed["synthetics"]["monitor_types"]
        assert len(parsed["synthetics"]["available_locations"]) > 0

    @pytest.mark.asyncio
    async def test_not_connected_returns_error(self):
        """Error when no active context."""
        AccountContext.reset_singleton()
        ctx = AccountContext()
        ctx.clear()

        result = await get_nrql_context()
        parsed = json.loads(result)

        assert "error" in parsed
        AccountContext.reset_singleton()
