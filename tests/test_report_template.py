"""Tests for core/report_template.py validator."""
import pytest

from core.report_template import validate_report_markdown


COMPLIANT_REPORT = """# 🔍 service-a — WARNING

**Window:** 60 min | **Account:** acct-123 | **Confidence:** HIGH

> Root cause summary.

## Domain Status
| Domain | Status | Finding |
|--------|--------|---------|
| APM | 🟡 | 391 errors — [View errors inbox](https://one.newrelic.com/nr1-core/errors-inbox/entity-inbox/GUID123?account=123) |
| K8s | 🟢 | 2/2 pods — [View workload](https://one.newrelic.com/nr1-core?account=123&filters=...) |
| Logs | ⚪ | Not configured |
| Alerts | 🔴 | 3 open — [View alerts](https://one.newrelic.com/alerts?account=123) |
| Synthetics | ⚪ | No monitors |
| Infra | 🟢 | Dependencies healthy — [View service map](https://one.newrelic.com/nr1-core?viz=service-map) |

## Findings
All domains covered above.
"""

TEXT_STATUS_REPORT = """
## Domain Status
| Domain | Status | Finding |
|--------|--------|---------|
| APM | WARNING | errors — [View errors inbox](https://one.newrelic.com/nr1-core/errors-inbox/abc) |
| K8s | HEALTHY | 2/2 pods — [View workload](https://one.newrelic.com/nr1-core?x=1) |
"""

EMOJI_PLUS_WORD_REPORT = """
## Domain Status
| Domain | Status | Finding |
|--------|--------|---------|
| APM | 🟡 WARNING | errors — [View errors inbox](https://one.newrelic.com/nr1-core/errors-inbox/abc) |
"""

MISSING_LINK_REPORT = """
## Domain Status
| Domain | Status | Finding |
|--------|--------|---------|
| APM | 🟡 | 391 errors observed across endpoints |
| K8s | 🟢 | 2/2 pods — [View workload](https://one.newrelic.com/nr1-core?x=1) |
"""

MISMATCHED_ANCHOR_REPORT = """
## Domain Status
| Domain | Status | Finding |
|--------|--------|---------|
| APM | 🟡 | errors — [View errors inbox](https://one.newrelic.com/launcher/data-exploration.query-builder?pane=abc) |
"""


class TestValidateReportMarkdown:
    def test_compliant_report_returns_empty(self):
        assert validate_report_markdown(COMPLIANT_REPORT) == []

    def test_empty_input_returns_empty(self):
        assert validate_report_markdown("") == []

    def test_report_without_domain_status_returns_empty(self):
        md = "# Just a heading\n\nSome prose."
        assert validate_report_markdown(md) == []

    def test_text_status_flagged(self):
        warnings = validate_report_markdown(TEXT_STATUS_REPORT)
        assert len(warnings) == 2
        assert any("APM" in w and "expected one of" in w for w in warnings)
        assert any("K8s" in w and "expected one of" in w for w in warnings)

    def test_emoji_plus_word_flagged(self):
        warnings = validate_report_markdown(EMOJI_PLUS_WORD_REPORT)
        assert len(warnings) == 1
        assert "both emoji and text word" in warnings[0]

    def test_missing_link_flagged(self):
        warnings = validate_report_markdown(MISSING_LINK_REPORT)
        assert len(warnings) == 1
        assert "APM" in warnings[0] and "no deep link" in warnings[0]

    def test_no_data_row_not_required_to_have_link(self):
        md = """
## Domain Status
| Domain | Status | Finding |
|--------|--------|---------|
| Logs | ⚪ | Not configured |
"""
        assert validate_report_markdown(md) == []

    def test_mismatched_anchor_flagged(self):
        warnings = validate_report_markdown(MISMATCHED_ANCHOR_REPORT)
        assert any("data-exploration" in w for w in warnings)

    def test_anchor_view_nrql_pointing_at_query_builder_is_fine(self):
        md = """
## Domain Status
| Domain | Status | Finding |
|--------|--------|---------|
| APM | 🟡 | errors — [View NRQL](https://one.newrelic.com/launcher/data-exploration.query-builder?pane=abc) |
"""
        # View NRQL anchor → query builder is the expected pairing.
        assert validate_report_markdown(md) == []

    def test_multiple_violations_all_reported(self):
        md = """
## Domain Status
| Domain | Status | Finding |
|--------|--------|---------|
| APM | WARNING | 391 errors |
| K8s | 🟢 HEALTHY | 2/2 pods — [View workload](https://one.newrelic.com/nr1-core?x=1) |
| Logs | 🟡 | errors — [View errors inbox](https://one.newrelic.com/launcher/data-exploration.query-builder?pane=abc) |
"""
        warnings = validate_report_markdown(md)
        # APM: text status + missing link = 2 warnings
        # K8s: emoji+word = 1 warning
        # Logs: anchor mismatch = 1 warning
        assert len(warnings) >= 3
