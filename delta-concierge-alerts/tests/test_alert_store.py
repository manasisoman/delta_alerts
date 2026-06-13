"""Tests for src/services/alert_store.py — DynamoDB-backed alert persistence."""

from datetime import datetime

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType


def _base_alert(**overrides) -> AlertRecord:
    """Build a valid AlertRecord with sensible defaults and optional overrides."""
    defaults = {
        "alert_id": "alert-001",
        "skymiles_number": "1234567890",
        "alert_type": AlertType.PASSPORT,
        "severity": AlertSeverity.CRITICAL,
        "reasons": ["Passport expired"],
        "created_at": "2025-01-15T10:00:00",
        "itinerary_ref": "DL-ABC123",
        "status": AlertStatus.ACTIVE,
        "resolved_at": None,
        "resolution": None,
        "ttl": None,
    }
    defaults.update(overrides)
    return AlertRecord(**defaults)


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up DynamoDB table with GSI for every test."""
    with moto.mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="ConciergeAlerts",
            KeySchema=[
                {"AttributeName": "skymiles_number", "KeyType": "HASH"},
                {"AttributeName": "alert_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "skymiles_number", "AttributeType": "S"},
                {"AttributeName": "alert_id", "AttributeType": "S"},
                {"AttributeName": "itinerary_ref", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "itinerary-ref-index",
                    "KeySchema": [
                        {"AttributeName": "itinerary_ref", "KeyType": "HASH"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table

        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")

        try:
            yield
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table


class TestSaveAlert:
    """Verifies save_alert persists items with optional fields conditionally."""

    def test_save_basic_alert_without_optional_fields(self):
        """Default alert with ttl=None, resolved_at=None, resolution=None."""
        record = _base_alert()

        alert_store_mod.save_alert(record)

        items = alert_store_mod.table.scan()["Items"]
        assert len(items) == 1
        item = items[0]
        assert item["skymiles_number"] == "1234567890"
        assert item["alert_id"] == "alert-001"
        assert item["status"] == "ACTIVE"
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_save_alert_with_ttl(self):
        """When ttl is set, item should include ttl attribute."""
        record = _base_alert(ttl=1700000000)

        alert_store_mod.save_alert(record)

        item = alert_store_mod.table.scan()["Items"][0]
        assert item["ttl"] == 1700000000

    def test_save_alert_with_resolved_at(self):
        """When resolved_at is set, item should include resolved_at attribute."""
        record = _base_alert(
            status=AlertStatus.RESOLVED,
            resolved_at="2025-02-01T12:00:00",
        )

        alert_store_mod.save_alert(record)

        item = alert_store_mod.table.scan()["Items"][0]
        assert item["resolved_at"] == "2025-02-01T12:00:00"

    def test_save_alert_with_resolution(self):
        """When resolution is set, item should include resolution attribute."""
        record = _base_alert(
            status=AlertStatus.RESOLVED,
            resolved_at="2025-02-01T12:00:00",
            resolution="Passport renewed",
        )

        alert_store_mod.save_alert(record)

        item = alert_store_mod.table.scan()["Items"][0]
        assert item["resolution"] == "Passport renewed"

    def test_save_alert_with_all_optional_fields(self):
        """All optional fields populated."""
        record = _base_alert(
            ttl=1700000000,
            resolved_at="2025-02-01T12:00:00",
            resolution="Auto-resolved",
        )

        alert_store_mod.save_alert(record)

        item = alert_store_mod.table.scan()["Items"][0]
        assert item["ttl"] == 1700000000
        assert item["resolved_at"] == "2025-02-01T12:00:00"
        assert item["resolution"] == "Auto-resolved"


class TestGetAlertsByMember:
    """Verifies querying all alerts for a SkyMiles member."""

    def test_returns_all_alerts_for_member(self):
        """Multiple alerts for the same member are all returned."""
        alert_store_mod.save_alert(_base_alert(alert_id="alert-001"))
        alert_store_mod.save_alert(_base_alert(alert_id="alert-002"))

        results = alert_store_mod.get_alerts_by_member("1234567890")

        assert len(results) == 2
        alert_ids = {r["alert_id"] for r in results}
        assert alert_ids == {"alert-001", "alert-002"}

    def test_returns_empty_for_unknown_member(self):
        """No alerts for a member that doesn't exist."""
        results = alert_store_mod.get_alerts_by_member("0000000000")

        assert results == []


class TestResolveAlertsForItinerary:
    """Verifies bulk-resolve of active/acknowledged alerts for an itinerary."""

    def test_resolves_active_alerts_matching_itinerary(self):
        """Active alerts for the matching confirmation_number get resolved."""
        alert_store_mod.save_alert(
            _base_alert(alert_id="alert-001", itinerary_ref="DL-ABC123")
        )
        alert_store_mod.save_alert(
            _base_alert(alert_id="alert-002", itinerary_ref="DL-ABC123")
        )

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "1234567890", "DL-ABC123", "Itinerary changed"
        )

        assert count == 2
        items = alert_store_mod.get_alerts_by_member("1234567890")
        for item in items:
            assert item["status"] == "RESOLVED"
            assert item["resolution"] == "Itinerary changed"

    def test_does_not_resolve_alerts_for_different_itinerary(self):
        """Alerts for a different itinerary_ref remain unchanged."""
        alert_store_mod.save_alert(
            _base_alert(alert_id="alert-001", itinerary_ref="DL-ABC123")
        )
        alert_store_mod.save_alert(
            _base_alert(alert_id="alert-002", itinerary_ref="DL-OTHER")
        )

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "1234567890", "DL-ABC123", "Itinerary changed"
        )

        assert count == 1
        items = alert_store_mod.get_alerts_by_member("1234567890")
        other = [i for i in items if i["alert_id"] == "alert-002"][0]
        assert other["status"] == "ACTIVE"

    def test_skips_already_resolved_alerts(self):
        """Already-resolved alerts don't get counted or re-resolved."""
        alert_store_mod.save_alert(
            _base_alert(
                alert_id="alert-001",
                itinerary_ref="DL-ABC123",
                status=AlertStatus.RESOLVED,
                resolved_at="2025-01-01T00:00:00",
                resolution="Previously resolved",
            )
        )

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "1234567890", "DL-ABC123", "New resolution"
        )

        assert count == 0

    def test_resolves_acknowledged_alerts(self):
        """Acknowledged alerts should also be resolved."""
        alert_store_mod.save_alert(
            _base_alert(
                alert_id="alert-001",
                itinerary_ref="DL-ABC123",
                status=AlertStatus.ACKNOWLEDGED,
            )
        )

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "1234567890", "DL-ABC123", "Auto-resolved"
        )

        assert count == 1

    def test_returns_zero_when_no_alerts_match(self):
        """No alerts for the itinerary returns 0."""
        count = alert_store_mod.resolve_alerts_for_itinerary(
            "1234567890", "DL-NONEXIST", "Some resolution"
        )

        assert count == 0


class TestGetAlertsByItinerary:
    """Verifies GSI-based query by itinerary confirmation number."""

    def test_returns_alerts_across_members_for_same_itinerary(self):
        """Alerts from different members sharing an itinerary are returned."""
        alert_store_mod.save_alert(
            _base_alert(
                alert_id="alert-001",
                skymiles_number="1111111111",
                itinerary_ref="DL-GROUP",
            )
        )
        alert_store_mod.save_alert(
            _base_alert(
                alert_id="alert-002",
                skymiles_number="2222222222",
                itinerary_ref="DL-GROUP",
            )
        )

        results = alert_store_mod.get_alerts_by_itinerary("DL-GROUP")

        assert len(results) == 2
        skymiles = {r["skymiles_number"] for r in results}
        assert skymiles == {"1111111111", "2222222222"}

    def test_returns_empty_for_unknown_itinerary(self):
        """No alerts for an itinerary that doesn't exist."""
        results = alert_store_mod.get_alerts_by_itinerary("DL-UNKNOWN")

        assert results == []


class TestGetGroupAlertSummary:
    """Verifies grouping alerts by skymiles_number for a booking."""

    def test_groups_alerts_by_member(self):
        """Alerts are grouped into a dict keyed by skymiles_number."""
        alert_store_mod.save_alert(
            _base_alert(
                alert_id="alert-001",
                skymiles_number="1111111111",
                itinerary_ref="DL-GROUP",
            )
        )
        alert_store_mod.save_alert(
            _base_alert(
                alert_id="alert-002",
                skymiles_number="1111111111",
                itinerary_ref="DL-GROUP",
            )
        )
        alert_store_mod.save_alert(
            _base_alert(
                alert_id="alert-003",
                skymiles_number="2222222222",
                itinerary_ref="DL-GROUP",
            )
        )

        summary = alert_store_mod.get_group_alert_summary("DL-GROUP")

        assert set(summary.keys()) == {"1111111111", "2222222222"}
        assert len(summary["1111111111"]) == 2
        assert len(summary["2222222222"]) == 1

    def test_returns_empty_dict_for_no_alerts(self):
        """No alerts for the itinerary yields an empty dict."""
        summary = alert_store_mod.get_group_alert_summary("DL-EMPTY")

        assert summary == {}


class TestExpireStaleAlerts:
    """Verifies TTL-based expiration of stale alerts."""

    def test_expires_active_alert_past_ttl(self):
        """Active alert with TTL in the past gets marked EXPIRED."""
        past_ttl = int(datetime(2020, 1, 1).timestamp())
        alert_store_mod.save_alert(
            _base_alert(alert_id="alert-001", ttl=past_ttl)
        )

        count = alert_store_mod.expire_stale_alerts("1234567890")

        assert count == 1
        items = alert_store_mod.get_alerts_by_member("1234567890")
        assert items[0]["status"] == "EXPIRED"

    def test_expires_acknowledged_alert_past_ttl(self):
        """Acknowledged alert with TTL in the past gets marked EXPIRED."""
        past_ttl = int(datetime(2020, 1, 1).timestamp())
        alert_store_mod.save_alert(
            _base_alert(
                alert_id="alert-001",
                status=AlertStatus.ACKNOWLEDGED,
                ttl=past_ttl,
            )
        )

        count = alert_store_mod.expire_stale_alerts("1234567890")

        assert count == 1
        items = alert_store_mod.get_alerts_by_member("1234567890")
        assert items[0]["status"] == "EXPIRED"

    def test_skips_alert_without_ttl(self):
        """Alert with ttl=None is never expired."""
        alert_store_mod.save_alert(_base_alert(alert_id="alert-001", ttl=None))

        count = alert_store_mod.expire_stale_alerts("1234567890")

        assert count == 0
        items = alert_store_mod.get_alerts_by_member("1234567890")
        assert items[0]["status"] == "ACTIVE"

    def test_skips_alert_with_future_ttl(self):
        """Alert with TTL in the future is not expired."""
        future_ttl = int(datetime(2030, 1, 1).timestamp())
        alert_store_mod.save_alert(
            _base_alert(alert_id="alert-001", ttl=future_ttl)
        )

        count = alert_store_mod.expire_stale_alerts("1234567890")

        assert count == 0
        items = alert_store_mod.get_alerts_by_member("1234567890")
        assert items[0]["status"] == "ACTIVE"

    def test_skips_already_resolved_alert_past_ttl(self):
        """Resolved alert past TTL is not re-expired."""
        past_ttl = int(datetime(2020, 1, 1).timestamp())
        alert_store_mod.save_alert(
            _base_alert(
                alert_id="alert-001",
                status=AlertStatus.RESOLVED,
                ttl=past_ttl,
                resolved_at="2025-01-01T00:00:00",
                resolution="Already resolved",
            )
        )

        count = alert_store_mod.expire_stale_alerts("1234567890")

        assert count == 0

    def test_expires_multiple_stale_alerts(self):
        """Multiple stale alerts are all expired; valid ones are skipped."""
        past_ttl = int(datetime(2020, 1, 1).timestamp())
        future_ttl = int(datetime(2030, 1, 1).timestamp())

        alert_store_mod.save_alert(
            _base_alert(alert_id="alert-001", ttl=past_ttl)
        )
        alert_store_mod.save_alert(
            _base_alert(alert_id="alert-002", ttl=past_ttl)
        )
        alert_store_mod.save_alert(
            _base_alert(alert_id="alert-003", ttl=future_ttl)
        )
        alert_store_mod.save_alert(
            _base_alert(alert_id="alert-004", ttl=None)
        )

        count = alert_store_mod.expire_stale_alerts("1234567890")

        assert count == 2

    def test_returns_zero_when_no_alerts_exist(self):
        """No alerts for the member returns 0."""
        count = alert_store_mod.expire_stale_alerts("0000000000")

        assert count == 0
