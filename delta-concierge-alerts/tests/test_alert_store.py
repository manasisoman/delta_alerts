"""Tests for the DynamoDB-backed alert persistence layer (alert_store).

Covers optional-field persistence, member/active/itinerary queries, the
acknowledge/resolve lifecycle, itinerary-change auto-resolution, group
summaries via the ``itinerary-ref-index`` GSI, and TTL-based expiry.
"""

from datetime import datetime

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


MEMBER = "9999999"
PAST_TTL = int(datetime(2020, 1, 1).timestamp())
FUTURE_TTL = int(datetime(2030, 1, 1).timestamp())


# ------------------------------------------------------------------
# Fixtures and helpers
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto and swap alert_store's module-level DynamoDB clients."""
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


def _make_alert(**overrides) -> AlertRecord:
    """Build a valid ACTIVE visa AlertRecord with optional field overrides."""
    fields = {
        "alert_id": "alert-001",
        "skymiles_number": MEMBER,
        "alert_type": AlertType.VISA,
        "severity": AlertSeverity.CRITICAL,
        "reasons": ["No valid visa on file for CN"],
        "created_at": "2030-01-01T00:00:00",
        "itinerary_ref": "DL-TEST",
    }
    fields.update(overrides)
    return AlertRecord(**fields)


def _get_item(alert_id: str = "alert-001", skymiles_number: str = MEMBER) -> dict:
    """Fetch a raw item from the mocked table."""
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


# ------------------------------------------------------------------
# save_alert
# ------------------------------------------------------------------

class TestSaveAlertHappyPath:
    """A default ACTIVE alert is persisted with all required attributes."""

    def test_persists_required_fields(self):
        alert = _make_alert()

        save_alert(alert)

        item = _get_item()
        assert item["skymiles_number"] == MEMBER
        assert item["alert_type"] == "VISA"
        assert item["severity"] == "CRITICAL"
        assert item["reasons"] == ["No valid visa on file for CN"]
        assert item["created_at"] == "2030-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == "ACTIVE"

    def test_omits_unset_optional_fields(self):
        alert = _make_alert()

        save_alert(alert)

        item = _get_item()
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item


class TestSaveAlertOptionalFields:
    """Optional fields are stored only when they are set on the record."""

    def test_ttl_is_stored_when_set(self):
        alert = _make_alert(ttl=FUTURE_TTL)

        save_alert(alert)

        assert int(_get_item()["ttl"]) == FUTURE_TTL

    def test_resolved_at_is_stored_when_set(self):
        alert = _make_alert(resolved_at="2030-01-02T00:00:00")

        save_alert(alert)

        item = _get_item()
        assert item["resolved_at"] == "2030-01-02T00:00:00"
        assert "resolution" not in item

    def test_resolution_is_stored_when_set(self):
        alert = _make_alert(resolution="Visa uploaded")

        save_alert(alert)

        item = _get_item()
        assert item["resolution"] == "Visa uploaded"
        assert "resolved_at" not in item

    def test_non_default_status_is_stored(self):
        alert = _make_alert(status=AlertStatus.ACKNOWLEDGED)

        save_alert(alert)

        assert _get_item()["status"] == "ACKNOWLEDGED"


class TestSaveAlertDocumentTypes:
    """Passport, visa, and layover/transit alerts round-trip their reasons."""

    def test_passport_validity_alert(self):
        alert = _make_alert(
            alert_type=AlertType.PASSPORT,
            severity=AlertSeverity.WARNING,
            reasons=["Passport has less than 6 months validity for JP"],
        )

        save_alert(alert)

        item = _get_item()
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "WARNING"
        assert "6 months" in item["reasons"][0]

    def test_layover_transit_visa_alert_keeps_all_reasons(self):
        reasons = [
            "Transit visa required for layover in CN",
            "No valid visa on file for JP",
        ]
        alert = _make_alert(reasons=reasons)

        save_alert(alert)

        assert _get_item()["reasons"] == reasons


# ------------------------------------------------------------------
# get_alerts_by_member / get_active_alerts
# ------------------------------------------------------------------

class TestGetAlertsByMember:
    """Member queries return only that member's alerts, in any status."""

    def test_returns_empty_list_for_unknown_member(self):
        result = get_alerts_by_member("0000000")

        assert result == []

    def test_returns_all_alerts_for_member_only(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", status=AlertStatus.RESOLVED))
        save_alert(_make_alert(alert_id="other", skymiles_number="1111111"))

        result = get_alerts_by_member(MEMBER)

        assert sorted(a["alert_id"] for a in result) == ["a1", "a2"]


class TestGetActiveAlerts:
    """Only ACTIVE and ACKNOWLEDGED alerts are considered actionable."""

    def test_returns_active_and_acknowledged_only(self):
        save_alert(_make_alert(alert_id="active"))
        save_alert(_make_alert(alert_id="ack", status=AlertStatus.ACKNOWLEDGED))
        save_alert(_make_alert(alert_id="resolved", status=AlertStatus.RESOLVED))
        save_alert(_make_alert(alert_id="expired", status=AlertStatus.EXPIRED))

        result = get_active_alerts(MEMBER)

        assert sorted(a["alert_id"] for a in result) == ["ack", "active"]

    def test_returns_empty_list_when_no_alerts(self):
        result = get_active_alerts(MEMBER)

        assert result == []


# ------------------------------------------------------------------
# acknowledge_alert / resolve_alert
# ------------------------------------------------------------------

class TestAcknowledgeAlert:
    """Acknowledging changes status but leaves resolution fields unset."""

    def test_sets_status_acknowledged(self):
        save_alert(_make_alert())

        acknowledge_alert(MEMBER, "alert-001")

        item = _get_item()
        assert item["status"] == "ACKNOWLEDGED"
        assert "resolved_at" not in item


class TestResolveAlert:
    """Resolving sets status, a resolved_at timestamp, and the resolution text."""

    def test_sets_resolved_fields(self):
        save_alert(_make_alert())

        resolve_alert(MEMBER, "alert-001", "Visa uploaded")

        item = _get_item()
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Visa uploaded"
        datetime.fromisoformat(item["resolved_at"])

    def test_resolved_alert_is_no_longer_active(self):
        save_alert(_make_alert())

        resolve_alert(MEMBER, "alert-001", "Visa uploaded")

        assert get_active_alerts(MEMBER) == []


# ------------------------------------------------------------------
# resolve_alerts_for_itinerary (itinerary changes)
# ------------------------------------------------------------------

class TestResolveAlertsForItinerary:
    """An itinerary change resolves only that booking's actionable alerts."""

    RESOLUTION = "Auto-resolved: itinerary changed, re-evaluating"

    def test_resolves_active_and_acknowledged_for_itinerary(self):
        save_alert(_make_alert(alert_id="active"))
        save_alert(_make_alert(alert_id="ack", status=AlertStatus.ACKNOWLEDGED))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", self.RESOLUTION)

        assert count == 2
        for alert_id in ("active", "ack"):
            item = _get_item(alert_id)
            assert item["status"] == "RESOLVED"
            assert item["resolution"] == self.RESOLUTION

    def test_leaves_other_itineraries_untouched(self):
        save_alert(_make_alert(alert_id="changed", itinerary_ref="DL-TEST"))
        save_alert(_make_alert(alert_id="other", itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", self.RESOLUTION)

        assert count == 1
        assert _get_item("other")["status"] == "ACTIVE"

    def test_skips_already_resolved_or_expired(self):
        save_alert(_make_alert(alert_id="resolved", status=AlertStatus.RESOLVED,
                               resolution="Original"))
        save_alert(_make_alert(alert_id="expired", status=AlertStatus.EXPIRED))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", self.RESOLUTION)

        assert count == 0
        assert _get_item("resolved")["resolution"] == "Original"
        assert _get_item("expired")["status"] == "EXPIRED"

    def test_leaves_other_members_untouched(self):
        save_alert(_make_alert(alert_id="other", skymiles_number="1111111"))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", self.RESOLUTION)

        assert count == 0
        assert _get_item("other", "1111111")["status"] == "ACTIVE"


# ------------------------------------------------------------------
# get_alerts_by_itinerary / get_group_alert_summary
# ------------------------------------------------------------------

class TestGetAlertsByItinerary:
    """GSI lookup returns alerts for every traveler on the booking."""

    def test_returns_alerts_across_travelers(self):
        save_alert(_make_alert(alert_id="a1", skymiles_number="1111111"))
        save_alert(_make_alert(alert_id="a2", skymiles_number="2222222"))
        save_alert(_make_alert(alert_id="a3", itinerary_ref="DL-OTHER"))

        result = get_alerts_by_itinerary("DL-TEST")

        assert sorted(a["alert_id"] for a in result) == ["a1", "a2"]

    def test_returns_empty_list_for_unknown_itinerary(self):
        result = get_alerts_by_itinerary("DL-NONE")

        assert result == []


class TestGetGroupAlertSummary:
    """Group summaries bucket a booking's alerts by skymiles_number."""

    def test_groups_alerts_by_traveler(self):
        save_alert(_make_alert(alert_id="p1", skymiles_number="1111111",
                               alert_type=AlertType.PASSPORT))
        save_alert(_make_alert(alert_id="v1", skymiles_number="1111111"))
        save_alert(_make_alert(alert_id="v2", skymiles_number="2222222"))

        summary = get_group_alert_summary("DL-TEST")

        assert set(summary) == {"1111111", "2222222"}
        assert sorted(a["alert_id"] for a in summary["1111111"]) == ["p1", "v1"]
        assert [a["alert_id"] for a in summary["2222222"]] == ["v2"]

    def test_empty_booking_returns_empty_dict(self):
        summary = get_group_alert_summary("DL-NONE")

        assert summary == {}


# ------------------------------------------------------------------
# expire_stale_alerts
# ------------------------------------------------------------------

class TestExpireStaleAlerts:
    """Only actionable alerts whose TTL has passed are marked EXPIRED."""

    def test_expires_active_alert_past_ttl(self):
        save_alert(_make_alert(ttl=PAST_TTL))

        count = expire_stale_alerts(MEMBER)

        assert count == 1
        assert _get_item()["status"] == "EXPIRED"

    def test_expires_acknowledged_alert_past_ttl(self):
        save_alert(_make_alert(ttl=PAST_TTL, status=AlertStatus.ACKNOWLEDGED))

        count = expire_stale_alerts(MEMBER)

        assert count == 1
        assert _get_item()["status"] == "EXPIRED"

    def test_skips_alert_with_future_ttl(self):
        save_alert(_make_alert(ttl=FUTURE_TTL))

        count = expire_stale_alerts(MEMBER)

        assert count == 0
        assert _get_item()["status"] == "ACTIVE"

    def test_skips_alert_without_ttl(self):
        save_alert(_make_alert())

        count = expire_stale_alerts(MEMBER)

        assert count == 0
        assert _get_item()["status"] == "ACTIVE"

    def test_skips_resolved_alert_past_ttl(self):
        save_alert(_make_alert(ttl=PAST_TTL, status=AlertStatus.RESOLVED))

        count = expire_stale_alerts(MEMBER)

        assert count == 0
        assert _get_item()["status"] == "RESOLVED"

    def test_counts_only_stale_alerts_in_mixed_set(self):
        save_alert(_make_alert(alert_id="stale", ttl=PAST_TTL))
        save_alert(_make_alert(alert_id="fresh", ttl=FUTURE_TTL))
        save_alert(_make_alert(alert_id="no-ttl"))

        count = expire_stale_alerts(MEMBER)

        assert count == 1
        assert _get_item("stale")["status"] == "EXPIRED"
        assert _get_item("fresh")["status"] == "ACTIVE"
        assert _get_item("no-ttl")["status"] == "ACTIVE"

    def test_no_alerts_returns_zero(self):
        count = expire_stale_alerts(MEMBER)

        assert count == 0
