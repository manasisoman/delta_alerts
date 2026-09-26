"""Unit tests for src.services.alert_store (DynamoDB persistence layer)."""

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


FAR_FUTURE_EPOCH = int(datetime(2030, 1, 1).timestamp())
PAST_EPOCH = int(datetime(2020, 1, 1).timestamp())


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto and create the ConciergeAlerts table with the itinerary GSI."""
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


def make_alert(
    alert_id: str = "alert-1",
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
    """Build an AlertRecord with sensible defaults."""
    return AlertRecord(
        alert_id=alert_id,
        skymiles_number=skymiles_number,
        alert_type=alert_type,
        severity=severity,
        reasons=reasons if reasons is not None else ["Passport expired"],
        created_at=created_at,
        itinerary_ref=itinerary_ref,
        status=status,
        resolved_at=resolved_at,
        resolution=resolution,
        ttl=ttl,
    )


def _get_item(skymiles_number: str = "9999999", alert_id: str = "alert-1") -> dict:
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


class TestSaveAlert:
    """save_alert persists required fields and only includes optional fields when set."""

    def test_happy_path_persists_required_fields(self):
        alert = make_alert(reasons=["Passport expired", "Visa missing for DE"])

        save_alert(alert)

        item = _get_item()
        assert item["skymiles_number"] == "9999999"
        assert item["alert_id"] == "alert-1"
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        assert item["reasons"] == ["Passport expired", "Visa missing for DE"]
        assert item["created_at"] == "2026-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == "ACTIVE"

    def test_optional_fields_omitted_when_none(self):
        save_alert(make_alert())

        item = _get_item()
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_ttl_persisted_when_set(self):
        save_alert(make_alert(ttl=FAR_FUTURE_EPOCH))

        assert int(_get_item()["ttl"]) == FAR_FUTURE_EPOCH

    def test_resolved_at_persisted_when_set(self):
        save_alert(make_alert(resolved_at="2026-02-01T00:00:00"))

        assert _get_item()["resolved_at"] == "2026-02-01T00:00:00"

    def test_resolution_persisted_when_set(self):
        save_alert(make_alert(resolution="Passport renewed"))

        assert _get_item()["resolution"] == "Passport renewed"

    def test_visa_alert_type_and_warning_severity(self):
        save_alert(
            make_alert(
                alert_type=AlertType.VISA,
                severity=AlertSeverity.WARNING,
                reasons=["Transit visa required for CN layover"],
            )
        )

        item = _get_item()
        assert item["alert_type"] == "VISA"
        assert item["severity"] == "WARNING"

    def test_save_overwrites_existing_alert(self):
        save_alert(make_alert(severity=AlertSeverity.WARNING))

        save_alert(make_alert(severity=AlertSeverity.CRITICAL))

        assert _get_item()["severity"] == "CRITICAL"
        assert len(get_alerts_by_member("9999999")) == 1


class TestGetAlertsByMember:
    """get_alerts_by_member returns all alerts for the partition key regardless of status."""

    def test_returns_empty_list_when_no_alerts(self):
        assert get_alerts_by_member("0000000") == []

    def test_returns_all_alerts_for_member(self):
        save_alert(make_alert(alert_id="a1"))
        save_alert(make_alert(alert_id="a2", status=AlertStatus.RESOLVED))
        save_alert(make_alert(alert_id="a3", status=AlertStatus.EXPIRED))

        items = get_alerts_by_member("9999999")

        assert sorted(i["alert_id"] for i in items) == ["a1", "a2", "a3"]

    def test_does_not_return_other_members_alerts(self):
        save_alert(make_alert(alert_id="a1", skymiles_number="1111111"))
        save_alert(make_alert(alert_id="a2", skymiles_number="2222222"))

        items = get_alerts_by_member("1111111")

        assert [i["skymiles_number"] for i in items] == ["1111111"]


class TestAcknowledgeAlert:
    """acknowledge_alert transitions status to ACKNOWLEDGED without touching other fields."""

    def test_sets_status_to_acknowledged(self):
        save_alert(make_alert())

        acknowledge_alert("9999999", "alert-1")

        assert _get_item()["status"] == "ACKNOWLEDGED"

    def test_other_fields_unchanged(self):
        save_alert(make_alert(ttl=FAR_FUTURE_EPOCH))

        acknowledge_alert("9999999", "alert-1")

        item = _get_item()
        assert item["severity"] == "CRITICAL"
        assert item["itinerary_ref"] == "DL-TEST"
        assert int(item["ttl"]) == FAR_FUTURE_EPOCH
        assert "resolution" not in item

    def test_only_targeted_alert_is_acknowledged(self):
        save_alert(make_alert(alert_id="a1"))
        save_alert(make_alert(alert_id="a2"))

        acknowledge_alert("9999999", "a1")

        assert _get_item(alert_id="a1")["status"] == "ACKNOWLEDGED"
        assert _get_item(alert_id="a2")["status"] == "ACTIVE"


class TestResolveAlert:
    """resolve_alert sets RESOLVED status, resolution text and a resolved_at timestamp."""

    def test_sets_status_and_resolution(self):
        save_alert(make_alert())

        resolve_alert("9999999", "alert-1", "Passport renewed")

        item = _get_item()
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Passport renewed"

    def test_sets_resolved_at_iso_timestamp(self):
        save_alert(make_alert())
        before = datetime.utcnow() - timedelta(seconds=5)

        resolve_alert("9999999", "alert-1", "Visa obtained")

        resolved_at = datetime.fromisoformat(_get_item()["resolved_at"])
        assert before <= resolved_at <= datetime.utcnow() + timedelta(seconds=5)

    def test_resolving_acknowledged_alert(self):
        save_alert(make_alert(status=AlertStatus.ACKNOWLEDGED))

        resolve_alert("9999999", "alert-1", "Done")

        assert _get_item()["status"] == "RESOLVED"


class TestGetActiveAlerts:
    """get_active_alerts returns only ACTIVE and ACKNOWLEDGED alerts."""

    def test_returns_empty_when_none(self):
        assert get_active_alerts("9999999") == []

    def test_includes_active_and_acknowledged(self):
        save_alert(make_alert(alert_id="active", status=AlertStatus.ACTIVE))
        save_alert(make_alert(alert_id="acked", status=AlertStatus.ACKNOWLEDGED))

        items = get_active_alerts("9999999")

        assert sorted(i["alert_id"] for i in items) == ["acked", "active"]

    def test_excludes_resolved(self):
        save_alert(make_alert(alert_id="resolved", status=AlertStatus.RESOLVED))
        save_alert(make_alert(alert_id="active"))

        items = get_active_alerts("9999999")

        assert [i["alert_id"] for i in items] == ["active"]

    def test_excludes_expired(self):
        save_alert(make_alert(alert_id="expired", status=AlertStatus.EXPIRED))
        save_alert(make_alert(alert_id="active"))

        items = get_active_alerts("9999999")

        assert [i["alert_id"] for i in items] == ["active"]

    def test_after_resolve_alert_is_no_longer_active(self):
        save_alert(make_alert())

        resolve_alert("9999999", "alert-1", "Fixed")

        assert get_active_alerts("9999999") == []


class TestResolveAlertsForItinerary:
    """resolve_alerts_for_itinerary resolves only active/acked alerts for one booking."""

    def test_returns_zero_when_no_alerts(self):
        assert resolve_alerts_for_itinerary("9999999", "DL-TEST", "changed") == 0

    def test_resolves_all_matching_alerts_and_returns_count(self):
        save_alert(make_alert(alert_id="passport", alert_type=AlertType.PASSPORT))
        save_alert(
            make_alert(
                alert_id="visa-layover",
                alert_type=AlertType.VISA,
                status=AlertStatus.ACKNOWLEDGED,
                reasons=["Transit visa required for CN layover"],
            )
        )

        count = resolve_alerts_for_itinerary(
            "9999999", "DL-TEST", "Auto-resolved: itinerary changed, re-evaluating"
        )

        assert count == 2
        for alert_id in ("passport", "visa-layover"):
            item = _get_item(alert_id=alert_id)
            assert item["status"] == "RESOLVED"
            assert item["resolution"] == "Auto-resolved: itinerary changed, re-evaluating"
            assert "resolved_at" in item

    def test_skips_alerts_for_other_itineraries(self):
        save_alert(make_alert(alert_id="this-trip", itinerary_ref="DL-AAA"))
        save_alert(make_alert(alert_id="other-trip", itinerary_ref="DL-BBB"))

        count = resolve_alerts_for_itinerary("9999999", "DL-AAA", "changed")

        assert count == 1
        assert _get_item(alert_id="this-trip")["status"] == "RESOLVED"
        assert _get_item(alert_id="other-trip")["status"] == "ACTIVE"

    def test_skips_already_resolved_alerts(self):
        save_alert(
            make_alert(
                alert_id="old",
                status=AlertStatus.RESOLVED,
                resolution="Original resolution",
            )
        )

        count = resolve_alerts_for_itinerary("9999999", "DL-TEST", "changed")

        assert count == 0
        assert _get_item(alert_id="old")["resolution"] == "Original resolution"

    def test_skips_expired_alerts(self):
        save_alert(make_alert(alert_id="stale", status=AlertStatus.EXPIRED))

        count = resolve_alerts_for_itinerary("9999999", "DL-TEST", "changed")

        assert count == 0
        assert _get_item(alert_id="stale")["status"] == "EXPIRED"

    def test_does_not_touch_other_members_on_same_booking(self):
        save_alert(make_alert(alert_id="a1", skymiles_number="1111111"))
        save_alert(make_alert(alert_id="a1", skymiles_number="2222222"))

        count = resolve_alerts_for_itinerary("1111111", "DL-TEST", "changed")

        assert count == 1
        assert _get_item("1111111", "a1")["status"] == "RESOLVED"
        assert _get_item("2222222", "a1")["status"] == "ACTIVE"


class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the itinerary-ref-index GSI across all travelers."""

    def test_returns_empty_when_no_alerts(self):
        assert get_alerts_by_itinerary("DL-NONE") == []

    def test_returns_alerts_across_travelers(self):
        save_alert(make_alert(alert_id="a1", skymiles_number="1111111"))
        save_alert(make_alert(alert_id="a1", skymiles_number="2222222"))

        items = get_alerts_by_itinerary("DL-TEST")

        assert sorted(i["skymiles_number"] for i in items) == ["1111111", "2222222"]

    def test_excludes_other_bookings(self):
        save_alert(make_alert(alert_id="a1", itinerary_ref="DL-AAA"))
        save_alert(make_alert(alert_id="a2", itinerary_ref="DL-BBB"))

        items = get_alerts_by_itinerary("DL-AAA")

        assert [i["alert_id"] for i in items] == ["a1"]

    def test_includes_all_statuses(self):
        save_alert(make_alert(alert_id="a1", status=AlertStatus.ACTIVE))
        save_alert(make_alert(alert_id="a2", status=AlertStatus.RESOLVED))

        items = get_alerts_by_itinerary("DL-TEST")

        assert len(items) == 2


class TestGetGroupAlertSummary:
    """get_group_alert_summary groups a booking's alerts by skymiles_number."""

    def test_empty_dict_when_no_alerts(self):
        assert get_group_alert_summary("DL-NONE") == {}

    def test_groups_by_traveler(self):
        save_alert(make_alert(alert_id="p", skymiles_number="1111111"))
        save_alert(
            make_alert(alert_id="v", skymiles_number="1111111", alert_type=AlertType.VISA)
        )
        save_alert(make_alert(alert_id="p", skymiles_number="2222222"))

        grouped = get_group_alert_summary("DL-TEST")

        assert set(grouped) == {"1111111", "2222222"}
        assert sorted(a["alert_id"] for a in grouped["1111111"]) == ["p", "v"]
        assert [a["alert_id"] for a in grouped["2222222"]] == ["p"]

    def test_ignores_other_bookings(self):
        save_alert(make_alert(alert_id="a1", skymiles_number="1111111", itinerary_ref="DL-AAA"))
        save_alert(make_alert(alert_id="a1", skymiles_number="3333333", itinerary_ref="DL-BBB"))

        grouped = get_group_alert_summary("DL-AAA")

        assert list(grouped) == ["1111111"]


class TestExpireStaleAlerts:
    """expire_stale_alerts marks past-TTL active/acked alerts EXPIRED and skips the rest."""

    def test_returns_zero_when_no_alerts(self):
        assert expire_stale_alerts("9999999") == 0

    def test_expires_active_alert_with_past_ttl(self):
        save_alert(make_alert(ttl=PAST_EPOCH))

        count = expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item()["status"] == "EXPIRED"

    def test_expires_acknowledged_alert_with_past_ttl(self):
        save_alert(make_alert(status=AlertStatus.ACKNOWLEDGED, ttl=PAST_EPOCH))

        count = expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item()["status"] == "EXPIRED"

    def test_skips_alert_without_ttl(self):
        save_alert(make_alert(ttl=None))

        count = expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item()["status"] == "ACTIVE"

    def test_skips_alert_with_future_ttl(self):
        save_alert(make_alert(ttl=FAR_FUTURE_EPOCH))

        count = expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item()["status"] == "ACTIVE"

    def test_skips_resolved_alert_with_past_ttl(self):
        save_alert(make_alert(status=AlertStatus.RESOLVED, ttl=PAST_EPOCH))

        count = expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item()["status"] == "RESOLVED"

    def test_skips_already_expired_alert(self):
        save_alert(make_alert(status=AlertStatus.EXPIRED, ttl=PAST_EPOCH))

        count = expire_stale_alerts("9999999")

        assert count == 0

    def test_mixed_alerts_only_stale_ones_expired(self):
        save_alert(make_alert(alert_id="stale", ttl=PAST_EPOCH))
        save_alert(make_alert(alert_id="fresh", ttl=FAR_FUTURE_EPOCH))
        save_alert(make_alert(alert_id="no-ttl"))
        save_alert(make_alert(alert_id="done", status=AlertStatus.RESOLVED, ttl=PAST_EPOCH))

        count = expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item(alert_id="stale")["status"] == "EXPIRED"
        assert _get_item(alert_id="fresh")["status"] == "ACTIVE"
        assert _get_item(alert_id="no-ttl")["status"] == "ACTIVE"
        assert _get_item(alert_id="done")["status"] == "RESOLVED"

    def test_does_not_expire_other_members_alerts(self):
        save_alert(make_alert(skymiles_number="1111111", ttl=PAST_EPOCH))
        save_alert(make_alert(skymiles_number="2222222", ttl=PAST_EPOCH))

        count = expire_stale_alerts("1111111")

        assert count == 1
        assert _get_item("2222222")["status"] == "ACTIVE"

    def test_expired_alerts_excluded_from_active(self):
        save_alert(make_alert(ttl=PAST_EPOCH))

        expire_stale_alerts("9999999")

        assert get_active_alerts("9999999") == []
