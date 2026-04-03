"""
Session memory for Sherlock — stores investigation snapshots within an MCP
server process lifetime. In-memory only; intentionally lost on restart.

This is session-scoped memory, not persistent storage. It allows Sherlock
to answer follow-up questions like "why did that happen again?" or "is the
same service still degraded?" without running a full new investigation.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class InvestigationSnapshot:
    """A lightweight record of a completed investigation."""

    timestamp: datetime
    account_id: str
    account_name: str
    service_name: str
    bare_name: str
    namespace: str
    severity: str               # CRITICAL | WARNING | HEALTHY
    root_cause: str
    causal_chain: str           # "A → B → C" format, empty if none detected
    causal_pattern: str         # DB_CASCADE | SHARED_INFRA | DEPLOY_REGRESSION |
                                # CHRONIC | TRAFFIC_FLOOD | NONE
    error_rate: Optional[float]
    is_otel: bool
    open_incident_ids: list[str] = field(default_factory=list)
    chronic_flag: bool = False
    stale_signal_flag: bool = False
    cross_account_entities: list[str] = field(default_factory=list)
    since_minutes: int = 60

    def age_minutes(self) -> float:
        """Minutes since this investigation completed."""
        now = datetime.now(timezone.utc)
        ts = self.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() / 60

    def is_recent(self, threshold_minutes: int = 120) -> bool:
        """True if investigation was within the last N minutes."""
        return self.age_minutes() < threshold_minutes

    def short_summary(self) -> str:
        """One-line summary for context blocks."""
        age = int(self.age_minutes())
        age_str = f"{age}m ago" if age < 60 else f"{age // 60}h {age % 60}m ago"
        chain = f" | {self.causal_chain}" if self.causal_chain else ""
        return (
            f"[{age_str}] {self.service_name} — {self.severity}"
            f"{' (CHRONIC)' if self.chronic_flag else ''}"
            f"{' (STALE ALERT)' if self.stale_signal_flag else ''}"
            f"{chain}"
        )


class SessionMemory:
    """
    Thread-safe singleton that maintains investigation history for the
    current MCP server session. Each account gets its own rolling buffer
    of the last MAX_PER_ACCOUNT snapshots.

    Lifetime: same as the MCP server process. Intentionally lost on restart.
    """

    _instance: Optional["SessionMemory"] = None
    _lock: threading.Lock = threading.Lock()

    MAX_PER_ACCOUNT = 10          # rolling buffer depth per account
    RECENT_THRESHOLD_MINUTES = 120  # how old before "not recent"

    def __new__(cls) -> "SessionMemory":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._history: dict[str, deque[InvestigationSnapshot]] = {}
                cls._instance._rw_lock = threading.RLock()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton — used in tests only."""
        with cls._lock:
            cls._instance = None

    # ── Write ─────────────────────────────────────────────────────────────────

    def record(self, snapshot: InvestigationSnapshot) -> None:
        """Save a completed investigation snapshot."""
        with self._rw_lock:
            account_id = snapshot.account_id
            if account_id not in self._history:
                self._history[account_id] = deque(maxlen=self.MAX_PER_ACCOUNT)
            self._history[account_id].appendleft(snapshot)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_recent(
        self,
        account_id: str,
        limit: int = 5,
        max_age_minutes: int = RECENT_THRESHOLD_MINUTES,
    ) -> list[InvestigationSnapshot]:
        """Return recent investigations for an account, newest first."""
        with self._rw_lock:
            history = self._history.get(account_id, deque())
            return [
                s for s in list(history)[:limit]
                if s.age_minutes() <= max_age_minutes
            ]

    def get_last(self, account_id: str) -> Optional[InvestigationSnapshot]:
        """Return the most recent investigation for an account."""
        recent = self.get_recent(account_id, limit=1)
        return recent[0] if recent else None

    def find_service(
        self,
        account_id: str,
        service_name: str,
        max_age_minutes: int = RECENT_THRESHOLD_MINUTES,
    ) -> Optional[InvestigationSnapshot]:
        """
        Find the most recent investigation for a specific service.
        Matches on full name, bare name, or namespace — fuzzy enough to
        handle "client-service" matching "eswd-prod/client-service".
        """
        name_lower = service_name.lower()
        with self._rw_lock:
            for snapshot in self._history.get(account_id, deque()):
                if snapshot.age_minutes() > max_age_minutes:
                    continue
                if (
                    name_lower in snapshot.service_name.lower()
                    or name_lower in snapshot.bare_name.lower()
                    or snapshot.bare_name.lower() in name_lower
                ):
                    return snapshot
        return None

    def has_history(self, account_id: str) -> bool:
        """True if any investigation exists for this account in this session."""
        return bool(self._history.get(account_id))

    def format_context_block(
        self,
        account_id: str,
        limit: int = 3,
    ) -> str:
        """
        Format recent investigations as a context block for the Team Lead.
        Returns empty string if no recent history.
        """
        recent = self.get_recent(account_id, limit=limit)
        if not recent:
            return ""

        lines = ["## Session Context — Recent Investigations\n"]
        for i, snap in enumerate(recent, 1):
            lines.append(f"{i}. {snap.short_summary()}")
            if snap.root_cause:
                lines.append(f"   Root cause: {snap.root_cause}")
            if snap.open_incident_ids:
                lines.append(
                    f"   Open incidents: {', '.join(snap.open_incident_ids[:3])}"
                )
        lines.append(
            "\nUse this context to answer follow-up questions without "
            "re-investigating unless the engineer asks for a fresh check.\n"
        )
        return "\n".join(lines)
