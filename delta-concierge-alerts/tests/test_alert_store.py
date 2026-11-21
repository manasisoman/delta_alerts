"""Tests for the DynamoDB-backed alert store.

Covers persistence of optional fields, member/active/itinerary queries,
acknowledge/resolve transitions, itinerary-scoped bulk resolution,
group summaries via the ``itinerary-ref-index`` GSI, and TTL expiry.
"""

import time
from datetime import datetime, timedelta

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto and create ConciergeAlerts with the itinerary-ref-index GSI."""
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
            yield ddb.Table("ConciergeAlerts")
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table


@pytest.fixture
def table(_mock_aws):
    """Expose the moto DynamoDB table."""
    return _mock_aws


def _make_record(**overrides) -> AlertRecord:
    """Build a complete AlertRecord with sensible defaults."""
    defaults = dict(
        alert_id="alert-001",
        skymiles_number="9999999",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport expires before travel"],
        created_at="2030-01-01T00:00:00",
        itinerary_ref="DL-TEST",
    )
    defaults.update(overrides)
    return AlertRecord(**defaults)


def _get_item(table, skymiles_number="9999999", alert_id="alert-001") -> dict:
    return table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


def _epoch(delta: timedelta) -> int:
    return int(time.time() + delta.total_seconds())


# ------------------------------------------------------------------
# save_alert
# ------------------------------------------------------------------

class TestSaveAlert:
    """save_alert persists required fields and only the optional fields that are set."""

    def test_required_fields_persisted(self, table):
        record = _make_record()

        alert_store_mod.save_alert(record)

        item = _get_item(table)
        assert item["skymiles_number"] == "9999999"
        assert item["alert_id"] == "alert-001"
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        assert item["reasons"] == ["Passport expires before travel"]
        assert item["created_at"] == "2030-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == "ACTIVE"

    def test_optional_fields_omitted_when_none(self, table):
        record = _make_record()

        alert_store_mod.save_alert(record)

        item = _get_item(table)
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_ttl_persisted_when_set(self, table):
        record = _make_record(ttl=1893456000)

        alert_store_mod.save_alert(record)

        assert int(_get_item(table)["ttl"]) == 1893456000

    def test_resolved_at_persisted_when_set(self, table):
        record = _make_record(resolved_at="2030-02-01T00:00:00")

        alert_store_mod.save_alert(record)

        assert _get_item(table)["resolved_at"] == "2030-02-01T00:00:00"

    def test_resolution_persisted_when_set(self, table):
        record = _make_record(resolution="Passport renewed")

        alert_store_mod.save_alert(record)

        assert _get_item(table)["resolution"] == "Passport renewed"

    def test_enum_values_serialised(self, table):
        record = _make_record(
            alert_type=AlertType.VISA,
            severity=AlertSeverity.WARNING,
            status=AlertStatus.ACKNOWLEDGED,
        )

        alert_store_mod.save_alert(record)

        item = _get_item(table)
        assert item["alert_type"] == "VISA"
        assert item["severity"] == "WARNING"
        assert item["status"] == "ACKNOWLEDGED"


# ------------------------------------------------------------------
# get_alerts_by_member / get_active_alerts
# ------------------------------------------------------------------

class TestMemberQueries:
    """Member queries return only that member's alerts, optionally filtered by status."""

    def test_get_alerts_by_member_returns_all_statuses(self):
        alert_store_mod.save_alert(_make_record(alert_id="a1"))
        alert_store_mod.save_alert(
            _make_record(alert_id="a2", status=AlertStatus.RESOLVED)
        )

        items = alert_store_mod.get_alerts_by_member("9999999")

        assert sorted(i["alert_id"] for i in items) == ["a1", "a2"]

    def test_get_alerts_by_member_excludes_other_members(self):
        alert_store_mod.save_alert(_make_record(alert_id="a1"))
        alert_store_mod.save_alert(
            _make_record(alert_id="a2", skymiles_number="1111111")
        )

        items = alert_store_mod.get_alerts_by_member("9999999")

        assert [i["alert_id"] for i in items] == ["a1"]

    def test_get_alerts_by_member_empty(self):
        assert alert_store_mod.get_alerts_by_member("0000000") == []

    def test_get_active_alerts_filters_to_active_and_acknowledged(self):
        for alert_id, status in [
            ("active", AlertStatus.ACTIVE),
            ("ack", AlertStatus.ACKNOWLEDGED),
            ("resolved", AlertStatus.RESOLVED),
            ("expired", AlertStatus.EXPIRED),
        ]:
            alert_store_mod.save_alert(_make_record(alert_id=alert_id, status=status))

        items = alert_store_mod.get_active_alerts("9999999")

        assert sorted(i["alert_id"] for i in items) == ["ack", "active"]

    def test_get_active_alerts_empty(self):
        alert_store_mod.save_alert(_make_record(status=AlertStatus.RESOLVED))

        assert alert_store_mod.get_active_alerts("9999999") == []


# ------------------------------------------------------------------
# acknowledge_alert / resolve_alert
# ------------------------------------------------------------------

class TestStatusTransitions:
    """acknowledge_alert and resolve_alert update status and resolution metadata."""

    def test_acknowledge_sets_status(self, table):
        alert_store_mod.save_alert(_make_record())

        alert_store_mod.acknowledge_alert("9999999", "alert-001")

        item = _get_item(table)
        assert item["status"] == "ACKNOWLEDGED"
        assert "resolved_at" not in item

    def test_resolve_sets_status_resolution_and_timestamp(self, table):
        alert_store_mod.save_alert(_make_record())

        alert_store_mod.resolve_alert("9999999", "alert-001", "Passport renewed")

        item = _get_item(table)
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Passport renewed"
        datetime.fromisoformat(item["resolved_at"])


# ------------------------------------------------------------------
# resolve_alerts_for_itinerary
# ------------------------------------------------------------------

class TestResolveAlertsForItinerary:
    """Only ACTIVE/ACKNOWLEDGED alerts on the matching itinerary are resolved."""

    def test_resolves_active_and_acknowledged_on_itinerary(self, table):
        alert_store_mod.save_alert(_make_record(alert_id="a1"))
        alert_store_mod.save_alert(
            _make_record(alert_id="a2", status=AlertStatus.ACKNOWLEDGED)
        )

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "9999999", "DL-TEST", "Itinerary changed"
        )

        assert count == 2
        for alert_id in ("a1", "a2"):
            item = _get_item(table, alert_id=alert_id)
            assert item["status"] == "RESOLVED"
            assert item["resolution"] == "Itinerary changed"

    def test_other_itinerary_untouched(self, table):
        alert_store_mod.save_alert(_make_record(alert_id="a1", itinerary_ref="DL-OTHER"))

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "9999999", "DL-TEST", "Itinerary changed"
        )

        assert count == 0
        assert _get_item(table, alert_id="a1")["status"] == "ACTIVE"

    def test_already_resolved_or_expired_untouched(self, table):
        alert_store_mod.save_alert(
            _make_record(
                alert_id="a1", status=AlertStatus.RESOLVED, resolution="Original"
            )
        )
        alert_store_mod.save_alert(_make_record(alert_id="a2", status=AlertStatus.EXPIRED))

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "9999999", "DL-TEST", "Itinerary changed"
        )

        assert count == 0
        assert _get_item(table, alert_id="a1")["resolution"] == "Original"
        assert _get_item(table, alert_id="a2")["status"] == "EXPIRED"

    def test_other_member_untouched(self, table):
        alert_store_mod.save_alert(_make_record(alert_id="a1", skymiles_number="1111111"))

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "9999999", "DL-TEST", "Itinerary changed"
        )

        assert count == 0
        assert _get_item(table, skymiles_number="1111111", alert_id="a1")["status"] == "ACTIVE"

    def test_no_alerts_returns_zero(self):
        count = alert_store_mod.resolve_alerts_for_itinerary(
            "9999999", "DL-TEST", "Itinerary changed"
        )

        assert count == 0


# ------------------------------------------------------------------
# get_alerts_by_itinerary / get_group_alert_summary
# ------------------------------------------------------------------

class TestItineraryQueries:
    """Itinerary queries span all travelers on a booking via the GSI."""

    def test_get_alerts_by_itinerary_across_members(self):
        alert_store_mod.save_alert(_make_record(alert_id="a1", skymiles_number="1111111"))
        alert_store_mod.save_alert(_make_record(alert_id="a2", skymiles_number="2222222"))
        alert_store_mod.save_alert(
            _make_record(alert_id="a3", skymiles_number="1111111", itinerary_ref="DL-OTHER")
        )

        items = alert_store_mod.get_alerts_by_itinerary("DL-TEST")

        assert sorted(i["alert_id"] for i in items) == ["a1", "a2"]

    def test_get_alerts_by_itinerary_empty(self):
        assert alert_store_mod.get_alerts_by_itinerary("DL-NONE") == []

    def test_group_summary_groups_by_member(self):
        alert_store_mod.save_alert(_make_record(alert_id="a1", skymiles_number="1111111"))
        alert_store_mod.save_alert(
            _make_record(alert_id="a2", skymiles_number="1111111", alert_type=AlertType.VISA)
        )
        alert_store_mod.save_alert(_make_record(alert_id="b1", skymiles_number="2222222"))

        summary = alert_store_mod.get_group_alert_summary("DL-TEST")

        assert set(summary) == {"1111111", "2222222"}
        assert sorted(a["alert_id"] for a in summary["1111111"]) == ["a1", "a2"]
        assert [a["alert_id"] for a in summary["2222222"]] == ["b1"]

    def test_group_summary_empty(self):
        assert alert_store_mod.get_group_alert_summary("DL-NONE") == {}


# ------------------------------------------------------------------
# expire_stale_alerts
# ------------------------------------------------------------------

class TestExpireStaleAlerts:
    """Only ACTIVE/ACKNOWLEDGED alerts whose TTL has passed become EXPIRED."""

    def test_past_ttl_active_expired(self, table):
        alert_store_mod.save_alert(_make_record(ttl=_epoch(timedelta(days=-1))))

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item(table)["status"] == "EXPIRED"

    def test_past_ttl_acknowledged_expired(self, table):
        alert_store_mod.save_alert(
            _make_record(status=AlertStatus.ACKNOWLEDGED, ttl=_epoch(timedelta(days=-1)))
        )

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item(table)["status"] == "EXPIRED"

    def test_future_ttl_untouched(self, table):
        alert_store_mod.save_alert(_make_record(ttl=_epoch(timedelta(days=30))))

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item(table)["status"] == "ACTIVE"

    def test_missing_ttl_skipped(self, table):
        alert_store_mod.save_alert(_make_record())

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item(table)["status"] == "ACTIVE"

    def test_past_ttl_resolved_untouched(self, table):
        alert_store_mod.save_alert(
            _make_record(status=AlertStatus.RESOLVED, ttl=_epoch(timedelta(days=-1)))
        )

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item(table)["status"] == "RESOLVED"

    def test_mixed_alerts_counts_only_expired(self, table):
        alert_store_mod.save_alert(_make_record(alert_id="stale", ttl=_epoch(timedelta(days=-1))))
        alert_store_mod.save_alert(_make_record(alert_id="fresh", ttl=_epoch(timedelta(days=30))))
        alert_store_mod.save_alert(_make_record(alert_id="no-ttl"))

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item(table, alert_id="stale")["status"] == "EXPIRED"
        assert _get_item(table, alert_id="fresh")["status"] == "ACTIVE"
        assert _get_item(table, alert_id="no-ttl")["status"] == "ACTIVE"
