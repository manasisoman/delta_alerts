"""Tests for DynamoDB-backed alert persistence in src.services.alert_store."""

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

MEMBER = "1234567890"
OTHER_MEMBER = "5555555555"
PAST_TTL = int(datetime(2020, 1, 1).timestamp())
FUTURE_TTL = int(datetime(2030, 1, 1).timestamp())


@pytest.fixture(autouse=True)
def _mock_dynamodb():
    """Activate moto and swap in a ConciergeAlerts table with the itinerary GSI."""
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


def _make_alert(**overrides) -> AlertRecord:
    """Build a valid ACTIVE AlertRecord with optional field overrides."""
    fields = {
        "alert_id": "alert-1",
        "skymiles_number": MEMBER,
        "alert_type": AlertType.PASSPORT,
        "severity": AlertSeverity.CRITICAL,
        "reasons": ["Passport expires before travel"],
        "created_at": "2030-01-01T00:00:00",
        "itinerary_ref": "DL-TEST",
    }
    fields.update(overrides)
    return AlertRecord(**fields)


def _get_item(alert_id: str, skymiles_number: str = MEMBER) -> dict:
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


class TestSaveAlert:
    """save_alert persists required fields and only the optional fields that are set."""

    def test_required_fields_persisted(self):
        alert = _make_alert()

        save_alert(alert)

        item = _get_item("alert-1")
        assert item == {
            "skymiles_number": MEMBER,
            "alert_id": "alert-1",
            "alert_type": "PASSPORT",
            "severity": "CRITICAL",
            "reasons": ["Passport expires before travel"],
            "created_at": "2030-01-01T00:00:00",
            "itinerary_ref": "DL-TEST",
            "status": "ACTIVE",
        }

    def test_optional_fields_omitted_when_none(self):
        alert = _make_alert()

        save_alert(alert)

        item = _get_item("alert-1")
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_ttl_persisted_when_set(self):
        alert = _make_alert(ttl=FUTURE_TTL)

        save_alert(alert)

        assert int(_get_item("alert-1")["ttl"]) == FUTURE_TTL

    def test_resolved_at_persisted_when_set(self):
        alert = _make_alert(resolved_at="2030-01-02T00:00:00")

        save_alert(alert)

        assert _get_item("alert-1")["resolved_at"] == "2030-01-02T00:00:00"

    def test_resolution_persisted_when_set(self):
        alert = _make_alert(resolution="Passport renewed")

        save_alert(alert)

        assert _get_item("alert-1")["resolution"] == "Passport renewed"

    def test_enum_values_stored_as_strings(self):
        alert = _make_alert(
            alert_type=AlertType.VISA,
            severity=AlertSeverity.WARNING,
            status=AlertStatus.ACKNOWLEDGED,
        )

        save_alert(alert)

        item = _get_item("alert-1")
        assert item["alert_type"] == "VISA"
        assert item["severity"] == "WARNING"
        assert item["status"] == "ACKNOWLEDGED"

    def test_save_overwrites_same_key(self):
        save_alert(_make_alert(reasons=["old"]))

        save_alert(_make_alert(reasons=["new"]))

        assert _get_item("alert-1")["reasons"] == ["new"]


class TestGetAlertsByMember:
    """get_alerts_by_member returns every alert for one partition key, regardless of status."""

    def test_returns_empty_list_for_unknown_member(self):
        assert get_alerts_by_member("0000000000") == []

    def test_returns_all_alerts_for_member(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", status=AlertStatus.RESOLVED))

        result = get_alerts_by_member(MEMBER)

        assert sorted(a["alert_id"] for a in result) == ["a1", "a2"]

    def test_excludes_other_members(self):
        save_alert(_make_alert(alert_id="mine"))
        save_alert(_make_alert(alert_id="theirs", skymiles_number=OTHER_MEMBER))

        result = get_alerts_by_member(MEMBER)

        assert [a["alert_id"] for a in result] == ["mine"]


class TestAcknowledgeAlert:
    """acknowledge_alert transitions only the targeted alert to ACKNOWLEDGED."""

    def test_sets_status_acknowledged(self):
        save_alert(_make_alert())

        acknowledge_alert(MEMBER, "alert-1")

        assert _get_item("alert-1")["status"] == "ACKNOWLEDGED"

    def test_leaves_other_alerts_untouched(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2"))

        acknowledge_alert(MEMBER, "a1")

        assert _get_item("a2")["status"] == "ACTIVE"


class TestResolveAlert:
    """resolve_alert sets RESOLVED status, a resolution note, and a resolved_at timestamp."""

    def test_sets_status_resolution_and_timestamp(self):
        save_alert(_make_alert())
        before = datetime.utcnow()

        resolve_alert(MEMBER, "alert-1", "Passport renewed")

        item = _get_item("alert-1")
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Passport renewed"
        assert datetime.fromisoformat(item["resolved_at"]) >= before


class TestGetActiveAlerts:
    """get_active_alerts returns only ACTIVE and ACKNOWLEDGED alerts."""

    def test_returns_empty_when_member_has_no_alerts(self):
        assert get_active_alerts(MEMBER) == []

    def test_includes_active_and_acknowledged(self):
        save_alert(_make_alert(alert_id="active"))
        save_alert(_make_alert(alert_id="ack", status=AlertStatus.ACKNOWLEDGED))

        result = get_active_alerts(MEMBER)

        assert sorted(a["alert_id"] for a in result) == ["ack", "active"]

    def test_excludes_resolved_and_expired(self):
        save_alert(_make_alert(alert_id="active"))
        save_alert(_make_alert(alert_id="resolved", status=AlertStatus.RESOLVED))
        save_alert(_make_alert(alert_id="expired", status=AlertStatus.EXPIRED))

        result = get_active_alerts(MEMBER)

        assert [a["alert_id"] for a in result] == ["active"]


class TestResolveAlertsForItinerary:
    """resolve_alerts_for_itinerary resolves only actionable alerts on the changed itinerary."""

    def test_returns_zero_when_no_alerts(self):
        assert resolve_alerts_for_itinerary(MEMBER, "DL-TEST", "changed") == 0

    def test_resolves_active_and_acknowledged_on_matching_itinerary(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", status=AlertStatus.ACKNOWLEDGED))

        count = resolve_alerts_for_itinerary(
            MEMBER, "DL-TEST", "Auto-resolved: itinerary changed, re-evaluating"
        )

        assert count == 2
        for alert_id in ("a1", "a2"):
            item = _get_item(alert_id)
            assert item["status"] == "RESOLVED"
            assert item["resolution"] == "Auto-resolved: itinerary changed, re-evaluating"

    def test_skips_alerts_on_other_itineraries(self):
        save_alert(_make_alert(alert_id="changed"))
        save_alert(_make_alert(alert_id="other", itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", "changed")

        assert count == 1
        assert _get_item("other")["status"] == "ACTIVE"

    def test_skips_already_resolved_or_expired_alerts(self):
        save_alert(_make_alert(alert_id="resolved", status=AlertStatus.RESOLVED, resolution="earlier"))
        save_alert(_make_alert(alert_id="expired", status=AlertStatus.EXPIRED))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", "changed")

        assert count == 0
        assert _get_item("resolved")["resolution"] == "earlier"
        assert _get_item("expired")["status"] == "EXPIRED"

    def test_skips_other_members_on_same_itinerary(self):
        save_alert(_make_alert(alert_id="companion", skymiles_number=OTHER_MEMBER))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", "changed")

        assert count == 0
        assert _get_item("companion", OTHER_MEMBER)["status"] == "ACTIVE"


class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the itinerary-ref-index GSI across travelers."""

    def test_returns_empty_for_unknown_itinerary(self):
        assert get_alerts_by_itinerary("DL-NONE") == []

    def test_returns_alerts_across_travelers_for_booking(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", skymiles_number=OTHER_MEMBER))
        save_alert(_make_alert(alert_id="a3", itinerary_ref="DL-OTHER"))

        result = get_alerts_by_itinerary("DL-TEST")

        assert sorted((a["skymiles_number"], a["alert_id"]) for a in result) == [
            (MEMBER, "a1"),
            (OTHER_MEMBER, "a2"),
        ]

    def test_includes_all_statuses(self):
        save_alert(_make_alert(alert_id="old", status=AlertStatus.RESOLVED))
        save_alert(_make_alert(alert_id="new"))

        result = get_alerts_by_itinerary("DL-TEST")

        assert sorted(a["status"] for a in result) == ["ACTIVE", "RESOLVED"]


class TestGetGroupAlertSummary:
    """get_group_alert_summary groups a booking's alerts by SkyMiles number."""

    def test_returns_empty_dict_for_unknown_itinerary(self):
        assert get_group_alert_summary("DL-NONE") == {}

    def test_groups_alerts_by_traveler(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", alert_type=AlertType.VISA))
        save_alert(_make_alert(alert_id="b1", skymiles_number=OTHER_MEMBER))

        summary = get_group_alert_summary("DL-TEST")

        assert set(summary) == {MEMBER, OTHER_MEMBER}
        assert sorted(a["alert_id"] for a in summary[MEMBER]) == ["a1", "a2"]
        assert [a["alert_id"] for a in summary[OTHER_MEMBER]] == ["b1"]

    def test_excludes_other_bookings(self):
        save_alert(_make_alert(alert_id="a1", itinerary_ref="DL-OTHER"))

        assert get_group_alert_summary("DL-TEST") == {}


class TestExpireStaleAlerts:
    """expire_stale_alerts marks only actionable alerts whose TTL has passed as EXPIRED."""

    def test_returns_zero_when_no_alerts(self):
        assert expire_stale_alerts(MEMBER) == 0

    def test_expires_active_alert_past_ttl(self):
        save_alert(_make_alert(ttl=PAST_TTL))

        count = expire_stale_alerts(MEMBER)

        assert count == 1
        assert _get_item("alert-1")["status"] == "EXPIRED"

    def test_expires_acknowledged_alert_past_ttl(self):
        save_alert(_make_alert(status=AlertStatus.ACKNOWLEDGED, ttl=PAST_TTL))

        count = expire_stale_alerts(MEMBER)

        assert count == 1
        assert _get_item("alert-1")["status"] == "EXPIRED"

    def test_skips_alert_without_ttl(self):
        save_alert(_make_alert())

        count = expire_stale_alerts(MEMBER)

        assert count == 0
        assert _get_item("alert-1")["status"] == "ACTIVE"

    def test_skips_alert_with_future_ttl(self):
        save_alert(_make_alert(ttl=FUTURE_TTL))

        count = expire_stale_alerts(MEMBER)

        assert count == 0
        assert _get_item("alert-1")["status"] == "ACTIVE"

    def test_skips_resolved_alert_past_ttl(self):
        save_alert(_make_alert(status=AlertStatus.RESOLVED, ttl=PAST_TTL))

        count = expire_stale_alerts(MEMBER)

        assert count == 0
        assert _get_item("alert-1")["status"] == "RESOLVED"

    def test_mixed_alerts_only_stale_actionable_expired(self):
        save_alert(_make_alert(alert_id="stale", ttl=PAST_TTL))
        save_alert(_make_alert(alert_id="fresh", ttl=FUTURE_TTL))
        save_alert(_make_alert(alert_id="no-ttl"))
        save_alert(_make_alert(alert_id="done", status=AlertStatus.RESOLVED, ttl=PAST_TTL))
        save_alert(_make_alert(alert_id="other", skymiles_number=OTHER_MEMBER, ttl=PAST_TTL))

        count = expire_stale_alerts(MEMBER)

        assert count == 1
        assert _get_item("stale")["status"] == "EXPIRED"
        assert _get_item("fresh")["status"] == "ACTIVE"
        assert _get_item("no-ttl")["status"] == "ACTIVE"
        assert _get_item("done")["status"] == "RESOLVED"
        assert _get_item("other", OTHER_MEMBER)["status"] == "ACTIVE"
