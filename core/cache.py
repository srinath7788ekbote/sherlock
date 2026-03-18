"""
TTL cache for account intelligence data.

Provides both in-memory and disk-based caching of AccountIntelligence
objects, with configurable TTL and background refresh support.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("sherlock.cache")

# Default TTL: 30 minutes.
DEFAULT_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "1800"))

# Disk cache directory.
CACHE_DIR = Path(__file__).resolve().parent.parent / ".sherlock" / "cache"


class IntelligenceCache:
    """TTL cache for AccountIntelligence data with disk and memory layers."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        """Initialize the intelligence cache.

        Args:
            ttl_seconds: Time-to-live for cached entries in seconds.
        """
        self._ttl = ttl_seconds
        self._memory: dict[str, dict[str, Any]] = {}
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _disk_path(self, account_id: str) -> Path:
        """Return the disk cache file path for an account.

        Args:
            account_id: The New Relic account ID.

        Returns:
            Path to the cache JSON file.
        """
        return CACHE_DIR / f"{account_id}.json"

    def get(self, account_id: str) -> dict | None:
        """Retrieve cached intelligence for an account.

        Checks memory first, then disk. Returns None if no cache exists
        or if the entry has expired.

        Args:
            account_id: The New Relic account ID.

        Returns:
            Cached intelligence dict or None.
        """
        # Check memory cache.
        mem_entry = self._memory.get(account_id)
        if mem_entry and not self._is_expired(mem_entry):
            logger.debug("Cache HIT (memory) for account %s", account_id)
            return mem_entry["data"]

        # Check disk cache.
        disk_path = self._disk_path(account_id)
        if disk_path.exists():
            try:
                raw = json.loads(disk_path.read_text(encoding="utf-8"))
                if not self._is_expired(raw):
                    # Promote to memory.
                    self._memory[account_id] = raw
                    logger.debug("Cache HIT (disk) for account %s", account_id)
                    return raw["data"]
                else:
                    logger.debug("Cache EXPIRED (disk) for account %s", account_id)
            except (json.JSONDecodeError, KeyError, OSError) as exc:
                logger.warning("Corrupt cache file for account %s: %s", account_id, exc)

        logger.debug("Cache MISS for account %s", account_id)
        return None

    def get_stale(self, account_id: str) -> dict | None:
        """Retrieve cached intelligence even if stale.

        Useful for returning stale data while a background refresh is in progress.

        Args:
            account_id: The New Relic account ID.

        Returns:
            Cached intelligence dict (possibly stale) or None.
        """
        mem_entry = self._memory.get(account_id)
        if mem_entry:
            return mem_entry["data"]

        disk_path = self._disk_path(account_id)
        if disk_path.exists():
            try:
                raw = json.loads(disk_path.read_text(encoding="utf-8"))
                return raw.get("data")
            except (json.JSONDecodeError, OSError):
                pass

        return None

    def set(self, account_id: str, data: dict) -> None:
        """Store intelligence data in both memory and disk caches.

        Args:
            account_id: The New Relic account ID.
            data: The AccountIntelligence data as a dict.
        """
        entry = {
            "data": data,
            "cached_at": time.time(),
            "ttl": self._ttl,
        }

        # Memory cache.
        self._memory[account_id] = entry

        # Disk cache.
        try:
            disk_path = self._disk_path(account_id)
            disk_path.write_text(json.dumps(entry, default=str), encoding="utf-8")
            logger.debug("Cache SET for account %s", account_id)
        except OSError as exc:
            logger.warning("Failed to write disk cache for account %s: %s", account_id, exc)

    def invalidate(self, account_id: str) -> None:
        """Remove cached intelligence for an account.

        Args:
            account_id: The New Relic account ID.
        """
        self._memory.pop(account_id, None)
        disk_path = self._disk_path(account_id)
        if disk_path.exists():
            try:
                disk_path.unlink()
            except OSError:
                pass
        logger.debug("Cache INVALIDATED for account %s", account_id)

    def is_stale(self, account_id: str) -> bool:
        """Check whether the cache for an account is stale or missing.

        Args:
            account_id: The New Relic account ID.

        Returns:
            True if cache is stale or missing.
        """
        mem_entry = self._memory.get(account_id)
        if mem_entry and not self._is_expired(mem_entry):
            return False

        disk_path = self._disk_path(account_id)
        if disk_path.exists():
            try:
                raw = json.loads(disk_path.read_text(encoding="utf-8"))
                return self._is_expired(raw)
            except (json.JSONDecodeError, OSError):
                pass

        return True

    def _is_expired(self, entry: dict) -> bool:
        """Check if a cache entry has expired.

        Args:
            entry: Cache entry with 'cached_at' and 'ttl' keys.

        Returns:
            True if the entry is past its TTL.
        """
        cached_at = entry.get("cached_at", 0)
        ttl = entry.get("ttl", self._ttl)
        return (time.time() - cached_at) > ttl
