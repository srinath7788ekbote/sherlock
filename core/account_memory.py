"""
Persistent service-to-account memory for Sherlock.

Maintains a local JSON index mapping service/entity names to the
New Relic account IDs where they were last seen. This eliminates
the need to connect-and-learn every account when investigating a
service — Sherlock can look up the account instantly and connect
directly.

The memory file lives under .sherlock/ which is already gitignored,
so no client data ever reaches the repo.

Lifetime: persists across MCP server restarts. Automatically
refreshed whenever learn_account() completes.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from core.intelligence import AccountIntelligence

logger = logging.getLogger("sherlock.account_memory")

# Minimum length for substring matching to avoid false positives.
_MIN_SUBSTRING_LEN = 4


class AccountMemoryEntry(BaseModel):
    """A single service→account mapping."""

    account_id: str
    account_name: str = ""
    profile_name: str = ""
    region: str = "US"
    last_seen: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AccountMemoryIndex(BaseModel):
    """The full persistent index."""

    version: int = 1
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # Key: lowercased service/entity name → AccountMemoryEntry
    services: dict[str, AccountMemoryEntry] = Field(default_factory=dict)
    # Key: account_id → profile_name (for reverse lookup)
    account_profiles: dict[str, str] = Field(default_factory=dict)
    # Key: account_id → account_name
    account_names: dict[str, str] = Field(default_factory=dict)


class AccountMemory:
    """
    Persistent service→account index backed by .sherlock/account_memory.json.

    Thread-safe singleton. Reads from disk on first access, writes after
    every learn_account refresh.

    IMPORTANT: This file is gitignored (.sherlock/ is in .gitignore).
    It contains client-specific data (account IDs, service names) that
    must NEVER be committed to the repository.
    """

    _instance: AccountMemory | None = None
    _lock: threading.Lock = threading.Lock()
    MEMORY_FILE: Path = (
        Path(__file__).resolve().parent.parent / ".sherlock" / "account_memory.json"
    )
    # Entries older than this are considered stale and re-learned.
    STALE_THRESHOLD_HOURS: int = 24

    def __new__(cls) -> AccountMemory:
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._index: AccountMemoryIndex | None = None  # lazy load
                cls._instance = inst
            return cls._instance

    # ── Persistence ──────────────────────────────────────────────────

    def _load(self) -> AccountMemoryIndex:
        """Load index from disk, or create empty if not found/corrupt."""
        if self._index is not None:
            return self._index

        try:
            if self.MEMORY_FILE.exists():
                raw = self.MEMORY_FILE.read_text(encoding="utf-8")
                self._index = AccountMemoryIndex.model_validate_json(raw)
                logger.info(
                    "Loaded account memory: %d services indexed",
                    len(self._index.services),
                )
                return self._index
        except Exception as exc:
            logger.warning(
                "Failed to load account memory (%s), starting fresh: %s",
                self.MEMORY_FILE,
                exc,
            )

        self._index = AccountMemoryIndex()
        return self._index

    def _save(self) -> None:
        """Persist current index to disk. Atomic write via temp file + rename."""
        if self._index is None:
            return

        self._index.updated_at = datetime.now(timezone.utc)

        try:
            self.MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = self._index.model_dump_json(indent=2)

            # Atomic write: write to temp file in same directory, then rename.
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self.MEMORY_FILE.parent),
                prefix=".account_memory_",
                suffix=".tmp",
            )
            try:
                os.write(fd, data.encode("utf-8"))
                os.close(fd)
                os.replace(tmp_path, str(self.MEMORY_FILE))
            except Exception:
                os.close(fd) if not os._exits else None  # noqa: SIM105
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

            logger.debug("Account memory saved: %d services", len(self._index.services))
        except Exception as exc:
            logger.warning("Failed to save account memory: %s", exc)

    # ── Recording ────────────────────────────────────────────────────

    def record_account_intelligence(
        self,
        account_id: str,
        account_name: str,
        profile_name: str,
        region: str,
        intelligence: AccountIntelligence,
    ) -> int:
        """
        Index ALL entity names from a learned AccountIntelligence.

        Returns:
            Number of entities indexed for this account.
        """
        with self._lock:
            index = self._load()
            now = datetime.now(timezone.utc)
            count = 0

            def _add(name: str) -> None:
                nonlocal count
                if not name or not name.strip():
                    return
                key = name.strip().lower()
                index.services[key] = AccountMemoryEntry(
                    account_id=account_id,
                    account_name=account_name,
                    profile_name=profile_name,
                    region=region,
                    last_seen=now,
                )
                count += 1

            # APM service names
            for name in getattr(intelligence.apm, "service_names", []):
                _add(name)

            # OTel service names
            for name in getattr(intelligence.otel, "service_names", []):
                _add(name)

            # Synthetic monitor names
            for name in getattr(intelligence.synthetics, "monitor_names", []):
                _add(name)

            # Browser app names
            for name in getattr(intelligence.browser, "app_names", []):
                _add(name)

            # Mobile app names
            for name in getattr(intelligence.mobile, "app_names", []):
                _add(name)

            # Workload names
            for name in getattr(intelligence.workloads, "workload_names", []):
                _add(name)

            # K8s namespace names
            for name in getattr(intelligence.k8s, "namespaces", []):
                _add(name)

            # K8s cluster names
            for name in getattr(intelligence.k8s, "cluster_names", []):
                _add(name)

            # Alert policy names
            for name in getattr(intelligence.alerts, "policy_names", []):
                _add(name)

            # Azure Service Bus queue entity names
            asb = getattr(intelligence, "azure_service_bus", None)
            if asb:
                for q in getattr(asb, "queues", []):
                    _add(getattr(q, "entity_name", ""))

            # Account-level mappings
            index.account_profiles[account_id] = profile_name
            index.account_names[account_id] = account_name

            self._save()

            logger.info(
                "Account memory updated: %d entities indexed for account %s (%s)",
                count,
                account_id,
                account_name,
            )
            return count

    # ── Lookup ───────────────────────────────────────────────────────

    def lookup_service(self, service_name: str) -> AccountMemoryEntry | None:
        """
        Find which account a service belongs to.

        Lookup strategy (ordered):
        1. Exact match (lowercased)
        2. Substring match — service_name contained in a known entity name
        3. Reverse substring — known entity name contained in service_name
        4. Bare name match — strip env prefix/suffix and try again

        Returns None if no match found.
        """
        with self._lock:
            index = self._load()

        if not service_name or not service_name.strip():
            return None

        needle = service_name.strip().lower()

        # 1. Exact match
        if needle in index.services:
            return index.services[needle]

        # Guard: skip substring matching for very short names
        if len(needle) < _MIN_SUBSTRING_LEN:
            return None

        # 2. Substring: needle contained in a known entity name
        for key, entry in index.services.items():
            if needle in key:
                return entry

        # 3. Reverse substring: known entity name contained in needle
        for key, entry in index.services.items():
            if len(key) >= _MIN_SUBSTRING_LEN and key in needle:
                return entry

        # 4. Bare name: strip common prefix patterns (e.g. "eswd-prod/")
        if "/" in needle:
            bare = needle.rsplit("/", 1)[-1]
            if len(bare) >= _MIN_SUBSTRING_LEN:
                # Try exact bare match
                if bare in index.services:
                    return index.services[bare]
                # Try bare as substring of known names
                for key, entry in index.services.items():
                    if bare in key:
                        return entry

        return None

    def get_profile_for_service(self, service_name: str) -> str | None:
        """
        Convenience: return the profile_name to connect for a given service.
        Returns None if service not found in memory.
        """
        entry = self.lookup_service(service_name)
        if entry and entry.profile_name:
            return entry.profile_name
        return None

    def is_stale(self, service_name: str) -> bool:
        """True if the entry is older than STALE_THRESHOLD_HOURS."""
        entry = self.lookup_service(service_name)
        if entry is None:
            return True
        age_hours = (
            datetime.now(timezone.utc) - entry.last_seen
        ).total_seconds() / 3600
        return age_hours > self.STALE_THRESHOLD_HOURS

    def get_all_accounts(self) -> list[dict]:
        """Return all known account_id → profile_name → account_name mappings."""
        with self._lock:
            index = self._load()

        results = []
        for acct_id, profile in index.account_profiles.items():
            results.append({
                "account_id": acct_id,
                "profile_name": profile,
                "account_name": index.account_names.get(acct_id, ""),
            })
        return results

    def clear(self) -> None:
        """Reset the memory (for testing)."""
        with self._lock:
            self._index = AccountMemoryIndex()
            if self.MEMORY_FILE.exists():
                try:
                    self.MEMORY_FILE.unlink()
                except Exception:
                    pass

    @classmethod
    def reset_singleton(cls) -> None:
        """Reset the singleton instance (for testing)."""
        with cls._lock:
            cls._instance = None
