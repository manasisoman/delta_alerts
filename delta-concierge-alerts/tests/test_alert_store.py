"""Tests for the DynamoDB-backed alert store.

Covers persisting alert records (including optional fields), member and
itinerary queries, status transitions (acknowledge/resolve), bulk
resolution for an itinerary, group summaries, and TTL-based expiry.
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


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_record(
    alert_id: str = "alert-001",
    skymiles_number: str = "9999999",
    alert_type: AlertType = AlertType.VISA,
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    reasons: list[str] | None = None,
    created_at: str = "2030-01-01T00:00:00",
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
        reasons=reasons if reasons is not None else ["No visa on file for CN"],
        created_at=created_at,
        itinerary_ref=itinerary_ref,
        status=status,
        resolved_at=resolved_at,
        resolution=resolution,
        ttl=ttl,
    )


def _get_item(skymiles_number: str = "9999999", alert_id: str = "alert-001") -> dict:
    """Read a single row straight from the mocked table."""
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    ).get("Item")


# ------------------------------------------------------------------
# save_alert
# ------------------------------------------------------------------

class TestSaveAlert:
    """save_alert writes required fields and only includes optional ones when set."""

    def test_saves_required_fields(self):
        record = _make_record()

        save_alert(record)

        item = _get_item()
        assert item["skymiles_number"] == "9999999"
        assert item["alert_id"] == "alert-001"
        assert item["alert_type"] == "VISA"
        assert item["severity"] == "CRITICAL"
        assert item["reasons"] == ["No visa on file for CN"]
        assert item["created_at"] == "2030-01-01T00:00:00"
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
        record = _make_record(ttl=1900000000)

        save_alert(record)

        assert _get_item()["ttl"] == 1900000000

    def test_includes_resolved_at_when_set(self):
        record = _make_record(resolved_at="2030-02-01T00:00:00")

        save_alert(record)

        assert _get_item()["resolved_at"] == "2030-02-01T00:00:00"

    def test_includes_resolution_when_set(self):
        record = _make_record(resolution="Visa uploaded")

        save_alert(record)

        assert _get_item()["resolution"] == "Visa uploaded"

    def test_persists_non_default_status(self):
        record = _make_record(status=AlertStatus.RESOLVED)

        save_alert(record)

        assert _get_item()["status"] == "RESOLVED"


# ------------------------------------------------------------------
# get_alerts_by_member / get_active_alerts
# ------------------------------------------------------------------

class TestMemberQueries:
    """Member queries return only that member's rows; active query filters by status."""

    def test_get_alerts_by_member_returns_all_for_member(self):
        save_alert(_make_record(alert_id="a1"))
        save_alert(_make_record(alert_id="a2", status=AlertStatus.RESOLVED))
        save_alert(_make_record(alert_id="a3", skymiles_number="1111111"))

        items = get_alerts_by_member("9999999")

        assert sorted(i["alert_id"] for i in items) == ["a1", "a2"]

    def test_get_alerts_by_member_empty_for_unknown_member(self):
        save_alert(_make_record())

        items = get_alerts_by_member("0000000")

        assert items == []

    def test_get_active_alerts_includes_active_and_acknowledged(self):
        save_alert(_make_record(alert_id="a1", status=AlertStatus.ACTIVE))
        save_alert(_make_record(alert_id="a2", status=AlertStatus.ACKNOWLEDGED))
        save_alert(_make_record(alert_id="a3", status=AlertStatus.RESOLVED))
        save_alert(_make_record(alert_id="a4", status=AlertStatus.EXPIRED))

        items = get_active_alerts("9999999")

        assert sorted(i["alert_id"] for i in items) == ["a1", "a2"]

    def test_get_active_alerts_empty_when_none_active(self):
        save_alert(_make_record(alert_id="a1", status=AlertStatus.RESOLVED))

        items = get_active_alerts("9999999")

        assert items == []


# ------------------------------------------------------------------
# acknowledge_alert / resolve_alert
# ------------------------------------------------------------------

class TestStatusTransitions:
    """Acknowledging and resolving update status and resolution metadata."""

    def test_acknowledge_sets_status(self):
        save_alert(_make_record())

        acknowledge_alert("9999999", "alert-001")

        assert _get_item()["status"] == "ACKNOWLEDGED"

    def test_acknowledge_leaves_other_fields_untouched(self):
        save_alert(_make_record())

        acknowledge_alert("9999999", "alert-001")

        item = _get_item()
        assert item["severity"] == "CRITICAL"
        assert "resolution" not in item

    def test_resolve_sets_status_resolution_and_timestamp(self):
        save_alert(_make_record())

        resolve_alert("9999999", "alert-001", "Visa uploaded")

        item = _get_item()
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Visa uploaded"
        assert "T" in item["resolved_at"]

    def test_resolve_from_acknowledged(self):
        save_alert(_make_record(status=AlertStatus.ACKNOWLEDGED))

        resolve_alert("9999999", "alert-001", "Done")

        assert _get_item()["status"] == "RESOLVED"


# ------------------------------------------------------------------
# resolve_alerts_for_itinerary
# ------------------------------------------------------------------

class TestResolveAlertsForItinerary:
    """Only active/acknowledged alerts matching the itinerary are resolved."""

    def test_resolves_matching_active_and_acknowledged(self):
        save_alert(_make_record(alert_id="a1", status=AlertStatus.ACTIVE))
        save_alert(_make_record(alert_id="a2", status=AlertStatus.ACKNOWLEDGED))

        count = resolve_alerts_for_itinerary("9999999", "DL-TEST", "Itinerary changed")

        assert count == 2
        assert _get_item(alert_id="a1")["status"] == "RESOLVED"
        assert _get_item(alert_id="a2")["status"] == "RESOLVED"
        assert _get_item(alert_id="a1")["resolution"] == "Itinerary changed"

    def test_skips_other_itinerary(self):
        save_alert(_make_record(alert_id="a1", itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary("9999999", "DL-TEST", "Itinerary changed")

        assert count == 0
        assert _get_item(alert_id="a1")["status"] == "ACTIVE"

    def test_skips_already_resolved_and_expired(self):
        save_alert(_make_record(alert_id="a1", status=AlertStatus.RESOLVED))
        save_alert(_make_record(alert_id="a2", status=AlertStatus.EXPIRED))

        count = resolve_alerts_for_itinerary("9999999", "DL-TEST", "Itinerary changed")

        assert count == 0
        assert _get_item(alert_id="a2")["status"] == "EXPIRED"

    def test_skips_other_member(self):
        save_alert(_make_record(alert_id="a1", skymiles_number="1111111"))

        count = resolve_alerts_for_itinerary("9999999", "DL-TEST", "Itinerary changed")

        assert count == 0
        assert _get_item("1111111", "a1")["status"] == "ACTIVE"

    def test_returns_zero_when_no_alerts(self):
        count = resolve_alerts_for_itinerary("9999999", "DL-TEST", "Itinerary changed")

        assert count == 0


# ------------------------------------------------------------------
# get_alerts_by_itinerary / get_group_alert_summary
# ------------------------------------------------------------------

class TestItineraryQueries:
    """Itinerary queries use the GSI and group results per traveler."""

    def test_get_alerts_by_itinerary_spans_travelers(self):
        save_alert(_make_record(alert_id="a1", skymiles_number="1111111"))
        save_alert(_make_record(alert_id="a2", skymiles_number="2222222"))
        save_alert(_make_record(alert_id="a3", skymiles_number="1111111", itinerary_ref="DL-OTHER"))

        items = get_alerts_by_itinerary("DL-TEST")

        assert sorted(i["alert_id"] for i in items) == ["a1", "a2"]

    def test_get_alerts_by_itinerary_empty_for_unknown(self):
        save_alert(_make_record())

        items = get_alerts_by_itinerary("DL-NOPE")

        assert items == []

    def test_group_summary_groups_by_skymiles_number(self):
        save_alert(_make_record(alert_id="a1", skymiles_number="1111111"))
        save_alert(_make_record(alert_id="a2", skymiles_number="1111111", alert_type=AlertType.PASSPORT))
        save_alert(_make_record(alert_id="a3", skymiles_number="2222222"))

        grouped = get_group_alert_summary("DL-TEST")

        assert set(grouped) == {"1111111", "2222222"}
        assert sorted(a["alert_id"] for a in grouped["1111111"]) == ["a1", "a2"]
        assert [a["alert_id"] for a in grouped["2222222"]] == ["a3"]

    def test_group_summary_empty_when_no_alerts(self):
        grouped = get_group_alert_summary("DL-TEST")

        assert grouped == {}


# ------------------------------------------------------------------
# expire_stale_alerts
# ------------------------------------------------------------------

class TestExpireStaleAlerts:
    """Only active/acknowledged alerts whose TTL has passed become EXPIRED."""

    PAST_TTL = 1000000000
    FUTURE_TTL = 4102444800  # 2100-01-01

    def test_expires_active_alert_with_past_ttl(self):
        save_alert(_make_record(alert_id="a1", ttl=self.PAST_TTL))

        count = expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item(alert_id="a1")["status"] == "EXPIRED"

    def test_expires_acknowledged_alert_with_past_ttl(self):
        save_alert(_make_record(alert_id="a1", ttl=self.PAST_TTL, status=AlertStatus.ACKNOWLEDGED))

        count = expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item(alert_id="a1")["status"] == "EXPIRED"

    def test_skips_alert_without_ttl(self):
        save_alert(_make_record(alert_id="a1"))

        count = expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item(alert_id="a1")["status"] == "ACTIVE"

    def test_skips_alert_with_future_ttl(self):
        save_alert(_make_record(alert_id="a1", ttl=self.FUTURE_TTL))

        count = expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item(alert_id="a1")["status"] == "ACTIVE"

    def test_skips_resolved_alert_with_past_ttl(self):
        save_alert(_make_record(alert_id="a1", ttl=self.PAST_TTL, status=AlertStatus.RESOLVED))

        count = expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item(alert_id="a1")["status"] == "RESOLVED"

    def test_ttl_equal_to_now_expires(self):
        now = int(time.time())
        save_alert(_make_record(alert_id="a1", ttl=now))

        count = expire_stale_alerts("9999999")

        assert count == 1

    def test_counts_only_expired_in_mixed_set(self):
        save_alert(_make_record(alert_id="a1", ttl=self.PAST_TTL))
        save_alert(_make_record(alert_id="a2", ttl=self.FUTURE_TTL))
        save_alert(_make_record(alert_id="a3"))
        save_alert(_make_record(alert_id="a4", ttl=self.PAST_TTL, status=AlertStatus.EXPIRED))

        count = expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item(alert_id="a1")["status"] == "EXPIRED"
        assert _get_item(alert_id="a2")["status"] == "ACTIVE"

    def test_returns_zero_for_member_with_no_alerts(self):
        count = expire_stale_alerts("0000000")

        assert count == 0
