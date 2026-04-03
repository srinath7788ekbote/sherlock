"""
Tests for session memory module.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.context import AccountContext
from core.session_memory import InvestigationSnapshot, SessionMemory


def _make_snapshot(
    account_id: str = "123456",
    service_name: str = "prod/my-service",
    severity: str = "CRITICAL",
    error_rate: float = 15.4,
    minutes_ago: int = 0,
    **kwargs,
) -> InvestigationSnapshot:
    """Helper to create a snapshot with sensible defaults."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    defaults = dict(
        timestamp=ts,
        account_id=account_id,
        account_name="Test Account",
        service_name=service_name,
        bare_name=service_name.split("/")[-1] if "/" in service_name else service_name,
        namespace=service_name.split("/")[0] if "/" in service_name else "",
        severity=severity,
        root_cause="DB connection pool exhausted",
        causal_chain="DB pool exhausted → health checks fail → 503s",
        causal_pattern="DB_CASCADE",
        error_rate=error_rate,
        is_otel=False,
    )
    defaults.update(kwargs)
    return InvestigationSnapshot(**defaults)


class TestInvestigationSnapshot:
    """Tests for InvestigationSnapshot dataclass."""

    def test_age_minutes(self):
        """age_minutes returns correct value for recent snapshot."""
        snap = _make_snapshot(minutes_ago=5)
        age = snap.age_minutes()
        assert 4.5 <= age <= 6.0

    def test_age_minutes_naive_timestamp(self):
        """age_minutes handles naive (no tz) timestamps."""
        naive_ts = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
        snap = _make_snapshot()
        snap.timestamp = naive_ts
        age = snap.age_minutes()
        assert 9.0 <= age <= 11.0

    def test_is_recent_true(self):
        """is_recent returns True for a 5-minute-old snapshot."""
        snap = _make_snapshot(minutes_ago=5)
        assert snap.is_recent(threshold_minutes=120) is True

    def test_is_recent_false(self):
        """is_recent returns False for a 3-hour-old snapshot."""
        snap = _make_snapshot(minutes_ago=180)
        assert snap.is_recent(threshold_minutes=120) is False

    def test_short_summary_contains_key_fields(self):
        """short_summary includes service name, severity, and chain."""
        snap = _make_snapshot(
            service_name="eswd-prod/client-service",
            severity="CRITICAL",
            minutes_ago=8,
        )
        snap.causal_chain = "DB down → pods restart → 503s"
        summary = snap.short_summary()
        assert "eswd-prod/client-service" in summary
        assert "CRITICAL" in summary
        assert "DB down" in summary

    def test_short_summary_chronic_flag(self):
        """short_summary shows CHRONIC tag when flagged."""
        snap = _make_snapshot(chronic_flag=True)
        assert "(CHRONIC)" in snap.short_summary()

    def test_short_summary_stale_signal_flag(self):
        """short_summary shows STALE ALERT tag when flagged."""
        snap = _make_snapshot(stale_signal_flag=True)
        assert "(STALE ALERT)" in snap.short_summary()


class TestSessionMemory:
    """Tests for SessionMemory singleton."""

    def setup_method(self):
        """Reset singleton before each test."""
        SessionMemory.reset()

    def teardown_method(self):
        """Reset singleton after each test."""
        SessionMemory.reset()

    def test_singleton(self):
        """SessionMemory is a singleton."""
        mem1 = SessionMemory()
        mem2 = SessionMemory()
        assert mem1 is mem2

    def test_reset_creates_new_instance(self):
        """reset() allows creating a fresh singleton."""
        mem1 = SessionMemory()
        SessionMemory.reset()
        mem2 = SessionMemory()
        assert mem1 is not mem2

    def test_record_and_get_last(self):
        """record() stores a snapshot retrievable via get_last()."""
        mem = SessionMemory()
        snap = _make_snapshot()
        mem.record(snap)

        last = mem.get_last("123456")
        assert last is not None
        assert last.service_name == "prod/my-service"
        assert last.severity == "CRITICAL"

    def test_get_last_empty(self):
        """get_last() returns None for unknown accounts."""
        mem = SessionMemory()
        assert mem.get_last("unknown") is None

    def test_get_recent_respects_limit(self):
        """get_recent() returns at most `limit` snapshots."""
        mem = SessionMemory()
        for i in range(5):
            mem.record(_make_snapshot(
                service_name=f"prod/svc-{i}",
                minutes_ago=i,
            ))

        recent = mem.get_recent("123456", limit=3)
        assert len(recent) == 3

    def test_get_recent_filters_by_age(self):
        """get_recent() excludes snapshots older than max_age_minutes."""
        mem = SessionMemory()
        mem.record(_make_snapshot(minutes_ago=5))
        mem.record(_make_snapshot(
            service_name="prod/old-service",
            minutes_ago=150,
        ))

        recent = mem.get_recent("123456", limit=10, max_age_minutes=120)
        assert len(recent) == 1
        assert recent[0].service_name == "prod/my-service"

    def test_rolling_buffer_max(self):
        """Buffer holds at most MAX_PER_ACCOUNT snapshots."""
        mem = SessionMemory()
        for i in range(15):
            mem.record(_make_snapshot(service_name=f"prod/svc-{i}"))

        # Should only have last 10
        all_recent = mem.get_recent("123456", limit=20, max_age_minutes=9999)
        assert len(all_recent) == SessionMemory.MAX_PER_ACCOUNT

    def test_find_service_by_full_name(self):
        """find_service matches on full service name."""
        mem = SessionMemory()
        mem.record(_make_snapshot(service_name="eswd-prod/client-service"))

        found = mem.find_service("123456", "eswd-prod/client-service")
        assert found is not None
        assert found.service_name == "eswd-prod/client-service"

    def test_find_service_by_bare_name(self):
        """find_service matches on bare name (without namespace)."""
        mem = SessionMemory()
        mem.record(_make_snapshot(service_name="eswd-prod/client-service"))

        found = mem.find_service("123456", "client-service")
        assert found is not None
        assert found.service_name == "eswd-prod/client-service"

    def test_find_service_not_found(self):
        """find_service returns None when no match."""
        mem = SessionMemory()
        mem.record(_make_snapshot(service_name="prod/my-service"))

        found = mem.find_service("123456", "nonexistent-service")
        assert found is None

    def test_has_history(self):
        """has_history returns True after recording."""
        mem = SessionMemory()
        assert mem.has_history("123456") is False
        mem.record(_make_snapshot())
        assert mem.has_history("123456") is True

    def test_account_isolation(self):
        """Different accounts have independent histories."""
        mem = SessionMemory()
        mem.record(_make_snapshot(account_id="111"))
        mem.record(_make_snapshot(account_id="222", service_name="prod/other"))

        assert mem.has_history("111") is True
        assert mem.has_history("222") is True
        assert mem.get_last("111").service_name == "prod/my-service"
        assert mem.get_last("222").service_name == "prod/other"

    def test_format_context_block_empty(self):
        """format_context_block returns empty string when no history."""
        mem = SessionMemory()
        block = mem.format_context_block("123456")
        assert block == ""

    def test_format_context_block_with_data(self):
        """format_context_block includes severity the service name."""
        mem = SessionMemory()
        mem.record(_make_snapshot(
            service_name="prod/my-service",
            severity="CRITICAL",
        ))

        block = mem.format_context_block("123456")
        assert "CRITICAL" in block
        assert "prod/my-service" in block
        assert "Session Context" in block
        assert "Root cause" in block


class TestGetSessionContextTool:
    """Tests for the get_session_context_tool function."""

    def setup_method(self):
        SessionMemory.reset()
        AccountContext.reset_singleton()

    def teardown_method(self):
        SessionMemory.reset()
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    async def test_not_connected(self):
        """Returns NOT_CONNECTED when no account is active."""
        from tools.intelligence_tools import get_session_context_tool

        result = await get_session_context_tool()
        parsed = json.loads(result)

        assert parsed["status"] == "NOT_CONNECTED"
        assert parsed["session_investigations"] == []

    @pytest.mark.asyncio
    async def test_no_history(self, mock_context):
        """Returns NO_HISTORY when session is fresh."""
        from tools.intelligence_tools import get_session_context_tool

        result = await get_session_context_tool()
        parsed = json.loads(result)

        assert parsed["status"] == "NO_HISTORY"
        assert parsed["session_investigations"] == []

    @pytest.mark.asyncio
    async def test_returns_recent_investigations(self, mock_context):
        """Returns recorded investigations."""
        from tools.intelligence_tools import get_session_context_tool

        mem = SessionMemory()
        mem.record(_make_snapshot(service_name="prod/svc-a", severity="WARNING"))
        mem.record(_make_snapshot(service_name="prod/svc-b", severity="HEALTHY"))

        result = await get_session_context_tool(limit=5)
        parsed = json.loads(result)

        assert parsed["status"] == "OK"
        assert len(parsed["session_investigations"]) == 2
        # Newest first
        assert parsed["session_investigations"][0]["service"] == "prod/svc-b"

    @pytest.mark.asyncio
    async def test_filter_by_service_name(self, mock_context):
        """Filtering by service_name returns matching snapshot."""
        from tools.intelligence_tools import get_session_context_tool

        mem = SessionMemory()
        mem.record(_make_snapshot(service_name="prod/svc-a", severity="WARNING"))
        mem.record(_make_snapshot(service_name="prod/svc-b", severity="HEALTHY"))

        result = await get_session_context_tool(service_name="svc-a")
        parsed = json.loads(result)

        assert parsed["status"] == "OK"
        assert len(parsed["session_investigations"]) == 1
        assert parsed["session_investigations"][0]["service"] == "prod/svc-a"

    @pytest.mark.asyncio
    async def test_service_not_found(self, mock_context):
        """Returns NOT_FOUND when filtered service has no history."""
        from tools.intelligence_tools import get_session_context_tool

        mem = SessionMemory()
        mem.record(_make_snapshot(service_name="prod/svc-a"))

        result = await get_session_context_tool(service_name="nonexistent")
        parsed = json.loads(result)

        assert parsed["status"] == "NOT_FOUND"
        assert parsed["session_investigations"] == []
