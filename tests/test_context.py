"""
Tests for AccountContext thread-safe singleton.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.context import AccountContext
from core.credentials import Credentials
from core.intelligence import AccountIntelligence
from core.exceptions import NotConnectedError


class TestAccountContext:
    """Tests for AccountContext singleton behavior."""

    def setup_method(self):
        """Reset singleton before each test."""
        AccountContext.reset_singleton()

    def teardown_method(self):
        """Clean up after each test."""
        AccountContext.reset_singleton()

    def test_singleton_pattern(self):
        """AccountContext always returns the same instance."""
        ctx1 = AccountContext()
        ctx2 = AccountContext()
        assert ctx1 is ctx2

    def test_reset_singleton(self):
        """reset_singleton creates a fresh instance."""
        ctx1 = AccountContext()
        ctx1.set_active(
            Credentials(account_id="111", api_key="NRAK-aaa", region="US"),
            AccountIntelligence(account_id="111"),
        )
        AccountContext.reset_singleton()
        ctx2 = AccountContext()
        assert not ctx2.is_connected()

    def test_set_and_get_active(self):
        """Credentials and intelligence can be set and retrieved."""
        ctx = AccountContext()
        creds = Credentials(account_id="123", api_key="NRAK-test", region="US")
        intel = AccountIntelligence(account_id="123")

        ctx.set_active(creds, intel)

        assert ctx.is_connected()
        assert ctx.get_active()[0].account_id == "123"
        assert isinstance(ctx.get_active()[1], AccountIntelligence)

    def test_get_active_when_not_connected(self):
        """get_active raises NotConnectedError when no active account."""
        ctx = AccountContext()
        with pytest.raises(NotConnectedError):
            ctx.get_active()

    def test_is_connected(self):
        """is_connected returns correct state."""
        ctx = AccountContext()
        assert not ctx.is_connected()

        ctx.set_active(
            Credentials(account_id="123", api_key="NRAK-test", region="US"),
            AccountIntelligence(account_id="123"),
        )
        assert ctx.is_connected()

    def test_clear(self):
        """clear removes active credentials and intelligence."""
        ctx = AccountContext()
        ctx.set_active(
            Credentials(account_id="123", api_key="NRAK-test", region="US"),
            AccountIntelligence(account_id="123"),
        )
        assert ctx.is_connected()

        ctx.clear()
        assert not ctx.is_connected()

    def test_thread_safety(self):
        """AccountContext is thread-safe across multiple threads."""
        ctx = AccountContext()
        results = []

        def set_context(account_id):
            creds = Credentials(account_id=account_id, api_key=f"NRAK-{account_id}", region="US")
            intel = AccountIntelligence(account_id=account_id)
            ctx.set_active(creds, intel)
            active_creds, _ = ctx.get_active()
            results.append(active_creds.account_id)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(set_context, str(i)) for i in range(10)]
            for f in futures:
                f.result()

        # All threads should have set a context (though the final value is non-deterministic)
        assert len(results) == 10
        # The context should still be connected
        assert ctx.is_connected()

    def test_multiple_set_active_overwrites(self):
        """Calling set_active multiple times overwrites the previous context."""
        ctx = AccountContext()

        ctx.set_active(
            Credentials(account_id="111", api_key="NRAK-first", region="US"),
            AccountIntelligence(account_id="111"),
        )
        assert ctx.get_active()[0].account_id == "111"

        ctx.set_active(
            Credentials(account_id="222", api_key="NRAK-second", region="EU"),
            AccountIntelligence(account_id="222"),
        )
        assert ctx.get_active()[0].account_id == "222"
        assert ctx.get_active()[0].region == "EU"
