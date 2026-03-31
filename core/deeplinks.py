"""
Deep-link URL builder for Sherlock.

Constructs clickable New Relic URLs that take engineers directly
to the exact chart, view, or entity corresponding to a finding.

All URL construction lives here and ONLY here.
No other file builds URLs directly.

Uses only stdlib: urllib.parse, base64.  Zero new package dependencies.
"""

import base64
import json
import logging
import urllib.parse

from core.context import AccountContext

logger = logging.getLogger("sherlock.deeplinks")

# ── Base URLs ────────────────────────────────────────────────────────────

NR_BASE_US = "https://one.newrelic.com"
NR_BASE_EU = "https://one.eu.newrelic.com"

NR_AIOPS_BASE = "https://aiops.service.newrelic.com"


def _base(region: str) -> str:
    """Return the correct New Relic base URL for the given region."""
    return NR_BASE_EU if region.upper() == "EU" else NR_BASE_US


# ── DeepLinkBuilder ──────────────────────────────────────────────────────


class DeepLinkBuilder:
    """Builds New Relic deep-link URLs for a specific account.

    Every public method returns ``str`` (complete URL) or ``None`` on
    failure.  Every method is wrapped in try/except so that a broken
    link **never** blocks a tool response.
    """

    def __init__(self, account_id: str, region: str) -> None:
        self._account_id = account_id
        self._base = _base(region)

    # ── NRQL / Query Builder links ─────────────────────────────────

    def nrql_chart(self, nrql: str, since_minutes: int) -> str | None:
        """Open the New Relic query builder with *nrql* pre-loaded.

        The NRQL must already contain the correct ``SINCE`` clause.
        Uses the launcher path with a base64-encoded ``pane`` parameter
        which is how NR1 nerdlets consume their initial configuration.
        """
        try:
            pane = json.dumps(
                {
                    "nerdletId": "data-exploration.query-builder",
                    "initialActiveInterface": "nrqlEditor",
                    "initialNrqlValue": nrql,
                    "initialAccountId": int(self._account_id),
                },
                separators=(",", ":"),
            )
            pane_b64 = base64.b64encode(pane.encode()).decode()
            return (
                f"{self._base}/launcher/data-exploration.query-builder"
                f"?pane={urllib.parse.quote(pane_b64, safe='')}"
                f"&platform[accountId]={self._account_id}"
            )
        except Exception:
            return None

    def spike_chart(self, timeseries_nrql: str, since_minutes: int) -> str | None:
        """Convenience wrapper for :meth:`nrql_chart` when showing a spike.

        The NRQL must contain ``TIMESERIES`` so the spike is visible.
        """
        try:
            return self.nrql_chart(timeseries_nrql, since_minutes)
        except Exception:
            return None

    # ── Entity links ───────────────────────────────────────────────

    def entity_link(self, entity_guid: str) -> str | None:
        """Open any entity by GUID (APM, Synthetic, Browser, etc.)."""
        try:
            return f"{self._base}/redirect/entity/{entity_guid}"
        except Exception:
            return None

    def apm_errors(self, entity_guid: str) -> str | None:
        """Open error inbox / error analysis for an APM service."""
        try:
            return (
                f"{self._base}/redirect/entity/{entity_guid}"
                f"?nerdletId=errors-inbox.homepage"
            )
        except Exception:
            return None

    def apm_transactions(self, entity_guid: str) -> str | None:
        """Open the transaction list for an APM service."""
        try:
            return (
                f"{self._base}/redirect/entity/{entity_guid}"
                f"?nerdletId=apm-nerdlets.apm-transactions-nerdlet"
            )
        except Exception:
            return None

    def distributed_traces(
        self,
        entity_guid: str,
        since_minutes: int,
        error_only: bool = False,
    ) -> str | None:
        """Open distributed tracing filtered to a service."""
        try:
            url = (
                f"{self._base}/distributed-tracing"
                f"?accountId={self._account_id}"
                f"&duration={since_minutes * 60 * 1000}"
                f"&entity.guid={entity_guid}"
            )
            if error_only:
                payload = json.dumps({"error": True}, separators=(",", ":"))
                b64 = base64.b64encode(payload.encode()).decode()
                url += f"&filters={urllib.parse.quote(b64, safe='')}"
            return url
        except Exception:
            return None

    # ── Log links ──────────────────────────────────────────────────

    def log_search(
        self,
        service_name: str,
        service_attribute: str,
        severity: str | None = None,
        since_minutes: int = 60,
    ) -> str | None:
        """Open New Relic Logs filtered to a service."""
        try:
            query = f"{service_attribute}:'{service_name}'"
            if severity:
                query += f" AND level:'{severity}'"
            pane = json.dumps(
                {
                    "nerdletId": "logger.log-tailer",
                    "accountId": int(self._account_id),
                    "duration": since_minutes * 60 * 1000,
                    "query": query,
                },
                separators=(",", ":"),
            )
            pane_b64 = base64.b64encode(pane.encode()).decode()
            return (
                f"{self._base}/launcher/logger.log-tailer"
                f"?pane={urllib.parse.quote(pane_b64, safe='')}"
                f"&platform[accountId]={self._account_id}"
            )
        except Exception:
            return None

    # ── Kubernetes links ───────────────────────────────────────────

    def k8s_explorer(self, namespace: str | None = None) -> str | None:
        """Open the K8s cluster explorer, optionally filtered."""
        try:
            url = (
                f"{self._base}/kubernetes"
                f"?accountId={self._account_id}"
            )
            if namespace:
                filters = json.dumps(
                    {"namespaceName": namespace}, separators=(",", ":")
                )
                url += f"&filters={urllib.parse.quote(filters, safe='')}"
            return url
        except Exception:
            return None

    def k8s_workload(
        self, namespace: str, deployment_name: str
    ) -> str | None:
        """Open K8s view filtered to a specific deployment."""
        try:
            filters = json.dumps(
                {"namespaceName": namespace, "deploymentName": deployment_name},
                separators=(",", ":"),
            )
            return (
                f"{self._base}/kubernetes"
                f"?accountId={self._account_id}"
                f"&filters={urllib.parse.quote(filters, safe='')}"
            )
        except Exception:
            return None

    # ── Synthetic links ────────────────────────────────────────────

    def synthetic_monitor(self, entity_guid: str) -> str | None:
        """Open synthetic monitor detail page."""
        try:
            return self.entity_link(entity_guid)
        except Exception:
            return None

    def synthetic_results(
        self,
        entity_guid: str,
        since_minutes: int,
        result_filter: str | None = None,
    ) -> str | None:
        """Open synthetic monitor run results."""
        try:
            url = (
                f"{self._base}/redirect/entity/{entity_guid}"
                f"?nerdletId=synthetics-nerdlets.synthetics-monitor-overview-react"
                f"&duration={since_minutes * 60 * 1000}"
            )
            if result_filter:
                url += f"&result={result_filter}"
            return url
        except Exception:
            return None

    # ── Alert links ────────────────────────────────────────────────

    def alert_incident(self, incident_id: str) -> str | None:
        """Open a specific alert incident via the AIOPS redirect URL."""
        try:
            return (
                f"{NR_AIOPS_BASE}/accounts/{self._account_id}"
                f"/incidents/{incident_id}/redirect"
            )
        except Exception:
            return None


# ── Module-level convenience ─────────────────────────────────────────────


def get_builder() -> DeepLinkBuilder | None:
    """Get a :class:`DeepLinkBuilder` for the currently active account.

    Reads credentials from context.  Returns ``None`` if not connected
    (never raises).
    """
    try:
        ctx = AccountContext()
        creds, _ = ctx.get_active()
        return DeepLinkBuilder(
            account_id=creds.account_id,
            region=creds.region,
        )
    except Exception:
        return None
