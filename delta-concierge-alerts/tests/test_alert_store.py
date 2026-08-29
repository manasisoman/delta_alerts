"""Critical-path tests for DynamoDB-backed alert persistence."""

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
    """Activate moto mocks and set up the alerts table with its GSI for every test."""
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
            yield
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table


def _future_epoch(days: int = 30) -> int:
    """Epoch seconds comfortably in the future."""
    return int((datetime.utcnow() + timedelta(days=days)).timestamp())


def _past_epoch(days: int = 30) -> int:
    """Epoch seconds comfortably in the past."""
    return int((datetime.utcnow() - timedelta(days=days)).timestamp())


def _make_record(
    alert_id: str = "alert-1",
    skymiles_number: str = "1234567890",
    alert_type: AlertType = AlertType.VISA,
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    reasons: list[str] | None = None,
    created_at: str = "2030-01-01T00:00:00",
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
        reasons=reasons if reasons is not None else ["No visa on file for CN"],
        created_at=created_at,
        itinerary_ref=itinerary_ref,
        status=status,
        resolved_at=resolved_at,
        resolution=resolution,
        ttl=ttl,
    )


def _get_item(skymiles_number: str, alert_id: str) -> dict:
    """Read a single item straight from the mocked table."""
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


class TestSaveAlert:
    """save_alert writes required attributes and only sets optional ones when present."""

    def test_required_attributes_persisted(self):
        record = _make_record()

        save_alert(record)

        item = _get_item("1234567890", "alert-1")
        assert item["alert_type"] == "VISA"
        assert item["severity"] == "CRITICAL"
        assert item["status"] == "ACTIVE"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["reasons"] == ["No visa on file for CN"]
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_ttl_persisted_when_set(self):
        ttl = _future_epoch()
        record = _make_record(ttl=ttl)

        save_alert(record)

        assert int(_get_item("1234567890", "alert-1")["ttl"]) == ttl

    def test_resolved_at_persisted_when_set(self):
        record = _make_record(resolved_at="2030-02-01T00:00:00")

        save_alert(record)

        assert _get_item("1234567890", "alert-1")["resolved_at"] == "2030-02-01T00:00:00"

    def test_resolution_persisted_when_set(self):
        record = _make_record(resolution="Visa obtained")

        save_alert(record)

        assert _get_item("1234567890", "alert-1")["resolution"] == "Visa obtained"


class TestGetAlertsByMember:
    """get_alerts_by_member returns only the queried member's alerts."""

    def test_returns_all_alerts_for_member(self):
        save_alert(_make_record(alert_id="alert-1"))
        save_alert(_make_record(alert_id="alert-2", alert_type=AlertType.PASSPORT))

        alerts = get_alerts_by_member("1234567890")

        assert {a["alert_id"] for a in alerts} == {"alert-1", "alert-2"}

    def test_excludes_other_members(self):
        save_alert(_make_record(alert_id="alert-1"))
        save_alert(_make_record(alert_id="alert-2", skymiles_number="9999999999"))

        alerts = get_alerts_by_member("1234567890")

        assert [a["alert_id"] for a in alerts] == ["alert-1"]

    def test_returns_empty_list_for_unknown_member(self):
        alerts = get_alerts_by_member("0000000000")

        assert alerts == []


class TestStatusTransitions:
    """acknowledge_alert and resolve_alert update status and resolution metadata."""

    def test_acknowledge_sets_acknowledged_status(self):
        save_alert(_make_record())

        acknowledge_alert("1234567890", "alert-1")

        assert _get_item("1234567890", "alert-1")["status"] == "ACKNOWLEDGED"

    def test_resolve_sets_resolution_fields(self):
        save_alert(_make_record())

        resolve_alert("1234567890", "alert-1", "Visa obtained")

        item = _get_item("1234567890", "alert-1")
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Visa obtained"
        assert item["resolved_at"]


class TestGetActiveAlerts:
    """get_active_alerts filters out anything no longer actionable."""

    def test_includes_active_and_acknowledged_only(self):
        save_alert(_make_record(alert_id="active"))
        save_alert(_make_record(alert_id="acked", status=AlertStatus.ACKNOWLEDGED))
        save_alert(_make_record(alert_id="resolved", status=AlertStatus.RESOLVED))
        save_alert(_make_record(alert_id="expired", status=AlertStatus.EXPIRED))

        alerts = get_active_alerts("1234567890")

        assert {a["alert_id"] for a in alerts} == {"active", "acked"}


class TestResolveAlertsForItinerary:
    """resolve_alerts_for_itinerary resolves only actionable alerts of one booking."""

    def test_resolves_matching_alerts_and_returns_count(self):
        save_alert(_make_record(alert_id="alert-1"))
        save_alert(_make_record(alert_id="alert-2", status=AlertStatus.ACKNOWLEDGED))

        count = resolve_alerts_for_itinerary(
            "1234567890", "DL-TEST", "Auto-resolved: itinerary changed"
        )

        assert count == 2
        for alert_id in ("alert-1", "alert-2"):
            item = _get_item("1234567890", alert_id)
            assert item["status"] == "RESOLVED"
            assert item["resolution"] == "Auto-resolved: itinerary changed"

    def test_skips_other_itineraries(self):
        save_alert(_make_record(alert_id="alert-1", itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary("1234567890", "DL-TEST", "Changed")

        assert count == 0
        assert _get_item("1234567890", "alert-1")["status"] == "ACTIVE"

    def test_skips_already_resolved_alerts(self):
        save_alert(_make_record(alert_id="alert-1", status=AlertStatus.RESOLVED))

        count = resolve_alerts_for_itinerary("1234567890", "DL-TEST", "Changed")

        assert count == 0


class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the itinerary-ref GSI across travelers."""

    def test_returns_alerts_for_all_travelers_on_booking(self):
        save_alert(_make_record(alert_id="alert-1", skymiles_number="1111111111"))
        save_alert(_make_record(alert_id="alert-2", skymiles_number="2222222222"))
        save_alert(
            _make_record(
                alert_id="alert-3",
                skymiles_number="3333333333",
                itinerary_ref="DL-OTHER",
            )
        )

        alerts = get_alerts_by_itinerary("DL-TEST")

        assert {a["skymiles_number"] for a in alerts} == {"1111111111", "2222222222"}

    def test_returns_empty_list_for_unknown_itinerary(self):
        alerts = get_alerts_by_itinerary("DL-NONE")

        assert alerts == []


class TestGetGroupAlertSummary:
    """get_group_alert_summary groups a booking's alerts by traveler."""

    def test_groups_alerts_by_skymiles_number(self):
        save_alert(_make_record(alert_id="alert-1", skymiles_number="1111111111"))
        save_alert(
            _make_record(
                alert_id="alert-2",
                skymiles_number="1111111111",
                alert_type=AlertType.PASSPORT,
            )
        )
        save_alert(_make_record(alert_id="alert-3", skymiles_number="2222222222"))

        summary = get_group_alert_summary("DL-TEST")

        assert set(summary) == {"1111111111", "2222222222"}
        assert {a["alert_id"] for a in summary["1111111111"]} == {"alert-1", "alert-2"}
        assert len(summary["2222222222"]) == 1

    def test_returns_empty_dict_when_no_alerts(self):
        summary = get_group_alert_summary("DL-NONE")

        assert summary == {}


class TestExpireStaleAlerts:
    """expire_stale_alerts only expires actionable alerts whose TTL has passed."""

    def test_expires_past_ttl_alert(self):
        save_alert(_make_record(alert_id="alert-1", ttl=_past_epoch()))

        count = expire_stale_alerts("1234567890")

        assert count == 1
        assert _get_item("1234567890", "alert-1")["status"] == "EXPIRED"

    def test_expires_acknowledged_alert(self):
        save_alert(
            _make_record(
                alert_id="alert-1",
                status=AlertStatus.ACKNOWLEDGED,
                ttl=_past_epoch(),
            )
        )

        count = expire_stale_alerts("1234567890")

        assert count == 1
        assert _get_item("1234567890", "alert-1")["status"] == "EXPIRED"

    def test_skips_alert_without_ttl(self):
        save_alert(_make_record(alert_id="alert-1"))

        count = expire_stale_alerts("1234567890")

        assert count == 0
        assert _get_item("1234567890", "alert-1")["status"] == "ACTIVE"

    def test_skips_future_ttl_alert(self):
        save_alert(_make_record(alert_id="alert-1", ttl=_future_epoch()))

        count = expire_stale_alerts("1234567890")

        assert count == 0
        assert _get_item("1234567890", "alert-1")["status"] == "ACTIVE"

    def test_skips_resolved_alert_past_ttl(self):
        save_alert(
            _make_record(
                alert_id="alert-1",
                status=AlertStatus.RESOLVED,
                ttl=_past_epoch(),
            )
        )

        count = expire_stale_alerts("1234567890")

        assert count == 0
        assert _get_item("1234567890", "alert-1")["status"] == "RESOLVED"
