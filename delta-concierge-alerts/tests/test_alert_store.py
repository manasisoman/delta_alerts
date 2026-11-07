"""Tests for the DynamoDB-backed alert store.

Covers persistence of optional AlertRecord fields, member and itinerary
queries (including the ``itinerary-ref-index`` GSI), status transitions
(acknowledge, resolve, bulk resolve for an itinerary), group summaries,
and TTL-based expiry of stale alerts.
"""

from datetime import datetime

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType


PAST_TTL = int(datetime(2020, 1, 1).timestamp())
FUTURE_TTL = int(datetime(2030, 1, 1).timestamp())


# ------------------------------------------------------------------
# Fixtures and helpers
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto and create the ConciergeAlerts table with its GSI."""
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
    fields = {
        "alert_id": "alert-001",
        "skymiles_number": "9999999",
        "alert_type": AlertType.PASSPORT,
        "severity": AlertSeverity.WARNING,
        "reasons": ["Passport expires soon"],
        "created_at": "2030-01-01T00:00:00",
        "itinerary_ref": "DL-TEST",
    }
    fields.update(overrides)
    return AlertRecord(**fields)


def _get_item(skymiles_number: str = "9999999", alert_id: str = "alert-001") -> dict:
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


def _ids(items: list[dict]) -> set[str]:
    return {item["alert_id"] for item in items}


# ------------------------------------------------------------------
# save_alert
# ------------------------------------------------------------------

class TestSaveAlert:
    """save_alert persists required fields and only the optional fields that are set."""

    def test_saves_required_fields_with_enum_values(self):
        record = _make_record()

        alert_store_mod.save_alert(record)

        item = _get_item()
        assert item["skymiles_number"] == "9999999"
        assert item["alert_id"] == "alert-001"
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "WARNING"
        assert item["reasons"] == ["Passport expires soon"]
        assert item["created_at"] == "2030-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == "ACTIVE"

    def test_omits_optional_fields_when_none(self):
        record = _make_record()

        alert_store_mod.save_alert(record)

        item = _get_item()
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

        item = _get_item()
        assert item["resolved_at"] == "2030-02-01T00:00:00"
        assert "resolution" not in item

    def test_saves_resolution_when_set(self):
        record = _make_record(resolution="Passport renewed")

        alert_store_mod.save_alert(record)

        item = _get_item()
        assert item["resolution"] == "Passport renewed"
        assert "resolved_at" not in item

    def test_saves_non_default_status(self):
        record = _make_record(status=AlertStatus.RESOLVED)

        alert_store_mod.save_alert(record)

        assert _get_item()["status"] == "RESOLVED"

    def test_overwrites_existing_alert_with_same_key(self):
        alert_store_mod.save_alert(_make_record(severity=AlertSeverity.WARNING))

        alert_store_mod.save_alert(_make_record(severity=AlertSeverity.CRITICAL))

        assert _get_item()["severity"] == "CRITICAL"


# ------------------------------------------------------------------
# get_alerts_by_member / get_active_alerts
# ------------------------------------------------------------------

class TestMemberQueries:
    """Member queries return only that member's alerts, optionally filtered by status."""

    def test_get_alerts_by_member_returns_all_member_alerts(self):
        alert_store_mod.save_alert(_make_record(alert_id="a1"))
        alert_store_mod.save_alert(_make_record(alert_id="a2", status=AlertStatus.RESOLVED))
        alert_store_mod.save_alert(_make_record(alert_id="other", skymiles_number="1111111"))

        result = alert_store_mod.get_alerts_by_member("9999999")

        assert _ids(result) == {"a1", "a2"}

    def test_get_alerts_by_member_returns_empty_for_unknown_member(self):
        alert_store_mod.save_alert(_make_record())

        result = alert_store_mod.get_alerts_by_member("0000000")

        assert result == []

    def test_get_active_alerts_includes_active_and_acknowledged(self):
        alert_store_mod.save_alert(_make_record(alert_id="active", status=AlertStatus.ACTIVE))
        alert_store_mod.save_alert(_make_record(alert_id="ack", status=AlertStatus.ACKNOWLEDGED))
        alert_store_mod.save_alert(_make_record(alert_id="resolved", status=AlertStatus.RESOLVED))
        alert_store_mod.save_alert(_make_record(alert_id="expired", status=AlertStatus.EXPIRED))

        result = alert_store_mod.get_active_alerts("9999999")

        assert _ids(result) == {"active", "ack"}

    def test_get_active_alerts_excludes_other_members(self):
        alert_store_mod.save_alert(_make_record(alert_id="other", skymiles_number="1111111"))

        result = alert_store_mod.get_active_alerts("9999999")

        assert result == []


# ------------------------------------------------------------------
# acknowledge_alert / resolve_alert
# ------------------------------------------------------------------

class TestStatusTransitions:
    """Acknowledge and resolve update status (and resolution metadata) of a single alert."""

    def test_acknowledge_sets_acknowledged_status(self):
        alert_store_mod.save_alert(_make_record())

        alert_store_mod.acknowledge_alert("9999999", "alert-001")

        assert _get_item()["status"] == "ACKNOWLEDGED"

    def test_acknowledge_preserves_other_fields(self):
        alert_store_mod.save_alert(_make_record())

        alert_store_mod.acknowledge_alert("9999999", "alert-001")

        item = _get_item()
        assert item["severity"] == "WARNING"
        assert item["itinerary_ref"] == "DL-TEST"

    def test_resolve_sets_status_resolution_and_timestamp(self):
        alert_store_mod.save_alert(_make_record())
        before = datetime.utcnow()

        alert_store_mod.resolve_alert("9999999", "alert-001", "Visa obtained")

        item = _get_item()
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Visa obtained"
        assert datetime.fromisoformat(item["resolved_at"]) >= before

    def test_resolve_only_affects_target_alert(self):
        alert_store_mod.save_alert(_make_record(alert_id="a1"))
        alert_store_mod.save_alert(_make_record(alert_id="a2"))

        alert_store_mod.resolve_alert("9999999", "a1", "done")

        assert _get_item(alert_id="a1")["status"] == "RESOLVED"
        assert _get_item(alert_id="a2")["status"] == "ACTIVE"


# ------------------------------------------------------------------
# resolve_alerts_for_itinerary
# ------------------------------------------------------------------

class TestResolveAlertsForItinerary:
    """Only the member's ACTIVE/ACKNOWLEDGED alerts on the given booking are resolved."""

    def test_resolves_active_and_acknowledged_for_matching_itinerary(self):
        alert_store_mod.save_alert(_make_record(alert_id="active"))
        alert_store_mod.save_alert(_make_record(alert_id="ack", status=AlertStatus.ACKNOWLEDGED))

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "9999999", "DL-TEST", "Auto-resolved: itinerary changed"
        )

        assert count == 2
        for alert_id in ("active", "ack"):
            item = _get_item(alert_id=alert_id)
            assert item["status"] == "RESOLVED"
            assert item["resolution"] == "Auto-resolved: itinerary changed"

    def test_ignores_alerts_on_other_itineraries(self):
        alert_store_mod.save_alert(_make_record(alert_id="other", itinerary_ref="DL-OTHER"))

        count = alert_store_mod.resolve_alerts_for_itinerary("9999999", "DL-TEST", "x")

        assert count == 0
        assert _get_item(alert_id="other")["status"] == "ACTIVE"

    def test_ignores_already_resolved_or_expired_alerts(self):
        alert_store_mod.save_alert(_make_record(alert_id="resolved", status=AlertStatus.RESOLVED))
        alert_store_mod.save_alert(_make_record(alert_id="expired", status=AlertStatus.EXPIRED))

        count = alert_store_mod.resolve_alerts_for_itinerary("9999999", "DL-TEST", "x")

        assert count == 0
        assert _get_item(alert_id="expired")["status"] == "EXPIRED"
        assert "resolution" not in _get_item(alert_id="resolved")

    def test_ignores_other_members_on_same_itinerary(self):
        alert_store_mod.save_alert(_make_record(alert_id="other", skymiles_number="1111111"))

        count = alert_store_mod.resolve_alerts_for_itinerary("9999999", "DL-TEST", "x")

        assert count == 0
        assert _get_item("1111111", "other")["status"] == "ACTIVE"

    def test_returns_zero_when_member_has_no_alerts(self):
        count = alert_store_mod.resolve_alerts_for_itinerary("9999999", "DL-TEST", "x")

        assert count == 0


# ------------------------------------------------------------------
# get_alerts_by_itinerary / get_group_alert_summary
# ------------------------------------------------------------------

class TestItineraryQueries:
    """Itinerary queries use the GSI to find alerts across all travelers on a booking."""

    def test_get_alerts_by_itinerary_returns_all_travelers(self):
        alert_store_mod.save_alert(_make_record(alert_id="a1", skymiles_number="1111111"))
        alert_store_mod.save_alert(_make_record(alert_id="a2", skymiles_number="2222222"))
        alert_store_mod.save_alert(_make_record(alert_id="a3", itinerary_ref="DL-OTHER"))

        result = alert_store_mod.get_alerts_by_itinerary("DL-TEST")

        assert _ids(result) == {"a1", "a2"}

    def test_get_alerts_by_itinerary_includes_all_statuses(self):
        alert_store_mod.save_alert(_make_record(alert_id="a1"))
        alert_store_mod.save_alert(_make_record(alert_id="a2", status=AlertStatus.RESOLVED))

        result = alert_store_mod.get_alerts_by_itinerary("DL-TEST")

        assert _ids(result) == {"a1", "a2"}

    def test_get_alerts_by_itinerary_returns_empty_for_unknown_booking(self):
        alert_store_mod.save_alert(_make_record())

        result = alert_store_mod.get_alerts_by_itinerary("DL-NONE")

        assert result == []

    def test_group_summary_groups_alerts_by_traveler(self):
        alert_store_mod.save_alert(_make_record(alert_id="a1", skymiles_number="1111111"))
        alert_store_mod.save_alert(
            _make_record(alert_id="a2", skymiles_number="1111111", alert_type=AlertType.VISA)
        )
        alert_store_mod.save_alert(_make_record(alert_id="b1", skymiles_number="2222222"))

        summary = alert_store_mod.get_group_alert_summary("DL-TEST")

        assert set(summary) == {"1111111", "2222222"}
        assert _ids(summary["1111111"]) == {"a1", "a2"}
        assert _ids(summary["2222222"]) == {"b1"}

    def test_group_summary_excludes_other_bookings(self):
        alert_store_mod.save_alert(_make_record(alert_id="a1", itinerary_ref="DL-OTHER"))

        summary = alert_store_mod.get_group_alert_summary("DL-TEST")

        assert summary == {}


# ------------------------------------------------------------------
# expire_stale_alerts
# ------------------------------------------------------------------

class TestExpireStaleAlerts:
    """Only ACTIVE/ACKNOWLEDGED alerts whose TTL has passed are marked EXPIRED."""

    def test_expires_active_alert_with_past_ttl(self):
        alert_store_mod.save_alert(_make_record(ttl=PAST_TTL))

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item()["status"] == "EXPIRED"

    def test_expires_acknowledged_alert_with_past_ttl(self):
        alert_store_mod.save_alert(
            _make_record(ttl=PAST_TTL, status=AlertStatus.ACKNOWLEDGED)
        )

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

    def test_skips_resolved_alert_with_past_ttl(self):
        alert_store_mod.save_alert(_make_record(ttl=PAST_TTL, status=AlertStatus.RESOLVED))

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item()["status"] == "RESOLVED"

    def test_counts_only_eligible_alerts_in_mixed_set(self):
        alert_store_mod.save_alert(_make_record(alert_id="stale", ttl=PAST_TTL))
        alert_store_mod.save_alert(_make_record(alert_id="fresh", ttl=FUTURE_TTL))
        alert_store_mod.save_alert(_make_record(alert_id="no-ttl"))
        alert_store_mod.save_alert(_make_record(alert_id="other-member", skymiles_number="1111111", ttl=PAST_TTL))

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item(alert_id="stale")["status"] == "EXPIRED"
        assert _get_item(alert_id="fresh")["status"] == "ACTIVE"
        assert _get_item("1111111", "other-member")["status"] == "ACTIVE"

    def test_returns_zero_when_member_has_no_alerts(self):
        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0
