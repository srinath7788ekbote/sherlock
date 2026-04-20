"""
Log search tool for Sherlock.

Provides log searching against the active account using NRQL queries
on the Log event type. Resolves service names and log attributes
from account intelligence.
"""

import json
import logging
import time

from client.newrelic import get_client
from core.context import AccountContext
from core.deeplinks import get_builder as _get_deeplink_builder
from core.sanitize import fuzzy_resolve_service, sanitize_nrql_string, sanitize_service_name

logger = logging.getLogger("sherlock.tools.logs")

# Fallback service attributes to try when the primary returns no results.
_SERVICE_ATTR_FALLBACKS = [
    "entity.name", "service.name", "serviceName", "appName", "app.name",
]

# NerdGraph NRQL query template.
GQL_NRQL_QUERY = """
{
  actor {
    account(id: %s) {
      nrql(query: "%s") {
        results
      }
    }
  }
}
"""

# NRQL log search template — dynamically assembled.
NRQL_LOG_BASE = "SELECT timestamp, message, %s, %s FROM Log WHERE 1=1"
NRQL_LOG_SERVICE_FILTER = " AND %s = '%s'"
NRQL_LOG_SEVERITY_FILTER = " AND %s IN (%s)"
NRQL_LOG_KEYWORD_FILTER = " AND message LIKE '%%%s%%'"
NRQL_LOG_SINCE = " SINCE %d minutes ago"
NRQL_LOG_ORDER = " ORDER BY timestamp DESC"
NRQL_LOG_LIMIT = " LIMIT %d"


async def search_logs(
    service_name: str | None = None,
    severity: str | None = None,
    keyword: str | None = None,
    since_minutes: int = 60,
    limit: int = 100,
) -> str:
    """Search logs for the active account.

    Dynamically builds NRQL using the account's discovered log attributes.
    Fuzzy-resolves service names against known APM services.

    Args:
        service_name: Optional service name to filter by.
        severity: Optional severity filter (e.g. 'ERROR', 'WARN', 'ERROR,WARN').
        keyword: Optional keyword to search in log messages.
        since_minutes: Time window in minutes.
        limit: Maximum log entries to return.

    Returns:
        JSON string with log search results.
    """
    start = time.time()
    try:
        ctx = AccountContext()
        credentials, intelligence = ctx.get_active()
        client = get_client()

        if not intelligence.logs.enabled:
            return json.dumps({
                "error": "Logging is not enabled for this account.",
                "tool": "search_logs",
                "hint": "Enable New Relic logging or check your account setup.",
                "data_available": False,
            })

        svc_attr = intelligence.logs.service_attribute or "service.name"
        sev_attr = intelligence.logs.severity_attribute or "level"

        # Build NRQL.
        nrql = NRQL_LOG_BASE % (svc_attr, sev_attr)

        resolved_name = None
        was_fuzzy = False
        if service_name:
            safe_name = sanitize_service_name(service_name)
            try:
                resolved_name, was_fuzzy, confidence = fuzzy_resolve_service(
                    safe_name, intelligence.apm.service_names, threshold=0.5,
                    naming_convention=intelligence.naming_convention,
                )
            except Exception:
                resolved_name = safe_name
            # Use LIKE matching for resilience — the log service.name may
            # not exactly equal the APM appName (e.g. different namespace
            # segments).  LIKE '%name%' covers both exact and partial matches.
            nrql += " AND `%s` LIKE '%%%s%%'" % (svc_attr, resolved_name)

        if severity:
            safe_severity = sanitize_nrql_string(severity)
            levels = [f"'{s.strip()}'" for s in safe_severity.split(",")]
            nrql += NRQL_LOG_SEVERITY_FILTER % (sev_attr, ", ".join(levels))

        if keyword:
            safe_keyword = sanitize_nrql_string(keyword)
            nrql += NRQL_LOG_KEYWORD_FILTER % safe_keyword

        nrql += NRQL_LOG_SINCE % since_minutes
        nrql += NRQL_LOG_ORDER
        nrql += NRQL_LOG_LIMIT % min(limit, 500)

        escaped_nrql = nrql.replace('"', '\\"')
        query = GQL_NRQL_QUERY % (credentials.account_id, escaped_nrql)
        result = await client.query(query)

        logs = (
            result.get("data", {})
            .get("actor", {})
            .get("account", {})
            .get("nrql", {})
            .get("results", [])
        )

        # If the primary service attribute returned no results, try fallbacks.
        used_fallback_attr = None
        if not logs and resolved_name:
            for alt_attr in _SERVICE_ATTR_FALLBACKS:
                if alt_attr == svc_attr:
                    continue
                logger.debug(
                    "Primary attr '%s' returned no logs; trying fallback attr '%s'",
                    svc_attr, alt_attr,
                )
                alt_nrql = NRQL_LOG_BASE % (alt_attr, sev_attr)
                alt_nrql += " AND `%s` LIKE '%%%s%%'" % (alt_attr, resolved_name)
                if severity:
                    safe_severity = sanitize_nrql_string(severity)
                    levels = [f"'{s.strip()}'" for s in safe_severity.split(",")]
                    alt_nrql += NRQL_LOG_SEVERITY_FILTER % (sev_attr, ", ".join(levels))
                if keyword:
                    safe_keyword = sanitize_nrql_string(keyword)
                    alt_nrql += NRQL_LOG_KEYWORD_FILTER % safe_keyword
                alt_nrql += NRQL_LOG_SINCE % since_minutes
                alt_nrql += NRQL_LOG_ORDER
                alt_nrql += NRQL_LOG_LIMIT % min(limit, 500)

                escaped_alt = alt_nrql.replace('"', '\\"')
                alt_query = GQL_NRQL_QUERY % (credentials.account_id, escaped_alt)
                try:
                    alt_result = await client.query(alt_query)
                    alt_logs = (
                        alt_result.get("data", {})
                        .get("actor", {})
                        .get("account", {})
                        .get("nrql", {})
                        .get("results", [])
                    )
                    if alt_logs:
                        logs = alt_logs
                        nrql = alt_nrql
                        svc_attr = alt_attr
                        used_fallback_attr = alt_attr
                        logger.info(
                            "Fallback attr '%s' found %d logs for '%s'",
                            alt_attr, len(alt_logs), resolved_name,
                        )
                        break
                except Exception as fb_exc:
                    logger.warning(
                        "Fallback attr '%s' failed for '%s': %s",
                        alt_attr, resolved_name, fb_exc,
                    )
                    continue

        # If all attribute fallbacks failed and the name contains a namespace
        # separator (e.g. "eswd-prod/client-service"), retry with just the
        # bare service name ("client-service").  Log attributes may store
        # only the bare name while entity.name uses the full namespaced form.
        if not logs and resolved_name and "/" in resolved_name:
            bare_name = resolved_name.rsplit("/", 1)[1]
            logger.debug(
                "Full name '%s' returned no logs; retrying with bare name '%s'",
                resolved_name, bare_name,
            )
            all_attrs = [svc_attr] + [
                a for a in _SERVICE_ATTR_FALLBACKS if a != svc_attr
            ]
            for alt_attr in all_attrs:
                alt_nrql = NRQL_LOG_BASE % (alt_attr, sev_attr)
                alt_nrql += " AND `%s` LIKE '%%%s%%'" % (alt_attr, bare_name)
                if severity:
                    safe_severity = sanitize_nrql_string(severity)
                    levels = [f"'{s.strip()}'" for s in safe_severity.split(",")]
                    alt_nrql += NRQL_LOG_SEVERITY_FILTER % (sev_attr, ", ".join(levels))
                if keyword:
                    safe_keyword = sanitize_nrql_string(keyword)
                    alt_nrql += NRQL_LOG_KEYWORD_FILTER % safe_keyword
                alt_nrql += NRQL_LOG_SINCE % since_minutes
                alt_nrql += NRQL_LOG_ORDER
                alt_nrql += NRQL_LOG_LIMIT % min(limit, 500)

                escaped_alt = alt_nrql.replace('"', '\\"')
                alt_query = GQL_NRQL_QUERY % (credentials.account_id, escaped_alt)
                try:
                    alt_result = await client.query(alt_query)
                    alt_logs = (
                        alt_result.get("data", {})
                        .get("actor", {})
                        .get("account", {})
                        .get("nrql", {})
                        .get("results", [])
                    )
                    if alt_logs:
                        logs = alt_logs
                        nrql = alt_nrql
                        svc_attr = alt_attr
                        used_fallback_attr = alt_attr
                        logger.info(
                            "Bare name fallback '%s' on attr '%s' found %d logs",
                            bare_name, alt_attr, len(alt_logs),
                        )
                        break
                except Exception as fb_exc:
                    logger.warning(
                        "Bare name fallback attr '%s' failed for '%s': %s",
                        alt_attr, bare_name, fb_exc,
                    )
                    continue

        # ── Step 0c: Platform log discovery ──
        # When Steps 0b exhausted and the target looks like a platform
        # component (Istio, ingress, kube-system, etc.), query logs by
        # discovered platform namespaces instead of service attributes.
        platform_log_source = False
        _PLATFORM_KEYWORDS = frozenset({
            "istio", "ingress", "gateway", "kube-", "nginx",
            "envoy", "linkerd", "traefik", "kong",
        })
        if (
            not logs
            and intelligence.logs.platform_namespaces
            and intelligence.logs.namespace_attribute
        ):
            # Trigger decision: activate if the target is a platform hint
            # or has no APM entity match.  Check the ORIGINAL service_name
            # for keyword matches — fuzzy resolution may have mapped a
            # platform component name (e.g. "envoy-proxy") to an unrelated
            # APM service.
            _trigger = False
            _original_lower = (service_name or "").lower()
            if resolved_name is None:
                _trigger = True
            elif any(kw in _original_lower for kw in _PLATFORM_KEYWORDS):
                _trigger = True
            elif any(kw in (resolved_name or "").lower() for kw in _PLATFORM_KEYWORDS):
                _trigger = True
            elif (
                resolved_name not in intelligence.apm.service_names
                and resolved_name not in intelligence.apm.service_guids
            ):
                _trigger = True

            if _trigger:
                try:
                    ns_attr = intelligence.logs.namespace_attribute
                    cl_attr = intelligence.logs.cluster_attribute
                    platform_ns = intelligence.logs.platform_namespaces

                    namespaces_csv = ", ".join(f"'{ns}'" for ns in platform_ns)

                    # Build the Step 0c NRQL query.
                    step_0c_nrql = (
                        f"SELECT timestamp, message, `{ns_attr}`, `{cl_attr}`, `{sev_attr}`,"
                        f" status, path, method, vhost, response_flags,"
                        f" response_code_details, upstream_cluster, upstream_host"
                        f" FROM Log"
                        f" WHERE `{ns_attr}` IN ({namespaces_csv})"
                    )

                    # Optional cluster filter for precision.
                    if intelligence.k8s.cluster_names:
                        first_cluster = intelligence.k8s.cluster_names[0]
                        step_0c_nrql += f" AND `{cl_attr}` LIKE '%{first_cluster}%'"

                    if severity:
                        safe_severity = sanitize_nrql_string(severity)
                        levels = [f"'{s.strip()}'" for s in safe_severity.split(",")]
                        step_0c_nrql += NRQL_LOG_SEVERITY_FILTER % (sev_attr, ", ".join(levels))

                    if keyword:
                        safe_keyword = sanitize_nrql_string(keyword)
                        step_0c_nrql += NRQL_LOG_KEYWORD_FILTER % safe_keyword

                    step_0c_nrql += NRQL_LOG_SINCE % since_minutes
                    step_0c_nrql += NRQL_LOG_ORDER
                    step_0c_nrql += NRQL_LOG_LIMIT % min(limit, 500)

                    escaped_0c = step_0c_nrql.replace('"', '\\"')
                    query_0c = GQL_NRQL_QUERY % (credentials.account_id, escaped_0c)
                    result_0c = await client.query(query_0c)
                    platform_logs = (
                        result_0c.get("data", {})
                        .get("actor", {})
                        .get("account", {})
                        .get("nrql", {})
                        .get("results", [])
                    )

                    if platform_logs:
                        logs = platform_logs
                        nrql = step_0c_nrql
                        used_fallback_attr = ns_attr
                        platform_log_source = True
                        logger.info(
                            "Step 0c platform log fallback found %d logs "
                            "via namespaces %s using '%s'",
                            len(platform_logs),
                            ", ".join(platform_ns),
                            ns_attr,
                        )
                except Exception as step_0c_exc:
                    logger.warning(
                        "Step 0c platform log fallback failed: %s", step_0c_exc,
                    )

        duration_ms = int((time.time() - start) * 1000)
        response: dict = {
            "account_id": credentials.account_id,
            "service_name": resolved_name,
            "severity_filter": severity,
            "keyword": keyword,
            "since_minutes": since_minutes,
            "total_logs": len(logs),
            "logs": logs,
            "nrql_used": nrql,
            "duration_ms": duration_ms,
        }
        if was_fuzzy and resolved_name:
            response["resolved_from"] = service_name
            response["note"] = f"Fuzzy matched '{service_name}' → '{resolved_name}'"
        if used_fallback_attr:
            response["note"] = response.get("note", "") + (
                f" Logs found via '{used_fallback_attr}' attribute"
                f" (primary '{intelligence.logs.service_attribute}' had no results)."
            )
        if platform_log_source:
            response["platform_log_source"] = True
            platform_ns = intelligence.logs.platform_namespaces
            ns_attr = intelligence.logs.namespace_attribute
            response["note"] = (
                response.get("note", "")
                + f" Platform log fallback: queried namespaces"
                f" {', '.join(platform_ns)} via '{ns_attr}'."
            )

        # Deep links — only when logs were found.
        if len(logs) > 0:
            try:
                _builder = _get_deeplink_builder()
                if _builder:
                    # Use log_search_nrql to route through the canonical
                    # /logger page (verified working 2026-04). This opens
                    # the NR Logs UI with a Lucene filter applied on the
                    # service attribute — the page NR users expect when
                    # investigating logs. The full NRQL is still included
                    # in ``nrql_used`` for engineers who want to run the
                    # query in the Query Builder directly.
                    service_attr = (
                        used_fallback_attr or intelligence.logs.service_attribute
                    )
                    response["links"] = {
                        "view_in_nr": _builder.log_search_nrql(
                            service_name=resolved_name,
                            service_attribute=service_attr,
                            severity=severity,
                            keyword=keyword,
                            since_minutes=since_minutes,
                        ),
                    }
                    # If there are errors in the results, generate an error-only link
                    has_errors = any(
                        str(log.get(sev_attr, "")).upper() in ("ERROR", "FATAL", "CRITICAL")
                        for log in logs
                    )
                    if has_errors:
                        response["links"]["error_logs"] = _builder.log_search_nrql(
                            service_name=resolved_name,
                            service_attribute=service_attr,
                            severity="ERROR,FATAL,CRITICAL",
                            keyword=keyword,
                            since_minutes=since_minutes,
                        )
            except Exception:
                pass

        return json.dumps(response)

    except Exception as exc:
        return json.dumps({
            "error": str(exc),
            "tool": "search_logs",
            "hint": "Check parameters. Use get_nrql_context('logs') for attribute names.",
            "data_available": False,
        })
