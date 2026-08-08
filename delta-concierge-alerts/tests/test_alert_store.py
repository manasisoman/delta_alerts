"""Tests for DynamoDB-backed alert persistence in src.services.alert_store."""

from datetime import datetime

import boto3
import moto
import pytest

import src.services.alert_store as alert_store
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType


@pytest.fixture(autouse=True)
def _mock_dynamodb():
    """Create the ConciergeAlerts table (with GSI) and patch module-level clients."""
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

        orig_dynamodb = alert_store.dynamodb
        orig_table = alert_store.table
        alert_store.dynamodb = ddb
        alert_store.table = ddb.Table("ConciergeAlerts")
        try:
            yield alert_store.table
        finally:
            alert_store.dynamodb = orig_dynamodb
            alert_store.table = orig_table


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
    """Helper to build an AlertRecord with sensible defaults."""
    return AlertRecord(
        alert_id=alert_id,
        skymiles_number=skymiles_number,
        alert_type=alert_type,
        severity=severity,
        reasons=reasons if reasons is not None else ["Passport expires too soon"],
        created_at=created_at,
        itinerary_ref=itinerary_ref,
        status=status,
        resolved_at=resolved_at,
        resolution=resolution,
        ttl=ttl,
    )


def future_epoch() -> int:
    """An epoch timestamp comfortably in the future."""
    return int(datetime(2030, 1, 1).timestamp())


def past_epoch() -> int:
    """An epoch timestamp comfortably in the past."""
    return int(datetime(2020, 1, 1).timestamp())


class TestSaveAlert:
    """save_alert persists all required fields and only includes optionals when set."""

    def test_required_fields_are_stored(self):
        record = make_alert()

        alert_store.save_alert(record)

        items = alert_store.get_alerts_by_member("9999999")
        assert len(items) == 1
        item = items[0]
        assert item["alert_id"] == "alert-1"
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        assert item["reasons"] == ["Passport expires too soon"]
        assert item["created_at"] == "2030-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == "ACTIVE"

    def test_optional_fields_absent_when_none(self):
        alert_store.save_alert(make_alert())

        item = alert_store.get_alerts_by_member("9999999")[0]

        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_ttl_is_stored_when_set(self):
        alert_store.save_alert(make_alert(ttl=future_epoch()))

        item = alert_store.get_alerts_by_member("9999999")[0]

        assert int(item["ttl"]) == future_epoch()

    def test_resolved_fields_are_stored_when_set(self):
        record = make_alert(
            status=AlertStatus.RESOLVED,
            resolved_at="2030-02-01T00:00:00",
            resolution="Traveler renewed passport",
        )

        alert_store.save_alert(record)

        item = alert_store.get_alerts_by_member("9999999")[0]
        assert item["status"] == "RESOLVED"
        assert item["resolved_at"] == "2030-02-01T00:00:00"
        assert item["resolution"] == "Traveler renewed passport"


class TestGetAlertsByMember:
    """get_alerts_by_member returns only the queried member's alerts."""

    def test_returns_empty_list_for_unknown_member(self):
        assert alert_store.get_alerts_by_member("0000000") == []

    def test_scopes_results_to_partition_key(self):
        alert_store.save_alert(make_alert(alert_id="a1"))
        alert_store.save_alert(make_alert(alert_id="a2"))
        alert_store.save_alert(make_alert(alert_id="a3", skymiles_number="1111111"))

        items = alert_store.get_alerts_by_member("9999999")

        assert sorted(i["alert_id"] for i in items) == ["a1", "a2"]


class TestAcknowledgeAlert:
    """acknowledge_alert flips only the target alert to ACKNOWLEDGED."""

    def test_status_becomes_acknowledged(self):
        alert_store.save_alert(make_alert(alert_id="a1"))
        alert_store.save_alert(make_alert(alert_id="a2"))

        alert_store.acknowledge_alert("9999999", "a1")

        by_id = {i["alert_id"]: i for i in alert_store.get_alerts_by_member("9999999")}
        assert by_id["a1"]["status"] == "ACKNOWLEDGED"
        assert by_id["a2"]["status"] == "ACTIVE"


class TestResolveAlert:
    """resolve_alert sets RESOLVED plus resolution metadata."""

    def test_sets_status_resolution_and_timestamp(self):
        alert_store.save_alert(make_alert(alert_id="a1"))

        alert_store.resolve_alert("9999999", "a1", "Visa obtained")

        item = alert_store.get_alerts_by_member("9999999")[0]
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Visa obtained"
        assert item["resolved_at"]


class TestGetActiveAlerts:
    """get_active_alerts returns ACTIVE and ACKNOWLEDGED alerts only."""

    def test_filters_out_resolved_and_expired(self):
        alert_store.save_alert(make_alert(alert_id="active", status=AlertStatus.ACTIVE))
        alert_store.save_alert(
            make_alert(alert_id="acked", status=AlertStatus.ACKNOWLEDGED)
        )
        alert_store.save_alert(
            make_alert(alert_id="resolved", status=AlertStatus.RESOLVED)
        )
        alert_store.save_alert(
            make_alert(alert_id="expired", status=AlertStatus.EXPIRED)
        )

        items = alert_store.get_active_alerts("9999999")

        assert sorted(i["alert_id"] for i in items) == ["acked", "active"]

    def test_returns_empty_when_no_alerts(self):
        assert alert_store.get_active_alerts("9999999") == []


class TestResolveAlertsForItinerary:
    """resolve_alerts_for_itinerary resolves only matching itinerary + status."""

    def test_resolves_matching_active_and_acknowledged(self):
        alert_store.save_alert(make_alert(alert_id="a1", itinerary_ref="DL-AAA"))
        alert_store.save_alert(
            make_alert(
                alert_id="a2",
                itinerary_ref="DL-AAA",
                status=AlertStatus.ACKNOWLEDGED,
            )
        )

        count = alert_store.resolve_alerts_for_itinerary(
            "9999999", "DL-AAA", "Auto-resolved: itinerary changed"
        )

        assert count == 2
        items = alert_store.get_alerts_by_member("9999999")
        assert all(i["status"] == "RESOLVED" for i in items)
        assert all(i["resolution"] == "Auto-resolved: itinerary changed" for i in items)

    def test_ignores_other_itineraries(self):
        alert_store.save_alert(make_alert(alert_id="a1", itinerary_ref="DL-AAA"))
        alert_store.save_alert(make_alert(alert_id="a2", itinerary_ref="DL-BBB"))

        count = alert_store.resolve_alerts_for_itinerary("9999999", "DL-AAA", "changed")

        assert count == 1
        by_id = {i["alert_id"]: i for i in alert_store.get_alerts_by_member("9999999")}
        assert by_id["a2"]["status"] == "ACTIVE"

    def test_ignores_already_resolved_alerts(self):
        alert_store.save_alert(
            make_alert(
                alert_id="a1", itinerary_ref="DL-AAA", status=AlertStatus.RESOLVED
            )
        )

        count = alert_store.resolve_alerts_for_itinerary("9999999", "DL-AAA", "changed")

        assert count == 0

    def test_returns_zero_when_nothing_matches(self):
        assert (
            alert_store.resolve_alerts_for_itinerary("9999999", "DL-NONE", "changed")
            == 0
        )


class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the itinerary-ref GSI across travelers."""

    def test_returns_alerts_for_all_travelers_on_booking(self):
        alert_store.save_alert(
            make_alert(alert_id="a1", skymiles_number="1111111", itinerary_ref="DL-GRP")
        )
        alert_store.save_alert(
            make_alert(alert_id="a2", skymiles_number="2222222", itinerary_ref="DL-GRP")
        )
        alert_store.save_alert(
            make_alert(alert_id="a3", skymiles_number="3333333", itinerary_ref="DL-OTH")
        )

        items = alert_store.get_alerts_by_itinerary("DL-GRP")

        assert sorted(i["skymiles_number"] for i in items) == ["1111111", "2222222"]

    def test_returns_empty_for_unknown_itinerary(self):
        assert alert_store.get_alerts_by_itinerary("DL-MISSING") == []


class TestGetGroupAlertSummary:
    """get_group_alert_summary groups itinerary alerts by skymiles_number."""

    def test_groups_multiple_alerts_per_traveler(self):
        alert_store.save_alert(
            make_alert(alert_id="a1", skymiles_number="1111111", itinerary_ref="DL-GRP")
        )
        alert_store.save_alert(
            make_alert(alert_id="a2", skymiles_number="1111111", itinerary_ref="DL-GRP")
        )
        alert_store.save_alert(
            make_alert(alert_id="a3", skymiles_number="2222222", itinerary_ref="DL-GRP")
        )

        grouped = alert_store.get_group_alert_summary("DL-GRP")

        assert set(grouped) == {"1111111", "2222222"}
        assert sorted(a["alert_id"] for a in grouped["1111111"]) == ["a1", "a2"]
        assert len(grouped["2222222"]) == 1

    def test_returns_empty_dict_when_no_alerts(self):
        assert alert_store.get_group_alert_summary("DL-MISSING") == {}


class TestExpireStaleAlerts:
    """expire_stale_alerts only expires past-TTL alerts that are still actionable."""

    def test_expires_past_ttl_active_and_acknowledged(self):
        alert_store.save_alert(make_alert(alert_id="a1", ttl=past_epoch()))
        alert_store.save_alert(
            make_alert(
                alert_id="a2", ttl=past_epoch(), status=AlertStatus.ACKNOWLEDGED
            )
        )

        count = alert_store.expire_stale_alerts("9999999")

        assert count == 2
        assert all(
            i["status"] == "EXPIRED" for i in alert_store.get_alerts_by_member("9999999")
        )

    def test_skips_alerts_without_ttl(self):
        alert_store.save_alert(make_alert(alert_id="a1"))

        count = alert_store.expire_stale_alerts("9999999")

        assert count == 0
        assert alert_store.get_alerts_by_member("9999999")[0]["status"] == "ACTIVE"

    def test_skips_future_ttl(self):
        alert_store.save_alert(make_alert(alert_id="a1", ttl=future_epoch()))

        count = alert_store.expire_stale_alerts("9999999")

        assert count == 0
        assert alert_store.get_alerts_by_member("9999999")[0]["status"] == "ACTIVE"

    def test_skips_already_resolved_alerts(self):
        alert_store.save_alert(
            make_alert(alert_id="a1", ttl=past_epoch(), status=AlertStatus.RESOLVED)
        )

        count = alert_store.expire_stale_alerts("9999999")

        assert count == 0
        assert alert_store.get_alerts_by_member("9999999")[0]["status"] == "RESOLVED"

    def test_returns_zero_when_member_has_no_alerts(self):
        assert alert_store.expire_stale_alerts("0000000") == 0
