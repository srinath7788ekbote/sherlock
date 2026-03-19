"""
Input sanitization and prompt injection defense for Sherlock.

Provides:
- NRQL string sanitization to prevent injection.
- Service and monitor name sanitization.
- Alert target parsing into candidate service names.
- Fuzzy resolution of user-provided names against known server-side names.
- Multi-candidate fuzzy resolution for adaptive investigations.
- Prompt injection scrubbing for all tool responses.
"""

import logging
import re
from difflib import SequenceMatcher, get_close_matches
from typing import Any

from core.exceptions import MonitorNotFoundError, ServiceNotFoundError

logger = logging.getLogger("sherlock.sanitize")


def check_env_mismatch(
    input_name: str, resolved_name: str, naming_convention: Any = None,
) -> str | None:
    """Return a warning string if the resolved env differs from the requested env.

    Returns None when there is no mismatch or no naming convention.
    """
    if not naming_convention or not getattr(naming_convention, "separator", None):
        return None
    sep = naming_convention.separator
    if sep not in input_name or sep not in resolved_name:
        return None
    pos = getattr(naming_convention, "env_position", None)
    if pos == "prefix":
        input_env = input_name.split(sep, 1)[0]
        resolved_env = resolved_name.split(sep, 1)[0]
    elif pos == "suffix":
        input_env = input_name.rsplit(sep, 1)[-1]
        resolved_env = resolved_name.rsplit(sep, 1)[-1]
    else:
        return None
    if input_env.lower() != resolved_env.lower():
        return (
            f"ENV MISMATCH: You requested '{input_env}' but no service exists in that "
            f"environment. Resolved to '{resolved_env}' instead. Verify the correct "
            f"environment prefix."
        )
    return None

# Maximum length for any user-provided string value embedded in NRQL.
MAX_INPUT_LENGTH = 200

# Characters/patterns stripped or escaped from NRQL string values.
NRQL_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    ("'", ""),
    ('"', ""),
    (";", ""),
    ("--", ""),
    ("/*", ""),
    ("*/", ""),
    ("\\", ""),
]

# Regex patterns that indicate prompt injection attempts.
INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+previous\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+all\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"new\s+instructions\s*:", re.IGNORECASE),
    re.compile(r"<\s*/?system\s*>", re.IGNORECASE),
    re.compile(r"\[/?INST\]", re.IGNORECASE),
    re.compile(r"forget\s+everything", re.IGNORECASE),
    re.compile(r"act\s+as\b", re.IGNORECASE),
    re.compile(r"roleplay\s+as\b", re.IGNORECASE),
    re.compile(r"reveal\s+your\s+prompt", re.IGNORECASE),
    re.compile(r"print\s+your\s+instructions", re.IGNORECASE),
    re.compile(r"ignore\s+all\s+prior", re.IGNORECASE),
    re.compile(r"override\s+instructions", re.IGNORECASE),
    re.compile(r"system\s*prompt", re.IGNORECASE),
]

REDACTED_MESSAGE = "[REDACTED: possible injection attempt]"


def sanitize_service_name(name: str) -> str:
    """Sanitize a service name for use in NRQL queries.

    Strips dangerous characters and enforces a maximum length.

    Args:
        name: Raw service name from user input.

    Returns:
        Sanitized service name safe for NRQL embedding.
    """
    sanitized = name.strip()
    for dangerous, replacement in NRQL_DANGEROUS_PATTERNS:
        sanitized = sanitized.replace(dangerous, replacement)
    sanitized = sanitized[:MAX_INPUT_LENGTH]
    return sanitized


def sanitize_nrql_string(value: str) -> str:
    """Sanitize a string value for embedding in NRQL queries.

    Removes characters that could cause NRQL injection and enforces
    a maximum length.

    Args:
        value: Raw string value from user input.

    Returns:
        Sanitized string safe for NRQL embedding.
    """
    sanitized = value.strip()
    for dangerous, replacement in NRQL_DANGEROUS_PATTERNS:
        sanitized = sanitized.replace(dangerous, replacement)
    sanitized = sanitized[:MAX_INPUT_LENGTH]
    return sanitized


# ── Environment prefixes and infra suffixes to strip ─────────────────────

_ENV_PREFIXES = re.compile(
    r"^(prod-|staging-|dev-|stg-|eswd-|[a-z0-9]{4}-)", re.IGNORECASE
)

_INFRA_SUFFIXES = re.compile(
    r"(-request-queue|-response-queue|-dead-letter|-dlq"
    r"|-queue|-topic|-pod|-deployment|-container|-svc|-service)$",
    re.IGNORECASE,
)


def strip_namespace_prefix(name: str) -> str:
    """Strip namespace/environment prefixes from a service-like name.

    Removes known env prefixes (prod-, staging-, dev-, stg-, eswd-,
    and any 4-char alphanumeric prefix followed by a dash).

    Args:
        name: The raw name possibly containing a namespace prefix.

    Returns:
        The name with prefix removed, or the original if no prefix matched.
    """
    return _ENV_PREFIXES.sub("", name)


def _normalize_candidate(name: str) -> str:
    """Normalize a candidate name: lowercase, strip prefixes/suffixes,
    replace hyphens/underscores with spaces."""
    result = name.strip().lower()
    # Strip env prefixes.
    result = _ENV_PREFIXES.sub("", result)
    # Strip infra suffixes (iteratively for compound suffixes).
    for _ in range(3):
        prev = result
        result = _INFRA_SUFFIXES.sub("", result)
        if result == prev:
            break
    return result


def parse_alert_target(raw_input: str) -> list[str]:
    """Parse any alert target format into candidate service names.

    Returns list of candidates, most specific first.
    Never raises — returns [raw_input] on any error.

    Handles:
      "eswd-prod/pdf-export-service"
        → ["pdf-export-service", "pdf-export", "export"]

      "prod-export-pdf-request-queue"
        → ["export-pdf", "pdf-export", "export"]

      "Kubernetes pod crash in eswd-prod/pdf-export-service"
        → ["pdf-export-service", "pdf-export"]

      "pdf-export-service (eswd-prod)"
        → ["pdf-export-service", "pdf-export"]

      "export service"
        → ["export service", "export"]

    Args:
        raw_input: Raw alert target string in any format.

    Returns:
        Deduplicated list of candidate service names.
    """
    try:
        if not raw_input or not raw_input.strip():
            return [raw_input] if raw_input else [""]

        candidates: list[str] = []
        text = raw_input.strip()

        # 1. Handle slash-separated namespace/service: "eswd-prod/pdf-export-service"
        if "/" in text:
            # Keep the full original input as the first candidate so that an
            # exact match is preferred (e.g. "eswd-prod/pdf-export-service"
            # should match that service rather than a different environment).
            candidates.append(text)

            parts = text.split("/")
            # Take the segment after the last slash.
            service_part = parts[-1].strip()
            if service_part:
                candidates.append(service_part)
                # Also try stripping suffixes.
                stripped = _INFRA_SUFFIXES.sub("", service_part)
                if stripped and stripped != service_part:
                    candidates.append(stripped)
                # Try further stripping to root.
                stripped2 = _INFRA_SUFFIXES.sub("", stripped)
                if stripped2 and stripped2 != stripped:
                    candidates.append(stripped2)
        # 2. Handle parenthesized namespace: "pdf-export-service (eswd-prod)"
        elif "(" in text and ")" in text:
            service_part = re.sub(r"\s*\([^)]*\)\s*", "", text).strip()
            if service_part:
                candidates.append(service_part)
                stripped = _INFRA_SUFFIXES.sub("", service_part)
                if stripped and stripped != service_part:
                    candidates.append(stripped)
        # 3. Handle natural language with entity-like tokens:
        #    "Kubernetes pod crash in eswd-prod/pdf-export-service"
        elif re.search(r"[A-Za-z]+ (pod|service|container|deployment|crash|alert|error|failure)", text, re.IGNORECASE):
            # Extract hyphenated tokens that look like service names.
            entity_tokens = re.findall(r"[\w][\w-]{2,}(?:/[\w][\w-]{2,})*", text)
            for token in entity_tokens:
                # If it has a slash, take after the last slash.
                if "/" in token:
                    token = token.split("/")[-1]
                # Skip common noise words.
                noise = {
                    "kubernetes", "pod", "crash", "alert", "error", "failure",
                    "warning", "critical", "high", "low", "the", "and", "for",
                    "service", "container", "deployment", "node", "cluster",
                }
                if token.lower() not in noise and len(token) > 2:
                    candidates.append(token)
                    stripped = _INFRA_SUFFIXES.sub("", token)
                    if stripped and stripped != token:
                        candidates.append(stripped)
        # 4. Default: treat as a service name directly.
        else:
            candidates.append(text)
            # Strip env prefix.
            no_prefix = strip_namespace_prefix(text)
            if no_prefix and no_prefix != text:
                candidates.append(no_prefix)
            # Strip infra suffixes.
            stripped = _INFRA_SUFFIXES.sub("", text)
            if stripped and stripped != text:
                candidates.append(stripped)
            # Try with both stripped.
            no_prefix_stripped = _INFRA_SUFFIXES.sub("", no_prefix) if no_prefix else ""
            if no_prefix_stripped and no_prefix_stripped not in candidates:
                candidates.append(no_prefix_stripped)

        # Further shorten candidates by splitting on hyphens and taking stems.
        extra_candidates: list[str] = []
        for c in candidates:
            parts = c.split("-")
            if len(parts) >= 2:
                # Try first two parts joined.
                stem = "-".join(parts[:2])
                if stem not in candidates and len(stem) > 2:
                    extra_candidates.append(stem)
                # Try just the first part if long enough.
                if len(parts[0]) > 3 and parts[0] not in candidates:
                    extra_candidates.append(parts[0])
        candidates.extend(extra_candidates)

        # Deduplicate preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for c in candidates:
            c_clean = c.strip()
            if c_clean and c_clean.lower() not in seen:
                seen.add(c_clean.lower())
                deduped.append(c_clean)

        return deduped if deduped else [raw_input]

    except Exception:
        # Never raise — degrade gracefully.
        return [raw_input]


def fuzzy_resolve_service_candidates(
    input_name: str,
    known_services: list[str],
    threshold: float = 0.45,
    max_candidates: int = 5,
) -> list[tuple[str, float]]:
    """Return up to max_candidates services matching input_name above threshold.

    Normalization before matching:
      - lowercase, hyphens/underscores → spaces
      - strip namespace prefixes

    Returns original (unnormalized) service names with scores,
    sorted by confidence descending.

    Args:
        input_name: The candidate name to match.
        known_services: List of known service names.
        threshold: Minimum similarity ratio to accept.
        max_candidates: Maximum number of matches to return.

    Returns:
        List of (service_name, score) tuples.

    Example:
        input: "export service"
        returns: [
            ("export-worker-prod", 0.67),
            ("pdf-export-service", 0.54),
        ]
    """
    if not known_services or not input_name:
        return []

    def _norm(s: str) -> str:
        n = s.lower().strip()
        n = n.replace("-", " ").replace("_", " ")
        n = _ENV_PREFIXES.sub("", n)
        return n

    normalized_input = _norm(input_name)
    results: list[tuple[str, float]] = []

    for service in known_services:
        normalized_service = _norm(service)

        # Exact match after normalization.
        if normalized_input == normalized_service:
            results.append((service, 1.0))
            continue

        # Substring match in either direction.
        if normalized_input in normalized_service or normalized_service in normalized_input:
            ratio = SequenceMatcher(None, normalized_input, normalized_service).ratio()
            ratio = max(ratio, 0.6)  # Boost substring matches.
            if ratio >= threshold:
                results.append((service, round(ratio, 3)))
                continue

        # Standard fuzzy match.
        ratio = SequenceMatcher(None, normalized_input, normalized_service).ratio()
        if ratio >= threshold:
            results.append((service, round(ratio, 3)))

    # Sort by score descending, then alphabetically.
    results.sort(key=lambda x: (-x[1], x[0]))
    return results[:max_candidates]


def fuzzy_resolve_service(
    input_name: str,
    known_services: list[str],
    threshold: float = 0.6,
    naming_convention: Any = None,
) -> tuple[str, bool, float]:
    """Fuzzy-resolve a user-provided service name against known services.

    **Resolution order:**
      0. Check the AccountContext resolution cache — if a previous tool call
         already confirmed the real entity name for this input via NRQL
         discovery, reuse it immediately.
      1. Exact case-insensitive match.
      2. Environment-preserving resolution (when naming_convention is provided).
      3. Substring match.
      4. Fuzzy SequenceMatcher.

    When a naming convention is known and the input contains the account's
    separator (e.g. "/"), the resolver:
      1. Splits input into env segment + bare service name
      2. If exact env+service exists in known_services, returns it (score 1.0)
      3. Fuzzy-matches the bare name portion
      4. Boosts candidates sharing the same env segment by 30%

    Args:
        input_name: The name the user typed.
        known_services: List of actual service names from the account.
        threshold: Minimum similarity ratio to accept a match.
        naming_convention: Optional NamingConvention from AccountIntelligence.

    Returns:
        Tuple of (resolved_name, was_fuzzy, confidence).

    Raises:
        ServiceNotFoundError: If no match meets the threshold.
    """
    if not known_services:
        raise ServiceNotFoundError(
            input_name=input_name, closest_matches=[], domain="apm"
        )

    # ── Step 0: check resolution cache ──
    try:
        from core.context import AccountContext

        ctx = AccountContext()
        if ctx.is_connected():
            cached = ctx.get_cached_resolution(input_name)
            if cached:
                logger.info(
                    "Cache hit: '%s' → '%s'", input_name, cached,
                )
                return (cached, cached.lower() != input_name.lower().strip(), 1.0)
    except Exception:
        pass  # context not available — continue with normal resolution

    # Exact match (case-insensitive).
    lower_input = input_name.lower().strip()
    for service in known_services:
        if service.lower() == lower_input:
            return (service, False, 1.0)

    # ── Environment-preserving resolution ──
    if (
        naming_convention
        and getattr(naming_convention, "separator", None)
        and getattr(naming_convention, "env_position", None)
    ):
        sep = naming_convention.separator
        if sep in input_name:
            # Extract env segment and bare name from input.
            if naming_convention.env_position == "prefix":
                parts = input_name.split(sep, 1)
                input_env = parts[0]
                input_bare = parts[1] if len(parts) > 1 else input_name
            else:  # suffix
                parts = input_name.rsplit(sep, 1)
                input_bare = parts[0]
                input_env = parts[1] if len(parts) > 1 else ""

            # Score all candidates by bare-name similarity + env boost.
            bare_lower = input_bare.lower().strip()
            candidates: list[tuple[str, float]] = []

            for service in known_services:
                # Extract env and bare from this service.
                svc_env = ""
                svc_bare = service
                if sep in service:
                    if naming_convention.env_position == "prefix":
                        svc_parts = service.split(sep, 1)
                        svc_env = svc_parts[0]
                        svc_bare = svc_parts[1] if len(svc_parts) > 1 else service
                    else:
                        svc_parts = service.rsplit(sep, 1)
                        svc_bare = svc_parts[0]
                        svc_env = svc_parts[1] if len(svc_parts) > 1 else ""

                # Score based on bare name similarity.
                svc_bare_lower = svc_bare.lower()
                if bare_lower == svc_bare_lower:
                    bare_ratio = 1.0
                elif bare_lower in svc_bare_lower or svc_bare_lower in bare_lower:
                    bare_ratio = max(
                        SequenceMatcher(None, bare_lower, svc_bare_lower).ratio(),
                        0.6,
                    )
                else:
                    bare_ratio = SequenceMatcher(
                        None, bare_lower, svc_bare_lower
                    ).ratio()

                if bare_ratio < threshold:
                    continue

                # Env scoring: boost same env, penalize mismatched env.
                # When the user explicitly specifies "eswd-prod/..." and a
                # candidate lives in "eswd-preprod", the mismatch must
                # outweigh even a perfect bare-name match.
                env_adjust = 0.0
                if input_env and svc_env:
                    if input_env.lower() == svc_env.lower():
                        env_adjust = 0.3   # same env → boost
                    else:
                        env_adjust = -0.4  # different env → penalty

                # Store raw (uncapped) score for sorting.
                raw_score = bare_ratio + env_adjust
                if raw_score < threshold:
                    continue
                candidates.append((service, round(raw_score, 3)))

            if candidates:
                # Sort by raw score descending, then alphabetically.
                candidates.sort(key=lambda x: (-x[1], x[0]))
                best_name, best_raw = candidates[0]
                best_score = min(best_raw, 1.0)
                was_fuzzy = best_name.lower() != input_name.lower()

                # Warn when the resolved env differs from the requested env.
                if was_fuzzy and input_env:
                    resolved_env = ""
                    if sep in best_name:
                        if naming_convention.env_position == "prefix":
                            resolved_env = best_name.split(sep, 1)[0]
                        else:
                            resolved_env = best_name.rsplit(sep, 1)[-1]
                    if resolved_env and input_env.lower() != resolved_env.lower():
                        logger.warning(
                            "ENV MISMATCH: requested env '%s' but resolved to '%s'. "
                            "No service found in the '%s' environment. "
                            "Closest match is '%s'.",
                            input_env, resolved_env, input_env, best_name,
                        )

                logger.info(
                    "Env-preserving resolution: '%s' -> '%s' (score=%.3f, env=%s)",
                    input_name, best_name, best_score, input_env,
                )
                return (best_name, was_fuzzy, best_score)

    # Substring match.
    for service in known_services:
        if lower_input in service.lower() or service.lower() in lower_input:
            ratio = SequenceMatcher(None, lower_input, service.lower()).ratio()
            if ratio >= threshold:
                return (service, True, round(ratio, 3))

    # Fuzzy match.
    matches = get_close_matches(lower_input, [s.lower() for s in known_services], n=3, cutoff=threshold)
    if matches:
        best_lower = matches[0]
        for service in known_services:
            if service.lower() == best_lower:
                ratio = SequenceMatcher(None, lower_input, best_lower).ratio()
                return (service, True, round(ratio, 3))

    # No match — find closest for error message.
    closest = get_close_matches(lower_input, [s.lower() for s in known_services], n=3, cutoff=0.3)
    closest_original = []
    for match in closest:
        for service in known_services:
            if service.lower() == match:
                closest_original.append(service)
                break

    raise ServiceNotFoundError(
        input_name=input_name,
        closest_matches=closest_original,
        domain="apm",
    )


def fuzzy_resolve_monitor(
    input_name: str,
    known_monitors: list[str],
    threshold: float = 0.5,
) -> tuple[str, bool, float]:
    """Fuzzy-resolve a user-provided monitor name against known synthetic monitors.

    Uses a lower threshold than services because monitor names tend to be
    more descriptive (e.g. 'Login Flow - Production') and less likely to be exact.

    Args:
        input_name: The monitor name the user typed.
        known_monitors: List of actual monitor names from the account.
        threshold: Minimum similarity ratio to accept a match.

    Returns:
        Tuple of (resolved_name, was_fuzzy, confidence).

    Raises:
        MonitorNotFoundError: If no match meets the threshold.
    """
    if not known_monitors:
        raise MonitorNotFoundError(
            input_name=input_name,
            closest_matches=[],
            known_monitors=[],
        )

    # Exact match (case-insensitive).
    lower_input = input_name.lower().strip()
    for monitor in known_monitors:
        if monitor.lower() == lower_input:
            return (monitor, False, 1.0)

    # Substring match.
    for monitor in known_monitors:
        if lower_input in monitor.lower() or monitor.lower() in lower_input:
            ratio = SequenceMatcher(None, lower_input, monitor.lower()).ratio()
            if ratio >= threshold:
                return (monitor, True, round(ratio, 3))

    # Token overlap — useful for "login flow prod" matching "Login Flow - Production".
    input_tokens = set(re.split(r"[\s\-_]+", lower_input))
    best_token_match: str | None = None
    best_token_ratio: float = 0.0
    for monitor in known_monitors:
        monitor_tokens = set(re.split(r"[\s\-_]+", monitor.lower()))
        overlap = len(input_tokens & monitor_tokens)
        total = max(len(input_tokens), len(monitor_tokens))
        if total > 0:
            token_ratio = overlap / total
            if token_ratio > best_token_ratio:
                best_token_ratio = token_ratio
                best_token_match = monitor

    if best_token_match and best_token_ratio >= threshold:
        return (best_token_match, True, round(best_token_ratio, 3))

    # Standard fuzzy match.
    matches = get_close_matches(
        lower_input, [m.lower() for m in known_monitors], n=3, cutoff=threshold
    )
    if matches:
        best_lower = matches[0]
        for monitor in known_monitors:
            if monitor.lower() == best_lower:
                ratio = SequenceMatcher(None, lower_input, best_lower).ratio()
                return (monitor, True, round(ratio, 3))

    # No match — find closest for error message.
    closest = get_close_matches(
        lower_input, [m.lower() for m in known_monitors], n=3, cutoff=0.2
    )
    closest_original = []
    for match in closest:
        for monitor in known_monitors:
            if monitor.lower() == match:
                closest_original.append(monitor)
                break

    raise MonitorNotFoundError(
        input_name=input_name,
        closest_matches=closest_original,
        known_monitors=known_monitors,
    )


def scrub_tool_response(data: Any, account_id: str = "", tool: str = "") -> Any:
    """Recursively scrub tool response data for prompt injection patterns.

    Walks dicts, lists, and strings. Any string matching a known injection
    pattern is replaced with a redacted message and a WARNING is logged.

    Args:
        data: The tool response data to scrub (dict, list, str, or primitive).
        account_id: Active account ID for logging context.
        tool: Tool name for logging context.

    Returns:
        Scrubbed copy of the data.
    """
    if isinstance(data, str):
        for pattern in INJECTION_PATTERNS:
            if pattern.search(data):
                logger.warning(
                    "Prompt injection attempt detected",
                    extra={
                        "account_id": account_id,
                        "tool": tool,
                        "pattern": pattern.pattern,
                        "snippet": data[:100],
                    },
                )
                return REDACTED_MESSAGE
        return data
    elif isinstance(data, dict):
        return {k: scrub_tool_response(v, account_id, tool) for k, v in data.items()}
    elif isinstance(data, list):
        return [scrub_tool_response(item, account_id, tool) for item in data]
    else:
        return data
