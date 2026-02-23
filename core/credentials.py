"""
Multi-tenant credential manager for Sherlock.

Stores profile metadata in ~/.sherlock/profiles.json and API keys
securely in the system keychain via the keyring library. API keys are
never written to disk in plain text.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
import keyring
from pydantic import BaseModel, Field, field_serializer

from core.exceptions import CredentialError

logger = logging.getLogger("sherlock.credentials")

# Keyring service name used for all stored API keys.
KEYRING_SERVICE = "sherlock"

# Base directory for all NewRelic MCP configuration.
CONFIG_DIR = Path.home() / ".sherlock"

# Path to the profiles metadata file.
PROFILES_FILE = CONFIG_DIR / "profiles.json"

# NerdGraph endpoints by region.
NERDGRAPH_ENDPOINTS: dict[str, str] = {
    "US": "https://api.newrelic.com/graphql",
    "EU": "https://api.eu.newrelic.com/graphql",
}

# Validation query — minimal query to verify credentials.
VALIDATION_QUERY = """
{
  actor {
    user {
      name
      email
    }
    account(id: %s) {
      name
    }
  }
}
"""


class Credentials(BaseModel):
    """New Relic API credentials for a single account."""

    account_id: str
    api_key: str = Field(exclude=True)
    region: Literal["US", "EU"] = "US"

    @field_serializer("api_key")
    def _redact_key(self, v: str) -> str:
        """Never serialize the real key."""
        return self.redacted_key

    @property
    def endpoint(self) -> str:
        """Return the NerdGraph endpoint URL for this credential's region."""
        return NERDGRAPH_ENDPOINTS[self.region]

    @property
    def redacted_key(self) -> str:
        """Return a redacted version of the API key for display/logging."""
        if len(self.api_key) <= 8:
            return "****"
        return f"{self.api_key[:4]}-***-{self.api_key[-4:]}"


class CredentialManager:
    """Manages New Relic credential profiles with secure keychain storage."""

    def __init__(self) -> None:
        """Initialize the credential manager and ensure config directories exist."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not PROFILES_FILE.exists():
            PROFILES_FILE.write_text("[]", encoding="utf-8")

    def _load_profiles_data(self) -> list[dict]:
        """Load raw profile metadata from disk.

        Returns:
            List of profile metadata dicts.
        """
        try:
            data = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save_profiles_data(self, profiles: list[dict]) -> None:
        """Write profile metadata to disk.

        Args:
            profiles: List of profile metadata dicts to persist.
        """
        PROFILES_FILE.write_text(
            json.dumps(profiles, indent=2, default=str), encoding="utf-8"
        )

    def save_profile(
        self,
        profile_name: str,
        account_id: str,
        api_key: str,
        region: str = "US",
    ) -> dict:
        """Save a new credential profile.

        Stores metadata in profiles.json and the API key in the system keychain.

        Args:
            profile_name: Unique human-readable name for this profile.
            account_id: New Relic account ID.
            api_key: New Relic User API key.
            region: 'US' or 'EU'.

        Returns:
            Dict with profile metadata.
        """
        region = region.upper()
        if region not in ("US", "EU"):
            region = "US"

        # Store API key in keychain.
        keyring.set_password(KEYRING_SERVICE, profile_name, api_key)

        # Update profiles metadata.
        profiles = self._load_profiles_data()
        # Remove existing profile with same name.
        profiles = [p for p in profiles if p.get("name") != profile_name]
        profile_meta = {
            "name": profile_name,
            "account_id": account_id,
            "region": region,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        profiles.append(profile_meta)
        self._save_profiles_data(profiles)

        logger.info("Profile saved: %s (account %s, region %s)", profile_name, account_id, region)
        return profile_meta

    def load_profile(self, profile_name: str) -> Credentials:
        """Load credentials for a named profile.

        Args:
            profile_name: The profile name to load.

        Returns:
            Credentials instance with account_id, api_key, and region.

        Raises:
            CredentialError: If the profile or its keychain entry is not found.
        """
        profiles = self._load_profiles_data()
        meta = next((p for p in profiles if p.get("name") == profile_name), None)
        if not meta:
            raise CredentialError(
                f"Profile '{profile_name}' not found.",
                account_id="",
                http_status=None,
            )

        api_key = keyring.get_password(KEYRING_SERVICE, profile_name)
        if not api_key:
            raise CredentialError(
                f"API key for profile '{profile_name}' not found in keychain.",
                account_id=meta.get("account_id", ""),
                http_status=None,
            )

        return Credentials(
            account_id=meta["account_id"],
            api_key=api_key,
            region=meta.get("region", "US"),
        )

    def delete_profile(self, profile_name: str) -> bool:
        """Delete a credential profile and its keychain entry.

        Args:
            profile_name: The profile name to delete.

        Returns:
            True if the profile was found and deleted.
        """
        profiles = self._load_profiles_data()
        new_profiles = [p for p in profiles if p.get("name") != profile_name]
        deleted = len(new_profiles) < len(profiles)

        if deleted:
            self._save_profiles_data(new_profiles)
            try:
                keyring.delete_password(KEYRING_SERVICE, profile_name)
            except keyring.errors.PasswordDeleteError:
                pass
            logger.info("Profile deleted: %s", profile_name)

        return deleted

    def list_profiles(self) -> list[dict]:
        """List all saved credential profiles (without API keys).

        Returns:
            List of profile metadata dicts with name, account_id, region, created_at.
        """
        return self._load_profiles_data()

    async def validate_credentials(
        self, account_id: str, api_key: str, region: str = "US"
    ) -> dict:
        """Validate credentials by making a test NerdGraph query.

        Args:
            account_id: New Relic account ID to validate.
            api_key: New Relic User API key.
            region: 'US' or 'EU'.

        Returns:
            Dict with valid, user_name, account_name, and error fields.
        """
        endpoint = NERDGRAPH_ENDPOINTS.get(region.upper(), NERDGRAPH_ENDPOINTS["US"])
        query = VALIDATION_QUERY % account_id

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    endpoint,
                    json={"query": query},
                    headers={"API-Key": api_key, "Content-Type": "application/json"},
                )

            if resp.status_code == 401:
                return {
                    "valid": False,
                    "user_name": "",
                    "account_name": "",
                    "error": "Invalid API key (HTTP 401).",
                }

            if resp.status_code == 403:
                return {
                    "valid": False,
                    "user_name": "",
                    "account_name": "",
                    "error": "Forbidden — key lacks required permissions (HTTP 403).",
                }

            if resp.status_code != 200:
                return {
                    "valid": False,
                    "user_name": "",
                    "account_name": "",
                    "error": f"Unexpected HTTP {resp.status_code}.",
                }

            body = resp.json()
            errors = body.get("errors")
            if errors:
                return {
                    "valid": False,
                    "user_name": "",
                    "account_name": "",
                    "error": errors[0].get("message", "Unknown NerdGraph error."),
                }

            actor = body.get("data", {}).get("actor", {})
            user = actor.get("user", {})
            account = actor.get("account", {})

            return {
                "valid": True,
                "user_name": user.get("name", ""),
                "account_name": account.get("name", ""),
                "error": "",
            }

        except httpx.TimeoutException:
            return {
                "valid": False,
                "user_name": "",
                "account_name": "",
                "error": "Connection timed out.",
            }
        except httpx.ConnectError:
            return {
                "valid": False,
                "user_name": "",
                "account_name": "",
                "error": "Could not connect to New Relic API.",
            }
        except Exception as exc:
            return {
                "valid": False,
                "user_name": "",
                "account_name": "",
                "error": f"Validation failed: {type(exc).__name__}",
            }
