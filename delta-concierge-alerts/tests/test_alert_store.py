"""Tests for the DynamoDB-backed alert persistence service."""

from datetime import datetime

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up the ConciergeAlerts table for every test."""
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
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table

        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")

        try:
            yield alert_store_mod.table
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table


@pytest.fixture
def table(_mock_aws):
    """Expose the moto-backed DynamoDB table created by _mock_aws."""
    return _mock_aws


def make_alert(
    alert_id: str = "alert-1",
    skymiles_number: str = "1234567890",
    alert_type: AlertType = AlertType.PASSPORT,
    severity: AlertSeverity = AlertSeverity.WARNING,
    reasons: list[str] | None = None,
    created_at: str = "2026-01-01T00:00:00",
    itinerary_ref: str = "DL-TEST",
    status: AlertStatus = AlertStatus.ACTIVE,
    resolved_at: str | None = None,
    resolution: str | None = None,
    ttl: int | None = None,
) -> AlertRecord:
    """Build an AlertRecord with sensible defaults; override only what's tested."""
    return AlertRecord(
        alert_id=alert_id,
        skymiles_number=skymiles_number,
        alert_type=alert_type,
        severity=severity,
        reasons=reasons if reasons is not None else ["Passport expires soon"],
        created_at=created_at,
        itinerary_ref=itinerary_ref,
        status=status,
        resolved_at=resolved_at,
        resolution=resolution,
        ttl=ttl,
    )


class TestSaveAlert:
    """save_alert persists an AlertRecord and includes only set optional fields."""

    def test_stores_basic_alert_without_optional_fields(self, table):
        record = make_alert()

        alert_store_mod.save_alert(record)

        item = table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "alert-1"}
        )["Item"]
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "WARNING"
        assert item["status"] == "ACTIVE"
        assert item["itinerary_ref"] == "DL-TEST"
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_stores_alert_with_ttl(self, table):
        record = make_alert(ttl=1893456000)

        alert_store_mod.save_alert(record)

        item = table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "alert-1"}
        )["Item"]
        assert int(item["ttl"]) == 1893456000

    def test_stores_alert_with_resolved_at_and_resolution(self, table):
        record = make_alert(
            status=AlertStatus.RESOLVED,
            resolved_at="2026-02-01T00:00:00",
            resolution="Handled by agent",
        )

        alert_store_mod.save_alert(record)

        item = table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "alert-1"}
        )["Item"]
        assert item["resolved_at"] == "2026-02-01T00:00:00"
        assert item["resolution"] == "Handled by agent"
        assert item["status"] == "RESOLVED"


class TestGetAlertsByMember:
    """get_alerts_by_member returns every alert stored under a partition key."""

    def test_returns_all_alerts_for_a_member(self, table):
        alert_store_mod.save_alert(make_alert(alert_id="a1"))
        alert_store_mod.save_alert(make_alert(alert_id="a2"))

        result = alert_store_mod.get_alerts_by_member("1234567890")

        assert {a["alert_id"] for a in result} == {"a1", "a2"}

    def test_returns_empty_list_for_unknown_member(self, table):
        alert_store_mod.save_alert(make_alert(alert_id="a1"))

        result = alert_store_mod.get_alerts_by_member("0000000000")

        assert result == []


class TestResolveAlertsForItinerary:
    """resolve_alerts_for_itinerary resolves only matching active/ack'd alerts."""

    def test_resolves_matching_active_alerts(self, table):
        alert_store_mod.save_alert(make_alert(alert_id="a1", itinerary_ref="DL-1"))
        alert_store_mod.save_alert(
            make_alert(alert_id="a2", itinerary_ref="DL-1", status=AlertStatus.ACKNOWLEDGED)
        )
        alert_store_mod.save_alert(make_alert(alert_id="a3", itinerary_ref="DL-2"))

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "1234567890", "DL-1", "Auto-resolved"
        )

        assert count == 2
        alerts = {a["alert_id"]: a for a in alert_store_mod.get_alerts_by_member("1234567890")}
        assert alerts["a1"]["status"] == "RESOLVED"
        assert alerts["a1"]["resolution"] == "Auto-resolved"
        assert alerts["a2"]["status"] == "RESOLVED"
        assert alerts["a3"]["status"] == "ACTIVE"

    def test_skips_already_resolved_alerts(self, table):
        alert_store_mod.save_alert(
            make_alert(alert_id="a1", itinerary_ref="DL-1", status=AlertStatus.RESOLVED)
        )

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "1234567890", "DL-1", "Auto-resolved"
        )

        assert count == 0

    def test_returns_zero_when_no_matching_alerts(self, table):
        alert_store_mod.save_alert(make_alert(alert_id="a1", itinerary_ref="DL-1"))

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "1234567890", "DL-9", "Auto-resolved"
        )

        assert count == 0


class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the itinerary-ref-index GSI."""

    def test_queries_via_gsi_across_members(self, table):
        alert_store_mod.save_alert(
            make_alert(alert_id="a1", skymiles_number="111", itinerary_ref="DL-SHARED")
        )
        alert_store_mod.save_alert(
            make_alert(alert_id="a2", skymiles_number="222", itinerary_ref="DL-SHARED")
        )
        alert_store_mod.save_alert(
            make_alert(alert_id="a3", skymiles_number="333", itinerary_ref="DL-OTHER")
        )

        result = alert_store_mod.get_alerts_by_itinerary("DL-SHARED")

        assert {a["skymiles_number"] for a in result} == {"111", "222"}


class TestGetGroupAlertSummary:
    """get_group_alert_summary groups an itinerary's alerts by skymiles_number."""

    def test_groups_alerts_by_skymiles_number(self, table):
        alert_store_mod.save_alert(
            make_alert(alert_id="a1", skymiles_number="111", itinerary_ref="DL-G")
        )
        alert_store_mod.save_alert(
            make_alert(alert_id="a2", skymiles_number="111", itinerary_ref="DL-G")
        )
        alert_store_mod.save_alert(
            make_alert(alert_id="a3", skymiles_number="222", itinerary_ref="DL-G")
        )

        summary = alert_store_mod.get_group_alert_summary("DL-G")

        assert set(summary.keys()) == {"111", "222"}
        assert {a["alert_id"] for a in summary["111"]} == {"a1", "a2"}
        assert {a["alert_id"] for a in summary["222"]} == {"a3"}


class TestExpireStaleAlerts:
    """expire_stale_alerts transitions past-TTL active alerts to EXPIRED."""

    def test_marks_past_ttl_active_alerts_as_expired(self, table):
        past = int(datetime.utcnow().timestamp()) - 3600
        alert_store_mod.save_alert(make_alert(alert_id="a1", ttl=past))

        count = alert_store_mod.expire_stale_alerts("1234567890")

        assert count == 1
        item = table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "a1"}
        )["Item"]
        assert item["status"] == "EXPIRED"

    def test_skips_alerts_without_ttl(self, table):
        alert_store_mod.save_alert(make_alert(alert_id="a1", ttl=None))

        count = alert_store_mod.expire_stale_alerts("1234567890")

        assert count == 0
        item = table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "a1"}
        )["Item"]
        assert item["status"] == "ACTIVE"

    def test_skips_alerts_not_yet_past_ttl(self, table):
        future = int(datetime.utcnow().timestamp()) + 3600
        alert_store_mod.save_alert(make_alert(alert_id="a1", ttl=future))

        count = alert_store_mod.expire_stale_alerts("1234567890")

        assert count == 0
        item = table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "a1"}
        )["Item"]
        assert item["status"] == "ACTIVE"
