"""Unit tests for the DynamoDB-backed alert persistence layer."""

import time

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_alert(**overrides) -> AlertRecord:
    """Build a complete valid AlertRecord with optional overrides."""
    defaults = dict(
        alert_id="ALERT-001",
        skymiles_number="9999999",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport expired"],
        created_at="2026-01-15T00:00:00",
        itinerary_ref="DL-TEST",
        status=AlertStatus.ACTIVE,
        resolved_at=None,
        resolution=None,
        ttl=None,
    )
    defaults.update(overrides)
    return AlertRecord(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks, create DynamoDB table with GSI, and patch module clients."""
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


# ---------------------------------------------------------------------------
# save_alert – optional field branches
# ---------------------------------------------------------------------------

class TestSaveAlertOptionalFields:
    """save_alert conditionally includes resolved_at, resolution, and ttl."""

    def test_save_with_resolved_at(self):
        """resolved_at is persisted when set."""
        record = _base_alert(resolved_at="2026-02-01T00:00:00", status=AlertStatus.RESOLVED)

        alert_store_mod.save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "ALERT-001"},
        )["Item"]
        assert item["resolved_at"] == "2026-02-01T00:00:00"

    def test_save_with_resolution(self):
        """resolution string is persisted when set."""
        record = _base_alert(resolution="Passport renewed", status=AlertStatus.RESOLVED)

        alert_store_mod.save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "ALERT-001"},
        )["Item"]
        assert item["resolution"] == "Passport renewed"

    def test_save_without_optional_fields(self):
        """Optional fields omitted from DynamoDB when None."""
        record = _base_alert()

        alert_store_mod.save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "ALERT-001"},
        )["Item"]
        assert "resolved_at" not in item
        assert "resolution" not in item
        assert "ttl" not in item

    def test_save_with_ttl(self):
        """ttl is persisted when set."""
        record = _base_alert(ttl=1700000000)

        alert_store_mod.save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "ALERT-001"},
        )["Item"]
        assert int(item["ttl"]) == 1700000000


# ---------------------------------------------------------------------------
# get_alerts_by_member
# ---------------------------------------------------------------------------

class TestGetAlertsByMember:
    """get_alerts_by_member returns all alerts for a given SkyMiles number."""

    def test_returns_all_alerts_for_member(self):
        """Two alerts stored → both returned."""
        alert_store_mod.save_alert(_base_alert(alert_id="A1"))
        alert_store_mod.save_alert(_base_alert(alert_id="A2"))

        result = alert_store_mod.get_alerts_by_member("9999999")

        assert len(result) == 2
        returned_ids = {item["alert_id"] for item in result}
        assert returned_ids == {"A1", "A2"}

    def test_returns_empty_for_unknown_member(self):
        """No alerts exist → empty list."""
        result = alert_store_mod.get_alerts_by_member("0000000")

        assert result == []

    def test_does_not_return_other_members_alerts(self):
        """Alerts for member A should not appear when querying member B."""
        alert_store_mod.save_alert(_base_alert(skymiles_number="AAA", alert_id="A1"))
        alert_store_mod.save_alert(_base_alert(skymiles_number="BBB", alert_id="B1"))

        result = alert_store_mod.get_alerts_by_member("AAA")

        assert len(result) == 1
        assert result[0]["alert_id"] == "A1"


# ---------------------------------------------------------------------------
# resolve_alerts_for_itinerary
# ---------------------------------------------------------------------------

class TestResolveAlertsForItinerary:
    """resolve_alerts_for_itinerary bulk-resolves matching active alerts."""

    def test_resolves_matching_active_alerts(self):
        """Active alerts matching confirmation are resolved and counted."""
        alert_store_mod.save_alert(
            _base_alert(alert_id="A1", itinerary_ref="DL-100", status=AlertStatus.ACTIVE)
        )
        alert_store_mod.save_alert(
            _base_alert(alert_id="A2", itinerary_ref="DL-100", status=AlertStatus.ACKNOWLEDGED)
        )

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "9999999", "DL-100", "Itinerary changed"
        )

        assert count == 2
        items = alert_store_mod.get_alerts_by_member("9999999")
        for item in items:
            assert item["status"] == AlertStatus.RESOLVED.value

    def test_skips_already_resolved_alerts(self):
        """Already-resolved alerts are not re-resolved."""
        alert_store_mod.save_alert(
            _base_alert(alert_id="A1", itinerary_ref="DL-100", status=AlertStatus.RESOLVED)
        )

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "9999999", "DL-100", "Itinerary changed"
        )

        assert count == 0

    def test_skips_alerts_for_other_itinerary(self):
        """Alerts for a different itinerary are not resolved."""
        alert_store_mod.save_alert(
            _base_alert(alert_id="A1", itinerary_ref="DL-200", status=AlertStatus.ACTIVE)
        )

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "9999999", "DL-100", "Itinerary changed"
        )

        assert count == 0

    def test_returns_zero_when_no_alerts(self):
        """No matching alerts → 0 returned."""
        count = alert_store_mod.resolve_alerts_for_itinerary(
            "9999999", "DL-100", "Itinerary changed"
        )

        assert count == 0


# ---------------------------------------------------------------------------
# get_alerts_by_itinerary (GSI)
# ---------------------------------------------------------------------------

class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the itinerary-ref-index GSI."""

    def test_returns_alerts_for_confirmation(self):
        """Alerts matching confirmation number are returned via GSI."""
        alert_store_mod.save_alert(
            _base_alert(skymiles_number="AAA", alert_id="A1", itinerary_ref="DL-500")
        )
        alert_store_mod.save_alert(
            _base_alert(skymiles_number="BBB", alert_id="B1", itinerary_ref="DL-500")
        )

        result = alert_store_mod.get_alerts_by_itinerary("DL-500")

        assert len(result) == 2

    def test_returns_empty_for_unknown_confirmation(self):
        """No matching confirmation → empty list."""
        result = alert_store_mod.get_alerts_by_itinerary("DL-NONE")

        assert result == []

    def test_does_not_return_other_itinerary_alerts(self):
        """Alerts for a different itinerary are excluded."""
        alert_store_mod.save_alert(
            _base_alert(alert_id="A1", itinerary_ref="DL-500")
        )
        alert_store_mod.save_alert(
            _base_alert(alert_id="A2", itinerary_ref="DL-600")
        )

        result = alert_store_mod.get_alerts_by_itinerary("DL-500")

        assert len(result) == 1


# ---------------------------------------------------------------------------
# get_group_alert_summary
# ---------------------------------------------------------------------------

class TestGetGroupAlertSummary:
    """get_group_alert_summary groups alerts by skymiles_number."""

    def test_groups_alerts_by_traveler(self):
        """Two travelers on same booking → dict with two keys."""
        alert_store_mod.save_alert(
            _base_alert(skymiles_number="AAA", alert_id="A1", itinerary_ref="DL-GRP")
        )
        alert_store_mod.save_alert(
            _base_alert(skymiles_number="BBB", alert_id="B1", itinerary_ref="DL-GRP")
        )
        alert_store_mod.save_alert(
            _base_alert(skymiles_number="AAA", alert_id="A2", itinerary_ref="DL-GRP")
        )

        summary = alert_store_mod.get_group_alert_summary("DL-GRP")

        assert set(summary.keys()) == {"AAA", "BBB"}
        assert len(summary["AAA"]) == 2
        assert len(summary["BBB"]) == 1

    def test_returns_empty_dict_for_no_alerts(self):
        """No alerts for booking → empty dict."""
        summary = alert_store_mod.get_group_alert_summary("DL-NONE")

        assert summary == {}


# ---------------------------------------------------------------------------
# expire_stale_alerts
# ---------------------------------------------------------------------------

class TestExpireStaleAlerts:
    """expire_stale_alerts marks TTL-past alerts as EXPIRED."""

    def test_expires_active_alert_past_ttl(self):
        """Active alert with TTL in the past is expired."""
        past_ttl = int(time.time()) - 3600
        alert_store_mod.save_alert(
            _base_alert(alert_id="A1", ttl=past_ttl, status=AlertStatus.ACTIVE)
        )

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 1
        items = alert_store_mod.get_alerts_by_member("9999999")
        assert items[0]["status"] == AlertStatus.EXPIRED.value

    def test_expires_acknowledged_alert_past_ttl(self):
        """Acknowledged alert with TTL in the past is expired."""
        past_ttl = int(time.time()) - 3600
        alert_store_mod.save_alert(
            _base_alert(alert_id="A1", ttl=past_ttl, status=AlertStatus.ACKNOWLEDGED)
        )

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 1

    def test_skips_alert_with_future_ttl(self):
        """Alert with TTL in the future is not expired."""
        future_ttl = int(time.time()) + 86400
        alert_store_mod.save_alert(
            _base_alert(alert_id="A1", ttl=future_ttl, status=AlertStatus.ACTIVE)
        )

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0
        items = alert_store_mod.get_alerts_by_member("9999999")
        assert items[0]["status"] == AlertStatus.ACTIVE.value

    def test_skips_alert_without_ttl(self):
        """Alert with no TTL is never expired."""
        alert_store_mod.save_alert(
            _base_alert(alert_id="A1", ttl=None, status=AlertStatus.ACTIVE)
        )

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0

    def test_skips_already_resolved_alert(self):
        """Resolved alert with past TTL is not re-expired."""
        past_ttl = int(time.time()) - 3600
        alert_store_mod.save_alert(
            _base_alert(alert_id="A1", ttl=past_ttl, status=AlertStatus.RESOLVED)
        )

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0

    def test_skips_already_expired_alert(self):
        """Already-expired alert is not counted again."""
        past_ttl = int(time.time()) - 3600
        alert_store_mod.save_alert(
            _base_alert(alert_id="A1", ttl=past_ttl, status=AlertStatus.EXPIRED)
        )

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0

    def test_mixed_alerts_only_stale_expired(self):
        """Only stale active/acknowledged alerts are expired in a mixed set."""
        past_ttl = int(time.time()) - 3600
        future_ttl = int(time.time()) + 86400

        alert_store_mod.save_alert(
            _base_alert(alert_id="A1", ttl=past_ttl, status=AlertStatus.ACTIVE)
        )
        alert_store_mod.save_alert(
            _base_alert(alert_id="A2", ttl=future_ttl, status=AlertStatus.ACTIVE)
        )
        alert_store_mod.save_alert(
            _base_alert(alert_id="A3", ttl=past_ttl, status=AlertStatus.RESOLVED)
        )
        alert_store_mod.save_alert(
            _base_alert(alert_id="A4", ttl=None, status=AlertStatus.ACTIVE)
        )

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 1
