"""
Active account context for Sherlock.

Provides a thread-safe singleton that holds the currently active
credentials and account intelligence. Tools read from this context
rather than passing credentials around.
"""

import logging
import threading
from typing import TYPE_CHECKING

from core.exceptions import NotConnectedError

if TYPE_CHECKING:
    from core.credentials import Credentials
    from core.intelligence import AccountIntelligence

logger = logging.getLogger("sherlock.context")


class AccountContext:
    """Thread-safe singleton holding the active New Relic account context."""

    _instance: "AccountContext | None" = None
    _lock = threading.Lock()

    def __new__(cls) -> "AccountContext":
        """Ensure only one AccountContext instance exists."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._credentials = None
                cls._instance._intelligence = None
            return cls._instance

    def set_active(
        self,
        credentials: "Credentials",
        intelligence: "AccountIntelligence",
    ) -> None:
        """Set the active account credentials and intelligence.

        Args:
            credentials: Validated Credentials for the active account.
            intelligence: Learned AccountIntelligence for the active account.
        """
        with self._lock:
            self._credentials = credentials
            self._intelligence = intelligence
            logger.info(
                "Active account set: %s (region %s)",
                credentials.account_id,
                credentials.region,
            )

    def get_active(self) -> tuple["Credentials", "AccountIntelligence"]:
        """Get the active credentials and intelligence.

        Returns:
            Tuple of (Credentials, AccountIntelligence).

        Raises:
            NotConnectedError: If no account is currently active.
        """
        with self._lock:
            if self._credentials is None or self._intelligence is None:
                raise NotConnectedError()
            return self._credentials, self._intelligence

    def is_connected(self) -> bool:
        """Check whether an account is currently connected.

        Returns:
            True if both credentials and intelligence are set.
        """
        with self._lock:
            return self._credentials is not None and self._intelligence is not None

    def clear(self) -> None:
        """Clear the active account context."""
        with self._lock:
            self._credentials = None
            self._intelligence = None
            logger.info("Active account context cleared.")

    @classmethod
    def reset_singleton(cls) -> None:
        """Reset the singleton instance. Used in testing only."""
        with cls._lock:
            cls._instance = None
