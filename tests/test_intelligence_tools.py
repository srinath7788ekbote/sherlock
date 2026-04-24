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
        mock_cache.get.return_value = None  # No cache → full learn path
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


class TestConnectAccountIntelligenceSource:
    """Tests for intelligence_source, skip_learn_account, and naming_convention
    fields added to connect_account response."""

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools._credential_manager")
    @patch("tools.intelligence_tools._cache")
    async def test_cached_returns_source_cached(
        self, mock_cache, mock_cred_mgr, mock_intelligence
    ):
        """connect_account with fresh cache returns intelligence_source='cached'."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(return_value={
            "valid": True,
            "account_name": "Acme Corp",
            "user_name": "test-user",
        })
        mock_cache.get.return_value = mock_intelligence.model_dump(mode="json")

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert parsed["intelligence_source"] == "cached"
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools._credential_manager")
    @patch("tools.intelligence_tools._cache")
    async def test_stale_returns_source_stale(
        self, mock_cache, mock_cred_mgr, mock_intelligence
    ):
        """connect_account with stale cache returns intelligence_source='stale_with_bg_refresh'."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(return_value={
            "valid": True,
            "account_name": "Acme Corp",
            "user_name": "test-user",
        })
        mock_cache.get.return_value = None
        mock_cache.get_stale.return_value = mock_intelligence.model_dump(mode="json")

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert parsed["intelligence_source"] == "stale_with_bg_refresh"
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools._credential_manager")
    @patch("tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    @patch("tools.intelligence_tools._cache")
    async def test_no_cache_returns_source_fresh(
        self, mock_cache, mock_learn, mock_cred_mgr, mock_intelligence
    ):
        """connect_account with no cache returns intelligence_source='fresh_learn'."""
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

        assert parsed["intelligence_source"] == "fresh_learn"
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools._credential_manager")
    @patch("tools.intelligence_tools._cache")
    async def test_skip_learn_true_for_cached(
        self, mock_cache, mock_cred_mgr, mock_intelligence
    ):
        """connect_account returns skip_learn_account=True when cached."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(return_value={
            "valid": True,
            "account_name": "Acme",
            "user_name": "user",
        })
        mock_cache.get.return_value = mock_intelligence.model_dump(mode="json")

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert parsed["skip_learn_account"] is True
        assert "skip_learn_reason" in parsed
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools._credential_manager")
    @patch("tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    @patch("tools.intelligence_tools._cache")
    async def test_skip_learn_false_for_fresh(
        self, mock_cache, mock_learn, mock_cred_mgr, mock_intelligence
    ):
        """connect_account returns skip_learn_account=False when fresh learn."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(return_value={
            "valid": True,
            "account_name": "Acme",
            "user_name": "user",
        })
        mock_cache.get.return_value = None
        mock_cache.get_stale.return_value = None
        mock_learn.return_value = mock_intelligence

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert parsed["skip_learn_account"] is False
        assert "skip_learn_reason" not in parsed
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools._credential_manager")
    @patch("tools.intelligence_tools._cache")
    async def test_naming_convention_in_response(
        self, mock_cache, mock_cred_mgr, mock_intelligence
    ):
        """connect_account response includes naming_convention summary."""
        AccountContext.reset_singleton()
        mock_cred_mgr.validate_credentials = AsyncMock(return_value={
            "valid": True,
            "account_name": "Acme",
            "user_name": "user",
        })
        mock_cache.get.return_value = mock_intelligence.model_dump(mode="json")

        result = await connect_account("123456", "NRAK-key", "US")
        parsed = json.loads(result)

        assert "naming_convention" in parsed
        nc = parsed["naming_convention"]
        assert "separator" in nc
        assert "env_position" in nc
        assert "env_values" in nc
        assert "apm_to_k8s_namespace_map" in nc
        assert "k8s_deployment_name_format" in nc
        assert "segment_roles" in nc
        AccountContext.reset_singleton()


class TestLearnAccountShortCircuit:
    """Tests for learn_account_tool server-side cache guardrail."""

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools._cache")
    async def test_returns_already_learned_when_cached(
        self, mock_cache, mock_context, mock_intelligence
    ):
        """learn_account short-circuits with 'already_learned' when cache has data."""
        mock_cache.get.return_value = mock_intelligence.model_dump(mode="json")

        result = await learn_account_tool()
        parsed = json.loads(result)

        assert parsed["status"] == "already_learned"
        assert parsed["account_id"] == "123456"
        assert "hint" in parsed
        mock_cache.invalidate.assert_not_called()

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    @patch("tools.intelligence_tools._cache")
    async def test_force_true_bypasses_cache(
        self, mock_cache, mock_learn, mock_context, mock_intelligence
    ):
        """learn_account with force=True always re-learns even with cache."""
        mock_cache.get.return_value = mock_intelligence.model_dump(mode="json")
        mock_learn.return_value = mock_intelligence

        result = await learn_account_tool(force=True)
        parsed = json.loads(result)

        assert parsed["status"] == "refreshed"
        mock_cache.invalidate.assert_called_once_with("123456")

    @pytest.mark.asyncio
    @patch("tools.intelligence_tools.learn_account", new_callable=AsyncMock)
    @patch("tools.intelligence_tools._cache")
    async def test_no_cache_does_full_learn(
        self, mock_cache, mock_learn, mock_context, mock_intelligence
    ):
        """learn_account without cache performs full learn."""
        mock_cache.get.return_value = None
        mock_learn.return_value = mock_intelligence

        result = await learn_account_tool()
        parsed = json.loads(result)

        assert parsed["status"] == "refreshed"
        mock_cache.invalidate.assert_called_once_with("123456")
