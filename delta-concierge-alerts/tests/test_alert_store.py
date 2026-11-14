"""Unit tests for the DynamoDB-backed alert store."""

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
    """Create a moto ConciergeAlerts table (with itinerary GSI) and patch the module table."""
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


@pytest.fixture
def ddb_table(_mock_dynamodb):
    """Expose the moto table for direct reads."""
    return _mock_dynamodb


def _make_alert(**overrides) -> AlertRecord:
    """Build a valid AlertRecord with optional field overrides."""
    fields = {
        "alert_id": "alert-1",
        "skymiles_number": MEMBER,
        "alert_type": AlertType.PASSPORT,
        "severity": AlertSeverity.WARNING,
        "reasons": ["Passport expires soon"],
        "created_at": "2030-01-01T00:00:00",
        "itinerary_ref": "DL-TEST",
    }
    fields.update(overrides)
    return AlertRecord(**fields)


def _get_item(table, alert_id: str, skymiles_number: str = MEMBER) -> dict:
    return table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


class TestSaveAlert:
    """save_alert persists required fields and only includes optional fields when set."""

    def test_saves_required_fields(self, ddb_table):
        save_alert(_make_alert())

        item = _get_item(ddb_table, "alert-1")
        assert item["skymiles_number"] == MEMBER
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "WARNING"
        assert item["reasons"] == ["Passport expires soon"]
        assert item["created_at"] == "2030-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == "ACTIVE"

    def test_omits_optional_fields_when_none(self, ddb_table):
        save_alert(_make_alert())

        item = _get_item(ddb_table, "alert-1")
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_stores_ttl_when_set(self, ddb_table):
        save_alert(_make_alert(ttl=FUTURE_TTL))

        item = _get_item(ddb_table, "alert-1")
        assert int(item["ttl"]) == FUTURE_TTL

    def test_stores_resolved_at_when_set(self, ddb_table):
        save_alert(_make_alert(resolved_at="2030-01-02T00:00:00"))

        item = _get_item(ddb_table, "alert-1")
        assert item["resolved_at"] == "2030-01-02T00:00:00"

    def test_stores_resolution_when_set(self, ddb_table):
        save_alert(_make_alert(resolution="Passport renewed"))

        item = _get_item(ddb_table, "alert-1")
        assert item["resolution"] == "Passport renewed"

    def test_stores_enum_values_for_visa_critical(self, ddb_table):
        save_alert(_make_alert(
            alert_type=AlertType.VISA,
            severity=AlertSeverity.CRITICAL,
            status=AlertStatus.ACKNOWLEDGED,
        ))

        item = _get_item(ddb_table, "alert-1")
        assert item["alert_type"] == "VISA"
        assert item["severity"] == "CRITICAL"
        assert item["status"] == "ACKNOWLEDGED"

    def test_overwrites_existing_alert_with_same_key(self, ddb_table):
        save_alert(_make_alert(severity=AlertSeverity.INFO))

        save_alert(_make_alert(severity=AlertSeverity.CRITICAL))

        assert _get_item(ddb_table, "alert-1")["severity"] == "CRITICAL"


class TestGetAlertsByMember:
    """get_alerts_by_member returns every alert for a member regardless of status."""

    def test_returns_empty_list_when_no_alerts(self):
        assert get_alerts_by_member(MEMBER) == []

    def test_returns_all_alerts_for_member(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", status=AlertStatus.RESOLVED))

        result = get_alerts_by_member(MEMBER)

        assert sorted(a["alert_id"] for a in result) == ["a1", "a2"]

    def test_excludes_other_members(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", skymiles_number=OTHER_MEMBER))

        result = get_alerts_by_member(MEMBER)

        assert [a["alert_id"] for a in result] == ["a1"]


class TestAcknowledgeAlert:
    """acknowledge_alert transitions the targeted alert to ACKNOWLEDGED only."""

    def test_sets_status_acknowledged(self, ddb_table):
        save_alert(_make_alert())

        acknowledge_alert(MEMBER, "alert-1")

        assert _get_item(ddb_table, "alert-1")["status"] == "ACKNOWLEDGED"

    def test_does_not_touch_other_alerts(self, ddb_table):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2"))

        acknowledge_alert(MEMBER, "a1")

        assert _get_item(ddb_table, "a2")["status"] == "ACTIVE"


class TestResolveAlert:
    """resolve_alert sets RESOLVED status, a resolved_at timestamp, and the resolution."""

    def test_sets_status_and_resolution(self, ddb_table):
        save_alert(_make_alert())

        resolve_alert(MEMBER, "alert-1", "Passport renewed")

        item = _get_item(ddb_table, "alert-1")
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Passport renewed"

    def test_sets_resolved_at_iso_timestamp(self, ddb_table):
        save_alert(_make_alert())
        before = datetime.utcnow()

        resolve_alert(MEMBER, "alert-1", "done")

        resolved_at = datetime.fromisoformat(_get_item(ddb_table, "alert-1")["resolved_at"])
        assert before <= resolved_at <= datetime.utcnow()


class TestGetActiveAlerts:
    """get_active_alerts returns only ACTIVE and ACKNOWLEDGED alerts."""

    def test_returns_empty_list_when_no_alerts(self):
        assert get_active_alerts(MEMBER) == []

    @pytest.mark.parametrize("status", [AlertStatus.ACTIVE, AlertStatus.ACKNOWLEDGED])
    def test_includes_actionable_statuses(self, status):
        save_alert(_make_alert(status=status))

        result = get_active_alerts(MEMBER)

        assert [a["alert_id"] for a in result] == ["alert-1"]

    @pytest.mark.parametrize("status", [AlertStatus.RESOLVED, AlertStatus.EXPIRED])
    def test_excludes_closed_statuses(self, status):
        save_alert(_make_alert(status=status))

        assert get_active_alerts(MEMBER) == []

    def test_excludes_other_members(self):
        save_alert(_make_alert(skymiles_number=OTHER_MEMBER))

        assert get_active_alerts(MEMBER) == []


class TestResolveAlertsForItinerary:
    """resolve_alerts_for_itinerary resolves only actionable alerts on the matching booking."""

    def test_returns_zero_when_no_alerts(self):
        assert resolve_alerts_for_itinerary(MEMBER, "DL-TEST", "changed") == 0

    def test_resolves_active_and_acknowledged_alerts(self, ddb_table):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", status=AlertStatus.ACKNOWLEDGED))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", "Itinerary changed")

        assert count == 2
        for alert_id in ("a1", "a2"):
            item = _get_item(ddb_table, alert_id)
            assert item["status"] == "RESOLVED"
            assert item["resolution"] == "Itinerary changed"
            assert "resolved_at" in item

    def test_skips_alerts_for_other_itineraries(self, ddb_table):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", "changed")

        assert count == 1
        assert _get_item(ddb_table, "a2")["status"] == "ACTIVE"

    @pytest.mark.parametrize("status", [AlertStatus.RESOLVED, AlertStatus.EXPIRED])
    def test_skips_closed_alerts(self, ddb_table, status):
        save_alert(_make_alert(status=status, resolution="original"))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", "changed")

        assert count == 0
        item = _get_item(ddb_table, "alert-1")
        assert item["status"] == status.value
        assert item["resolution"] == "original"

    def test_skips_other_members_on_same_itinerary(self, ddb_table):
        save_alert(_make_alert(skymiles_number=OTHER_MEMBER))

        count = resolve_alerts_for_itinerary(MEMBER, "DL-TEST", "changed")

        assert count == 0
        assert _get_item(ddb_table, "alert-1", OTHER_MEMBER)["status"] == "ACTIVE"


class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the GSI across all travelers on a booking."""

    def test_returns_empty_list_when_no_alerts(self):
        assert get_alerts_by_itinerary("DL-NONE") == []

    def test_returns_alerts_across_members(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", skymiles_number=OTHER_MEMBER))

        result = get_alerts_by_itinerary("DL-TEST")

        assert sorted((a["skymiles_number"], a["alert_id"]) for a in result) == [
            (MEMBER, "a1"),
            (OTHER_MEMBER, "a2"),
        ]

    def test_includes_all_statuses(self):
        save_alert(_make_alert(alert_id="a1", status=AlertStatus.RESOLVED))

        result = get_alerts_by_itinerary("DL-TEST")

        assert [a["status"] for a in result] == ["RESOLVED"]

    def test_excludes_other_itineraries(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", itinerary_ref="DL-OTHER"))

        result = get_alerts_by_itinerary("DL-TEST")

        assert [a["alert_id"] for a in result] == ["a1"]


class TestGetGroupAlertSummary:
    """get_group_alert_summary groups a booking's alerts by skymiles_number."""

    def test_returns_empty_dict_when_no_alerts(self):
        assert get_group_alert_summary("DL-NONE") == {}

    def test_groups_alerts_by_member(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2"))
        save_alert(_make_alert(alert_id="b1", skymiles_number=OTHER_MEMBER))

        result = get_group_alert_summary("DL-TEST")

        assert set(result) == {MEMBER, OTHER_MEMBER}
        assert sorted(a["alert_id"] for a in result[MEMBER]) == ["a1", "a2"]
        assert [a["alert_id"] for a in result[OTHER_MEMBER]] == ["b1"]

    def test_excludes_other_itineraries(self):
        save_alert(_make_alert(alert_id="a1", itinerary_ref="DL-OTHER"))

        assert get_group_alert_summary("DL-TEST") == {}


class TestExpireStaleAlerts:
    """expire_stale_alerts marks only actionable alerts past their TTL as EXPIRED."""

    def test_returns_zero_when_no_alerts(self):
        assert expire_stale_alerts(MEMBER) == 0

    @pytest.mark.parametrize("status", [AlertStatus.ACTIVE, AlertStatus.ACKNOWLEDGED])
    def test_expires_actionable_alert_past_ttl(self, ddb_table, status):
        save_alert(_make_alert(ttl=PAST_TTL, status=status))

        count = expire_stale_alerts(MEMBER)

        assert count == 1
        assert _get_item(ddb_table, "alert-1")["status"] == "EXPIRED"

    def test_skips_alert_without_ttl(self, ddb_table):
        save_alert(_make_alert())

        count = expire_stale_alerts(MEMBER)

        assert count == 0
        assert _get_item(ddb_table, "alert-1")["status"] == "ACTIVE"

    def test_skips_alert_with_future_ttl(self, ddb_table):
        save_alert(_make_alert(ttl=FUTURE_TTL))

        count = expire_stale_alerts(MEMBER)

        assert count == 0
        assert _get_item(ddb_table, "alert-1")["status"] == "ACTIVE"

    @pytest.mark.parametrize("status", [AlertStatus.RESOLVED, AlertStatus.EXPIRED])
    def test_skips_closed_alert_past_ttl(self, ddb_table, status):
        save_alert(_make_alert(ttl=PAST_TTL, status=status))

        count = expire_stale_alerts(MEMBER)

        assert count == 0
        assert _get_item(ddb_table, "alert-1")["status"] == status.value

    def test_counts_only_expired_alerts_in_mixed_set(self, ddb_table):
        save_alert(_make_alert(alert_id="stale", ttl=PAST_TTL))
        save_alert(_make_alert(alert_id="fresh", ttl=FUTURE_TTL))
        save_alert(_make_alert(alert_id="no-ttl"))
        save_alert(_make_alert(alert_id="resolved", ttl=PAST_TTL, status=AlertStatus.RESOLVED))

        count = expire_stale_alerts(MEMBER)

        assert count == 1
        assert _get_item(ddb_table, "stale")["status"] == "EXPIRED"
        assert _get_item(ddb_table, "fresh")["status"] == "ACTIVE"
        assert _get_item(ddb_table, "no-ttl")["status"] == "ACTIVE"
        assert _get_item(ddb_table, "resolved")["status"] == "RESOLVED"

    def test_does_not_expire_other_members(self, ddb_table):
        save_alert(_make_alert(skymiles_number=OTHER_MEMBER, ttl=PAST_TTL))

        count = expire_stale_alerts(MEMBER)

        assert count == 0
        assert _get_item(ddb_table, "alert-1", OTHER_MEMBER)["status"] == "ACTIVE"
