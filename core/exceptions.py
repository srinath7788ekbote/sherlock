"""
Custom exceptions for Sherlock.

All custom exceptions used throughout the application are defined here.
Every other module imports from this file.
"""


class ReadOnlyViolation(Exception):
    """Raised when a mutation or write operation is attempted against the New Relic API."""

    def __init__(self, message: str, blocked_keyword: str) -> None:
        """Initialize ReadOnlyViolation.

        Args:
            message: Human-readable description of the violation.
            blocked_keyword: The specific keyword that triggered the block.
        """
        self.message = message
        self.blocked_keyword = blocked_keyword
        super().__init__(self.message)


class ServiceNotFoundError(Exception):
    """Raised when fuzzy name resolution fails to find a matching service."""

    def __init__(
        self, input_name: str, closest_matches: list[str], domain: str
    ) -> None:
        """Initialize ServiceNotFoundError.

        Args:
            input_name: The name the user provided.
            closest_matches: The best fuzzy matches found (may be empty).
            domain: The domain searched — 'apm', 'k8s', 'synthetics', or 'alerts'.
        """
        self.input_name = input_name
        self.closest_matches = closest_matches
        self.domain = domain
        msg = f"No {domain} service matching '{input_name}' found."
        if closest_matches:
            msg += f" Closest matches: {closest_matches}"
        super().__init__(msg)


class CredentialError(Exception):
    """Raised on authentication or credential-related failures."""

    def __init__(self, message: str, account_id: str, http_status: int | None = None) -> None:
        """Initialize CredentialError.

        Args:
            message: Human-readable error description.
            account_id: The account ID that failed authentication.
            http_status: HTTP status code if available.
        """
        self.account_id = account_id
        self.http_status = http_status
        super().__init__(message)


class IntelligenceError(Exception):
    """Raised when learn_account fails critically."""

    def __init__(
        self, message: str, account_id: str, partial_result: dict | None = None
    ) -> None:
        """Initialize IntelligenceError.

        Args:
            message: Human-readable error description.
            account_id: The account ID for which learning failed.
            partial_result: Any partial intelligence data gathered before the failure.
        """
        self.account_id = account_id
        self.partial_result = partial_result
        super().__init__(message)


class NotConnectedError(Exception):
    """Raised when a tool is called before connect_account."""

    def __init__(self) -> None:
        """Initialize NotConnectedError with standard message."""
        super().__init__("No active account. Call connect_account() first.")


class MonitorNotFoundError(ServiceNotFoundError):
    """Raised when fuzzy resolution fails for a synthetic monitor name."""

    def __init__(
        self,
        input_name: str,
        closest_matches: list[str],
        known_monitors: list[str],
    ) -> None:
        """Initialize MonitorNotFoundError.

        Args:
            input_name: The monitor name the user provided.
            closest_matches: The best fuzzy matches found.
            known_monitors: All known monitor names for the account.
        """
        self.known_monitors = known_monitors
        super().__init__(
            input_name=input_name,
            closest_matches=closest_matches,
            domain="synthetics",
        )
