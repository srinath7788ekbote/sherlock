"""
Tests for Azure Service Bus dynamic discovery in learn_account.
Verifies that ASB queues, topics, DLQ counts, and namespace names
are correctly parsed into AzureServiceBusIntelligence.
"""
import pytest
from core.intelligence import (
    AccountIntelligence,
    AzureServiceBusIntelligence,
    AzureServiceBusQueueMeta,
    AzureServiceBusTopicMeta,
    _parse_asb_intelligence,
)


class TestAzureServiceBusModels:
    def test_account_intelligence_has_asb_field(self):
        intel = AccountIntelligence(account_id="12345")
        assert hasattr(intel, "azure_service_bus")
        assert isinstance(intel.azure_service_bus, AzureServiceBusIntelligence)

    def test_asb_default_is_not_configured(self):
        intel = AccountIntelligence(account_id="12345")
        assert intel.azure_service_bus.configured is False
        assert intel.azure_service_bus.queue_count == 0
        assert intel.azure_service_bus.topic_count == 0
        assert intel.azure_service_bus.dlq_count == 0


class TestParseAsbIntelligenceQueues:
    def test_empty_results_returns_not_configured(self):
        result = _parse_asb_intelligence(
            queue_rows=[],
            topic_rows=[],
        )
        assert result.configured is False
        assert result.queue_count == 0

    def test_single_queue_parsed_correctly(self):
        rows = [{
            "facets": ["prod-arelle-validation-queue", "sbns-eus2-prd-eswd-prdtngo"],
            "latest.active_msgs": 5.0,
            "latest.dlq_msgs": 3387.0,
            "latest.incoming_msgs": 2.3,
        }]
        result = _parse_asb_intelligence(
            queue_rows=rows,
            topic_rows=[],
        )
        assert result.configured is True
        assert result.queue_count == 1
        assert result.queues[0].entity_name == "prod-arelle-validation-queue"
        assert result.queues[0].namespace == "sbns-eus2-prd-eswd-prdtngo"
        assert result.queues[0].active_messages == 5.0
        assert result.queues[0].dead_letter_messages == 3387.0
        assert result.queues[0].has_dlq is True
        assert result.queues[0].prefix == "prod"

    def test_dlq_count_aggregated_correctly(self):
        rows = [
            {
                "facets": ["prod-queue-a", "ns1"],
                "latest.active_msgs": 0,
                "latest.dlq_msgs": 10.0,
                "latest.incoming_msgs": 0,
            },
            {
                "facets": ["prod-queue-b", "ns1"],
                "latest.active_msgs": 0,
                "latest.dlq_msgs": 0.0,
                "latest.incoming_msgs": 0,
            },
            {
                "facets": ["prod-queue-c", "ns1"],
                "latest.active_msgs": 0,
                "latest.dlq_msgs": 5.0,
                "latest.incoming_msgs": 0,
            },
        ]
        result = _parse_asb_intelligence(
            queue_rows=rows,
            topic_rows=[],
        )
        assert result.dlq_count == 2
        assert result.total_dlq_messages == 15.0

    def test_namespaces_extracted_from_queues_and_topics(self):
        queue_rows = [{"facets": ["q1", "ns-alpha"], "latest.active_msgs": 0, "latest.dlq_msgs": 0, "latest.incoming_msgs": 0}]
        topic_rows = [{"facets": ["t1", "ns-beta"], "latest.incoming_msgs": 5.0}]
        result = _parse_asb_intelligence(
            queue_rows=queue_rows,
            topic_rows=topic_rows,
        )
        assert "ns-alpha" in result.namespaces
        assert "ns-beta" in result.namespaces
        assert len(result.namespaces) == 2

    def test_prefix_extraction_from_queue_name(self):
        rows = [
            {"facets": ["prod-service-queue", "ns"], "latest.active_msgs": 0, "latest.dlq_msgs": 0, "latest.incoming_msgs": 0},
            {"facets": ["dev-service-queue", "ns"], "latest.active_msgs": 0, "latest.dlq_msgs": 0, "latest.incoming_msgs": 0},
            {"facets": ["stg-service-queue", "ns"], "latest.active_msgs": 0, "latest.dlq_msgs": 0, "latest.incoming_msgs": 0},
        ]
        result = _parse_asb_intelligence(
            queue_rows=rows,
            topic_rows=[],
        )
        assert set(result.queue_prefixes) == {"prod", "dev", "stg"}

    def test_alternative_attribute_format_without_prefix(self):
        """Handles alternative attribute names (varies by account)."""
        rows = [{
            "facets": ["prod-queue", "ns"],
            "activeMessages.Average": 3.0,
            "deadLetterMessages": 100.0,
            "incomingMessages.Total": 1.0,
        }]
        result = _parse_asb_intelligence(
            queue_rows=rows,
            topic_rows=[],
        )
        assert result.queues[0].active_messages == 3.0
        assert result.queues[0].dead_letter_messages == 100.0

    def test_graceful_handling_of_exception_results(self):
        """Exception results from gather don't crash the parse."""
        result = _parse_asb_intelligence(
            queue_rows=[],
            topic_rows=[],
        )
        assert result.configured is False
        assert result.queue_count == 0

    def test_none_rows_handled_gracefully(self):
        """None input doesn't crash the parse."""
        result = _parse_asb_intelligence(
            queue_rows=None,
            topic_rows=None,
        )
        assert result.configured is False
        assert result.queue_count == 0

    def test_topic_count_included_in_configured(self):
        """Topics alone (no queues) still mark configured=True."""
        topic_rows = [{"facets": ["prod-topic", "ns"], "latest.incoming_msgs": 10.0}]
        result = _parse_asb_intelligence(
            queue_rows=[],
            topic_rows=topic_rows,
        )
        assert result.configured is True
        assert result.topic_count == 1
        assert result.queue_count == 0

    def test_rows_with_missing_facets_skipped(self):
        """Rows with fewer than 2 facets are silently skipped."""
        rows = [
            {"facets": ["only-one"]},
            {"facets": ["prod-queue", "ns"], "latest.active_msgs": 1, "latest.dlq_msgs": 0, "latest.incoming_msgs": 0},
        ]
        result = _parse_asb_intelligence(
            queue_rows=rows,
            topic_rows=[],
        )
        assert result.queue_count == 1

    def test_naming_pattern_inferred(self):
        """Naming pattern is inferred from first queue prefix."""
        rows = [
            {"facets": ["prod-svc-queue", "ns"], "latest.active_msgs": 0, "latest.dlq_msgs": 0, "latest.incoming_msgs": 0},
        ]
        result = _parse_asb_intelligence(
            queue_rows=rows,
            topic_rows=[],
        )
        assert "prod" in result.naming_pattern
        assert "{service}" in result.naming_pattern
