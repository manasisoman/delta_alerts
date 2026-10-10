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

MEMBER = "1234567890"
OTHER_MEMBER = "5555555555"
FAR_FUTURE_TTL = int(datetime(2035, 1, 1).timestamp())
PAST_TTL = int(datetime(2020, 1, 1).timestamp())


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto and create ConciergeAlerts (with itinerary-ref-index GSI)."""
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
            yield ddb
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table


def _make_alert(**overrides) -> AlertRecord:
    """Build a valid ACTIVE passport AlertRecord with optional overrides."""
    fields = {
        "alert_id": "alert-1",
        "skymiles_number": MEMBER,
        "alert_type": AlertType.PASSPORT,
        "severity": AlertSeverity.CRITICAL,
        "reasons": ["Passport expires before return to CN (requires 6 months)"],
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
        assert item["skymiles_number"] == MEMBER
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        assert item["reasons"] == alert.reasons
        assert item["created_at"] == "2030-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == "ACTIVE"

    def test_optional_fields_omitted_when_none(self):
        alert = _make_alert()

        save_alert(alert)

        item = _get_item("alert-1")
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_ttl_persisted_when_set(self):
        alert = _make_alert(ttl=FAR_FUTURE_TTL)

        save_alert(alert)

        assert int(_get_item("alert-1")["ttl"]) == FAR_FUTURE_TTL

    def test_resolved_at_persisted_when_set(self):
        alert = _make_alert(resolved_at="2030-02-01T00:00:00")

        save_alert(alert)

        assert _get_item("alert-1")["resolved_at"] == "2030-02-01T00:00:00"

    def test_resolution_persisted_when_set(self):
        alert = _make_alert(resolution="Passport renewed")

        save_alert(alert)

        assert _get_item("alert-1")["resolution"] == "Passport renewed"

    def test_visa_alert_type_and_status_persisted(self):
        alert = _make_alert(
            alert_type=AlertType.VISA,
            severity=AlertSeverity.WARNING,
            status=AlertStatus.ACKNOWLEDGED,
            reasons=["Transit visa required for layover in CN"],
        )

        save_alert(alert)

        item = _get_item("alert-1")
        assert item["alert_type"] == "VISA"
        assert item["severity"] == "WARNING"
        assert item["status"] == "ACKNOWLEDGED"
        assert item["reasons"] == ["Transit visa required for layover in CN"]


class TestGetAlertsByMember:
    """get_alerts_by_member returns every alert for the member, regardless of status."""

    def test_returns_empty_list_when_no_alerts(self):
        result = get_alerts_by_member(MEMBER)

        assert result == []

    def test_returns_all_statuses_for_member_only(self):
        save_alert(_make_alert(alert_id="a-active"))
        save_alert(_make_alert(alert_id="a-resolved", status=AlertStatus.RESOLVED))
        save_alert(_make_alert(alert_id="other", skymiles_number=OTHER_MEMBER))

        result = get_alerts_by_member(MEMBER)

        assert sorted(a["alert_id"] for a in result) == ["a-active", "a-resolved"]


class TestStatusTransitions:
    """acknowledge_alert and resolve_alert update status and resolution metadata."""

    def test_acknowledge_sets_status(self):
        save_alert(_make_alert())

        acknowledge_alert(MEMBER, "alert-1")

        item = _get_item("alert-1")
        assert item["status"] == "ACKNOWLEDGED"
        assert "resolved_at" not in item

    def test_resolve_sets_status_resolution_and_timestamp(self):
        save_alert(_make_alert())
        before = datetime.utcnow()

        resolve_alert(MEMBER, "alert-1", "Visa obtained")

        item = _get_item("alert-1")
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Visa obtained"
        resolved_at = datetime.fromisoformat(item["resolved_at"])
        assert before - timedelta(seconds=1) <= resolved_at <= datetime.utcnow()


class TestGetActiveAlerts:
    """get_active_alerts returns only ACTIVE and ACKNOWLEDGED alerts."""

    def test_returns_empty_list_when_no_alerts(self):
        result = get_active_alerts(MEMBER)

        assert result == []

    def test_filters_out_resolved_and_expired(self):
        save_alert(_make_alert(alert_id="active"))
        save_alert(_make_alert(alert_id="acked", status=AlertStatus.ACKNOWLEDGED))
        save_alert(_make_alert(alert_id="resolved", status=AlertStatus.RESOLVED))
        save_alert(_make_alert(alert_id="expired", status=AlertStatus.EXPIRED))

        result = get_active_alerts(MEMBER)

        assert sorted(a["alert_id"] for a in result) == ["acked", "active"]


class TestResolveAlertsForItinerary:
    """Itinerary changes resolve only actionable alerts tied to that confirmation number."""

    def test_returns_zero_when_no_matching_alerts(self):
        save_alert(_make_alert(itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", "itinerary changed")

        assert count == 0
        assert _get_item("alert-1")["status"] == "ACTIVE"

    def test_resolves_active_and_acknowledged_passport_and_visa_alerts(self):
        save_alert(_make_alert(alert_id="passport"))
        save_alert(
            _make_alert(
                alert_id="visa",
                alert_type=AlertType.VISA,
                status=AlertStatus.ACKNOWLEDGED,
                reasons=["Transit visa required for layover in CN"],
            )
        )

        count = resolve_alerts_for_itinerary(
            MEMBER, "DL-TEST", "Auto-resolved: itinerary changed, re-evaluating"
        )

        assert count == 2
        for alert_id in ("passport", "visa"):
            item = _get_item(alert_id)
            assert item["status"] == "RESOLVED"
            assert item["resolution"] == "Auto-resolved: itinerary changed, re-evaluating"

    def test_leaves_alerts_for_other_itineraries_untouched(self):
        save_alert(_make_alert(alert_id="this-trip"))
        save_alert(_make_alert(alert_id="other-trip", itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", "itinerary changed")

        assert count == 1
        assert _get_item("other-trip")["status"] == "ACTIVE"

    def test_skips_already_resolved_and_expired_alerts(self):
        save_alert(
            _make_alert(
                alert_id="resolved",
                status=AlertStatus.RESOLVED,
                resolution="Passport renewed",
            )
        )
        save_alert(_make_alert(alert_id="expired", status=AlertStatus.EXPIRED))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", "itinerary changed")

        assert count == 0
        assert _get_item("resolved")["resolution"] == "Passport renewed"
        assert _get_item("expired")["status"] == "EXPIRED"

    def test_does_not_touch_other_members_on_same_itinerary(self):
        save_alert(_make_alert(alert_id="companion", skymiles_number=OTHER_MEMBER))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", "itinerary changed")

        assert count == 0
        assert _get_item("companion", OTHER_MEMBER)["status"] == "ACTIVE"


class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the GSI across every traveler on a booking."""

    def test_returns_empty_list_for_unknown_itinerary(self):
        save_alert(_make_alert())

        result = get_alerts_by_itinerary("DL-NONE")

        assert result == []

    def test_returns_alerts_across_travelers_for_itinerary(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="b1", skymiles_number=OTHER_MEMBER))
        save_alert(_make_alert(alert_id="x1", itinerary_ref="DL-OTHER"))

        result = get_alerts_by_itinerary("DL-TEST")

        assert sorted((a["skymiles_number"], a["alert_id"]) for a in result) == [
            (MEMBER, "a1"),
            (OTHER_MEMBER, "b1"),
        ]


class TestGetGroupAlertSummary:
    """get_group_alert_summary groups a booking's alerts by skymiles_number."""

    def test_returns_empty_dict_when_no_alerts(self):
        result = get_group_alert_summary("DL-TEST")

        assert result == {}

    def test_groups_multiple_alerts_per_traveler(self):
        save_alert(_make_alert(alert_id="passport"))
        save_alert(_make_alert(alert_id="visa", alert_type=AlertType.VISA))
        save_alert(_make_alert(alert_id="companion", skymiles_number=OTHER_MEMBER))
        save_alert(_make_alert(alert_id="unrelated", itinerary_ref="DL-OTHER"))

        result = get_group_alert_summary("DL-TEST")

        assert set(result) == {MEMBER, OTHER_MEMBER}
        assert sorted(a["alert_id"] for a in result[MEMBER]) == ["passport", "visa"]
        assert [a["alert_id"] for a in result[OTHER_MEMBER]] == ["companion"]


class TestExpireStaleAlerts:
    """expire_stale_alerts marks only actionable alerts whose TTL has passed as EXPIRED."""

    def test_returns_zero_when_no_alerts(self):
        count = expire_stale_alerts(MEMBER)

        assert count == 0

    def test_expires_active_alert_with_past_ttl(self):
        save_alert(_make_alert(ttl=PAST_TTL))

        count = expire_stale_alerts(MEMBER)

        assert count == 1
        assert _get_item("alert-1")["status"] == "EXPIRED"

    def test_expires_acknowledged_alert_with_past_ttl(self):
        save_alert(_make_alert(ttl=PAST_TTL, status=AlertStatus.ACKNOWLEDGED))

        count = expire_stale_alerts(MEMBER)

        assert count == 1
        assert _get_item("alert-1")["status"] == "EXPIRED"

    def test_skips_alert_without_ttl(self):
        save_alert(_make_alert())

        count = expire_stale_alerts(MEMBER)

        assert count == 0
        assert _get_item("alert-1")["status"] == "ACTIVE"

    def test_skips_alert_with_future_ttl(self):
        save_alert(_make_alert(ttl=FAR_FUTURE_TTL))

        count = expire_stale_alerts(MEMBER)

        assert count == 0
        assert _get_item("alert-1")["status"] == "ACTIVE"

    def test_skips_resolved_alert_with_past_ttl(self):
        save_alert(_make_alert(ttl=PAST_TTL, status=AlertStatus.RESOLVED))

        count = expire_stale_alerts(MEMBER)

        assert count == 0
        assert _get_item("alert-1")["status"] == "RESOLVED"

    def test_counts_only_stale_alerts_in_mixed_set(self):
        save_alert(_make_alert(alert_id="stale-1", ttl=PAST_TTL))
        save_alert(_make_alert(alert_id="stale-2", ttl=PAST_TTL, alert_type=AlertType.VISA))
        save_alert(_make_alert(alert_id="fresh", ttl=FAR_FUTURE_TTL))
        save_alert(_make_alert(alert_id="no-ttl"))

        count = expire_stale_alerts(MEMBER)

        assert count == 2
        assert _get_item("fresh")["status"] == "ACTIVE"
        assert _get_item("no-ttl")["status"] == "ACTIVE"
