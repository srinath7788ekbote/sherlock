"""
Active account context for Sherlock.

Provides a thread-safe singleton that holds the currently active
credentials and account intelligence. Tools read from this context
rather than passing credentials around.

Also maintains a **service name resolution cache** so that once Sherlock
discovers the actual APM entity name for a user-provided input (e.g. via
NRQL discovery), every subsequent tool call reuses that mapping instead
of fuzzy-matching independently.
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
                cls._instance._resolved_names: dict[str, str] = {}
            return cls._instance

    # ── Resolution cache ─────────────────────────────────────────────

    def cache_resolved_name(self, input_name: str, resolved_name: str) -> None:
        """Store a mapping from user input to the real APM entity name.

        Called by discovery or any tool that confirms the actual entity
        name from New Relic data.  Subsequent calls to
        ``get_cached_resolution`` will return this mapping.

        Args:
            input_name:    The name the user/tool originally provided.
            resolved_name: The actual service name confirmed by NR data.
        """
        with self._lock:
            key = input_name.strip().lower()
            self._resolved_names[key] = resolved_name
            logger.debug(
                "Cached resolution: '%s' → '%s'", input_name, resolved_name,
            )

    def get_cached_resolution(self, input_name: str) -> str | None:
        """Return the previously resolved real name, or None.

        Args:
            input_name: The user-provided / fuzzy name to look up.

        Returns:
            The confirmed APM entity name if previously cached, else None.
        """
        with self._lock:
            return self._resolved_names.get(input_name.strip().lower())

    # ── Active account management ────────────────────────────────────

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
            self._resolved_names = {}
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
            self._resolved_names = {}
            logger.info("Active account context cleared.")

    @classmethod
    def reset_singleton(cls) -> None:
        """Reset the singleton instance. Used in testing only."""
        with cls._lock:
            cls._instance = None
