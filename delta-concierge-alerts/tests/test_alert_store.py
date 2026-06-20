"""Tests for the DynamoDB-backed alert persistence layer."""

import time

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType
from src.services.alert_store import (
    acknowledge_alert,
    expire_stale_alerts,
    get_active_alerts,
    get_alerts_by_itinerary,
    get_alerts_by_member,
    get_group_alert_summary,
    resolve_alert,
    resolve_alerts_for_itinerary,
    save_alert,
)


def _base_alert(**overrides) -> AlertRecord:
    """Build a valid AlertRecord with sensible defaults and optional overrides."""
    defaults = dict(
        alert_id="alert-001",
        skymiles_number="SM123",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport expired"],
        created_at="2026-01-01T00:00:00",
        itinerary_ref="DL-CONF-1",
        status=AlertStatus.ACTIVE,
        resolved_at=None,
        resolution=None,
        ttl=None,
    )
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


# ---------------------------------------------------------------------------
# save_alert
# ---------------------------------------------------------------------------
class TestSaveAlertTTL:
    """save_alert conditionally includes TTL in the persisted item."""

    def test_save_alert_without_ttl(self):
        """When ttl is None the item should not contain a ttl attribute."""
        record = _base_alert(ttl=None)

        save_alert(record)

        items = alert_store_mod.table.scan()["Items"]
        assert len(items) == 1
        assert "ttl" not in items[0]

    def test_save_alert_with_ttl(self):
        """When ttl is set the item should contain the ttl value."""
        record = _base_alert(ttl=1700000000)

        save_alert(record)

        items = alert_store_mod.table.scan()["Items"]
        assert len(items) == 1
        assert items[0]["ttl"] == 1700000000


class TestSaveAlertResolution:
    """save_alert conditionally includes resolved_at and resolution."""

    def test_save_alert_with_resolved_at_and_resolution(self):
        """Both resolved_at and resolution should be persisted when set."""
        record = _base_alert(
            status=AlertStatus.RESOLVED,
            resolved_at="2026-06-01T12:00:00",
            resolution="Passport renewed",
        )

        save_alert(record)

        items = alert_store_mod.table.scan()["Items"]
        assert len(items) == 1
        assert items[0]["resolved_at"] == "2026-06-01T12:00:00"
        assert items[0]["resolution"] == "Passport renewed"

    def test_save_alert_without_resolved_at_and_resolution(self):
        """Neither resolved_at nor resolution should appear when not set."""
        record = _base_alert()

        save_alert(record)

        items = alert_store_mod.table.scan()["Items"]
        assert len(items) == 1
        assert "resolved_at" not in items[0]
        assert "resolution" not in items[0]

    def test_save_alert_core_fields(self):
        """All mandatory fields are persisted correctly."""
        record = _base_alert()

        save_alert(record)

        item = alert_store_mod.table.scan()["Items"][0]
        assert item["skymiles_number"] == "SM123"
        assert item["alert_id"] == "alert-001"
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        assert item["reasons"] == ["Passport expired"]
        assert item["created_at"] == "2026-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-CONF-1"
        assert item["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# get_alerts_by_member
# ---------------------------------------------------------------------------
class TestGetAlertsByMember:
    """get_alerts_by_member returns all alerts for a given SkyMiles number."""

    def test_returns_all_alerts_for_member(self):
        """Multiple alerts for the same member are all returned."""
        save_alert(_base_alert(alert_id="a1"))
        save_alert(_base_alert(alert_id="a2"))

        result = get_alerts_by_member("SM123")

        assert len(result) == 2
        returned_ids = {item["alert_id"] for item in result}
        assert returned_ids == {"a1", "a2"}

    def test_returns_empty_for_unknown_member(self):
        """No alerts for an unknown member returns empty list."""
        result = get_alerts_by_member("UNKNOWN")

        assert result == []

    def test_does_not_return_other_members_alerts(self):
        """Alerts for other members are not included."""
        save_alert(_base_alert(skymiles_number="SM123", alert_id="a1"))
        save_alert(_base_alert(skymiles_number="SM999", alert_id="a2"))

        result = get_alerts_by_member("SM123")

        assert len(result) == 1
        assert result[0]["alert_id"] == "a1"


# ---------------------------------------------------------------------------
# acknowledge_alert / resolve_alert
# ---------------------------------------------------------------------------
class TestAcknowledgeAlert:
    """acknowledge_alert transitions status to ACKNOWLEDGED."""

    def test_acknowledge_sets_status(self):
        """After acknowledging, the item status should be ACKNOWLEDGED."""
        save_alert(_base_alert())

        acknowledge_alert("SM123", "alert-001")

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "SM123", "alert_id": "alert-001"}
        )["Item"]
        assert item["status"] == AlertStatus.ACKNOWLEDGED.value


class TestResolveAlert:
    """resolve_alert transitions status to RESOLVED with metadata."""

    def test_resolve_sets_status_and_metadata(self):
        """After resolving, status, resolved_at, and resolution are set."""
        save_alert(_base_alert())

        resolve_alert("SM123", "alert-001", "Passport renewed")

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "SM123", "alert_id": "alert-001"}
        )["Item"]
        assert item["status"] == AlertStatus.RESOLVED.value
        assert item["resolution"] == "Passport renewed"
        assert "resolved_at" in item


# ---------------------------------------------------------------------------
# get_active_alerts
# ---------------------------------------------------------------------------
class TestGetActiveAlerts:
    """get_active_alerts returns only ACTIVE and ACKNOWLEDGED alerts."""

    def test_returns_active_and_acknowledged(self):
        """Both ACTIVE and ACKNOWLEDGED alerts should be returned."""
        save_alert(_base_alert(alert_id="a1", status=AlertStatus.ACTIVE))
        save_alert(_base_alert(alert_id="a2", status=AlertStatus.ACKNOWLEDGED))
        save_alert(_base_alert(alert_id="a3", status=AlertStatus.RESOLVED))
        save_alert(_base_alert(alert_id="a4", status=AlertStatus.EXPIRED))

        result = get_active_alerts("SM123")

        returned_ids = {item["alert_id"] for item in result}
        assert returned_ids == {"a1", "a2"}


# ---------------------------------------------------------------------------
# resolve_alerts_for_itinerary
# ---------------------------------------------------------------------------
class TestResolveAlertsForItinerary:
    """resolve_alerts_for_itinerary resolves matching alerts in bulk."""

    def test_resolves_matching_active_alerts(self):
        """Active alerts matching the itinerary are resolved and counted."""
        save_alert(_base_alert(alert_id="a1", itinerary_ref="DL-100"))
        save_alert(_base_alert(alert_id="a2", itinerary_ref="DL-100"))
        save_alert(_base_alert(alert_id="a3", itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary("SM123", "DL-100", "Itinerary changed")

        assert count == 2
        for aid in ("a1", "a2"):
            item = alert_store_mod.table.get_item(
                Key={"skymiles_number": "SM123", "alert_id": aid}
            )["Item"]
            assert item["status"] == AlertStatus.RESOLVED.value
            assert item["resolution"] == "Itinerary changed"

    def test_no_matching_alerts_returns_zero(self):
        """When no alerts match the itinerary, returns 0."""
        save_alert(_base_alert(alert_id="a1", itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary("SM123", "DL-100", "Itinerary changed")

        assert count == 0

    def test_skips_resolved_alerts(self):
        """Already-resolved alerts are not re-resolved."""
        save_alert(
            _base_alert(
                alert_id="a1",
                itinerary_ref="DL-100",
                status=AlertStatus.RESOLVED,
            )
        )
        save_alert(_base_alert(alert_id="a2", itinerary_ref="DL-100"))

        count = resolve_alerts_for_itinerary("SM123", "DL-100", "Re-evaluated")

        assert count == 1

    def test_resolves_acknowledged_alerts(self):
        """ACKNOWLEDGED alerts matching the itinerary should also be resolved."""
        save_alert(
            _base_alert(
                alert_id="a1",
                itinerary_ref="DL-100",
                status=AlertStatus.ACKNOWLEDGED,
            )
        )

        count = resolve_alerts_for_itinerary("SM123", "DL-100", "Re-evaluated")

        assert count == 1


# ---------------------------------------------------------------------------
# get_alerts_by_itinerary (GSI query)
# ---------------------------------------------------------------------------
class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries alerts via the itinerary-ref-index GSI."""

    def test_returns_alerts_for_itinerary(self):
        """Alerts matching the confirmation number are returned."""
        save_alert(_base_alert(alert_id="a1", itinerary_ref="DL-200"))
        save_alert(_base_alert(alert_id="a2", itinerary_ref="DL-200", skymiles_number="SM999"))
        save_alert(_base_alert(alert_id="a3", itinerary_ref="DL-OTHER"))

        result = get_alerts_by_itinerary("DL-200")

        assert len(result) == 2
        returned_ids = {item["alert_id"] for item in result}
        assert returned_ids == {"a1", "a2"}

    def test_returns_empty_for_unknown_itinerary(self):
        """No alerts for an unknown itinerary returns empty list."""
        result = get_alerts_by_itinerary("NONEXISTENT")

        assert result == []


# ---------------------------------------------------------------------------
# get_group_alert_summary
# ---------------------------------------------------------------------------
class TestGetGroupAlertSummary:
    """get_group_alert_summary groups alerts by skymiles_number."""

    def test_groups_alerts_by_member(self):
        """Alerts for the same itinerary are grouped by skymiles_number."""
        save_alert(_base_alert(alert_id="a1", skymiles_number="SM100", itinerary_ref="DL-300"))
        save_alert(_base_alert(alert_id="a2", skymiles_number="SM100", itinerary_ref="DL-300"))
        save_alert(_base_alert(alert_id="a3", skymiles_number="SM200", itinerary_ref="DL-300"))

        result = get_group_alert_summary("DL-300")

        assert set(result.keys()) == {"SM100", "SM200"}
        assert len(result["SM100"]) == 2
        assert len(result["SM200"]) == 1

    def test_empty_itinerary_returns_empty_dict(self):
        """No alerts returns an empty grouping."""
        result = get_group_alert_summary("NONEXISTENT")

        assert result == {}


# ---------------------------------------------------------------------------
# expire_stale_alerts
# ---------------------------------------------------------------------------
class TestExpireStaleAlerts:
    """expire_stale_alerts marks past-TTL alerts as EXPIRED."""

    def test_expires_alerts_past_ttl(self):
        """Alerts with TTL in the past should be marked EXPIRED."""
        past_ttl = int(time.time()) - 3600
        save_alert(_base_alert(alert_id="a1", ttl=past_ttl))

        count = expire_stale_alerts("SM123")

        assert count == 1
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "SM123", "alert_id": "a1"}
        )["Item"]
        assert item["status"] == AlertStatus.EXPIRED.value

    def test_no_expired_alerts_returns_zero(self):
        """Alerts with TTL in the future should not be expired."""
        future_ttl = int(time.time()) + 86400
        save_alert(_base_alert(alert_id="a1", ttl=future_ttl))

        count = expire_stale_alerts("SM123")

        assert count == 0
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "SM123", "alert_id": "a1"}
        )["Item"]
        assert item["status"] == AlertStatus.ACTIVE.value

    def test_skips_alerts_without_ttl(self):
        """Alerts without a TTL attribute should be skipped entirely."""
        save_alert(_base_alert(alert_id="a1", ttl=None))

        count = expire_stale_alerts("SM123")

        assert count == 0

    def test_skips_already_resolved_alerts(self):
        """Alerts with status RESOLVED should not be expired even if past TTL."""
        past_ttl = int(time.time()) - 3600
        save_alert(
            _base_alert(
                alert_id="a1",
                ttl=past_ttl,
                status=AlertStatus.RESOLVED,
            )
        )

        count = expire_stale_alerts("SM123")

        assert count == 0
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "SM123", "alert_id": "a1"}
        )["Item"]
        assert item["status"] == AlertStatus.RESOLVED.value

    def test_expires_acknowledged_alert_past_ttl(self):
        """ACKNOWLEDGED alerts past TTL should also be expired."""
        past_ttl = int(time.time()) - 3600
        save_alert(
            _base_alert(
                alert_id="a1",
                ttl=past_ttl,
                status=AlertStatus.ACKNOWLEDGED,
            )
        )

        count = expire_stale_alerts("SM123")

        assert count == 1
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "SM123", "alert_id": "a1"}
        )["Item"]
        assert item["status"] == AlertStatus.EXPIRED.value

    def test_mixed_alerts_only_expires_eligible(self):
        """Only past-TTL ACTIVE/ACKNOWLEDGED alerts are expired."""
        past_ttl = int(time.time()) - 3600
        future_ttl = int(time.time()) + 86400

        save_alert(_base_alert(alert_id="a1", ttl=past_ttl, status=AlertStatus.ACTIVE))
        save_alert(_base_alert(alert_id="a2", ttl=future_ttl, status=AlertStatus.ACTIVE))
        save_alert(_base_alert(alert_id="a3", ttl=None))
        save_alert(_base_alert(alert_id="a4", ttl=past_ttl, status=AlertStatus.RESOLVED))

        count = expire_stale_alerts("SM123")

        assert count == 1
