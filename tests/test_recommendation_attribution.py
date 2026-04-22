"""
End-to-end tests for recommendation deep-link GUID attribution.

Regression tests for the April 21 bug: a recommendation naming service X
must attach X's GUID (or no GUID), never Y's GUID.

Uses generic placeholder names (no tenant-specific values).
"""

import pytest

from core.deeplinks import resolve_apm_guid
from core.intelligence import AccountIntelligence, APMIntelligence


def _make_intel(*, candidates, reporting_guids=None, service_guids=None):
    """Build a minimal intelligence object for recommendation attribution tests."""
    apm = APMIntelligence(
        service_names=list(candidates.keys()),
        service_guids=service_guids or {},
        service_guid_candidates=candidates,
        reporting_guids=reporting_guids or set(),
    )
    return AccountIntelligence(account_id="999999", apm=apm)


class TestRecommendationAttribution:
    """Verify that recommendation links always reference the named service."""

    def test_recommendation_link_uses_named_service_guid(self):
        """Regression test for April 21 bug: recommendation naming service-a
        must resolve to service-a's GUID, not service-b's.

        Simulates the scenario where both services exist in the account and
        the recommendation text references service-a specifically.
        """
        intel = _make_intel(
            candidates={
                "service-a": [
                    {"guid": "guid-a", "reporting": True, "tags": {}, "alert_severity": "WARNING"},
                ],
                "service-b": [
                    {"guid": "guid-b", "reporting": True, "tags": {}, "alert_severity": "CRITICAL"},
                ],
            },
            reporting_guids={"guid-a", "guid-b"},
            service_guids={"service-a": "guid-a", "service-b": "guid-b"},
        )

        # The recommendation names service-a — resolve MUST return service-a's GUID.
        recommendation_target = "service-a"
        resolved = resolve_apm_guid(recommendation_target, intel)
        assert resolved == "guid-a", (
            f"Recommendation for '{recommendation_target}' resolved to {resolved}, "
            f"expected 'guid-a'. This is the April 21 bug: wrong service GUID in link."
        )

        # Verify that looking up service-b returns service-b's GUID.
        assert resolve_apm_guid("service-b", intel) == "guid-b"

    def test_recommendation_link_omitted_when_named_service_has_no_guid(self):
        """Service not in APM intelligence → no link, textual recommendation only."""
        intel = _make_intel(
            candidates={
                "service-a": [
                    {"guid": "guid-a", "reporting": True, "tags": {}, "alert_severity": ""},
                ],
            },
            reporting_guids={"guid-a"},
            service_guids={"service-a": "guid-a"},
        )

        # "unknown-service" has no candidates → resolve returns None.
        assert resolve_apm_guid("unknown-service", intel) is None

    def test_recommendation_link_omitted_when_named_service_guid_ambiguous(self):
        """Duplicate names with both reporting → no entity-view link (generic fallback)."""
        intel = _make_intel(
            candidates={
                "shared-svc": [
                    {"guid": "guid-cluster-a", "reporting": True, "tags": {}, "alert_severity": "WARNING"},
                    {"guid": "guid-cluster-b", "reporting": True, "tags": {}, "alert_severity": "WARNING"},
                ],
            },
            reporting_guids={"guid-cluster-a", "guid-cluster-b"},
            service_guids={"shared-svc": "guid-cluster-a"},
        )

        # Ambiguous — resolve_apm_guid returns None (require_reporting=True default).
        resolved = resolve_apm_guid("shared-svc", intel)
        assert resolved is None, (
            f"Ambiguous GUID should return None, got {resolved}. "
            f"When ambiguous, recommendations should use generic fallback links."
        )

    def test_recommendation_never_cross_attributes_guids(self):
        """Even when service-b has CRITICAL severity, looking up service-a
        must still return service-a's GUID, not service-b's."""
        intel = _make_intel(
            candidates={
                "service-a": [
                    {"guid": "guid-a-only", "reporting": True, "tags": {}, "alert_severity": ""},
                ],
                "service-b": [
                    {"guid": "guid-b-critical", "reporting": True, "tags": {}, "alert_severity": "CRITICAL"},
                ],
            },
            reporting_guids={"guid-a-only", "guid-b-critical"},
            service_guids={"service-a": "guid-a-only", "service-b": "guid-b-critical"},
        )

        assert resolve_apm_guid("service-a", intel) == "guid-a-only"
        assert resolve_apm_guid("service-b", intel) == "guid-b-critical"

    def test_recommendation_with_single_reporting_among_duplicates(self):
        """When one of two duplicate-name entities is reporting, resolve picks it."""
        intel = _make_intel(
            candidates={
                "shared-svc": [
                    {"guid": "guid-dark", "reporting": False, "tags": {}, "alert_severity": ""},
                    {"guid": "guid-live", "reporting": True, "tags": {}, "alert_severity": "WARNING"},
                ],
            },
            reporting_guids={"guid-live"},
            service_guids={"shared-svc": "guid-live"},
        )

        resolved = resolve_apm_guid("shared-svc", intel)
        assert resolved == "guid-live"
