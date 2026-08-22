"""Tests for the DynamoDB-backed alert store service."""

from datetime import datetime, timedelta

import boto3
import moto
import pytest
from botocore.exceptions import ClientError

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


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up the ConciergeAlerts table with its GSI."""
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

        # Save original module-level boto3 clients
        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table

        # Patch module-level boto3 clients to use the moto context
        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")

        try:
            yield
        finally:
            # Restore original clients so other test modules aren't affected
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table


def make_alert_record(
    alert_id: str = "alert-001",
    skymiles_number: str = "9999999",
    alert_type: AlertType = AlertType.PASSPORT,
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    reasons: list[str] | None = None,
    created_at: str = "2026-01-01T00:00:00",
    itinerary_ref: str = "DL-TEST",
    status: AlertStatus = AlertStatus.ACTIVE,
    resolved_at: str | None = None,
    resolution: str | None = None,
    ttl: int | None = None,
) -> AlertRecord:
    """Helper to build an AlertRecord with sensible defaults."""
    return AlertRecord(
        alert_id=alert_id,
        skymiles_number=skymiles_number,
        alert_type=alert_type,
        severity=severity,
        reasons=reasons or ["Passport expired"],
        created_at=created_at,
        itinerary_ref=itinerary_ref,
        status=status,
        resolved_at=resolved_at,
        resolution=resolution,
        ttl=ttl,
    )


class TestSaveAlert:
    """save_alert persists required fields and conditionally includes optionals."""

    def test_happy_path_persists_required_fields(self):
        record = make_alert_record()

        save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        assert item["reasons"] == ["Passport expired"]
        assert item["created_at"] == "2026-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == "ACTIVE"
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_ttl_included_when_set(self):
        record = make_alert_record(ttl=1893456000)

        save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert item["ttl"] == 1893456000

    def test_resolved_fields_included_when_set(self):
        record = make_alert_record(
            status=AlertStatus.RESOLVED,
            resolved_at="2026-02-01T00:00:00",
            resolution="Passport renewed",
        )

        save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert item["status"] == "RESOLVED"
        assert item["resolved_at"] == "2026-02-01T00:00:00"
        assert item["resolution"] == "Passport renewed"


class TestGetAlertsByMember:
    """get_alerts_by_member returns all alerts for a partition key."""

    def test_returns_all_alerts_for_member(self):
        save_alert(make_alert_record(alert_id="alert-001"))
        save_alert(make_alert_record(alert_id="alert-002", alert_type=AlertType.VISA))
        save_alert(make_alert_record(alert_id="alert-003", skymiles_number="1111111"))

        alerts = get_alerts_by_member("9999999")

        assert len(alerts) == 2
        assert {a["alert_id"] for a in alerts} == {"alert-001", "alert-002"}

    def test_returns_empty_list_for_unknown_member(self):
        save_alert(make_alert_record())

        alerts = get_alerts_by_member("0000000")

        assert alerts == []


class TestAcknowledgeAlert:
    """acknowledge_alert transitions an alert's status to ACKNOWLEDGED."""

    def test_sets_status_to_acknowledged(self):
        save_alert(make_alert_record())

        acknowledge_alert("9999999", "alert-001")

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert item["status"] == "ACKNOWLEDGED"


class TestResolveAlert:
    """resolve_alert sets status, resolved_at timestamp, and resolution text."""

    def test_sets_resolved_status_and_fields(self):
        save_alert(make_alert_record())

        resolve_alert("9999999", "alert-001", "Visa obtained")

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Visa obtained"
        assert item["resolved_at"] is not None


class TestGetActiveAlerts:
    """get_active_alerts filters to ACTIVE and ACKNOWLEDGED statuses only."""

    def test_returns_only_active_and_acknowledged(self):
        save_alert(make_alert_record(alert_id="alert-active", status=AlertStatus.ACTIVE))
        save_alert(
            make_alert_record(alert_id="alert-ack", status=AlertStatus.ACKNOWLEDGED)
        )
        save_alert(
            make_alert_record(alert_id="alert-resolved", status=AlertStatus.RESOLVED)
        )
        save_alert(
            make_alert_record(alert_id="alert-expired", status=AlertStatus.EXPIRED)
        )

        alerts = get_active_alerts("9999999")

        assert {a["alert_id"] for a in alerts} == {"alert-active", "alert-ack"}


class TestResolveAlertsForItinerary:
    """resolve_alerts_for_itinerary resolves matching alerts and returns the count."""

    def test_resolves_matching_active_and_acknowledged_alerts(self):
        save_alert(make_alert_record(alert_id="alert-001", itinerary_ref="DL-ABC"))
        save_alert(
            make_alert_record(
                alert_id="alert-002",
                itinerary_ref="DL-ABC",
                status=AlertStatus.ACKNOWLEDGED,
            )
        )
        save_alert(make_alert_record(alert_id="alert-003", itinerary_ref="DL-XYZ"))

        count = resolve_alerts_for_itinerary(
            "9999999", "DL-ABC", "Auto-resolved: itinerary changed, re-evaluating"
        )

        assert count == 2
        resolved = get_alerts_by_member("9999999")
        by_id = {a["alert_id"]: a for a in resolved}
        assert by_id["alert-001"]["status"] == "RESOLVED"
        assert by_id["alert-002"]["status"] == "RESOLVED"
        assert by_id["alert-003"]["status"] == "ACTIVE"

    def test_skips_already_resolved_alerts(self):
        save_alert(
            make_alert_record(
                alert_id="alert-001",
                itinerary_ref="DL-ABC",
                status=AlertStatus.RESOLVED,
            )
        )

        count = resolve_alerts_for_itinerary("9999999", "DL-ABC", "n/a")

        assert count == 0

    def test_returns_zero_when_no_alerts_match(self):
        count = resolve_alerts_for_itinerary("9999999", "DL-NONE", "n/a")

        assert count == 0


class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the itinerary-ref GSI across travelers."""

    def test_returns_alerts_across_travelers_for_booking(self):
        save_alert(
            make_alert_record(
                alert_id="alert-001", skymiles_number="1111111", itinerary_ref="DL-GRP"
            )
        )
        save_alert(
            make_alert_record(
                alert_id="alert-002", skymiles_number="2222222", itinerary_ref="DL-GRP"
            )
        )
        save_alert(
            make_alert_record(
                alert_id="alert-003", skymiles_number="3333333", itinerary_ref="DL-OTH"
            )
        )

        alerts = get_alerts_by_itinerary("DL-GRP")

        assert len(alerts) == 2
        assert {a["skymiles_number"] for a in alerts} == {"1111111", "2222222"}

    def test_returns_empty_list_for_unknown_confirmation(self):
        alerts = get_alerts_by_itinerary("DL-NONE")

        assert alerts == []

    def test_raises_client_error_when_index_missing(self):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="NoIndexTable",
            KeySchema=[
                {"AttributeName": "skymiles_number", "KeyType": "HASH"},
                {"AttributeName": "alert_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "skymiles_number", "AttributeType": "S"},
                {"AttributeName": "alert_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        orig_table = alert_store_mod.table
        alert_store_mod.table = ddb.Table("NoIndexTable")

        try:
            with pytest.raises(ClientError):
                get_alerts_by_itinerary("DL-GRP")
        finally:
            alert_store_mod.table = orig_table


class TestGetGroupAlertSummary:
    """get_group_alert_summary groups a booking's alerts by skymiles_number."""

    def test_groups_alerts_by_traveler(self):
        save_alert(
            make_alert_record(
                alert_id="alert-001", skymiles_number="1111111", itinerary_ref="DL-GRP"
            )
        )
        save_alert(
            make_alert_record(
                alert_id="alert-002",
                skymiles_number="1111111",
                alert_type=AlertType.VISA,
                itinerary_ref="DL-GRP",
            )
        )
        save_alert(
            make_alert_record(
                alert_id="alert-003", skymiles_number="2222222", itinerary_ref="DL-GRP"
            )
        )

        summary = get_group_alert_summary("DL-GRP")

        assert set(summary.keys()) == {"1111111", "2222222"}
        assert len(summary["1111111"]) == 2
        assert len(summary["2222222"]) == 1

    def test_returns_empty_dict_when_no_alerts(self):
        summary = get_group_alert_summary("DL-NONE")

        assert summary == {}


class TestExpireStaleAlerts:
    """expire_stale_alerts marks past-TTL actionable alerts as EXPIRED."""

    @staticmethod
    def _epoch(delta_days: int) -> int:
        return int((datetime.utcnow() + timedelta(days=delta_days)).timestamp())

    def test_expires_past_ttl_active_alert(self):
        save_alert(make_alert_record(alert_id="alert-001", ttl=self._epoch(-1)))

        count = expire_stale_alerts("9999999")

        assert count == 1
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert item["status"] == "EXPIRED"

    def test_expires_past_ttl_acknowledged_alert(self):
        save_alert(
            make_alert_record(
                alert_id="alert-001",
                status=AlertStatus.ACKNOWLEDGED,
                ttl=self._epoch(-1),
            )
        )

        count = expire_stale_alerts("9999999")

        assert count == 1

    def test_skips_alert_without_ttl(self):
        save_alert(make_alert_record(alert_id="alert-001"))

        count = expire_stale_alerts("9999999")

        assert count == 0
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert item["status"] == "ACTIVE"

    def test_skips_alert_with_future_ttl(self):
        save_alert(make_alert_record(alert_id="alert-001", ttl=self._epoch(365)))

        count = expire_stale_alerts("9999999")

        assert count == 0

    def test_skips_past_ttl_alert_with_resolved_status(self):
        save_alert(
            make_alert_record(
                alert_id="alert-001",
                status=AlertStatus.RESOLVED,
                ttl=self._epoch(-1),
            )
        )

        count = expire_stale_alerts("9999999")

        assert count == 0
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert item["status"] == "RESOLVED"
