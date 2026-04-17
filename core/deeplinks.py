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
        self._account_id = str(account_id)
        self._base = _base(region)

    # ── GUID helpers ───────────────────────────────────────────────

    def _build_guid(self, entity_id: str) -> str:
        """Build a base64-encoded NR entity GUID from an entity ID.

        Format: ``{account_id}|APM|APPLICATION|{entity_id}``
        """
        raw = f"{self._account_id}|APM|APPLICATION|{entity_id}"
        return base64.b64encode(raw.encode()).decode().rstrip("=")

    # ── APM overview links ─────────────────────────────────────────

    def apm_overview(
        self,
        entity_guid: str,
        since_minutes: int | None = None,
    ) -> str | None:
        """Open the APM overview page for a service.

        Uses the current ``nr1-core/apm/overview/{guid}`` path.
        """
        try:
            url = (
                f"{self._base}/nr1-core/apm/overview/{entity_guid}"
                f"?account={self._account_id}"
            )
            if since_minutes:
                url += f"&duration={since_minutes * 60 * 1000}"
            return url
        except Exception:
            return None

    # ── Service map link ───────────────────────────────────────────

    def service_map(self, entity_guid: str) -> str | None:
        """Open the service map centred on an entity."""
        try:
            return (
                f"{self._base}/nr1-core"
                f"?account={self._account_id}"
                f"&entity={entity_guid}"
                f"&viz=service-map"
            )
        except Exception:
            return None

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

    def apm_errors(
        self, entity_guid: str, since_minutes: int = 30
    ) -> str | None:
        """Open the errors inbox for an APM service.

        Verified 2026-04: the correct NR1 route is
        ``/nr1-core/errors-inbox/entity-inbox/<GUID>``. The old
        ``?nerdletId=errors-inbox.homepage`` redirect URL silently lands
        on the APM summary page instead of the errors inbox.
        """
        try:
            return (
                f"{self._base}/nr1-core/errors-inbox/entity-inbox/{entity_guid}"
                f"?duration={since_minutes * 60 * 1000}"
                f"&filters=selectedInstance%20IN%20%28%29"
            )
        except Exception:
            return None

    def apm_transactions(
        self, entity_guid: str, since_minutes: int = 30
    ) -> str | None:
        """Open the transaction list for an APM service.

        Verified 2026-04: the correct NR1 route is
        ``/nr1-core/apm-features/transactions/<GUID>``. The old
        ``?nerdletId=apm-nerdlets.apm-transactions-nerdlet`` redirect
        silently lands on the APM summary page.
        """
        try:
            return (
                f"{self._base}/nr1-core/apm-features/transactions/{entity_guid}"
                f"?duration={since_minutes * 60 * 1000}"
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
        """Open New Relic Logs filtered to a service.

        Uses the NR1 launcher ``pane=`` format with base64-encoded JSON
        to pre-load the Lucene query in the logger.log-tailer nerdlet.
        This correctly handles dotted attribute names like ``entity.name``.
        """
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
    #
    # NR retired the legacy ``/kubernetes?accountId=X`` route — it now
    # redirects to the Catalog home page. The current working path is the
    # entity explorer (``/nr1-core``) filtered on both ``domain`` and
    # ``type``. Verified 2026-04 against a user-shared working URL from
    # the live NR UI. Omitting ``domain`` causes NR to drop the type
    # filter and fall back to "All Entities".

    _K8S_DOMAINS = "'EXT','INFRA','UNINSTRUMENTED'"

    _K8S_ENTITY_TYPES = (
        "'ARGOCD','CALICO','COREDNS','ENVOY','ISTIO_SERVICE','KEDA',"
        "'NGINX_INGRESS_CONTROLLER','PROMETHEUS_SERVER',"
        "'KUBERNETESCLUSTER','KUBERNETES_APISERVER','KUBERNETES_CRONJOB',"
        "'KUBERNETES_DAEMONSET','KUBERNETES_DEPLOYMENT','KUBERNETES_JOB',"
        "'KUBERNETES_NAMESPACE','KUBERNETES_PERSISTENTVOLUME',"
        "'KUBERNETES_PERSISTENTVOLUMECLAIM','KUBERNETES_POD',"
        "'KUBERNETES_REPLICASET','KUBERNETES_STATEFULSET'"
    )

    def _k8s_base_filter(self) -> str:
        return (
            f"domain IN ({self._K8S_DOMAINS})"
            f" AND type IN ({self._K8S_ENTITY_TYPES})"
        )

    def k8s_explorer(self, namespace: str | None = None) -> str | None:
        """Open the K8s cluster explorer, optionally filtered to a namespace."""
        try:
            filter_expr = self._k8s_base_filter()
            if namespace:
                filter_expr += (
                    f" AND tags.namespaceName = '{namespace}'"
                )
            return (
                f"{self._base}/nr1-core"
                f"?account={self._account_id}"
                f"&filters={urllib.parse.quote(f'({filter_expr})', safe='')}"
            )
        except Exception:
            return None

    def k8s_workload(
        self, namespace: str, deployment_name: str
    ) -> str | None:
        """Open K8s view filtered to a specific deployment."""
        try:
            filter_expr = (
                f"{self._k8s_base_filter()}"
                f" AND tags.namespaceName = '{namespace}'"
                f" AND tags.deploymentName = '{deployment_name}'"
            )
            return (
                f"{self._base}/nr1-core"
                f"?account={self._account_id}"
                f"&filters={urllib.parse.quote(f'({filter_expr})', safe='')}"
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
        """Open synthetic monitor run results.

        Verified 2026-04: the correct NR1 route is
        ``/synthetics/monitor-result-list/<GUID>``. The old
        ``?nerdletId=synthetics-nerdlets...`` redirect URL silently lands
        on the monitor summary page instead of the results list.
        """
        try:
            url = (
                f"{self._base}/synthetics/monitor-result-list/{entity_guid}"
                f"?duration={since_minutes * 60 * 1000}"
            )
            if result_filter:
                url += f"&result={result_filter}"
            return url
        except Exception:
            return None

    # ── Alert links ────────────────────────────────────────────────
    #
    # The old AIOPS ``/accounts/<id>/incidents/<id>/redirect`` URL returns
    # "We can't display this alert event" unless the incident ID is a
    # *current, live* event that the session has cached. For historical
    # or stale incident IDs (the majority of what Sherlock surfaces) the
    # page is useless. Verified 2026-04: the ``/alerts`` page with a
    # large enough ``duration`` window shows the same incident in context
    # along with neighbouring events, which is what engineers want.

    def alert_incident(
        self, incident_id: str, since_minutes: int = 4320
    ) -> str | None:
        """Open the alerts page with a window that includes the incident.

        ``since_minutes`` default of 4320 (72h) matches the NR UI default
        for the alerts view and covers the retention window for most
        incident records.

        ``incident_id`` is accepted for API compatibility but is no
        longer encoded in the URL — NR's router does not accept a
        client-supplied incident ID, and the incident detail drawer
        requires a server-stored session state. The alerts page opens
        the full list, from which the user can click the specific
        incident.
        """
        try:
            _ = incident_id  # kept for API compatibility
            return (
                f"{self._base}/alerts"
                f"?account={self._account_id}"
                f"&duration={since_minutes * 60 * 1000}"
            )
        except Exception:
            return None

    # ── NRQL-based log links (preferred) ───────────────────────────

    def log_search_nrql(
        self,
        service_name: str,
        service_attribute: str,
        severity: str | None = None,
        keyword: str | None = None,
        since_minutes: int = 60,
        limit: int = 100,
    ) -> str | None:
        """Open New Relic Query Builder pre-loaded with a log search NRQL.

        Uses nrql_chart() with the pane= format to pre-load the NRQL
        query in the query builder. Reliable across all attribute names
        including dotted ones like entity.name.
        """
        try:
            sev_attr = "level"
            nrql = (
                f"SELECT timestamp, message, `{service_attribute}`, {sev_attr} "
                f"FROM Log "
                f"WHERE `{service_attribute}` LIKE '%{service_name}%'"
            )
            if severity:
                levels = ", ".join(f"'{s.strip()}'" for s in severity.split(","))
                nrql += f" AND `{sev_attr}` IN ({levels})"
            if keyword:
                nrql += f" AND message LIKE '%{keyword}%'"
            nrql += f" SINCE {since_minutes} minutes ago ORDER BY timestamp DESC LIMIT {min(limit, 100)}"
            return self.nrql_chart(nrql, since_minutes)
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
