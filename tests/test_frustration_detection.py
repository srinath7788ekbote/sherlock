"""
Tests for frustration detection and retry awareness.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.context import AccountContext
from core.session_memory import InvestigationSnapshot, SessionMemory
from core.utils import detect_frustration_signals


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


class TestLanguageSignal:
    """Tests for frustration keyword detection (language signal)."""

    def setup_method(self):
        SessionMemory.reset()

    def teardown_method(self):
        SessionMemory.reset()

    def test_detects_wtf(self):
        result = detect_frustration_signals("wtf is going on", "123456")
        assert result["language_signal"] is True
        assert result["frustrated"] is True

    def test_detects_still_broken(self):
        result = detect_frustration_signals("why is it STILL broken??", "123456", "my-service")
        assert result["language_signal"] is True
        assert result["frustrated"] is True

    def test_detects_still_failing(self):
        result = detect_frustration_signals("the service is still failing", "123456")
        assert result["language_signal"] is True

    def test_detects_same_issue_again(self):
        result = detect_frustration_signals("same issue again with client-service", "123456")
        assert result["language_signal"] is True

    def test_detects_no_data_again(self):
        result = detect_frustration_signals("no data again from the logs", "123456")
        assert result["language_signal"] is True

    def test_detects_still_showing_nothing(self):
        result = detect_frustration_signals("WHY IS IT STILL SHOWING NOTHING", "123456")
        assert result["language_signal"] is True

    def test_detects_keeps_failing(self):
        result = detect_frustration_signals("this service keeps failing", "123456")
        assert result["language_signal"] is True

    def test_detects_so_frustrating(self):
        result = detect_frustration_signals("this is so frustrating", "123456")
        assert result["language_signal"] is True

    def test_normal_prompt_no_signal(self):
        """Normal investigation prompt should NOT trigger."""
        result = detect_frustration_signals("investigate my-service", "123456", "my-service")
        assert result["language_signal"] is False
        assert result["frustrated"] is False

    def test_normal_health_check_no_signal(self):
        """Health check question should NOT trigger."""
        result = detect_frustration_signals("how is client-service performing?", "123456")
        assert result["language_signal"] is False

    def test_empty_prompt_no_signal(self):
        result = detect_frustration_signals("", "123456")
        assert result["language_signal"] is False
        assert result["frustrated"] is False


class TestRetrySignal:
    """Tests for retry count detection (session memory signal)."""

    def setup_method(self):
        SessionMemory.reset()

    def teardown_method(self):
        SessionMemory.reset()

    def test_retry_signal_fires_after_threshold(self):
        """Retry signal fires when same service investigated >= threshold times."""
        mem = SessionMemory()
        for _ in range(3):
            mem.record(_make_snapshot(
                service_name="prod/my-service",
                minutes_ago=5,
            ))

        result = detect_frustration_signals(
            "investigate my-service", "123456", "my-service"
        )
        assert result["retry_signal"] is True
        assert result["retry_count"] == 3
        assert result["frustrated"] is True

    def test_no_retry_signal_below_threshold(self):
        """Retry signal does NOT fire for a single investigation."""
        mem = SessionMemory()
        mem.record(_make_snapshot(service_name="prod/my-service"))

        result = detect_frustration_signals(
            "investigate my-service", "123456", "my-service"
        )
        assert result["retry_signal"] is False
        assert result["retry_count"] == 1

    def test_retry_threshold_exact(self):
        """Retry signal fires at exactly the threshold (default=2)."""
        mem = SessionMemory()
        for _ in range(2):
            mem.record(_make_snapshot(service_name="prod/my-service"))

        result = detect_frustration_signals(
            "investigate my-service", "123456", "my-service"
        )
        assert result["retry_signal"] is True
        assert result["retry_count"] == 2

    def test_retry_outside_window(self):
        """Old investigations outside the window don't count."""
        mem = SessionMemory()
        for _ in range(3):
            mem.record(_make_snapshot(
                service_name="prod/my-service",
                minutes_ago=30,  # Outside default 20-min window
            ))

        result = detect_frustration_signals(
            "investigate my-service", "123456", "my-service"
        )
        assert result["retry_signal"] is False
        assert result["retry_count"] == 0

    def test_prior_severities_tracked(self):
        """Prior severities are returned correctly."""
        mem = SessionMemory()
        mem.record(_make_snapshot(severity="CRITICAL"))
        mem.record(_make_snapshot(severity="WARNING"))
        mem.record(_make_snapshot(severity="CRITICAL"))

        result = detect_frustration_signals(
            "investigate my-service", "123456", "my-service"
        )
        assert result["prior_severities"] == ["CRITICAL", "WARNING", "CRITICAL"]

    def test_retry_no_service_uses_last(self):
        """Without service_name, uses the last investigated service."""
        mem = SessionMemory()
        for _ in range(3):
            mem.record(_make_snapshot(service_name="prod/my-service"))

        result = detect_frustration_signals("investigate again", "123456")
        assert result["retry_signal"] is True
        assert result["retry_count"] == 3


class TestCombinedSignals:
    """Tests for combined language + retry signals."""

    def setup_method(self):
        SessionMemory.reset()

    def teardown_method(self):
        SessionMemory.reset()

    def test_both_signals_fire(self):
        """Both language and retry signals fire together."""
        mem = SessionMemory()
        for _ in range(3):
            mem.record(_make_snapshot(service_name="prod/my-service"))

        result = detect_frustration_signals(
            "WHY IS IT STILL BROKEN", "123456", "my-service"
        )
        assert result["language_signal"] is True
        assert result["retry_signal"] is True
        assert result["frustrated"] is True

    def test_language_only(self):
        """Frustrated with language signal only, no retry history."""
        result = detect_frustration_signals("wtf is happening", "123456", "my-service")
        assert result["language_signal"] is True
        assert result["retry_signal"] is False
        assert result["frustrated"] is True

    def test_retry_only(self):
        """Frustrated with retry signal only, calm language."""
        mem = SessionMemory()
        for _ in range(3):
            mem.record(_make_snapshot(service_name="prod/my-service"))

        result = detect_frustration_signals(
            "check my-service please", "123456", "my-service"
        )
        assert result["language_signal"] is False
        assert result["retry_signal"] is True
        assert result["frustrated"] is True

    def test_neither_signal(self):
        """Not frustrated — normal prompt, no retry history."""
        result = detect_frustration_signals(
            "investigate payments-svc", "123456", "payments-svc"
        )
        assert result["language_signal"] is False
        assert result["retry_signal"] is False
        assert result["frustrated"] is False


class TestRecommendation:
    """Tests for escalation recommendation text."""

    def setup_method(self):
        SessionMemory.reset()

    def teardown_method(self):
        SessionMemory.reset()

    def test_recommendation_for_retry_loop(self):
        """Recommendation includes retry count."""
        mem = SessionMemory()
        for _ in range(3):
            mem.record(_make_snapshot(service_name="prod/my-service"))

        result = detect_frustration_signals(
            "investigate my-service", "123456", "my-service"
        )
        assert "investigated 3x" in result["recommendation"]
        assert "skip repeated queries" in result["recommendation"]

    def test_recommendation_for_consistent_critical(self):
        """Recommendation flags consistently CRITICAL."""
        mem = SessionMemory()
        for _ in range(3):
            mem.record(_make_snapshot(
                service_name="prod/my-service",
                severity="CRITICAL",
            ))

        result = detect_frustration_signals(
            "investigate my-service", "123456", "my-service"
        )
        assert "consistently CRITICAL" in result["recommendation"]

    def test_recommendation_for_language_only(self):
        """First investigation with frustration language suggests cross-account check."""
        result = detect_frustration_signals(
            "wtf is this service doing", "123456", "some-svc"
        )
        assert "cross-account" in result["recommendation"]

    def test_no_recommendation_when_calm(self):
        """No recommendation when not frustrated."""
        result = detect_frustration_signals(
            "investigate payments-svc", "123456", "payments-svc"
        )
        assert result["recommendation"] == ""


class TestSessionMemoryFailureGraceful:
    """Tests that session memory failures are handled gracefully."""

    def setup_method(self):
        SessionMemory.reset()

    def teardown_method(self):
        SessionMemory.reset()

    def test_broken_session_memory_doesnt_crash(self):
        """If SessionMemory raises, frustration detection still works."""
        with patch(
            "core.session_memory.SessionMemory",
            side_effect=RuntimeError("session memory exploded"),
        ):
            result = detect_frustration_signals(
                "still broken!", "123456", "my-service"
            )
            # Language signal still works
            assert result["language_signal"] is True
            assert result["frustrated"] is True
            # Retry signal gracefully defaults
            assert result["retry_signal"] is False
            assert result["retry_count"] == 0


class TestGetFrustrationContextTool:
    """Tests for the MCP tool wrapper."""

    def setup_method(self):
        SessionMemory.reset()
        AccountContext.reset_singleton()

    def teardown_method(self):
        SessionMemory.reset()
        AccountContext.reset_singleton()

    @pytest.mark.asyncio
    async def test_returns_normal_mode(self):
        """Returns NORMAL mode for calm, first-time investigation."""
        from tools.intelligence_tools import get_frustration_context_tool

        result = await get_frustration_context_tool(
            prompt="investigate payments-svc",
            service_name="payments-svc",
        )
        parsed = json.loads(result)
        assert parsed["status"] == "OK"
        assert parsed["mode"] == "NORMAL"
        assert parsed["frustrated"] is False

    @pytest.mark.asyncio
    async def test_returns_escalation_mode_language(self):
        """Returns ESCALATION mode when language signal fires."""
        from tools.intelligence_tools import get_frustration_context_tool

        result = await get_frustration_context_tool(
            prompt="why is it STILL broken??",
            service_name="my-service",
        )
        parsed = json.loads(result)
        assert parsed["status"] == "OK"
        assert parsed["mode"] == "ESCALATION"
        assert parsed["language_signal"] is True

    @pytest.mark.asyncio
    async def test_returns_escalation_mode_retry(self, mock_context):
        """Returns ESCALATION mode when retry signal fires."""
        from tools.intelligence_tools import get_frustration_context_tool

        mem = SessionMemory()
        for _ in range(3):
            mem.record(_make_snapshot(service_name="prod/my-service"))

        result = await get_frustration_context_tool(
            prompt="investigate my-service",
            service_name="my-service",
        )
        parsed = json.loads(result)
        assert parsed["status"] == "OK"
        assert parsed["mode"] == "ESCALATION"
        assert parsed["retry_signal"] is True
        assert parsed["retry_count"] == 3
