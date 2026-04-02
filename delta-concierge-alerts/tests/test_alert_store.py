"""Tests for DynamoDB-backed alert persistence in alert_store.py.

Covers the previously-missed lines: save_alert optional fields,
get_alerts_by_member, resolve_alerts_for_itinerary,
get_alerts_by_itinerary (GSI), get_group_alert_summary, and
expire_stale_alerts.
"""

import time

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
from src.models.types import (
    AlertRecord,
    AlertSeverity,
    AlertStatus,
    AlertType,
)
from src.services.alert_store import (
    expire_stale_alerts,
    get_alerts_by_itinerary,
    get_alerts_by_member,
    get_group_alert_summary,
    resolve_alerts_for_itinerary,
    save_alert,
)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up DynamoDB table with GSI for every test."""
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
            yield
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table


def _make_alert_record(
    alert_id: str = "alert-001",
    skymiles_number: str = "9999999",
    alert_type: AlertType = AlertType.PASSPORT,
    severity: AlertSeverity = AlertSeverity.WARNING,
    reasons: list[str] | None = None,
    created_at: str = "2026-01-15T10:00:00",
    itinerary_ref: str = "DL-ABC123",
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
        reasons=reasons or ["Passport expires within 6 months of travel"],
        created_at=created_at,
        itinerary_ref=itinerary_ref,
        status=status,
        resolved_at=resolved_at,
        resolution=resolution,
        ttl=ttl,
    )


def _seed_item(
    skymiles_number: str = "9999999",
    alert_id: str = "alert-001",
    status: str = AlertStatus.ACTIVE.value,
    itinerary_ref: str = "DL-ABC123",
    ttl: int | None = None,
) -> None:
    """Insert a minimal alert row directly into the mocked DynamoDB table."""
    item = {
        "skymiles_number": skymiles_number,
        "alert_id": alert_id,
        "alert_type": AlertType.PASSPORT.value,
        "severity": AlertSeverity.WARNING.value,
        "reasons": ["Test reason"],
        "created_at": "2026-01-15T10:00:00",
        "itinerary_ref": itinerary_ref,
        "status": status,
    }
    if ttl is not None:
        item["ttl"] = ttl
    alert_store_mod.table.put_item(Item=item)


# ------------------------------------------------------------------
# save_alert — optional resolved_at and resolution fields
# ------------------------------------------------------------------

class TestSaveAlertOptionalFields:
    """save_alert persists resolved_at and resolution when present."""

    def test_save_alert_with_resolved_at_and_resolution(self):
        record = _make_alert_record(
            status=AlertStatus.RESOLVED,
            resolved_at="2026-02-01T12:00:00",
            resolution="Passport renewed",
        )

        save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert item["resolved_at"] == "2026-02-01T12:00:00"
        assert item["resolution"] == "Passport renewed"
        assert item["status"] == AlertStatus.RESOLVED.value

    def test_save_alert_with_resolved_at_only(self):
        record = _make_alert_record(
            resolved_at="2026-02-01T12:00:00",
            resolution=None,
        )

        save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert item["resolved_at"] == "2026-02-01T12:00:00"
        assert "resolution" not in item

    def test_save_alert_with_resolution_only(self):
        record = _make_alert_record(
            resolved_at=None,
            resolution="Auto-resolved",
        )

        save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert "resolved_at" not in item
        assert item["resolution"] == "Auto-resolved"

    def test_save_alert_without_optional_fields(self):
        record = _make_alert_record(resolved_at=None, resolution=None)

        save_alert(record)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-001"}
        )["Item"]
        assert "resolved_at" not in item
        assert "resolution" not in item


# ------------------------------------------------------------------
# get_alerts_by_member
# ------------------------------------------------------------------

class TestGetAlertsByMember:
    """get_alerts_by_member returns all alerts for a given SkyMiles number."""

    def test_returns_all_alerts_for_member(self):
        _seed_item(alert_id="a1")
        _seed_item(alert_id="a2")
        _seed_item(alert_id="a3")

        result = get_alerts_by_member("9999999")

        assert len(result) == 3
        ids = {a["alert_id"] for a in result}
        assert ids == {"a1", "a2", "a3"}

    def test_returns_empty_for_unknown_member(self):
        _seed_item(skymiles_number="1111111", alert_id="a1")

        result = get_alerts_by_member("0000000")

        assert result == []

    def test_does_not_return_other_members_alerts(self):
        _seed_item(skymiles_number="1111111", alert_id="a1")
        _seed_item(skymiles_number="2222222", alert_id="a2")

        result = get_alerts_by_member("1111111")

        assert len(result) == 1
        assert result[0]["alert_id"] == "a1"


# ------------------------------------------------------------------
# resolve_alerts_for_itinerary
# ------------------------------------------------------------------

class TestResolveAlertsForItinerary:
    """resolve_alerts_for_itinerary resolves matching active/acked alerts."""

    def test_resolves_active_alerts_for_itinerary(self):
        _seed_item(alert_id="a1", itinerary_ref="DL-100", status=AlertStatus.ACTIVE.value)
        _seed_item(alert_id="a2", itinerary_ref="DL-100", status=AlertStatus.ACTIVE.value)

        count = resolve_alerts_for_itinerary("9999999", "DL-100", "Itinerary changed")

        assert count == 2
        for aid in ("a1", "a2"):
            item = alert_store_mod.table.get_item(
                Key={"skymiles_number": "9999999", "alert_id": aid}
            )["Item"]
            assert item["status"] == AlertStatus.RESOLVED.value
            assert item["resolution"] == "Itinerary changed"
            assert "resolved_at" in item

    def test_resolves_acknowledged_alerts(self):
        _seed_item(alert_id="a1", itinerary_ref="DL-200", status=AlertStatus.ACKNOWLEDGED.value)

        count = resolve_alerts_for_itinerary("9999999", "DL-200", "Re-evaluating")

        assert count == 1
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a1"}
        )["Item"]
        assert item["status"] == AlertStatus.RESOLVED.value

    def test_skips_already_resolved_alerts(self):
        _seed_item(alert_id="a1", itinerary_ref="DL-300", status=AlertStatus.RESOLVED.value)

        count = resolve_alerts_for_itinerary("9999999", "DL-300", "Should not touch")

        assert count == 0

    def test_skips_alerts_for_different_itinerary(self):
        _seed_item(alert_id="a1", itinerary_ref="DL-400")
        _seed_item(alert_id="a2", itinerary_ref="DL-OTHER")

        count = resolve_alerts_for_itinerary("9999999", "DL-400", "Resolve one")

        assert count == 1

    def test_returns_zero_when_no_matching_alerts(self):
        count = resolve_alerts_for_itinerary("9999999", "DL-NONE", "No alerts")

        assert count == 0


# ------------------------------------------------------------------
# get_alerts_by_itinerary (GSI query)
# ------------------------------------------------------------------

class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the itinerary-ref-index GSI."""

    def test_returns_alerts_across_members(self):
        _seed_item(skymiles_number="1111111", alert_id="a1", itinerary_ref="DL-GRP")
        _seed_item(skymiles_number="2222222", alert_id="a2", itinerary_ref="DL-GRP")

        result = get_alerts_by_itinerary("DL-GRP")

        assert len(result) == 2
        members = {a["skymiles_number"] for a in result}
        assert members == {"1111111", "2222222"}

    def test_returns_empty_for_unknown_confirmation(self):
        _seed_item(itinerary_ref="DL-EXIST")

        result = get_alerts_by_itinerary("DL-NOPE")

        assert result == []

    def test_does_not_return_different_itinerary_alerts(self):
        _seed_item(alert_id="a1", itinerary_ref="DL-X")
        _seed_item(alert_id="a2", itinerary_ref="DL-Y")

        result = get_alerts_by_itinerary("DL-X")

        assert len(result) == 1
        assert result[0]["itinerary_ref"] == "DL-X"


# ------------------------------------------------------------------
# get_group_alert_summary
# ------------------------------------------------------------------

class TestGetGroupAlertSummary:
    """get_group_alert_summary groups alerts by skymiles_number."""

    def test_groups_alerts_by_member(self):
        _seed_item(skymiles_number="1111111", alert_id="a1", itinerary_ref="DL-GRP2")
        _seed_item(skymiles_number="1111111", alert_id="a2", itinerary_ref="DL-GRP2")
        _seed_item(skymiles_number="2222222", alert_id="a3", itinerary_ref="DL-GRP2")

        result = get_group_alert_summary("DL-GRP2")

        assert set(result.keys()) == {"1111111", "2222222"}
        assert len(result["1111111"]) == 2
        assert len(result["2222222"]) == 1

    def test_returns_empty_dict_when_no_alerts(self):
        result = get_group_alert_summary("DL-EMPTY")

        assert result == {}

    def test_single_member_single_alert(self):
        _seed_item(skymiles_number="3333333", alert_id="a1", itinerary_ref="DL-SOLO")

        result = get_group_alert_summary("DL-SOLO")

        assert list(result.keys()) == ["3333333"]
        assert len(result["3333333"]) == 1


# ------------------------------------------------------------------
# expire_stale_alerts
# ------------------------------------------------------------------

class TestExpireStaleAlerts:
    """expire_stale_alerts marks alerts past their TTL as EXPIRED."""

    def test_expires_alerts_past_ttl(self):
        past_ttl = int(time.time()) - 3600  # 1 hour ago
        _seed_item(alert_id="a1", ttl=past_ttl, status=AlertStatus.ACTIVE.value)

        count = expire_stale_alerts("9999999")

        assert count == 1
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a1"}
        )["Item"]
        assert item["status"] == AlertStatus.EXPIRED.value

    def test_skips_alerts_without_ttl(self):
        _seed_item(alert_id="a1", ttl=None, status=AlertStatus.ACTIVE.value)

        count = expire_stale_alerts("9999999")

        assert count == 0
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a1"}
        )["Item"]
        assert item["status"] == AlertStatus.ACTIVE.value

    def test_skips_already_resolved_alerts(self):
        past_ttl = int(time.time()) - 3600
        _seed_item(alert_id="a1", ttl=past_ttl, status=AlertStatus.RESOLVED.value)

        count = expire_stale_alerts("9999999")

        assert count == 0
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a1"}
        )["Item"]
        assert item["status"] == AlertStatus.RESOLVED.value

    def test_skips_alerts_with_future_ttl(self):
        future_ttl = int(time.time()) + 86400  # 24 hours from now
        _seed_item(alert_id="a1", ttl=future_ttl, status=AlertStatus.ACTIVE.value)

        count = expire_stale_alerts("9999999")

        assert count == 0
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a1"}
        )["Item"]
        assert item["status"] == AlertStatus.ACTIVE.value

    def test_expires_acknowledged_alerts_past_ttl(self):
        past_ttl = int(time.time()) - 3600
        _seed_item(alert_id="a1", ttl=past_ttl, status=AlertStatus.ACKNOWLEDGED.value)

        count = expire_stale_alerts("9999999")

        assert count == 1
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a1"}
        )["Item"]
        assert item["status"] == AlertStatus.EXPIRED.value

    def test_mixed_alerts_only_expires_eligible(self):
        past_ttl = int(time.time()) - 3600
        future_ttl = int(time.time()) + 86400

        _seed_item(alert_id="a1", ttl=past_ttl, status=AlertStatus.ACTIVE.value)
        _seed_item(alert_id="a2", ttl=future_ttl, status=AlertStatus.ACTIVE.value)
        _seed_item(alert_id="a3", ttl=past_ttl, status=AlertStatus.RESOLVED.value)
        _seed_item(alert_id="a4", ttl=None, status=AlertStatus.ACTIVE.value)
        _seed_item(alert_id="a5", ttl=past_ttl, status=AlertStatus.ACKNOWLEDGED.value)

        count = expire_stale_alerts("9999999")

        assert count == 2  # a1 and a5
        assert alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a1"}
        )["Item"]["status"] == AlertStatus.EXPIRED.value
        assert alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a2"}
        )["Item"]["status"] == AlertStatus.ACTIVE.value
        assert alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a3"}
        )["Item"]["status"] == AlertStatus.RESOLVED.value
        assert alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a4"}
        )["Item"]["status"] == AlertStatus.ACTIVE.value
        assert alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a5"}
        )["Item"]["status"] == AlertStatus.EXPIRED.value

    def test_returns_zero_for_member_with_no_alerts(self):
        count = expire_stale_alerts("0000000")

        assert count == 0
