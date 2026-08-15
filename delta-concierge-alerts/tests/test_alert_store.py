"""Tests for DynamoDB-backed alert persistence in src/services/alert_store.py."""

from datetime import datetime, timedelta

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


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto and point the module-level DynamoDB table at a mock table."""
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
    """Expose the moto-backed DynamoDB table."""
    return _mock_aws


def make_alert(
    alert_id: str = "alert-1",
    skymiles_number: str = "9999999",
    alert_type: AlertType = AlertType.PASSPORT,
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    reasons: list[str] | None = None,
    created_at: str = "2030-01-01T00:00:00",
    itinerary_ref: str = "DL-TEST",
    status: AlertStatus = AlertStatus.ACTIVE,
    resolved_at: str | None = None,
    resolution: str | None = None,
    ttl: int | None = None,
) -> AlertRecord:
    """Build an AlertRecord with sensible defaults."""
    return AlertRecord(
        alert_id=alert_id,
        skymiles_number=skymiles_number,
        alert_type=alert_type,
        severity=severity,
        reasons=reasons if reasons is not None else ["Passport expires before travel"],
        created_at=created_at,
        itinerary_ref=itinerary_ref,
        status=status,
        resolved_at=resolved_at,
        resolution=resolution,
        ttl=ttl,
    )


def _future_epoch() -> int:
    return int((datetime.utcnow() + timedelta(days=30)).timestamp())


def _past_epoch() -> int:
    return int((datetime.utcnow() - timedelta(days=30)).timestamp())


class TestSaveAlert:
    """save_alert writes required attributes and only includes optionals when set."""

    def test_required_attributes_persisted(self, table):
        save_alert(make_alert())

        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-1"}
        )["Item"]
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        assert item["status"] == "ACTIVE"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["reasons"] == ["Passport expires before travel"]

    def test_optional_attributes_omitted_when_none(self, table):
        save_alert(make_alert())

        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-1"}
        )["Item"]
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_ttl_persisted_when_set(self, table):
        ttl = _future_epoch()
        save_alert(make_alert(ttl=ttl))

        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-1"}
        )["Item"]
        assert int(item["ttl"]) == ttl

    def test_resolution_fields_persisted_when_set(self, table):
        save_alert(
            make_alert(
                status=AlertStatus.RESOLVED,
                resolved_at="2030-02-01T00:00:00",
                resolution="Visa renewed",
            )
        )

        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-1"}
        )["Item"]
        assert item["status"] == "RESOLVED"
        assert item["resolved_at"] == "2030-02-01T00:00:00"
        assert item["resolution"] == "Visa renewed"


class TestGetAlertsByMember:
    """get_alerts_by_member returns only the queried member's alerts."""

    def test_returns_all_alerts_for_member(self):
        save_alert(make_alert(alert_id="alert-1"))
        save_alert(make_alert(alert_id="alert-2", alert_type=AlertType.VISA))
        save_alert(make_alert(alert_id="alert-3", skymiles_number="1111111"))

        alerts = get_alerts_by_member("9999999")

        assert {a["alert_id"] for a in alerts} == {"alert-1", "alert-2"}

    def test_returns_empty_list_for_unknown_member(self):
        assert get_alerts_by_member("0000000") == []


class TestAcknowledgeAlert:
    """acknowledge_alert flips status to ACKNOWLEDGED without touching siblings."""

    def test_status_updated(self, table):
        save_alert(make_alert(alert_id="alert-1"))
        save_alert(make_alert(alert_id="alert-2"))

        acknowledge_alert("9999999", "alert-1")

        items = {i["alert_id"]: i for i in table.scan()["Items"]}
        assert items["alert-1"]["status"] == "ACKNOWLEDGED"
        assert items["alert-2"]["status"] == "ACTIVE"


class TestResolveAlert:
    """resolve_alert sets RESOLVED status plus resolution metadata."""

    def test_status_and_resolution_recorded(self, table):
        save_alert(make_alert(alert_id="alert-1"))

        resolve_alert("9999999", "alert-1", "Passport renewed")

        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-1"}
        )["Item"]
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Passport renewed"
        assert item["resolved_at"]


class TestGetActiveAlerts:
    """get_active_alerts filters to ACTIVE and ACKNOWLEDGED alerts only."""

    def test_active_and_acknowledged_returned(self):
        save_alert(make_alert(alert_id="active", status=AlertStatus.ACTIVE))
        save_alert(make_alert(alert_id="ack", status=AlertStatus.ACKNOWLEDGED))
        save_alert(make_alert(alert_id="resolved", status=AlertStatus.RESOLVED))
        save_alert(make_alert(alert_id="expired", status=AlertStatus.EXPIRED))

        alerts = get_active_alerts("9999999")

        assert {a["alert_id"] for a in alerts} == {"active", "ack"}

    def test_empty_when_nothing_actionable(self):
        save_alert(make_alert(alert_id="resolved", status=AlertStatus.RESOLVED))

        assert get_active_alerts("9999999") == []


class TestResolveAlertsForItinerary:
    """Itinerary changes resolve that booking's actionable alerts only."""

    def test_resolves_matching_itinerary_alerts(self, table):
        save_alert(make_alert(alert_id="a1", itinerary_ref="DL-CHANGED"))
        save_alert(
            make_alert(
                alert_id="a2",
                itinerary_ref="DL-CHANGED",
                status=AlertStatus.ACKNOWLEDGED,
                alert_type=AlertType.VISA,
            )
        )
        save_alert(make_alert(alert_id="a3", itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary(
            "9999999", "DL-CHANGED", "Auto-resolved: itinerary changed"
        )

        assert count == 2
        items = {i["alert_id"]: i for i in table.scan()["Items"]}
        assert items["a1"]["status"] == "RESOLVED"
        assert items["a1"]["resolution"] == "Auto-resolved: itinerary changed"
        assert items["a2"]["status"] == "RESOLVED"
        assert items["a3"]["status"] == "ACTIVE"

    def test_already_resolved_alerts_skipped(self, table):
        save_alert(
            make_alert(
                alert_id="a1",
                itinerary_ref="DL-CHANGED",
                status=AlertStatus.RESOLVED,
                resolution="Handled by agent",
            )
        )

        count = resolve_alerts_for_itinerary("9999999", "DL-CHANGED", "Re-evaluating")

        assert count == 0
        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a1"}
        )["Item"]
        assert item["resolution"] == "Handled by agent"

    def test_returns_zero_when_no_alerts_match(self):
        save_alert(make_alert(alert_id="a1", itinerary_ref="DL-OTHER"))

        assert resolve_alerts_for_itinerary("9999999", "DL-MISSING", "Changed") == 0


class TestGetAlertsByItinerary:
    """The itinerary-ref GSI returns alerts across all travelers on a booking."""

    def test_returns_alerts_for_all_travelers(self):
        save_alert(make_alert(alert_id="a1", skymiles_number="1111111"))
        save_alert(make_alert(alert_id="a2", skymiles_number="2222222"))
        save_alert(make_alert(alert_id="a3", itinerary_ref="DL-OTHER"))

        alerts = get_alerts_by_itinerary("DL-TEST")

        assert {(a["skymiles_number"], a["alert_id"]) for a in alerts} == {
            ("1111111", "a1"),
            ("2222222", "a2"),
        }

    def test_empty_for_unknown_itinerary(self):
        assert get_alerts_by_itinerary("DL-NONE") == []


class TestGetGroupAlertSummary:
    """Group summaries bucket a booking's alerts by traveler."""

    def test_alerts_grouped_by_skymiles_number(self):
        save_alert(make_alert(alert_id="a1", skymiles_number="1111111"))
        save_alert(
            make_alert(
                alert_id="a2", skymiles_number="1111111", alert_type=AlertType.VISA
            )
        )
        save_alert(make_alert(alert_id="a3", skymiles_number="2222222"))

        grouped = get_group_alert_summary("DL-TEST")

        assert set(grouped) == {"1111111", "2222222"}
        assert {a["alert_id"] for a in grouped["1111111"]} == {"a1", "a2"}
        assert len(grouped["2222222"]) == 1

    def test_empty_dict_when_no_alerts(self):
        assert get_group_alert_summary("DL-NONE") == {}


class TestExpireStaleAlerts:
    """Only past-TTL actionable alerts transition to EXPIRED."""

    def test_past_ttl_actionable_alerts_expired(self, table):
        save_alert(make_alert(alert_id="stale-active", ttl=_past_epoch()))
        save_alert(
            make_alert(
                alert_id="stale-ack",
                status=AlertStatus.ACKNOWLEDGED,
                ttl=_past_epoch(),
            )
        )

        count = expire_stale_alerts("9999999")

        assert count == 2
        items = {i["alert_id"]: i for i in table.scan()["Items"]}
        assert items["stale-active"]["status"] == "EXPIRED"
        assert items["stale-ack"]["status"] == "EXPIRED"

    def test_future_ttl_untouched(self, table):
        save_alert(make_alert(alert_id="fresh", ttl=_future_epoch()))

        count = expire_stale_alerts("9999999")

        assert count == 0
        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "fresh"}
        )["Item"]
        assert item["status"] == "ACTIVE"

    def test_alerts_without_ttl_skipped(self, table):
        save_alert(make_alert(alert_id="no-ttl"))

        count = expire_stale_alerts("9999999")

        assert count == 0
        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "no-ttl"}
        )["Item"]
        assert item["status"] == "ACTIVE"

    def test_resolved_alerts_not_expired(self, table):
        save_alert(
            make_alert(
                alert_id="resolved",
                status=AlertStatus.RESOLVED,
                ttl=_past_epoch(),
            )
        )

        count = expire_stale_alerts("9999999")

        assert count == 0
        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "resolved"}
        )["Item"]
        assert item["status"] == "RESOLVED"
