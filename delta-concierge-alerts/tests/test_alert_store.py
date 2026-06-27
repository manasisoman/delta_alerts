"""Tests for the DynamoDB-backed alert persistence service."""

import time

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType
from src.services.alert_store import (
    expire_stale_alerts,
    get_alerts_by_itinerary,
    get_alerts_by_member,
    get_group_alert_summary,
    resolve_alerts_for_itinerary,
    save_alert,
)


def _base_alert(**overrides) -> AlertRecord:
    """Build a valid AlertRecord with sensible defaults."""
    defaults = dict(
        alert_id="alert-001",
        skymiles_number="9999999",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport expired"],
        created_at="2026-01-01T00:00:00",
        itinerary_ref="DL-TEST",
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


class TestSaveAlert:
    """Verify save_alert handles optional fields (ttl, resolved_at, resolution)."""

    def test_save_with_ttl(self):
        """Item in DynamoDB includes ttl when AlertRecord.ttl is set."""
        future_epoch = int(time.time()) + 86400
        record = _base_alert(ttl=future_epoch)

        save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert int(item["ttl"]) == future_epoch

    def test_save_without_ttl(self):
        """Item in DynamoDB has no ttl key when AlertRecord.ttl is None."""
        record = _base_alert(ttl=None)

        save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert "ttl" not in item

    def test_save_with_resolved_at_and_resolution(self):
        """Item contains resolved_at and resolution when both are set."""
        record = _base_alert(
            status=AlertStatus.RESOLVED,
            resolved_at="2026-06-01T12:00:00",
            resolution="Passport renewed",
        )

        save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert item["resolved_at"] == "2026-06-01T12:00:00"
        assert item["resolution"] == "Passport renewed"

    def test_save_without_resolved_at_and_resolution(self):
        """Item omits resolved_at and resolution when both are None."""
        record = _base_alert(resolved_at=None, resolution=None)

        save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert "resolved_at" not in item
        assert "resolution" not in item


class TestGetAlertsByMember:
    """Verify get_alerts_by_member returns all alerts for a partition key."""

    def test_returns_all_alerts_for_member(self):
        """Multiple alerts under the same skymiles_number are all returned."""
        save_alert(_base_alert(alert_id="alert-001"))
        save_alert(_base_alert(alert_id="alert-002", severity=AlertSeverity.WARNING))
        save_alert(_base_alert(alert_id="alert-003", alert_type=AlertType.VISA))

        results = get_alerts_by_member("9999999")

        assert len(results) == 3
        returned_ids = {r["alert_id"] for r in results}
        assert returned_ids == {"alert-001", "alert-002", "alert-003"}

    def test_returns_empty_for_unknown_member(self):
        """No results for a skymiles_number with no alerts."""
        results = get_alerts_by_member("0000000")

        assert results == []


class TestResolveAlertsForItinerary:
    """Verify resolve_alerts_for_itinerary resolves matching active alerts."""

    def test_resolves_active_alerts_for_itinerary(self):
        """Active alerts matching the confirmation_number are resolved."""
        save_alert(_base_alert(alert_id="alert-001", itinerary_ref="DL-ABC"))
        save_alert(_base_alert(alert_id="alert-002", itinerary_ref="DL-ABC"))
        save_alert(_base_alert(alert_id="alert-003", itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary(
            "9999999", "DL-ABC", "Itinerary changed"
        )

        assert count == 2
        items = get_alerts_by_member("9999999")
        for item in items:
            if item["itinerary_ref"] == "DL-ABC":
                assert item["status"] == AlertStatus.RESOLVED.value
            else:
                assert item["status"] == AlertStatus.ACTIVE.value

    def test_resolves_acknowledged_alerts(self):
        """Acknowledged alerts matching the itinerary are also resolved."""
        save_alert(
            _base_alert(
                alert_id="alert-ack",
                itinerary_ref="DL-ABC",
                status=AlertStatus.ACKNOWLEDGED,
            )
        )

        count = resolve_alerts_for_itinerary(
            "9999999", "DL-ABC", "Re-evaluated"
        )

        assert count == 1
        items = get_alerts_by_member("9999999")
        assert items[0]["status"] == AlertStatus.RESOLVED.value

    def test_skips_already_resolved_alerts(self):
        """Already-resolved alerts are not counted or re-resolved."""
        save_alert(
            _base_alert(
                alert_id="alert-resolved",
                itinerary_ref="DL-ABC",
                status=AlertStatus.RESOLVED,
            )
        )

        count = resolve_alerts_for_itinerary(
            "9999999", "DL-ABC", "Should skip"
        )

        assert count == 0

    def test_returns_zero_when_no_match(self):
        """Returns 0 when no alerts match the confirmation_number."""
        save_alert(_base_alert(alert_id="alert-001", itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary(
            "9999999", "DL-NOMATCH", "Nothing to resolve"
        )

        assert count == 0


class TestGetAlertsByItinerary:
    """Verify get_alerts_by_itinerary queries the GSI correctly."""

    def test_returns_alerts_by_itinerary_ref(self):
        """Alerts sharing an itinerary_ref are returned via the GSI."""
        save_alert(_base_alert(alert_id="alert-001", itinerary_ref="DL-GROUP"))
        save_alert(
            _base_alert(
                alert_id="alert-002",
                skymiles_number="8888888",
                itinerary_ref="DL-GROUP",
            )
        )
        save_alert(_base_alert(alert_id="alert-003", itinerary_ref="DL-OTHER"))

        results = get_alerts_by_itinerary("DL-GROUP")

        assert len(results) == 2
        returned_ids = {r["alert_id"] for r in results}
        assert returned_ids == {"alert-001", "alert-002"}

    def test_returns_empty_for_unknown_itinerary(self):
        """No results for an itinerary_ref with no alerts."""
        results = get_alerts_by_itinerary("DL-NONEXISTENT")

        assert results == []


class TestGetGroupAlertSummary:
    """Verify get_group_alert_summary groups alerts by skymiles_number."""

    def test_groups_by_skymiles_number(self):
        """Alerts from different travelers under the same booking are grouped."""
        save_alert(
            _base_alert(
                alert_id="alert-001",
                skymiles_number="1111111",
                itinerary_ref="DL-FAM",
            )
        )
        save_alert(
            _base_alert(
                alert_id="alert-002",
                skymiles_number="1111111",
                itinerary_ref="DL-FAM",
            )
        )
        save_alert(
            _base_alert(
                alert_id="alert-003",
                skymiles_number="2222222",
                itinerary_ref="DL-FAM",
            )
        )

        summary = get_group_alert_summary("DL-FAM")

        assert set(summary.keys()) == {"1111111", "2222222"}
        assert len(summary["1111111"]) == 2
        assert len(summary["2222222"]) == 1

    def test_returns_empty_dict_for_no_alerts(self):
        """Empty dict when no alerts exist for the confirmation_number."""
        summary = get_group_alert_summary("DL-NONE")

        assert summary == {}


class TestExpireStaleAlerts:
    """Verify expire_stale_alerts transitions past-TTL alerts to EXPIRED."""

    def test_expires_alert_with_past_ttl(self):
        """Active alert with TTL in the past is marked EXPIRED."""
        past_epoch = int(time.time()) - 3600
        save_alert(_base_alert(alert_id="alert-old", ttl=past_epoch))

        count = expire_stale_alerts("9999999")

        assert count == 1
        items = get_alerts_by_member("9999999")
        assert items[0]["status"] == AlertStatus.EXPIRED.value

    def test_does_not_expire_alert_with_future_ttl(self):
        """Active alert with TTL in the future remains unchanged."""
        future_epoch = int(time.time()) + 86400
        save_alert(_base_alert(alert_id="alert-future", ttl=future_epoch))

        count = expire_stale_alerts("9999999")

        assert count == 0
        items = get_alerts_by_member("9999999")
        assert items[0]["status"] == AlertStatus.ACTIVE.value

    def test_skips_alerts_without_ttl(self):
        """Alerts with no ttl field are not expired."""
        save_alert(_base_alert(alert_id="alert-no-ttl", ttl=None))

        count = expire_stale_alerts("9999999")

        assert count == 0
        items = get_alerts_by_member("9999999")
        assert items[0]["status"] == AlertStatus.ACTIVE.value

    def test_does_not_expire_already_resolved(self):
        """Resolved alert with past TTL is not transitioned to EXPIRED."""
        past_epoch = int(time.time()) - 3600
        save_alert(
            _base_alert(
                alert_id="alert-resolved",
                ttl=past_epoch,
                status=AlertStatus.RESOLVED,
            )
        )

        count = expire_stale_alerts("9999999")

        assert count == 0
        items = get_alerts_by_member("9999999")
        assert items[0]["status"] == AlertStatus.RESOLVED.value

    def test_expires_acknowledged_with_past_ttl(self):
        """Acknowledged alert with past TTL is also marked EXPIRED."""
        past_epoch = int(time.time()) - 3600
        save_alert(
            _base_alert(
                alert_id="alert-ack",
                ttl=past_epoch,
                status=AlertStatus.ACKNOWLEDGED,
            )
        )

        count = expire_stale_alerts("9999999")

        assert count == 1
        items = get_alerts_by_member("9999999")
        assert items[0]["status"] == AlertStatus.EXPIRED.value

    def test_mixed_ttl_alerts(self):
        """Only past-TTL active/acknowledged alerts are expired in a batch."""
        past_epoch = int(time.time()) - 3600
        future_epoch = int(time.time()) + 86400

        save_alert(_base_alert(alert_id="alert-past", ttl=past_epoch))
        save_alert(_base_alert(alert_id="alert-future", ttl=future_epoch))
        save_alert(_base_alert(alert_id="alert-none", ttl=None))

        count = expire_stale_alerts("9999999")

        assert count == 1
        items = {i["alert_id"]: i for i in get_alerts_by_member("9999999")}
        assert items["alert-past"]["status"] == AlertStatus.EXPIRED.value
        assert items["alert-future"]["status"] == AlertStatus.ACTIVE.value
        assert items["alert-none"]["status"] == AlertStatus.ACTIVE.value
