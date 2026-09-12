"""Tests for the DynamoDB-backed alert store.

Covers save_alert's optional-attribute branches, member/itinerary queries,
status transitions (acknowledge, resolve, expire) and the bulk
resolve-for-itinerary and group-summary helpers.
"""

import time

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


# ------------------------------------------------------------------
# Fixtures
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


def _make_record(
    alert_id: str = "alert-001",
    skymiles_number: str = "9999999",
    itinerary_ref: str = "DL-TEST",
    status: AlertStatus = AlertStatus.ACTIVE,
    ttl: int | None = None,
    resolved_at: str | None = None,
    resolution: str | None = None,
) -> AlertRecord:
    """Build an AlertRecord with sensible defaults."""
    return AlertRecord(
        alert_id=alert_id,
        skymiles_number=skymiles_number,
        alert_type=AlertType.VISA,
        severity=AlertSeverity.CRITICAL,
        reasons=["No visa on file for CN"],
        created_at="2026-01-01T00:00:00",
        itinerary_ref=itinerary_ref,
        status=status,
        ttl=ttl,
        resolved_at=resolved_at,
        resolution=resolution,
    )


def _get_item(skymiles_number: str = "9999999", alert_id: str = "alert-001") -> dict:
    """Fetch a single raw item from the mocked table."""
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    ).get("Item")


# ------------------------------------------------------------------
# save_alert
# ------------------------------------------------------------------

class TestSaveAlert:
    """save_alert persists required fields and only sets optional ones when present."""

    def test_persists_required_fields(self):
        record = _make_record()

        save_alert(record)

        item = _get_item()
        assert item["skymiles_number"] == "9999999"
        assert item["alert_id"] == "alert-001"
        assert item["alert_type"] == "VISA"
        assert item["severity"] == "CRITICAL"
        assert item["reasons"] == ["No visa on file for CN"]
        assert item["created_at"] == "2026-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == "ACTIVE"

    def test_omits_optional_fields_when_none(self):
        record = _make_record()

        save_alert(record)

        item = _get_item()
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_includes_ttl_when_set(self):
        record = _make_record(ttl=1_900_000_000)

        save_alert(record)

        assert _get_item()["ttl"] == 1_900_000_000

    def test_includes_resolved_at_when_set(self):
        record = _make_record(resolved_at="2026-02-01T00:00:00")

        save_alert(record)

        assert _get_item()["resolved_at"] == "2026-02-01T00:00:00"

    def test_includes_resolution_when_set(self):
        record = _make_record(resolution="Visa uploaded")

        save_alert(record)

        assert _get_item()["resolution"] == "Visa uploaded"

    def test_persists_non_default_status(self):
        record = _make_record(status=AlertStatus.RESOLVED)

        save_alert(record)

        assert _get_item()["status"] == "RESOLVED"


# ------------------------------------------------------------------
# Member queries
# ------------------------------------------------------------------

class TestGetAlertsByMember:
    """get_alerts_by_member returns only the partition's items regardless of status."""

    def test_returns_empty_list_when_no_alerts(self):
        result = get_alerts_by_member("0000000")

        assert result == []

    def test_returns_all_alerts_for_member_only(self):
        save_alert(_make_record(alert_id="a1"))
        save_alert(_make_record(alert_id="a2", status=AlertStatus.RESOLVED))
        save_alert(_make_record(alert_id="a3", skymiles_number="1111111"))

        result = get_alerts_by_member("9999999")

        assert sorted(a["alert_id"] for a in result) == ["a1", "a2"]


class TestGetActiveAlerts:
    """get_active_alerts filters to ACTIVE and ACKNOWLEDGED statuses."""

    def test_returns_empty_list_when_no_alerts(self):
        result = get_active_alerts("0000000")

        assert result == []

    def test_excludes_resolved_and_expired(self):
        save_alert(_make_record(alert_id="active", status=AlertStatus.ACTIVE))
        save_alert(_make_record(alert_id="acked", status=AlertStatus.ACKNOWLEDGED))
        save_alert(_make_record(alert_id="resolved", status=AlertStatus.RESOLVED))
        save_alert(_make_record(alert_id="expired", status=AlertStatus.EXPIRED))

        result = get_active_alerts("9999999")

        assert sorted(a["alert_id"] for a in result) == ["acked", "active"]


# ------------------------------------------------------------------
# Status transitions
# ------------------------------------------------------------------

class TestAcknowledgeAlert:
    """acknowledge_alert sets status to ACKNOWLEDGED without touching other fields."""

    def test_sets_status_acknowledged(self):
        save_alert(_make_record())

        acknowledge_alert("9999999", "alert-001")

        item = _get_item()
        assert item["status"] == "ACKNOWLEDGED"
        assert item["reasons"] == ["No visa on file for CN"]


class TestResolveAlert:
    """resolve_alert sets RESOLVED status, a resolution and a resolved_at timestamp."""

    def test_sets_resolved_fields(self):
        save_alert(_make_record())

        resolve_alert("9999999", "alert-001", "Visa uploaded")

        item = _get_item()
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Visa uploaded"
        assert item["resolved_at"].startswith("20")

    def test_resolved_alert_no_longer_active(self):
        save_alert(_make_record())

        resolve_alert("9999999", "alert-001", "Visa uploaded")

        assert get_active_alerts("9999999") == []


# ------------------------------------------------------------------
# resolve_alerts_for_itinerary
# ------------------------------------------------------------------

class TestResolveAlertsForItinerary:
    """resolve_alerts_for_itinerary resolves only matching, actionable alerts."""

    def test_returns_zero_when_nothing_matches(self):
        save_alert(_make_record(itinerary_ref="OTHER"))

        count = resolve_alerts_for_itinerary("9999999", "DL-TEST", "changed")

        assert count == 0
        assert _get_item()["status"] == "ACTIVE"

    def test_resolves_active_and_acknowledged_for_itinerary(self):
        save_alert(_make_record(alert_id="a1", status=AlertStatus.ACTIVE))
        save_alert(_make_record(alert_id="a2", status=AlertStatus.ACKNOWLEDGED))

        count = resolve_alerts_for_itinerary("9999999", "DL-TEST", "changed")

        assert count == 2
        for alert_id in ("a1", "a2"):
            item = _get_item(alert_id=alert_id)
            assert item["status"] == "RESOLVED"
            assert item["resolution"] == "changed"
            assert "resolved_at" in item

    def test_skips_already_resolved_alerts(self):
        save_alert(_make_record(alert_id="done", status=AlertStatus.RESOLVED))
        save_alert(_make_record(alert_id="open", status=AlertStatus.ACTIVE))

        count = resolve_alerts_for_itinerary("9999999", "DL-TEST", "changed")

        assert count == 1

    def test_skips_other_itineraries(self):
        save_alert(_make_record(alert_id="match", itinerary_ref="DL-TEST"))
        save_alert(_make_record(alert_id="other", itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary("9999999", "DL-TEST", "changed")

        assert count == 1
        assert _get_item(alert_id="other")["status"] == "ACTIVE"

    def test_skips_other_members(self):
        save_alert(_make_record(alert_id="mine"))
        save_alert(_make_record(alert_id="theirs", skymiles_number="1111111"))

        count = resolve_alerts_for_itinerary("9999999", "DL-TEST", "changed")

        assert count == 1
        assert _get_item("1111111", "theirs")["status"] == "ACTIVE"


# ------------------------------------------------------------------
# Itinerary (GSI) queries
# ------------------------------------------------------------------

class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary finds alerts across travelers via the GSI."""

    def test_returns_empty_list_when_no_alerts(self):
        result = get_alerts_by_itinerary("NOPE")

        assert result == []

    def test_returns_alerts_across_members(self):
        save_alert(_make_record(alert_id="a1", skymiles_number="1111111"))
        save_alert(_make_record(alert_id="a2", skymiles_number="2222222"))
        save_alert(_make_record(alert_id="a3", itinerary_ref="DL-OTHER"))

        result = get_alerts_by_itinerary("DL-TEST")

        assert sorted(a["alert_id"] for a in result) == ["a1", "a2"]


class TestGetGroupAlertSummary:
    """get_group_alert_summary groups a booking's alerts by skymiles_number."""

    def test_returns_empty_dict_when_no_alerts(self):
        result = get_group_alert_summary("NOPE")

        assert result == {}

    def test_groups_by_member(self):
        save_alert(_make_record(alert_id="a1", skymiles_number="1111111"))
        save_alert(_make_record(alert_id="a2", skymiles_number="1111111"))
        save_alert(_make_record(alert_id="b1", skymiles_number="2222222"))

        result = get_group_alert_summary("DL-TEST")

        assert set(result) == {"1111111", "2222222"}
        assert sorted(a["alert_id"] for a in result["1111111"]) == ["a1", "a2"]
        assert [a["alert_id"] for a in result["2222222"]] == ["b1"]


# ------------------------------------------------------------------
# expire_stale_alerts
# ------------------------------------------------------------------

class TestExpireStaleAlerts:
    """expire_stale_alerts marks only actionable alerts with a past TTL as EXPIRED."""

    PAST_TTL = int(time.time()) - 3600
    FUTURE_TTL = int(time.time()) + 3600

    def test_returns_zero_when_no_alerts(self):
        count = expire_stale_alerts("0000000")

        assert count == 0

    def test_skips_alerts_without_ttl(self):
        save_alert(_make_record())

        count = expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item()["status"] == "ACTIVE"

    def test_skips_alerts_with_future_ttl(self):
        save_alert(_make_record(ttl=self.FUTURE_TTL))

        count = expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item()["status"] == "ACTIVE"

    def test_expires_active_alert_with_past_ttl(self):
        save_alert(_make_record(ttl=self.PAST_TTL))

        count = expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item()["status"] == "EXPIRED"

    def test_expires_acknowledged_alert_with_past_ttl(self):
        save_alert(_make_record(status=AlertStatus.ACKNOWLEDGED, ttl=self.PAST_TTL))

        count = expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item()["status"] == "EXPIRED"

    def test_skips_resolved_alert_with_past_ttl(self):
        save_alert(_make_record(status=AlertStatus.RESOLVED, ttl=self.PAST_TTL))

        count = expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item()["status"] == "RESOLVED"

    def test_counts_only_expired_in_mixed_set(self):
        save_alert(_make_record(alert_id="stale", ttl=self.PAST_TTL))
        save_alert(_make_record(alert_id="fresh", ttl=self.FUTURE_TTL))
        save_alert(_make_record(alert_id="no-ttl"))
        save_alert(_make_record(alert_id="done", status=AlertStatus.RESOLVED, ttl=self.PAST_TTL))

        count = expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item(alert_id="stale")["status"] == "EXPIRED"
        assert _get_item(alert_id="fresh")["status"] == "ACTIVE"
        assert _get_item(alert_id="no-ttl")["status"] == "ACTIVE"
        assert _get_item(alert_id="done")["status"] == "RESOLVED"
