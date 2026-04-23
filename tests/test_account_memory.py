"""
Tests for core.account_memory — persistent service-to-account memory.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.account_memory import AccountMemory, AccountMemoryEntry, AccountMemoryIndex
from core.intelligence import (
    AccountIntelligence,
    AccountMeta,
    AlertsIntelligence,
    APMIntelligence,
    AzureServiceBusIntelligence,
    AzureServiceBusQueueMeta,
    BrowserIntelligence,
    K8sIntelligence,
    LogsIntelligence,
    MobileIntelligence,
    OTelIntelligence,
    SyntheticsIntelligence,
    WorkloadIntelligence,
)


@pytest.fixture(autouse=True)
def _reset_singleton(tmp_path):
    """Reset the AccountMemory singleton and redirect to a temp file."""
    AccountMemory.reset_singleton()
    # Override MEMORY_FILE to write into tmp_path, not the real .sherlock/
    original = AccountMemory.MEMORY_FILE
    AccountMemory.MEMORY_FILE = tmp_path / "account_memory.json"
    yield
    AccountMemory.MEMORY_FILE = original
    AccountMemory.reset_singleton()


def _make_intelligence(
    account_id: str = "123456",
    apm_names: list[str] | None = None,
    otel_names: list[str] | None = None,
    synth_names: list[str] | None = None,
    browser_names: list[str] | None = None,
    mobile_names: list[str] | None = None,
    workload_names: list[str] | None = None,
    k8s_namespaces: list[str] | None = None,
    k8s_clusters: list[str] | None = None,
    alert_policies: list[str] | None = None,
    asb_queues: list[AzureServiceBusQueueMeta] | None = None,
) -> AccountIntelligence:
    """Build a minimal AccountIntelligence with the specified entity names."""
    return AccountIntelligence(
        account_id=account_id,
        apm=APMIntelligence(service_names=apm_names or []),
        otel=OTelIntelligence(
            service_names=otel_names or [],
            service_count=len(otel_names or []),
        ),
        synthetics=SyntheticsIntelligence(monitor_names=synth_names or []),
        browser=BrowserIntelligence(app_names=browser_names or []),
        mobile=MobileIntelligence(
            app_names=mobile_names or [],
            app_count=len(mobile_names or []),
        ),
        workloads=WorkloadIntelligence(
            workload_names=workload_names or [],
            workload_count=len(workload_names or []),
        ),
        k8s=K8sIntelligence(
            namespaces=k8s_namespaces or [],
            cluster_names=k8s_clusters or [],
        ),
        alerts=AlertsIntelligence(policy_names=alert_policies or []),
        azure_service_bus=AzureServiceBusIntelligence(
            queues=asb_queues or [],
            configured=bool(asb_queues),
        ),
    )


# ── Core AccountMemory Tests ────────────────────────────────────────────


class TestAccountMemoryInit:

    def test_creates_empty_index_on_first_access(self):
        """AccountMemory creates empty index if file not found."""
        mem = AccountMemory()
        assert mem.get_all_accounts() == []

    def test_loads_existing_index_from_disk(self, tmp_path):
        """AccountMemory reads existing index on init."""
        # Pre-write a memory file
        index = AccountMemoryIndex(
            services={
                "my-service": AccountMemoryEntry(
                    account_id="111",
                    account_name="Test",
                    profile_name="PROF",
                )
            },
            account_profiles={"111": "PROF"},
            account_names={"111": "Test"},
        )
        AccountMemory.MEMORY_FILE.write_text(
            index.model_dump_json(indent=2), encoding="utf-8"
        )

        mem = AccountMemory()
        entry = mem.lookup_service("my-service")
        assert entry is not None
        assert entry.account_id == "111"

    def test_handles_corrupt_json_gracefully(self):
        """If JSON is corrupt, starts with empty index (no crash)."""
        AccountMemory.MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        AccountMemory.MEMORY_FILE.write_text("NOT VALID JSON{{{", encoding="utf-8")

        mem = AccountMemory()
        # Should not raise, should have empty index
        assert mem.get_all_accounts() == []


class TestRecordAccountIntelligence:

    def test_indexes_all_apm_service_names(self):
        intel = _make_intelligence(apm_names=["svc-a", "svc-b", "svc-c"])
        mem = AccountMemory()
        count = mem.record_account_intelligence(
            "111", "Acme", "PROF", "US", intel,
        )
        assert mem.lookup_service("svc-a") is not None
        assert mem.lookup_service("svc-b") is not None
        assert mem.lookup_service("svc-c") is not None
        assert count >= 3

    def test_indexes_otel_service_names(self):
        intel = _make_intelligence(otel_names=["otel-svc-1", "otel-svc-2"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        assert mem.lookup_service("otel-svc-1") is not None
        assert mem.lookup_service("otel-svc-2") is not None

    def test_indexes_synthetic_monitor_names(self):
        intel = _make_intelligence(synth_names=["Login Check - Prod"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        assert mem.lookup_service("Login Check - Prod") is not None

    def test_indexes_browser_app_names(self):
        intel = _make_intelligence(browser_names=["My Browser App"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        assert mem.lookup_service("My Browser App") is not None

    def test_indexes_mobile_app_names(self):
        intel = _make_intelligence(mobile_names=["MyMobileApp"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        assert mem.lookup_service("MyMobileApp") is not None

    def test_indexes_workload_names(self):
        intel = _make_intelligence(workload_names=["Payment Workload"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        assert mem.lookup_service("Payment Workload") is not None

    def test_indexes_k8s_namespaces(self):
        intel = _make_intelligence(k8s_namespaces=["eswd-prod", "eswd-stg"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        assert mem.lookup_service("eswd-prod") is not None
        assert mem.lookup_service("eswd-stg") is not None

    def test_indexes_k8s_clusters(self):
        intel = _make_intelligence(k8s_clusters=["main-cluster-prod"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        assert mem.lookup_service("main-cluster-prod") is not None

    def test_indexes_alert_policy_names(self):
        intel = _make_intelligence(alert_policies=["Critical - Payment"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        assert mem.lookup_service("Critical - Payment") is not None

    def test_indexes_asb_queue_names(self):
        queues = [
            AzureServiceBusQueueMeta(entity_name="prod-validation-queue"),
            AzureServiceBusQueueMeta(entity_name="prod-export-queue"),
        ]
        intel = _make_intelligence(asb_queues=queues)
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        assert mem.lookup_service("prod-validation-queue") is not None
        assert mem.lookup_service("prod-export-queue") is not None

    def test_stores_account_profile_mapping(self):
        intel = _make_intelligence(apm_names=["svc-x"])
        mem = AccountMemory()
        mem.record_account_intelligence("222", "BetaCorp", "BETA", "EU", intel)
        accounts = mem.get_all_accounts()
        assert any(a["account_id"] == "222" and a["profile_name"] == "BETA" for a in accounts)

    def test_stores_account_name_mapping(self):
        intel = _make_intelligence(apm_names=["svc-x"])
        mem = AccountMemory()
        mem.record_account_intelligence("222", "BetaCorp", "BETA", "EU", intel)
        accounts = mem.get_all_accounts()
        assert any(a["account_name"] == "BetaCorp" for a in accounts)

    def test_returns_indexed_count(self):
        intel = _make_intelligence(
            apm_names=["a", "b"],
            otel_names=["c"],
            synth_names=["d"],
        )
        mem = AccountMemory()
        count = mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        assert count == 4

    def test_updates_existing_entries_on_re_learn(self):
        intel1 = _make_intelligence(apm_names=["svc-alpha"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF_OLD", "US", intel1)

        entry1 = mem.lookup_service("svc-alpha")
        assert entry1 is not None
        assert entry1.profile_name == "PROF_OLD"

        intel2 = _make_intelligence(apm_names=["svc-alpha"])
        mem.record_account_intelligence("111", "Acme", "PROF_NEW", "US", intel2)

        entry2 = mem.lookup_service("svc-alpha")
        assert entry2 is not None
        assert entry2.profile_name == "PROF_NEW"

    def test_persists_to_disk_after_record(self):
        intel = _make_intelligence(apm_names=["disk-svc"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)

        assert AccountMemory.MEMORY_FILE.exists()
        raw = json.loads(AccountMemory.MEMORY_FILE.read_text(encoding="utf-8"))
        assert "disk-svc" in raw["services"]

    def test_skips_empty_names(self):
        intel = _make_intelligence(apm_names=["good-svc", "", "  "])
        mem = AccountMemory()
        count = mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        assert count == 1  # only "good-svc"


class TestLookupService:

    def _populate(self):
        intel = _make_intelligence(
            apm_names=[
                "eswd-prod/pdf-export-service",
                "eswd-prod/client-service",
                "eswd-prod/sifi-adapter",
            ],
        )
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        return mem

    def test_exact_match_case_insensitive(self):
        mem = self._populate()
        entry = mem.lookup_service("ESWD-PROD/PDF-EXPORT-SERVICE")
        assert entry is not None
        assert entry.account_id == "111"

    def test_substring_match_service_in_entity(self):
        """Finds 'pdf-export' in 'eswd-prod/pdf-export-service'."""
        mem = self._populate()
        entry = mem.lookup_service("pdf-export")
        assert entry is not None
        assert entry.account_id == "111"

    def test_substring_match_entity_in_service(self):
        """Finds entry when input is a superset of a known name."""
        intel = _make_intelligence(apm_names=["sifi-adapter"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        entry = mem.lookup_service("eswd-prod/sifi-adapter")
        assert entry is not None

    def test_returns_none_when_not_found(self):
        mem = self._populate()
        assert mem.lookup_service("nonexistent-service") is None

    def test_no_false_positives_on_short_substrings(self):
        """Very short names (1-2 chars) don't cause false matches."""
        mem = self._populate()
        assert mem.lookup_service("ab") is None
        assert mem.lookup_service("e") is None

    def test_bare_name_match_via_slash(self):
        """Bare name after '/' matches against full indexed names."""
        intel = _make_intelligence(apm_names=["eswd-prod/client-service"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        # Query with different prefix but same bare name
        entry = mem.lookup_service("other-ns/client-service")
        assert entry is not None
        assert entry.account_id == "111"

    def test_empty_input_returns_none(self):
        mem = self._populate()
        assert mem.lookup_service("") is None
        assert mem.lookup_service("   ") is None


class TestIsStale:

    def test_fresh_entry_not_stale(self):
        intel = _make_intelligence(apm_names=["fresh-svc"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        assert not mem.is_stale("fresh-svc")

    def test_old_entry_is_stale(self):
        intel = _make_intelligence(apm_names=["old-svc"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        # Manually backdate
        with mem._lock:
            index = mem._load()
            index.services["old-svc"].last_seen = datetime.now(timezone.utc) - timedelta(hours=25)
        assert mem.is_stale("old-svc")

    def test_unknown_service_is_stale(self):
        mem = AccountMemory()
        assert mem.is_stale("no-such-svc")


class TestGetProfileForService:

    def test_returns_profile_name_for_known_service(self):
        intel = _make_intelligence(apm_names=["my-svc"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "MY_PROF", "US", intel)
        assert mem.get_profile_for_service("my-svc") == "MY_PROF"

    def test_returns_none_for_unknown_service(self):
        mem = AccountMemory()
        assert mem.get_profile_for_service("nope") is None


class TestGetAllAccounts:

    def test_returns_all_account_mappings(self):
        mem = AccountMemory()
        intel1 = _make_intelligence(apm_names=["svc-a"])
        intel2 = _make_intelligence(apm_names=["svc-b"])
        mem.record_account_intelligence("111", "Alpha", "PROF_A", "US", intel1)
        mem.record_account_intelligence("222", "Beta", "PROF_B", "EU", intel2)
        accounts = mem.get_all_accounts()
        assert len(accounts) == 2
        ids = {a["account_id"] for a in accounts}
        assert ids == {"111", "222"}


class TestMultiAccountScenarios:

    def test_two_accounts_different_services(self):
        mem = AccountMemory()
        intel_a = _make_intelligence(apm_names=["alpha-svc"])
        intel_b = _make_intelligence(apm_names=["beta-svc"])
        mem.record_account_intelligence("111", "Alpha", "PROF_A", "US", intel_a)
        mem.record_account_intelligence("222", "Beta", "PROF_B", "EU", intel_b)

        entry_a = mem.lookup_service("alpha-svc")
        assert entry_a is not None
        assert entry_a.account_id == "111"
        assert entry_a.profile_name == "PROF_A"

        entry_b = mem.lookup_service("beta-svc")
        assert entry_b is not None
        assert entry_b.account_id == "222"
        assert entry_b.profile_name == "PROF_B"

    def test_service_moves_between_accounts(self):
        mem = AccountMemory()
        intel1 = _make_intelligence(apm_names=["migrated-svc"])
        mem.record_account_intelligence("111", "Old", "PROF_OLD", "US", intel1)
        assert mem.lookup_service("migrated-svc").account_id == "111"

        intel2 = _make_intelligence(apm_names=["migrated-svc"])
        mem.record_account_intelligence("222", "New", "PROF_NEW", "US", intel2)
        assert mem.lookup_service("migrated-svc").account_id == "222"


class TestSingletonAndThreadSafety:

    def test_singleton_pattern(self):
        a = AccountMemory()
        b = AccountMemory()
        assert a is b

    def test_concurrent_reads_and_writes(self):
        """Concurrent record + lookup don't crash."""
        mem = AccountMemory()
        errors = []

        def writer(idx: int):
            try:
                intel = _make_intelligence(apm_names=[f"thread-svc-{idx}"])
                mem.record_account_intelligence(
                    str(idx), f"Acct{idx}", f"PROF{idx}", "US", intel,
                )
            except Exception as e:
                errors.append(e)

        def reader(idx: int):
            try:
                mem.lookup_service(f"thread-svc-{idx}")
            except Exception as e:
                errors.append(e)

        threads = []
        for i in range(10):
            threads.append(threading.Thread(target=writer, args=(i,)))
            threads.append(threading.Thread(target=reader, args=(i,)))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []


class TestAtomicWrite:

    def test_atomic_write_survives_interruption(self, tmp_path):
        """If existing file exists and a new write happens, old data isn't lost on success."""
        intel1 = _make_intelligence(apm_names=["original-svc"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel1)

        # Verify file exists with original data
        assert AccountMemory.MEMORY_FILE.exists()

        # Record more data
        intel2 = _make_intelligence(apm_names=["new-svc"])
        mem.record_account_intelligence("222", "Beta", "PROF2", "US", intel2)

        # Both entries should be present
        raw = json.loads(AccountMemory.MEMORY_FILE.read_text(encoding="utf-8"))
        assert "original-svc" in raw["services"]
        assert "new-svc" in raw["services"]


class TestClear:

    def test_clear_removes_all_entries(self):
        intel = _make_intelligence(apm_names=["to-clear"])
        mem = AccountMemory()
        mem.record_account_intelligence("111", "Acme", "PROF", "US", intel)
        assert mem.lookup_service("to-clear") is not None

        mem.clear()
        assert mem.lookup_service("to-clear") is None
        assert mem.get_all_accounts() == []
        assert not AccountMemory.MEMORY_FILE.exists()


# ── resolve_account Tool Tests ──────────────────────────────────────────


class TestResolveAccountTool:

    @pytest.fixture(autouse=True)
    def _setup(self):
        """Pre-populate memory for resolve_account tests."""
        mem = AccountMemory()
        intel = _make_intelligence(apm_names=["eswd-prod/pdf-export-service"])
        mem.record_account_intelligence(
            "3007677", "DFIN ActiveDisclosure", "DFIN_AD", "US", intel,
        )

    @pytest.mark.asyncio
    async def test_returns_found_for_known_service(self):
        from tools.intelligence_tools import resolve_account

        result = json.loads(await resolve_account(service_name="pdf-export-service"))
        assert result["status"] == "FOUND"
        assert result["profile_name"] == "DFIN_AD"
        assert result["account_id"] == "3007677"

    @pytest.mark.asyncio
    async def test_returns_stale_for_old_entry(self):
        from tools.intelligence_tools import resolve_account

        # Backdate the entry
        mem = AccountMemory()
        with mem._lock:
            index = mem._load()
            for key in index.services:
                index.services[key].last_seen = datetime.now(timezone.utc) - timedelta(hours=25)

        result = json.loads(await resolve_account(service_name="pdf-export-service"))
        assert result["status"] == "STALE"
        assert result["profile_name"] == "DFIN_AD"

    @pytest.mark.asyncio
    async def test_returns_not_found_for_unknown(self):
        from tools.intelligence_tools import resolve_account

        result = json.loads(await resolve_account(service_name="totally-unknown"))
        assert result["status"] == "NOT_FOUND"
        assert "known_accounts" in result

    @pytest.mark.asyncio
    async def test_returns_action_string_for_connect(self):
        from tools.intelligence_tools import resolve_account

        result = json.loads(await resolve_account(service_name="pdf-export-service"))
        assert "connect_account" in result["action"]
        assert "DFIN_AD" in result["action"]


# ── connect_account service_name Auto-Resolve Tests ─────────────────────


class TestConnectAccountAutoResolve:

    @pytest.fixture(autouse=True)
    def _setup(self):
        """Pre-populate memory."""
        mem = AccountMemory()
        intel = _make_intelligence(apm_names=["eswd-prod/client-service"])
        mem.record_account_intelligence(
            "3007677", "DFIN ActiveDisclosure", "DFIN_AD", "US", intel,
        )

    @pytest.mark.asyncio
    async def test_auto_resolves_profile_from_service_name(self):
        """When service_name given without profile, resolves from memory."""
        from tools.intelligence_tools import connect_account

        # Mock out the actual connection flow — we just verify profile gets resolved
        with patch("tools.intelligence_tools._credential_manager") as mock_cm:
            mock_cm.load_profile.return_value = MagicMock(
                account_id="3007677",
                api_key="NRAK-test",
                region="US",
            )
            mock_cm.validate_credentials = AsyncMock(return_value={
                "valid": True,
                "account_name": "DFIN AD",
                "user_name": "test",
            })
            mock_cm.list_profiles.return_value = []

            with patch("tools.intelligence_tools.learn_account") as mock_learn:
                mock_intel = _make_intelligence(apm_names=["eswd-prod/client-service"])
                mock_learn.return_value = mock_intel

                with patch("tools.intelligence_tools._cache") as mock_cache:
                    mock_cache.get.return_value = None
                    mock_cache.get_stale.return_value = None

                    with patch("tools.intelligence_tools.load_graph", return_value=None):
                        with patch("tools.intelligence_tools.graph_is_stale", return_value=True):
                            result = json.loads(
                                await connect_account(service_name="client-service")
                            )
                            # Should have auto-resolved and loaded DFIN_AD
                            mock_cm.load_profile.assert_called_with("DFIN_AD")

    @pytest.mark.asyncio
    async def test_explicit_profile_overrides_service_name(self):
        """When both profile_name and service_name given, profile wins."""
        from tools.intelligence_tools import connect_account

        with patch("tools.intelligence_tools._credential_manager") as mock_cm:
            mock_cm.load_profile.return_value = MagicMock(
                account_id="999",
                api_key="NRAK-other",
                region="US",
            )
            mock_cm.validate_credentials = AsyncMock(return_value={
                "valid": True,
                "account_name": "Other",
                "user_name": "test",
            })
            mock_cm.list_profiles.return_value = []

            with patch("tools.intelligence_tools.learn_account") as mock_learn:
                mock_intel = _make_intelligence(apm_names=["other-svc"])
                mock_learn.return_value = mock_intel

                with patch("tools.intelligence_tools._cache") as mock_cache:
                    mock_cache.get.return_value = None
                    mock_cache.get_stale.return_value = None

                    with patch("tools.intelligence_tools.load_graph", return_value=None):
                        with patch("tools.intelligence_tools.graph_is_stale", return_value=True):
                            result = json.loads(
                                await connect_account(
                                    profile_name="EXPLICIT_PROF",
                                    service_name="client-service",
                                )
                            )
                            # Should use EXPLICIT_PROF, not DFIN_AD
                            mock_cm.load_profile.assert_called_with("EXPLICIT_PROF")

    @pytest.mark.asyncio
    async def test_falls_back_when_service_not_in_memory(self):
        """When service not in memory, normal flow continues (needs explicit creds)."""
        from tools.intelligence_tools import connect_account

        result = json.loads(
            await connect_account(service_name="totally-unknown-svc")
        )
        # Without profile or account_id, should get an error about missing creds
        assert "error" in result


# ── Integration with learn_account ──────────────────────────────────────


class TestLearnAccountMemoryIntegration:

    @pytest.mark.asyncio
    async def test_connect_account_populates_memory(self):
        """After connect_account, all services are in AccountMemory."""
        from tools.intelligence_tools import connect_account

        intel = _make_intelligence(
            apm_names=["svc-1", "svc-2"],
            synth_names=["Mon 1"],
        )

        with patch("tools.intelligence_tools._credential_manager") as mock_cm:
            mock_cm.load_profile.return_value = MagicMock(
                account_id="555",
                api_key="NRAK-test",
                region="US",
            )
            mock_cm.validate_credentials = AsyncMock(return_value={
                "valid": True,
                "account_name": "TestAcct",
                "user_name": "tester",
            })
            mock_cm.list_profiles.return_value = []

            with patch("tools.intelligence_tools.learn_account", return_value=intel):
                with patch("tools.intelligence_tools._cache") as mock_cache:
                    mock_cache.get.return_value = None
                    mock_cache.get_stale.return_value = None

                    with patch("tools.intelligence_tools.load_graph", return_value=None):
                        with patch("tools.intelligence_tools.graph_is_stale", return_value=True):
                            await connect_account(profile_name="TEST_PROF")

        mem = AccountMemory()
        assert mem.lookup_service("svc-1") is not None
        assert mem.lookup_service("svc-2") is not None
        assert mem.lookup_service("Mon 1") is not None

    @pytest.mark.asyncio
    async def test_learn_account_tool_updates_memory(self):
        """After learn_account_tool (force refresh), memory is updated."""
        from core.context import AccountContext
        from core.credentials import Credentials
        from tools.intelligence_tools import learn_account_tool

        intel = _make_intelligence(
            apm_names=["refreshed-svc"],
            k8s_namespaces=["my-ns"],
        )

        AccountContext.reset_singleton()
        ctx = AccountContext()
        creds = Credentials(account_id="777", api_key="NRAK-x", region="US")
        ctx.set_active(creds, intel)

        with patch("tools.intelligence_tools.learn_account", return_value=intel):
            with patch("tools.intelligence_tools._cache"):
                result = json.loads(await learn_account_tool())

        assert result.get("status") == "refreshed"
        mem = AccountMemory()
        assert mem.lookup_service("refreshed-svc") is not None
        assert mem.lookup_service("my-ns") is not None

        ctx.clear()
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    async def test_background_refresh_updates_memory(self):
        """_background_refresh also updates AccountMemory."""
        from core.context import AccountContext
        from core.credentials import Credentials
        from tools.intelligence_tools import _background_refresh

        intel = _make_intelligence(apm_names=["bg-svc"])

        AccountContext.reset_singleton()
        ctx = AccountContext()
        creds = Credentials(account_id="888", api_key="NRAK-y", region="US")
        ctx.set_active(creds, intel)

        with patch("tools.intelligence_tools.learn_account", return_value=intel):
            with patch("tools.intelligence_tools._cache"):
                await _background_refresh(creds, "888")

        mem = AccountMemory()
        assert mem.lookup_service("bg-svc") is not None

        ctx.clear()
        AccountContext.reset_singleton()
