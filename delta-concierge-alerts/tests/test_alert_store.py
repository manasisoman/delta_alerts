"""Tests for the DynamoDB-backed alert store.

Covers persistence of optional fields, member/active/itinerary queries,
status transitions (acknowledge, resolve, bulk resolve), group summaries
via the itinerary-ref-index GSI, and TTL-based expiry.
"""

from datetime import datetime

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType


FUTURE_TTL = int(datetime(2031, 1, 1).timestamp())
PAST_TTL = int(datetime(2020, 1, 1).timestamp())


# ------------------------------------------------------------------
# Fixtures and helpers
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


def _make_record(**overrides) -> AlertRecord:
    """Build a complete AlertRecord with sensible defaults."""
    defaults = dict(
        alert_id="alert-001",
        skymiles_number="9999999",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport expires before 2030-09-01"],
        created_at="2030-01-01T00:00:00",
        itinerary_ref="DL-TEST",
    )
    defaults.update(overrides)
    return AlertRecord(**defaults)


def _get_item(skymiles_number: str = "9999999", alert_id: str = "alert-001") -> dict:
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


# ------------------------------------------------------------------
# save_alert
# ------------------------------------------------------------------

class TestSaveAlert:
    """save_alert stores required fields and only the optional fields that are set."""

    def test_saves_required_fields_without_optional_attributes(self):
        record = _make_record()

        alert_store_mod.save_alert(record)

        item = _get_item()
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        assert item["reasons"] == ["Passport expires before 2030-09-01"]
        assert item["created_at"] == "2030-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == "ACTIVE"
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_saves_ttl_when_set(self):
        record = _make_record(ttl=FUTURE_TTL)

        alert_store_mod.save_alert(record)

        assert int(_get_item()["ttl"]) == FUTURE_TTL

    def test_saves_resolved_at_when_set(self):
        record = _make_record(resolved_at="2030-02-01T00:00:00")

        alert_store_mod.save_alert(record)

        assert _get_item()["resolved_at"] == "2030-02-01T00:00:00"

    def test_saves_resolution_when_set(self):
        record = _make_record(resolution="Passport renewed")

        alert_store_mod.save_alert(record)

        assert _get_item()["resolution"] == "Passport renewed"

    def test_saves_non_default_status(self):
        record = _make_record(status=AlertStatus.RESOLVED)

        alert_store_mod.save_alert(record)

        assert _get_item()["status"] == "RESOLVED"


# ------------------------------------------------------------------
# get_alerts_by_member / get_active_alerts
# ------------------------------------------------------------------

class TestGetAlertsByMember:
    """get_alerts_by_member returns every alert for one partition key only."""

    def test_returns_all_alerts_for_member_regardless_of_status(self):
        alert_store_mod.save_alert(_make_record(alert_id="a1"))
        alert_store_mod.save_alert(_make_record(alert_id="a2", status=AlertStatus.RESOLVED))
        alert_store_mod.save_alert(_make_record(alert_id="a3", skymiles_number="1111111"))

        alerts = alert_store_mod.get_alerts_by_member("9999999")

        assert sorted(a["alert_id"] for a in alerts) == ["a1", "a2"]

    def test_returns_empty_list_for_unknown_member(self):
        alerts = alert_store_mod.get_alerts_by_member("0000000")

        assert alerts == []


class TestGetActiveAlerts:
    """get_active_alerts returns only ACTIVE and ACKNOWLEDGED alerts."""

    def test_filters_out_resolved_and_expired(self):
        alert_store_mod.save_alert(_make_record(alert_id="active"))
        alert_store_mod.save_alert(_make_record(alert_id="ack", status=AlertStatus.ACKNOWLEDGED))
        alert_store_mod.save_alert(_make_record(alert_id="resolved", status=AlertStatus.RESOLVED))
        alert_store_mod.save_alert(_make_record(alert_id="expired", status=AlertStatus.EXPIRED))

        alerts = alert_store_mod.get_active_alerts("9999999")

        assert sorted(a["alert_id"] for a in alerts) == ["ack", "active"]

    def test_returns_empty_list_when_no_actionable_alerts(self):
        alert_store_mod.save_alert(_make_record(status=AlertStatus.RESOLVED))

        alerts = alert_store_mod.get_active_alerts("9999999")

        assert alerts == []


# ------------------------------------------------------------------
# acknowledge_alert / resolve_alert
# ------------------------------------------------------------------

class TestAcknowledgeAlert:
    """acknowledge_alert transitions the alert to ACKNOWLEDGED."""

    def test_sets_status_acknowledged(self):
        alert_store_mod.save_alert(_make_record())

        alert_store_mod.acknowledge_alert("9999999", "alert-001")

        assert _get_item()["status"] == "ACKNOWLEDGED"


class TestResolveAlert:
    """resolve_alert sets RESOLVED status, resolution text and a resolved_at timestamp."""

    def test_sets_status_resolution_and_resolved_at(self):
        alert_store_mod.save_alert(_make_record())

        alert_store_mod.resolve_alert("9999999", "alert-001", "Passport renewed")

        item = _get_item()
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Passport renewed"
        datetime.fromisoformat(item["resolved_at"])


# ------------------------------------------------------------------
# resolve_alerts_for_itinerary
# ------------------------------------------------------------------

class TestResolveAlertsForItinerary:
    """Only actionable alerts matching the itinerary are resolved; the count is returned."""

    def test_resolves_active_and_acknowledged_for_matching_itinerary(self):
        alert_store_mod.save_alert(_make_record(alert_id="a1"))
        alert_store_mod.save_alert(_make_record(alert_id="a2", status=AlertStatus.ACKNOWLEDGED))

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "9999999", "DL-TEST", "Itinerary changed"
        )

        assert count == 2
        for alert_id in ("a1", "a2"):
            item = _get_item(alert_id=alert_id)
            assert item["status"] == "RESOLVED"
            assert item["resolution"] == "Itinerary changed"

    def test_skips_alerts_for_other_itineraries(self):
        alert_store_mod.save_alert(_make_record(alert_id="other", itinerary_ref="DL-OTHER"))

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "9999999", "DL-TEST", "Itinerary changed"
        )

        assert count == 0
        assert _get_item(alert_id="other")["status"] == "ACTIVE"

    def test_skips_already_resolved_alerts(self):
        alert_store_mod.save_alert(
            _make_record(status=AlertStatus.RESOLVED, resolution="Original fix")
        )

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "9999999", "DL-TEST", "Itinerary changed"
        )

        assert count == 0
        assert _get_item()["resolution"] == "Original fix"

    def test_returns_zero_when_member_has_no_alerts(self):
        count = alert_store_mod.resolve_alerts_for_itinerary(
            "0000000", "DL-TEST", "Itinerary changed"
        )

        assert count == 0


# ------------------------------------------------------------------
# get_alerts_by_itinerary / get_group_alert_summary
# ------------------------------------------------------------------

class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the GSI across all travelers on a booking."""

    def test_returns_alerts_across_travelers_for_booking(self):
        alert_store_mod.save_alert(_make_record(alert_id="a1", skymiles_number="1111111"))
        alert_store_mod.save_alert(_make_record(alert_id="a2", skymiles_number="2222222"))
        alert_store_mod.save_alert(_make_record(alert_id="a3", itinerary_ref="DL-OTHER"))

        alerts = alert_store_mod.get_alerts_by_itinerary("DL-TEST")

        assert sorted(a["alert_id"] for a in alerts) == ["a1", "a2"]

    def test_returns_empty_list_for_unknown_booking(self):
        alerts = alert_store_mod.get_alerts_by_itinerary("DL-NONE")

        assert alerts == []


class TestGetGroupAlertSummary:
    """get_group_alert_summary groups a booking's alerts by skymiles_number."""

    def test_groups_alerts_by_traveler(self):
        alert_store_mod.save_alert(_make_record(alert_id="a1", skymiles_number="1111111"))
        alert_store_mod.save_alert(_make_record(alert_id="a2", skymiles_number="1111111"))
        alert_store_mod.save_alert(_make_record(alert_id="b1", skymiles_number="2222222"))

        summary = alert_store_mod.get_group_alert_summary("DL-TEST")

        assert set(summary) == {"1111111", "2222222"}
        assert sorted(a["alert_id"] for a in summary["1111111"]) == ["a1", "a2"]
        assert [a["alert_id"] for a in summary["2222222"]] == ["b1"]

    def test_returns_empty_dict_when_booking_has_no_alerts(self):
        summary = alert_store_mod.get_group_alert_summary("DL-NONE")

        assert summary == {}


# ------------------------------------------------------------------
# expire_stale_alerts
# ------------------------------------------------------------------

class TestExpireStaleAlerts:
    """Only actionable alerts whose TTL has passed are marked EXPIRED."""

    def test_expires_active_alert_past_ttl(self):
        alert_store_mod.save_alert(_make_record(ttl=PAST_TTL))

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item()["status"] == "EXPIRED"

    def test_expires_acknowledged_alert_past_ttl(self):
        alert_store_mod.save_alert(_make_record(ttl=PAST_TTL, status=AlertStatus.ACKNOWLEDGED))

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item()["status"] == "EXPIRED"

    def test_skips_alert_without_ttl(self):
        alert_store_mod.save_alert(_make_record())

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item()["status"] == "ACTIVE"

    def test_skips_alert_with_future_ttl(self):
        alert_store_mod.save_alert(_make_record(ttl=FUTURE_TTL))

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item()["status"] == "ACTIVE"

    def test_skips_resolved_alert_past_ttl(self):
        alert_store_mod.save_alert(_make_record(ttl=PAST_TTL, status=AlertStatus.RESOLVED))

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item()["status"] == "RESOLVED"

    def test_counts_only_stale_alerts_in_mixed_set(self):
        alert_store_mod.save_alert(_make_record(alert_id="stale", ttl=PAST_TTL))
        alert_store_mod.save_alert(_make_record(alert_id="fresh", ttl=FUTURE_TTL))
        alert_store_mod.save_alert(_make_record(alert_id="no-ttl"))

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item(alert_id="stale")["status"] == "EXPIRED"
        assert _get_item(alert_id="fresh")["status"] == "ACTIVE"
        assert _get_item(alert_id="no-ttl")["status"] == "ACTIVE"
