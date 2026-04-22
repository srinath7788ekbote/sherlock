"""
Report-template validator for Sherlock team-lead synthesis output.

Pure function that lints a markdown investigation report against the
conventions documented in .github/agents/sherlock-team-lead.agent.md
"Report Template — ENFORCEMENT RULES" section.

Opt-in: the team-lead agent calls validate_report_markdown(draft) to
self-check its output before sending. Returns a list of warning strings;
empty list means the report is compliant.

Not wired into any MCP tool. Not invoked automatically. This is a
diagnostic aid for agents, not a runtime gate.
"""
from __future__ import annotations

import re

# The only acceptable Status column values in the Domain Status table.
_STATUS_EMOJI = {"🔴", "🟡", "🟢", "⚪"}

# Words that indicate the agent wrote text instead of emoji (or in addition to).
_STATUS_WORDS = {"CRITICAL", "WARNING", "HEALTHY", "NO_DATA", "OK", "FAIL"}

# Anchor text phrases that must NOT label a query-builder URL.
_ENTITY_VIEW_ANCHORS = {
    "View errors inbox",
    "View service overview",
    "View workload",
    "View K8s workload",
    "View K8s explorer",
    "View transactions",
    "View Logs UI",
    "View in NR",
}


def validate_report_markdown(md: str) -> list[str]:
    """Validate a team-lead investigation report against template rules.

    Returns a list of human-readable warning strings. Empty list = compliant.

    Rules checked (see sherlock-team-lead.agent.md §Report Template — ENFORCEMENT RULES):
      1. Domain Status rows have emoji in the Status column (not text word)
      2. Status emoji is not paired with the redundant text word
      3. Non-NO_DATA rows have a deep link in the Finding column
      4. Anchor text matching an entity-view phrase points at /nr1-core
         (not /launcher/data-exploration.query-builder)

    False positives are possible — this is a lint, not a gate. Agents may
    override any warning if the context justifies it.
    """
    warnings: list[str] = []
    if not md or "## Domain Status" not in md:
        # No domain-status table present — nothing to validate.
        return warnings

    # Extract the Domain Status table (between the heading and the next ## heading).
    lines = md.splitlines()
    in_table = False
    table_rows: list[str] = []
    for line in lines:
        if line.strip().startswith("## Domain Status"):
            in_table = True
            continue
        if in_table and line.strip().startswith("## "):
            break
        if in_table and line.strip().startswith("|"):
            table_rows.append(line)

    # Skip header and separator rows.
    data_rows = [r for r in table_rows[2:] if r.strip()]

    for row in data_rows:
        # Split on "|", strip empty leading/trailing cells.
        cells = [c.strip() for c in row.split("|")[1:-1]]
        if len(cells) < 3:
            continue  # malformed row; let another checker handle it
        domain, status, finding = cells[0], cells[1], cells[2]

        # Rule 1: status has an emoji.
        has_emoji = any(e in status for e in _STATUS_EMOJI)
        has_word = any(w in status.upper() for w in _STATUS_WORDS)

        if not has_emoji:
            warnings.append(
                f"Domain '{domain}' row: Status column is '{status}' — "
                f"expected one of 🔴/🟡/🟢/⚪ per template rule 1."
            )
        elif has_word:
            # Rule 2: emoji without redundant text word.
            warnings.append(
                f"Domain '{domain}' row: Status has both emoji and text word "
                f"('{status}') — template requires emoji only (rule 2)."
            )

        # Rule 3: non-NO_DATA rows have a link.
        is_no_data = "⚪" in status
        has_link = "](" in finding and "http" in finding
        if not is_no_data and not has_link:
            warnings.append(
                f"Domain '{domain}' row: Finding has no deep link "
                f"('{finding[:60]}...') — template rule 3 requires a link "
                f"unless status is ⚪."
            )

    # Rule 4: anchor-text vs destination mismatch.
    # Find every "[anchor](url)" and flag entity-view anchors pointing
    # at the NRQL query builder.
    link_pattern = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
    for m in link_pattern.finditer(md):
        anchor = m.group(1).strip()
        url = m.group(2)
        if anchor in _ENTITY_VIEW_ANCHORS and "data-exploration.query-builder" in url:
            warnings.append(
                f"Link anchor '{anchor}' points at the NRQL query builder "
                f"(data-exploration.query-builder) — template rule 4: "
                f"entity-view anchors must point at /nr1-core entity paths."
            )

    return warnings
