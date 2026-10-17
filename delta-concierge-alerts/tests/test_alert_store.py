"""Tests for DynamoDB-backed alert persistence in alert_store."""

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

PAST_TTL = int(datetime(2020, 1, 1).timestamp())
FUTURE_TTL = int(datetime(2030, 1, 1).timestamp())


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
            yield alert_store_mod.table
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table


def _make_alert(**overrides) -> AlertRecord:
    """Build a valid ACTIVE passport AlertRecord; override any field via kwargs."""
    defaults = dict(
        alert_id="alert-1",
        skymiles_number="1111111",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport expires within 6 months of travel to CN"],
        created_at="2030-01-01T00:00:00",
        itinerary_ref="DL-TEST",
    )
    defaults.update(overrides)
    return AlertRecord(**defaults)


def _get_item(table, skymiles_number: str = "1111111", alert_id: str = "alert-1") -> dict:
    return table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


class TestSaveAlert:
    """save_alert persists required fields and only the optional fields that are set."""

    def test_saves_required_fields(self, _mock_aws):
        alert = _make_alert()

        save_alert(alert)

        item = _get_item(_mock_aws)
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        assert item["reasons"] == ["Passport expires within 6 months of travel to CN"]
        assert item["created_at"] == "2030-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == "ACTIVE"

    def test_omits_unset_optional_fields(self, _mock_aws):
        alert = _make_alert()

        save_alert(alert)

        item = _get_item(_mock_aws)
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_stores_ttl_when_set(self, _mock_aws):
        alert = _make_alert(ttl=FUTURE_TTL)

        save_alert(alert)

        assert int(_get_item(_mock_aws)["ttl"]) == FUTURE_TTL

    def test_stores_resolved_at_when_set(self, _mock_aws):
        alert = _make_alert(resolved_at="2030-01-02T00:00:00")

        save_alert(alert)

        assert _get_item(_mock_aws)["resolved_at"] == "2030-01-02T00:00:00"

    def test_stores_resolution_when_set(self, _mock_aws):
        alert = _make_alert(resolution="Passport renewed")

        save_alert(alert)

        assert _get_item(_mock_aws)["resolution"] == "Passport renewed"

    def test_stores_visa_layover_alert(self, _mock_aws):
        alert = _make_alert(
            alert_type=AlertType.VISA,
            severity=AlertSeverity.WARNING,
            reasons=["Transit visa required for layover in CN"],
        )

        save_alert(alert)

        item = _get_item(_mock_aws)
        assert item["alert_type"] == "VISA"
        assert item["severity"] == "WARNING"
        assert item["reasons"] == ["Transit visa required for layover in CN"]


class TestGetAlertsByMember:
    """get_alerts_by_member returns every alert for the member, regardless of status."""

    def test_returns_all_member_alerts(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", status=AlertStatus.RESOLVED))

        items = get_alerts_by_member("1111111")

        assert sorted(i["alert_id"] for i in items) == ["a1", "a2"]

    def test_excludes_other_members(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", skymiles_number="2222222"))

        items = get_alerts_by_member("1111111")

        assert [i["alert_id"] for i in items] == ["a1"]

    def test_returns_empty_list_when_no_alerts(self):
        items = get_alerts_by_member("1111111")

        assert items == []


class TestStatusTransitions:
    """acknowledge_alert and resolve_alert update status and resolution metadata."""

    def test_acknowledge_sets_acknowledged_status(self, _mock_aws):
        save_alert(_make_alert())

        acknowledge_alert("1111111", "alert-1")

        assert _get_item(_mock_aws)["status"] == "ACKNOWLEDGED"

    def test_resolve_sets_status_resolution_and_timestamp(self, _mock_aws):
        save_alert(_make_alert())
        before = datetime.utcnow() - timedelta(seconds=1)

        resolve_alert("1111111", "alert-1", "Visa uploaded")

        item = _get_item(_mock_aws)
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Visa uploaded"
        assert datetime.fromisoformat(item["resolved_at"]) >= before


class TestGetActiveAlerts:
    """get_active_alerts returns only ACTIVE and ACKNOWLEDGED alerts."""

    def test_includes_active_and_acknowledged(self):
        save_alert(_make_alert(alert_id="active"))
        save_alert(_make_alert(alert_id="ack", status=AlertStatus.ACKNOWLEDGED))

        items = get_active_alerts("1111111")

        assert sorted(i["alert_id"] for i in items) == ["ack", "active"]

    def test_excludes_resolved_and_expired(self):
        save_alert(_make_alert(alert_id="active"))
        save_alert(_make_alert(alert_id="resolved", status=AlertStatus.RESOLVED))
        save_alert(_make_alert(alert_id="expired", status=AlertStatus.EXPIRED))

        items = get_active_alerts("1111111")

        assert [i["alert_id"] for i in items] == ["active"]

    def test_returns_empty_list_when_no_alerts(self):
        items = get_active_alerts("1111111")

        assert items == []


class TestResolveAlertsForItinerary:
    """Itinerary changes resolve only actionable alerts tied to that confirmation number."""

    def test_resolves_active_and_acknowledged_for_itinerary(self, _mock_aws):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", status=AlertStatus.ACKNOWLEDGED))

        count = resolve_alerts_for_itinerary(
            "1111111", "DL-TEST", "Auto-resolved: itinerary changed, re-evaluating"
        )

        assert count == 2
        for alert_id in ("a1", "a2"):
            item = _get_item(_mock_aws, alert_id=alert_id)
            assert item["status"] == "RESOLVED"
            assert item["resolution"] == "Auto-resolved: itinerary changed, re-evaluating"

    def test_ignores_alerts_for_other_itineraries(self, _mock_aws):
        save_alert(_make_alert(alert_id="other", itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary("1111111", "DL-TEST", "changed")

        assert count == 0
        assert _get_item(_mock_aws, alert_id="other")["status"] == "ACTIVE"

    def test_ignores_already_resolved_alerts(self, _mock_aws):
        save_alert(
            _make_alert(alert_id="done", status=AlertStatus.RESOLVED, resolution="Passport renewed")
        )

        count = resolve_alerts_for_itinerary("1111111", "DL-TEST", "changed")

        assert count == 0
        assert _get_item(_mock_aws, alert_id="done")["resolution"] == "Passport renewed"

    def test_ignores_other_members_on_same_itinerary(self, _mock_aws):
        save_alert(_make_alert(alert_id="companion", skymiles_number="2222222"))

        count = resolve_alerts_for_itinerary("1111111", "DL-TEST", "changed")

        assert count == 0
        assert _get_item(_mock_aws, "2222222", "companion")["status"] == "ACTIVE"

    def test_returns_zero_when_no_alerts(self):
        count = resolve_alerts_for_itinerary("1111111", "DL-TEST", "changed")

        assert count == 0


class TestItineraryQueries:
    """GSI-backed queries find and group alerts across travelers on one booking."""

    def test_get_alerts_by_itinerary_spans_travelers(self):
        save_alert(_make_alert(alert_id="a1", skymiles_number="1111111"))
        save_alert(_make_alert(alert_id="a2", skymiles_number="2222222"))
        save_alert(_make_alert(alert_id="a3", itinerary_ref="DL-OTHER"))

        items = get_alerts_by_itinerary("DL-TEST")

        assert sorted(i["alert_id"] for i in items) == ["a1", "a2"]

    def test_get_alerts_by_itinerary_empty(self):
        items = get_alerts_by_itinerary("DL-NONE")

        assert items == []

    def test_group_summary_groups_by_traveler(self):
        save_alert(_make_alert(alert_id="p1", skymiles_number="1111111"))
        save_alert(
            _make_alert(alert_id="v1", skymiles_number="1111111", alert_type=AlertType.VISA)
        )
        save_alert(_make_alert(alert_id="p2", skymiles_number="2222222"))

        summary = get_group_alert_summary("DL-TEST")

        assert set(summary) == {"1111111", "2222222"}
        assert sorted(a["alert_id"] for a in summary["1111111"]) == ["p1", "v1"]
        assert [a["alert_id"] for a in summary["2222222"]] == ["p2"]

    def test_group_summary_empty(self):
        summary = get_group_alert_summary("DL-NONE")

        assert summary == {}


class TestExpireStaleAlerts:
    """Only actionable alerts whose TTL has passed are marked EXPIRED."""

    def test_expires_active_alert_past_ttl(self, _mock_aws):
        save_alert(_make_alert(ttl=PAST_TTL))

        count = expire_stale_alerts("1111111")

        assert count == 1
        assert _get_item(_mock_aws)["status"] == "EXPIRED"

    def test_expires_acknowledged_alert_past_ttl(self, _mock_aws):
        save_alert(_make_alert(ttl=PAST_TTL, status=AlertStatus.ACKNOWLEDGED))

        count = expire_stale_alerts("1111111")

        assert count == 1
        assert _get_item(_mock_aws)["status"] == "EXPIRED"

    def test_skips_alert_without_ttl(self, _mock_aws):
        save_alert(_make_alert())

        count = expire_stale_alerts("1111111")

        assert count == 0
        assert _get_item(_mock_aws)["status"] == "ACTIVE"

    def test_skips_alert_with_future_ttl(self, _mock_aws):
        save_alert(_make_alert(ttl=FUTURE_TTL))

        count = expire_stale_alerts("1111111")

        assert count == 0
        assert _get_item(_mock_aws)["status"] == "ACTIVE"

    def test_skips_resolved_alert_past_ttl(self, _mock_aws):
        save_alert(_make_alert(ttl=PAST_TTL, status=AlertStatus.RESOLVED))

        count = expire_stale_alerts("1111111")

        assert count == 0
        assert _get_item(_mock_aws)["status"] == "RESOLVED"

    def test_counts_only_expired_alerts_in_mixed_set(self, _mock_aws):
        save_alert(_make_alert(alert_id="stale", ttl=PAST_TTL))
        save_alert(_make_alert(alert_id="fresh", ttl=FUTURE_TTL))
        save_alert(_make_alert(alert_id="no-ttl"))

        count = expire_stale_alerts("1111111")

        assert count == 1
        assert _get_item(_mock_aws, alert_id="stale")["status"] == "EXPIRED"
        assert _get_item(_mock_aws, alert_id="fresh")["status"] == "ACTIVE"
        assert _get_item(_mock_aws, alert_id="no-ttl")["status"] == "ACTIVE"
