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


# ── APM GUID Resolution ─────────────────────────────────────────────────


def resolve_apm_guid(
    service_name: str,
    intelligence,
    *,
    require_reporting: bool = True,
) -> str | None:
    """Resolve a service name to an APM entity GUID with validation.

    Returns the GUID only when:
    - Exactly one candidate matches the name, OR
    - Multiple candidates match but exactly one is currently reporting
      (when require_reporting=True), OR
    - Multiple candidates match and none/all are reporting — returns the
      preferred candidate from service_guids (same tie-break the discovery
      block uses)

    Returns None when:
    - The name is not in intelligence.apm.service_guid_candidates
    - The resolved GUID is not in intelligence.apm.reporting_guids
      (when require_reporting=True)
    - Any exception occurs

    This helper is the ONLY supported way for response-building code to
    attach an APM entity-view link to text that names a service. Callers
    that bypass this helper risk mis-attributing GUIDs across services
    with similar names.
    """
    try:
        if not service_name:
            return None
        apm = getattr(intelligence, "apm", None)
        if not apm:
            return None

        candidates = apm.service_guid_candidates.get(service_name) or []
        if not candidates:
            return None

        if len(candidates) == 1:
            guid = candidates[0].get("guid")
            if not guid:
                return None
            if require_reporting and guid not in apm.reporting_guids:
                return None
            return guid

        # Multiple candidates — prefer the single reporting one.
        reporting = [c for c in candidates if c.get("reporting")]
        if len(reporting) == 1:
            return reporting[0].get("guid")

        # Two or more reporting, or zero reporting — fall back to the
        # preferred GUID chosen by discovery. Only safe when not require_reporting.
        if not require_reporting:
            return apm.service_guids.get(service_name)

        # Require reporting + ambiguous — DO NOT GUESS. Return None; caller
        # omits the entity-view link.
        return None
    except Exception:
        return None


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
        since_minutes: int = 30,
    ) -> str | None:
        """Open the APM overview page for a service.

        Verified 2026-04: the correct NR1 route is
        ``/nr1-core/apm/overview/<GUID>``.
        Required query params: ``account``, ``duration``.
        """
        try:
            return (
                f"{self._base}/nr1-core/apm/overview/{entity_guid}"
                f"?account={self._account_id}"
                f"&duration={since_minutes * 60 * 1000}"
            )
        except Exception:
            return None

    # ── Service map link ───────────────────────────────────────────

    def service_map(
        self, entity_guid: str, since_minutes: int = 30
    ) -> str | None:
        """Open the relationships map (service map) for an entity.

        Verified 2026-04: the correct NR1 route is
        ``/nr1-core/entity-relationships-experience-maps/relationships-map-experience/<GUID>``.
        Required query params: ``account``, ``duration``, ``filters``.
        """
        try:
            return (
                f"{self._base}/nr1-core/entity-relationships-experience-maps/"
                f"relationships-map-experience/{entity_guid}"
                f"?account={self._account_id}"
                f"&duration={since_minutes * 60 * 1000}"
                f"&filters=selectedInstance%20IN%20%28%29"
            )
        except Exception:
            return None

    # ── NRQL / Query Builder links ─────────────────────────────────
    #
    # NR retired the standalone query builder launcher URL (2026-04).
    # The ``/launcher/data-exploration.query-builder?pane=`` path now
    # redirects to the Notebooks page. The query builder is now a
    # bottom-panel overlay ("Query your data") available on every NR
    # page — there is no URL that pre-loads NRQL.
    #
    # These methods return ``None`` so callers omit the chart link.
    # Tool responses include the raw NRQL string in a ``nrql`` key
    # so engineers can copy-paste into the "Query your data" panel.

    def nrql_chart(self, nrql: str, since_minutes: int) -> str | None:
        """Retired — NR no longer supports a query-builder deep link.

        The ``/launcher/data-exploration.query-builder?pane=`` URL now
        redirects to Notebooks. Returns ``None`` so callers omit the
        link. The NRQL query is still available in the response body
        for copy-paste into NR's "Query your data" bottom panel.
        """
        return None

    def spike_chart(self, timeseries_nrql: str, since_minutes: int) -> str | None:
        """Retired — delegates to :meth:`nrql_chart` which returns None."""
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
        ``/nr1-core/errors-inbox/entity-inbox/<GUID>``.
        Required query params: ``account``, ``duration``, ``filters``.
        """
        try:
            return (
                f"{self._base}/nr1-core/errors-inbox/entity-inbox/{entity_guid}"
                f"?account={self._account_id}"
                f"&duration={since_minutes * 60 * 1000}"
                f"&filters=selectedInstance%20IN%20%28%29"
            )
        except Exception:
            return None

    def apm_transactions(
        self, entity_guid: str, since_minutes: int = 30
    ) -> str | None:
        """Open the transaction list for an APM service.

        Verified 2026-04: the correct NR1 route is
        ``/nr1-core/apm-features/transactions/<GUID>``.
        Required query params: ``account``, ``duration``, ``filters``.
        """
        try:
            return (
                f"{self._base}/nr1-core/apm-features/transactions/{entity_guid}"
                f"?account={self._account_id}"
                f"&duration={since_minutes * 60 * 1000}"
                f"&filters=selectedInstance%20IN%20%28%29"
            )
        except Exception:
            return None

    def distributed_traces(
        self,
        entity_guid: str,
        since_minutes: int = 30,
        error_only: bool = False,
    ) -> str | None:
        """Open the distributed trace list for a specific APM entity.

        Verified 2026-04: the correct NR1 route is
        ``/nr1-core/distributed-tracing/distributed-trace-list/<GUID>``.
        Required query params: ``account``, ``duration``, ``filters``.
        The old ``/distributed-tracing?entity.guid=`` opens the global
        tracing explorer instead of the service-scoped trace list.
        """
        try:
            url = (
                f"{self._base}/nr1-core/distributed-tracing/"
                f"distributed-trace-list/{entity_guid}"
                f"?account={self._account_id}"
                f"&duration={since_minutes * 60 * 1000}"
                f"&filters=selectedInstance%20IN%20%28%29"
            )
            return url
        except Exception:
            return None

    # ── NRQL-based log links ─────────────────────────────────────────
    #
    # The Lucene-style log_search() method was removed because NR1
    # deprecated the logger.log-tailer nerdlet and because the Lucene
    # parser silently drops dotted attributes like entity.name, which
    # breaks OTel-instrumented tenants. Use log_search_nrql() instead —
    # it opens the NRQL query builder with a pre-loaded query.

    # ── Kubernetes links ───────────────────────────────────────────
    #
    # Verified 2026-04: NR uses simple equality filters with backtick-
    # quoted ``tags.k8s.*`` attribute names.  The legacy ``IN (...)``
    # syntax triggers "legacy filters no longer supported" in the NR UI.
    # Cluster explorer requires a cluster GUID for the direct view;
    # without it, falls back to the entity list.

    def k8s_explorer(
        self,
        namespace: str | None = None,
        *,
        cluster: str | None = None,
        cluster_guid: str | None = None,
        since_minutes: int = 5,
    ) -> str | None:
        """Open the K8s cluster explorer.

        Verified 2026-04: the canonical K8s cluster view is
        ``/nr1-core/kubernetes-cluster-explorer/k8s-cluster-explorer/{GUID}``.
        When ``cluster_guid`` is available it opens the cluster directly.
        Without a GUID, falls back to the entity list filtered to
        ``KUBERNETESCLUSTER`` entities, optionally scoped by cluster name.

        The legacy ``/nr1-core?filters=(domain IN (...) AND type IN (...))``
        syntax triggers "legacy filters no longer supported" in the NR UI.
        """
        try:
            if cluster_guid:
                filter_expr = "(domain = 'INFRA' AND type = 'KUBERNETESCLUSTER')"
                return (
                    f"{self._base}/nr1-core/kubernetes-cluster-explorer/"
                    f"k8s-cluster-explorer/{cluster_guid}"
                    f"?account={self._account_id}"
                    f"&duration={since_minutes * 60 * 1000}"
                    f"&filters={urllib.parse.quote(filter_expr, safe='')}"
                )

            # Fallback: entity list filtered to K8s clusters.
            # Note: KUBERNETESCLUSTER entities do not have tags.k8s.clusterName
            # on themselves (the cluster name is the entity name), so we
            # cannot filter by cluster name here — just list all clusters.
            filter_expr = "(domain = 'INFRA' AND type = 'KUBERNETESCLUSTER')"
            return (
                f"{self._base}/nr1-core"
                f"?account={self._account_id}"
                f"&filters={urllib.parse.quote(filter_expr, safe='')}"
            )
        except Exception:
            return None

    def k8s_workload(
        self, namespace: str, deployment_name: str, *,
        cluster: str | None = None, deployment_guid: str | None = None,
        since_minutes: int = 5,
    ) -> str | None:
        """Open K8s view filtered to a specific deployment.

        Verified 2026-04: the correct NR1 filter syntax uses simple
        ``domain = 'INFRA' AND type = 'KUBERNETES_DEPLOYMENT'`` with
        backtick-quoted ``tags.k8s.*`` attribute names.  The legacy
        ``domain IN (...)`` / ``type IN (...)`` syntax is rejected.

        When ``deployment_guid`` is supplied, routes to the canonical
        ``k8s-deployment-overview/{guid}`` entity view URL.  Falls back
        to an entity-search filter URL when GUID is not known.
        """
        try:
            if deployment_guid:
                filter_expr = (
                    "(domain = 'INFRA' AND type = 'KUBERNETES_DEPLOYMENT')"
                )
                if cluster:
                    filter_expr += (
                        f" AND `tags.k8s.clusterName` = '{cluster}'"
                    )
                filter_expr += (
                    f" AND `tags.k8s.deploymentName` = '{deployment_name}'"
                )
                return (
                    f"{self._base}/nr1-core/kubernetes-cluster-explorer/"
                    f"k8s-deployment-overview/{deployment_guid}"
                    f"?account={self._account_id}"
                    f"&duration={since_minutes * 60 * 1000}"
                    f"&filters={urllib.parse.quote(filter_expr, safe='')}"
                )

            # Fallback: entity list filtered to this deployment.
            filter_expr = (
                "(domain = 'INFRA' AND type = 'KUBERNETES_DEPLOYMENT')"
            )
            if cluster:
                filter_expr += (
                    f" AND `tags.k8s.clusterName` = '{cluster}'"
                )
            filter_expr += (
                f" AND `tags.k8s.deploymentName` = '{deployment_name}'"
            )
            return (
                f"{self._base}/nr1-core"
                f"?account={self._account_id}"
                f"&filters={urllib.parse.quote(filter_expr, safe='')}"
            )
        except Exception:
            return None

    # ── Synthetic links ────────────────────────────────────────────

    def synthetic_monitor(
        self, entity_guid: str, since_minutes: int = 30
    ) -> str | None:
        """Open synthetic monitor overview page.

        Verified 2026-04: the direct ``/synthetics/monitor-overview/<GUID>``
        route with ``account`` and ``duration`` params lands on the monitor
        summary page without a redirect hop. The old ``entity_link`` redirect
        works too but loses the duration context.
        """
        try:
            return (
                f"{self._base}/synthetics/monitor-overview/{entity_guid}"
                f"?account={self._account_id}"
                f"&duration={since_minutes * 60 * 1000}"
            )
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
        ``/synthetics/monitor-result-list/<GUID>`` with ``account``
        and ``duration`` params.
        """
        try:
            url = (
                f"{self._base}/synthetics/monitor-result-list/{entity_guid}"
                f"?account={self._account_id}"
                f"&duration={since_minutes * 60 * 1000}"
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

    # ── Log UI links (entity view) ────────────────────────────────

    def log_search_ui(
        self,
        service_name: str | None = None,
        service_attribute: str = "entity.name",
        severity: str | None = None,
        keyword: str | None = None,
        namespace: str | None = None,
        cluster: str | None = None,
        since_minutes: int = 60,
    ) -> str | None:
        """Open the New Relic Logs UI (logger.log-tailer nerdlet) with a
        pre-filtered query.

        Unlike log_search_nrql() which opens the NRQL query builder,
        this method opens the actual Logs UI with the log stream viewer,
        timeline histogram, and pattern summary.

        Builds a filter string using NR Logs query language (Lucene-style).
        All parameters are optional — omitting all produces an unfiltered
        Logs UI link.
        """
        try:
            parts: list[str] = []
            if service_name and service_attribute:
                parts.append(f'{service_attribute}:"{service_name}"')
            if severity:
                levels = [s.strip() for s in severity.split(",") if s.strip()]
                if len(levels) == 1:
                    parts.append(f'level:"{levels[0]}"')
                elif len(levels) > 1:
                    parts.append(
                        "(" + " OR ".join(f'level:"{lv}"' for lv in levels) + ")"
                    )
            if keyword:
                parts.append(f'message:"{keyword}"')
            if namespace:
                parts.append(f'namespace_name:"{namespace}"')
            if cluster:
                parts.append(f'cluster_name:"{cluster}"')

            query = " AND ".join(parts) if parts else ""

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

    # ── NRQL-based log links (query builder) ───────────────────────

    def log_search_nrql(
        self,
        service_name: str,
        service_attribute: str,
        severity: str | None = None,
        keyword: str | None = None,
        since_minutes: int = 60,
        limit: int = 100,
    ) -> str | None:
        """Retired — NR no longer supports a query-builder deep link.

        Delegates to :meth:`nrql_chart` which returns ``None``.
        The NRQL query is included in the tool response body instead.
        """
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
