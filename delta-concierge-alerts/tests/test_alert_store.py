"""Tests for DynamoDB-backed alert persistence in src.services.alert_store.

Covers optional-attribute persistence in save_alert, member/active/itinerary
queries, acknowledge/resolve status transitions, itinerary-change
auto-resolution, group summaries via the itinerary-ref GSI, and TTL-based
expiry of stale alerts.
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


FUTURE_TTL = int(datetime(2030, 1, 1).timestamp())
PAST_TTL = int(datetime(2020, 1, 1).timestamp())


# ------------------------------------------------------------------
# Fixtures and helpers
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto and point alert_store at a mocked ConciergeAlerts table."""
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
    """Build a valid ACTIVE CRITICAL visa AlertRecord with optional overrides."""
    fields = {
        "alert_id": "alert-001",
        "skymiles_number": "1234567890",
        "alert_type": AlertType.VISA,
        "severity": AlertSeverity.CRITICAL,
        "reasons": ["No visa on file for CN"],
        "created_at": "2030-01-01T00:00:00",
        "itinerary_ref": "DL-TEST",
    }
    fields.update(overrides)
    return AlertRecord(**fields)


def _get_item(skymiles_number: str = "1234567890", alert_id: str = "alert-001") -> dict:
    """Fetch a raw item from the mocked table."""
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


def _ids(items: list[dict]) -> set[str]:
    return {item["alert_id"] for item in items}


# ------------------------------------------------------------------
# save_alert
# ------------------------------------------------------------------

class TestSaveAlertRequiredFields:
    """save_alert persists every required field, serialising enums to values."""

    def test_happy_path_persists_required_fields(self):
        alert = _make_alert()

        save_alert(alert)

        item = _get_item()
        assert item["skymiles_number"] == "1234567890"
        assert item["alert_id"] == "alert-001"
        assert item["alert_type"] == "VISA"
        assert item["severity"] == "CRITICAL"
        assert item["reasons"] == ["No visa on file for CN"]
        assert item["created_at"] == "2030-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == "ACTIVE"

    def test_optional_fields_omitted_when_none(self):
        alert = _make_alert()

        save_alert(alert)

        item = _get_item()
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_passport_warning_alert_type_and_severity(self):
        alert = _make_alert(
            alert_type=AlertType.PASSPORT,
            severity=AlertSeverity.WARNING,
            reasons=["Passport expires within 6 months of travel to JP"],
        )

        save_alert(alert)

        item = _get_item()
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "WARNING"

    def test_layover_transit_reason_round_trips(self):
        alert = _make_alert(reasons=["Transit visa required for layover in CN"])

        save_alert(alert)

        assert _get_item()["reasons"] == ["Transit visa required for layover in CN"]

    def test_non_default_status_persisted(self):
        alert = _make_alert(status=AlertStatus.ACKNOWLEDGED)

        save_alert(alert)

        assert _get_item()["status"] == "ACKNOWLEDGED"


class TestSaveAlertOptionalFields:
    """Each optional attribute is written only when it is set on the record."""

    def test_ttl_persisted_when_set(self):
        alert = _make_alert(ttl=FUTURE_TTL)

        save_alert(alert)

        assert int(_get_item()["ttl"]) == FUTURE_TTL

    def test_resolved_at_persisted_when_set(self):
        alert = _make_alert(resolved_at="2030-01-02T00:00:00")

        save_alert(alert)

        assert _get_item()["resolved_at"] == "2030-01-02T00:00:00"

    def test_resolution_persisted_when_set(self):
        alert = _make_alert(resolution="Traveler uploaded new visa")

        save_alert(alert)

        assert _get_item()["resolution"] == "Traveler uploaded new visa"


# ------------------------------------------------------------------
# Member queries
# ------------------------------------------------------------------

class TestGetAlertsByMember:
    """get_alerts_by_member returns every alert under the member's partition key."""

    def test_returns_all_alerts_for_member(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", status=AlertStatus.RESOLVED))

        result = get_alerts_by_member("1234567890")

        assert _ids(result) == {"a1", "a2"}

    def test_excludes_other_members(self):
        save_alert(_make_alert(alert_id="mine"))
        save_alert(_make_alert(alert_id="theirs", skymiles_number="5555555555"))

        result = get_alerts_by_member("1234567890")

        assert _ids(result) == {"mine"}

    def test_returns_empty_list_when_no_alerts(self):
        result = get_alerts_by_member("0000000000")

        assert result == []


class TestGetActiveAlerts:
    """get_active_alerts returns only ACTIVE and ACKNOWLEDGED alerts."""

    @pytest.mark.parametrize("status", [AlertStatus.ACTIVE, AlertStatus.ACKNOWLEDGED])
    def test_actionable_status_included(self, status):
        save_alert(_make_alert(status=status))

        result = get_active_alerts("1234567890")

        assert _ids(result) == {"alert-001"}

    @pytest.mark.parametrize("status", [AlertStatus.RESOLVED, AlertStatus.EXPIRED])
    def test_terminal_status_excluded(self, status):
        save_alert(_make_alert(status=status))

        result = get_active_alerts("1234567890")

        assert result == []

    def test_returns_empty_list_when_no_alerts(self):
        result = get_active_alerts("0000000000")

        assert result == []


# ------------------------------------------------------------------
# Status transitions
# ------------------------------------------------------------------

class TestAcknowledgeAlert:
    """acknowledge_alert transitions an alert's status to ACKNOWLEDGED."""

    def test_sets_status_acknowledged(self):
        save_alert(_make_alert())

        acknowledge_alert("1234567890", "alert-001")

        assert _get_item()["status"] == "ACKNOWLEDGED"

    def test_does_not_touch_other_alerts(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2"))

        acknowledge_alert("1234567890", "a1")

        assert _get_item(alert_id="a2")["status"] == "ACTIVE"


class TestResolveAlert:
    """resolve_alert sets RESOLVED status, a resolved_at timestamp and resolution."""

    def test_sets_status_resolution_and_timestamp(self):
        save_alert(_make_alert())
        before = datetime.utcnow()

        resolve_alert("1234567890", "alert-001", "Visa approved")

        item = _get_item()
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Visa approved"
        assert datetime.fromisoformat(item["resolved_at"]) >= before

    def test_resolved_alert_no_longer_active(self):
        save_alert(_make_alert())

        resolve_alert("1234567890", "alert-001", "Passport renewed")

        assert get_active_alerts("1234567890") == []


# ------------------------------------------------------------------
# Itinerary changes
# ------------------------------------------------------------------

class TestResolveAlertsForItinerary:
    """Itinerary changes resolve only the member's actionable alerts for that booking."""

    RESOLUTION = "Auto-resolved: itinerary changed, re-evaluating"

    def test_resolves_active_and_acknowledged_alerts(self):
        save_alert(_make_alert(alert_id="a1"))
        save_alert(_make_alert(alert_id="a2", status=AlertStatus.ACKNOWLEDGED))

        count = resolve_alerts_for_itinerary("1234567890", "DL-TEST", self.RESOLUTION)

        assert count == 2
        for alert_id in ("a1", "a2"):
            item = _get_item(alert_id=alert_id)
            assert item["status"] == "RESOLVED"
            assert item["resolution"] == self.RESOLUTION

    def test_ignores_alerts_for_other_itineraries(self):
        save_alert(_make_alert(alert_id="other", itinerary_ref="DL-OTHER"))

        count = resolve_alerts_for_itinerary("1234567890", "DL-TEST", self.RESOLUTION)

        assert count == 0
        assert _get_item(alert_id="other")["status"] == "ACTIVE"

    @pytest.mark.parametrize("status", [AlertStatus.RESOLVED, AlertStatus.EXPIRED])
    def test_ignores_terminal_status_alerts(self, status):
        save_alert(_make_alert(status=status))

        count = resolve_alerts_for_itinerary("1234567890", "DL-TEST", self.RESOLUTION)

        assert count == 0
        assert _get_item()["status"] == status.value

    def test_ignores_other_members_on_same_itinerary(self):
        save_alert(_make_alert(alert_id="companion", skymiles_number="5555555555"))

        count = resolve_alerts_for_itinerary("1234567890", "DL-TEST", self.RESOLUTION)

        assert count == 0
        assert _get_item("5555555555", "companion")["status"] == "ACTIVE"

    def test_returns_zero_when_no_alerts(self):
        count = resolve_alerts_for_itinerary("1234567890", "DL-TEST", self.RESOLUTION)

        assert count == 0

    def test_passport_and_layover_visa_alerts_both_resolved(self):
        save_alert(_make_alert(
            alert_id="passport",
            alert_type=AlertType.PASSPORT,
            reasons=["Passport validity below 6 months required by CN"],
        ))
        save_alert(_make_alert(
            alert_id="transit",
            reasons=["Transit visa required for layover in CN"],
        ))

        count = resolve_alerts_for_itinerary("1234567890", "DL-TEST", self.RESOLUTION)

        assert count == 2
        assert get_active_alerts("1234567890") == []


# ------------------------------------------------------------------
# Group / itinerary GSI queries
# ------------------------------------------------------------------

class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the itinerary-ref GSI across travelers."""

    def test_returns_alerts_across_travelers(self):
        save_alert(_make_alert(alert_id="a1", skymiles_number="111"))
        save_alert(_make_alert(alert_id="a2", skymiles_number="222"))

        result = get_alerts_by_itinerary("DL-TEST")

        assert _ids(result) == {"a1", "a2"}

    def test_excludes_other_itineraries(self):
        save_alert(_make_alert(alert_id="mine"))
        save_alert(_make_alert(alert_id="other", itinerary_ref="DL-OTHER"))

        result = get_alerts_by_itinerary("DL-TEST")

        assert _ids(result) == {"mine"}

    def test_returns_empty_list_when_no_alerts(self):
        result = get_alerts_by_itinerary("DL-NONE")

        assert result == []


class TestGetGroupAlertSummary:
    """get_group_alert_summary groups a booking's alerts by skymiles_number."""

    def test_groups_alerts_per_traveler(self):
        save_alert(_make_alert(alert_id="p1", skymiles_number="111", alert_type=AlertType.PASSPORT))
        save_alert(_make_alert(alert_id="v1", skymiles_number="111"))
        save_alert(_make_alert(alert_id="v2", skymiles_number="222"))

        summary = get_group_alert_summary("DL-TEST")

        assert set(summary) == {"111", "222"}
        assert _ids(summary["111"]) == {"p1", "v1"}
        assert _ids(summary["222"]) == {"v2"}

    def test_excludes_travelers_on_other_itineraries(self):
        save_alert(_make_alert(alert_id="v1", skymiles_number="111"))
        save_alert(_make_alert(alert_id="v2", skymiles_number="222", itinerary_ref="DL-OTHER"))

        summary = get_group_alert_summary("DL-TEST")

        assert set(summary) == {"111"}

    def test_returns_empty_dict_when_no_alerts(self):
        summary = get_group_alert_summary("DL-NONE")

        assert summary == {}


# ------------------------------------------------------------------
# TTL expiry
# ------------------------------------------------------------------

class TestExpireStaleAlerts:
    """Only actionable alerts whose TTL has passed are transitioned to EXPIRED."""

    def test_past_ttl_active_alert_expired(self):
        save_alert(_make_alert(ttl=PAST_TTL))

        count = expire_stale_alerts("1234567890")

        assert count == 1
        assert _get_item()["status"] == "EXPIRED"

    def test_past_ttl_acknowledged_alert_expired(self):
        save_alert(_make_alert(ttl=PAST_TTL, status=AlertStatus.ACKNOWLEDGED))

        count = expire_stale_alerts("1234567890")

        assert count == 1
        assert _get_item()["status"] == "EXPIRED"

    def test_ttl_exactly_now_is_expired(self):
        now_ttl = int(datetime.utcnow().timestamp())
        save_alert(_make_alert(ttl=now_ttl))

        count = expire_stale_alerts("1234567890")

        assert count == 1

    def test_future_ttl_not_expired(self):
        save_alert(_make_alert(ttl=FUTURE_TTL))

        count = expire_stale_alerts("1234567890")

        assert count == 0
        assert _get_item()["status"] == "ACTIVE"

    def test_missing_ttl_skipped(self):
        save_alert(_make_alert())

        count = expire_stale_alerts("1234567890")

        assert count == 0
        assert _get_item()["status"] == "ACTIVE"

    def test_past_ttl_resolved_alert_not_expired(self):
        save_alert(_make_alert(ttl=PAST_TTL, status=AlertStatus.RESOLVED))

        count = expire_stale_alerts("1234567890")

        assert count == 0
        assert _get_item()["status"] == "RESOLVED"

    def test_mixed_alerts_count_only_stale(self):
        save_alert(_make_alert(alert_id="stale", ttl=PAST_TTL))
        save_alert(_make_alert(alert_id="fresh", ttl=FUTURE_TTL))
        save_alert(_make_alert(alert_id="no-ttl"))

        count = expire_stale_alerts("1234567890")

        assert count == 1
        assert _get_item(alert_id="stale")["status"] == "EXPIRED"
        assert _get_item(alert_id="fresh")["status"] == "ACTIVE"
        assert _get_item(alert_id="no-ttl")["status"] == "ACTIVE"

    def test_other_members_untouched(self):
        save_alert(_make_alert(skymiles_number="5555555555", ttl=PAST_TTL))

        count = expire_stale_alerts("1234567890")

        assert count == 0
        assert _get_item("5555555555")["status"] == "ACTIVE"
