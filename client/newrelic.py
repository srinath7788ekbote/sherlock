"""
Async NerdGraph client for Sherlock.

Provides a read-only enforced, retrying, async HTTP client for the
New Relic NerdGraph GraphQL API. Every query is checked against a
blocklist of mutation operations before being sent.
"""

import asyncio
import logging
import re
import sys
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.context import AccountContext
from core.credentials import Credentials
from core.exceptions import ReadOnlyViolation

logger = logging.getLogger("sherlock.client")

# Operations that are never allowed — read-only enforcement.
BLOCKED_OPERATIONS: list[str] = [
    "mutation",
    "delete",
    "destroy",
    "remove",
    "update",
    "create",
    "modify",
    "alertsmuting",
    "alertspolicy",
    "dashboarddelete",
    "entitydelete",
    "syntheticscreate",
    "syntheticsupdate",
    "syntheticsdelete",
    "syntheticmonitordelete",
    "syntheticmonitorupdate",
]

# Pre-compiled regex for word-boundary matching of blocked ops.
BLOCKED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(rf"\b{op}\b", re.IGNORECASE) for op in BLOCKED_OPERATIONS
]


class NerdGraphClient:
    """Async HTTP client for the New Relic NerdGraph API.

    All queries pass through read-only enforcement before network requests.
    Retries on transient errors with exponential backoff.
    """

    def __init__(
        self,
        credentials: Credentials,
        timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        """Initialize the NerdGraph client.

        Args:
            credentials: Validated New Relic credentials.
            timeout: Default request timeout in seconds.
            max_retries: Maximum retry attempts for transient failures.
        """
        self._credentials = credentials
        self._timeout = timeout
        self._max_retries = max_retries
        self._headers = {
            "API-Key": credentials.api_key,
            "Content-Type": "application/json",
        }
        self._endpoint = credentials.endpoint

    def _assert_read_only(self, query: str) -> None:
        """Verify that a query does not contain any mutation operations.

        This runs before EVERY network request, no exceptions.

        Args:
            query: The GraphQL query string.

        Raises:
            ReadOnlyViolation: If the query contains a blocked operation.
        """
        normalized = query.strip().lower()

        # Check if query starts with 'mutation'.
        if normalized.startswith("mutation"):
            raise ReadOnlyViolation(
                message="Mutation queries are not allowed. This server is read-only.",
                blocked_keyword="mutation",
            )

        # Check for blocked operation names as word boundaries.
        for pattern in BLOCKED_PATTERNS:
            match = pattern.search(normalized)
            if match:
                raise ReadOnlyViolation(
                    message=(
                        f"Blocked operation '{match.group()}' detected. "
                        "This server is read-only."
                    ),
                    blocked_keyword=match.group(),
                )

    async def query(
        self,
        gql: str,
        variables: dict[str, Any] | None = None,
        timeout_override: int | None = None,
    ) -> dict:
        """Execute a NerdGraph GraphQL query with retry and read-only enforcement.

        Args:
            gql: The GraphQL query string.
            variables: Optional GraphQL variables.
            timeout_override: Optional per-request timeout override in seconds.

        Returns:
            The parsed JSON response dict.

        Raises:
            ReadOnlyViolation: If the query contains a mutation.
            httpx.HTTPStatusError: On non-retryable HTTP errors.
        """
        self._assert_read_only(gql)

        timeout = timeout_override or self._timeout
        payload: dict[str, Any] = {"query": gql}
        if variables:
            payload["variables"] = variables

        return await self._execute_with_retry(payload, timeout)

    async def _execute_with_retry(self, payload: dict, timeout: int) -> dict:
        """Execute an HTTP request with retry logic.

        Args:
            payload: The JSON payload to send.
            timeout: Request timeout in seconds.

        Returns:
            Parsed JSON response.
        """

        @retry(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=4),
            retry=retry_if_exception_type(
                (httpx.TimeoutException, httpx.ConnectError, _RetryableHTTPError)
            ),
            reraise=True,
        )
        async def _do_request() -> dict:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    self._endpoint,
                    json=payload,
                    headers=self._headers,
                )

                # Handle rate limiting.
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After", "2")
                    try:
                        wait_time = int(retry_after)
                    except ValueError:
                        wait_time = 2
                    logger.warning("Rate limited (429), waiting %ds", wait_time)
                    await asyncio.sleep(wait_time)
                    raise _RetryableHTTPError(f"429 Too Many Requests")

                # Retry on server errors.
                if resp.status_code in (500, 502, 503, 504):
                    raise _RetryableHTTPError(f"HTTP {resp.status_code}")

                # Don't retry client errors.
                if resp.status_code in (400, 401, 403):
                    resp.raise_for_status()

                resp.raise_for_status()

                body = resp.json()

                # Log query info (never the key).
                query_preview = payload.get("query", "")[:50]
                response_size = len(resp.content)
                logger.debug(
                    "NerdGraph query: '%s...' response: %d bytes",
                    query_preview,
                    response_size,
                )

                # Handle partial NerdGraph errors.
                if "errors" in body and body.get("data"):
                    logger.warning(
                        "NerdGraph partial error: %s",
                        body["errors"][0].get("message", "unknown"),
                    )

                return body

        return await _do_request()

    async def batch_query(self, queries: list[dict]) -> list[dict]:
        """Execute multiple NerdGraph queries in parallel.

        Individual failures return error dicts rather than failing the batch.

        Args:
            queries: List of dicts with 'query' and optional 'variables' keys.

        Returns:
            List of response dicts (or error dicts) in the same order.
        """

        async def _safe_query(q: dict) -> dict:
            try:
                return await self.query(
                    q["query"],
                    variables=q.get("variables"),
                    timeout_override=q.get("timeout"),
                )
            except ReadOnlyViolation:
                raise
            except Exception as exc:
                return {"error": str(exc), "query_preview": q["query"][:50]}

        results = await asyncio.gather(
            *[_safe_query(q) for q in queries], return_exceptions=False
        )
        return list(results)


class _RetryableHTTPError(Exception):
    """Internal exception to signal that an HTTP error should be retried."""

    pass


def get_client() -> NerdGraphClient:
    """Get a NerdGraphClient using the currently active account context.

    Returns:
        NerdGraphClient configured with active credentials.

    Raises:
        NotConnectedError: If no account is active.
    """
    ctx = AccountContext()
    credentials, _ = ctx.get_active()
    return NerdGraphClient(credentials)
