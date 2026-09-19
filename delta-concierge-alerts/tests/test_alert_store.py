"""Tests for the DynamoDB-backed alert store.

Covers persistence of optional AlertRecord fields, member/itinerary
queries (including the ``itinerary-ref-index`` GSI), status transitions
(acknowledge, resolve), bulk resolution on itinerary change, grouping of
alerts per traveler for a shared booking, and TTL-based expiry.
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
    """Activate moto and create the alerts table (with its GSI) for every test."""
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
    alert_type: AlertType = AlertType.PASSPORT,
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    reasons: list[str] | None = None,
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
        reasons=reasons if reasons is not None else ["Passport has expired"],
        created_at="2026-01-01T00:00:00",
        itinerary_ref=itinerary_ref,
        status=status,
        resolved_at=resolved_at,
        resolution=resolution,
        ttl=ttl,
    )


def _seed_alert(**overrides) -> AlertRecord:
    """Persist an AlertRecord built from ``overrides`` and return it."""
    record = _make_record(**overrides)
    save_alert(record)
    return record


def _get_item(skymiles_number: str = "9999999", alert_id: str = "alert-001") -> dict:
    """Read a single alert row straight from the mocked table."""
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


def _past_epoch() -> int:
    return int(time.time()) - 3600


def _future_epoch() -> int:
    return int(time.time()) + 3600


# ------------------------------------------------------------------
# save_alert
# ------------------------------------------------------------------

class TestSaveAlert:
    """Required fields are always written; optional fields only when set."""

    def test_saves_required_fields_and_omits_optional_ones(self):
        record = _make_record()

        save_alert(record)

        item = _get_item()
        assert item["skymiles_number"] == "9999999"
        assert item["alert_id"] == "alert-001"
        assert item["alert_type"] == AlertType.PASSPORT.value
        assert item["severity"] == AlertSeverity.CRITICAL.value
        assert item["reasons"] == ["Passport has expired"]
        assert item["created_at"] == "2026-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == AlertStatus.ACTIVE.value
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_saves_ttl_when_present(self):
        record = _make_record(ttl=1_800_000_000)

        save_alert(record)

        assert _get_item()["ttl"] == 1_800_000_000

    def test_saves_resolved_at_when_present(self):
        record = _make_record(resolved_at="2026-02-01T00:00:00")

        save_alert(record)

        assert _get_item()["resolved_at"] == "2026-02-01T00:00:00"

    def test_saves_resolution_when_present(self):
        record = _make_record(resolution="Renewed passport")

        save_alert(record)

        assert _get_item()["resolution"] == "Renewed passport"

    def test_saves_non_default_status(self):
        record = _make_record(status=AlertStatus.RESOLVED)

        save_alert(record)

        assert _get_item()["status"] == AlertStatus.RESOLVED.value

    def test_overwrites_existing_alert_with_same_key(self):
        _seed_alert(severity=AlertSeverity.INFO)

        save_alert(_make_record(severity=AlertSeverity.CRITICAL))

        assert _get_item()["severity"] == AlertSeverity.CRITICAL.value
        assert len(get_alerts_by_member("9999999")) == 1


# ------------------------------------------------------------------
# get_alerts_by_member
# ------------------------------------------------------------------

class TestGetAlertsByMember:
    """Returns every alert for the partition key regardless of status."""

    def test_returns_all_alerts_for_member(self):
        _seed_alert(alert_id="a1", status=AlertStatus.ACTIVE)
        _seed_alert(alert_id="a2", status=AlertStatus.RESOLVED)
        _seed_alert(alert_id="a3", status=AlertStatus.EXPIRED)

        alerts = get_alerts_by_member("9999999")

        assert sorted(a["alert_id"] for a in alerts) == ["a1", "a2", "a3"]

    def test_excludes_other_members(self):
        _seed_alert(alert_id="mine")
        _seed_alert(alert_id="theirs", skymiles_number="1111111")

        alerts = get_alerts_by_member("9999999")

        assert [a["alert_id"] for a in alerts] == ["mine"]

    def test_returns_empty_list_for_unknown_member(self):
        alerts = get_alerts_by_member("0000000")

        assert alerts == []


# ------------------------------------------------------------------
# acknowledge_alert
# ------------------------------------------------------------------

class TestAcknowledgeAlert:
    """Sets status to ACKNOWLEDGED without touching other attributes."""

    def test_sets_status_to_acknowledged(self):
        _seed_alert(reasons=["No visa on file for CN"])

        acknowledge_alert("9999999", "alert-001")

        item = _get_item()
        assert item["status"] == AlertStatus.ACKNOWLEDGED.value
        assert item["reasons"] == ["No visa on file for CN"]

    def test_only_targets_the_given_alert(self):
        _seed_alert(alert_id="a1")
        _seed_alert(alert_id="a2")

        acknowledge_alert("9999999", "a1")

        assert _get_item(alert_id="a1")["status"] == AlertStatus.ACKNOWLEDGED.value
        assert _get_item(alert_id="a2")["status"] == AlertStatus.ACTIVE.value


# ------------------------------------------------------------------
# resolve_alert
# ------------------------------------------------------------------

class TestResolveAlert:
    """Sets status to RESOLVED and records resolution details."""

    def test_sets_status_resolution_and_timestamp(self):
        _seed_alert()

        resolve_alert("9999999", "alert-001", "Renewed passport")

        item = _get_item()
        assert item["status"] == AlertStatus.RESOLVED.value
        assert item["resolution"] == "Renewed passport"
        assert isinstance(item["resolved_at"], str)
        assert item["resolved_at"].startswith("20")

    def test_resolves_acknowledged_alert(self):
        _seed_alert(status=AlertStatus.ACKNOWLEDGED)

        resolve_alert("9999999", "alert-001", "Obtained visa")

        assert _get_item()["status"] == AlertStatus.RESOLVED.value


# ------------------------------------------------------------------
# get_active_alerts
# ------------------------------------------------------------------

class TestGetActiveAlerts:
    """Only ACTIVE and ACKNOWLEDGED alerts are considered actionable."""

    def test_includes_active_alerts(self):
        _seed_alert(alert_id="a1", status=AlertStatus.ACTIVE)

        alerts = get_active_alerts("9999999")

        assert [a["alert_id"] for a in alerts] == ["a1"]

    def test_includes_acknowledged_alerts(self):
        _seed_alert(alert_id="a1", status=AlertStatus.ACKNOWLEDGED)

        alerts = get_active_alerts("9999999")

        assert [a["alert_id"] for a in alerts] == ["a1"]

    def test_excludes_resolved_alerts(self):
        _seed_alert(alert_id="a1", status=AlertStatus.RESOLVED)

        alerts = get_active_alerts("9999999")

        assert alerts == []

    def test_excludes_expired_alerts(self):
        _seed_alert(alert_id="a1", status=AlertStatus.EXPIRED)

        alerts = get_active_alerts("9999999")

        assert alerts == []

    def test_filters_mixed_statuses(self):
        _seed_alert(alert_id="a1", status=AlertStatus.ACTIVE)
        _seed_alert(alert_id="a2", status=AlertStatus.ACKNOWLEDGED)
        _seed_alert(alert_id="a3", status=AlertStatus.RESOLVED)
        _seed_alert(alert_id="a4", status=AlertStatus.EXPIRED)

        alerts = get_active_alerts("9999999")

        assert sorted(a["alert_id"] for a in alerts) == ["a1", "a2"]


# ------------------------------------------------------------------
# resolve_alerts_for_itinerary
# ------------------------------------------------------------------

class TestResolveAlertsForItinerary:
    """Itinerary changes auto-resolve only actionable alerts on that booking."""

    def test_resolves_active_alerts_for_matching_itinerary(self):
        _seed_alert(alert_id="a1", itinerary_ref="DL-ABC", status=AlertStatus.ACTIVE)
        _seed_alert(alert_id="a2", itinerary_ref="DL-ABC", status=AlertStatus.ACKNOWLEDGED)

        count = resolve_alerts_for_itinerary(
            "9999999", "DL-ABC", "Auto-resolved: itinerary changed, re-evaluating"
        )

        assert count == 2
        for alert_id in ("a1", "a2"):
            item = _get_item(alert_id=alert_id)
            assert item["status"] == AlertStatus.RESOLVED.value
            assert item["resolution"] == "Auto-resolved: itinerary changed, re-evaluating"
            assert "resolved_at" in item

    def test_leaves_other_itineraries_untouched(self):
        _seed_alert(alert_id="changed", itinerary_ref="DL-ABC")
        _seed_alert(alert_id="unchanged", itinerary_ref="DL-XYZ")

        count = resolve_alerts_for_itinerary("9999999", "DL-ABC", "Itinerary changed")

        assert count == 1
        assert _get_item(alert_id="changed")["status"] == AlertStatus.RESOLVED.value
        assert _get_item(alert_id="unchanged")["status"] == AlertStatus.ACTIVE.value

    def test_skips_already_resolved_alerts(self):
        _seed_alert(
            alert_id="done",
            itinerary_ref="DL-ABC",
            status=AlertStatus.RESOLVED,
            resolution="Renewed passport",
        )

        count = resolve_alerts_for_itinerary("9999999", "DL-ABC", "Itinerary changed")

        assert count == 0
        assert _get_item(alert_id="done")["resolution"] == "Renewed passport"

    def test_skips_expired_alerts(self):
        _seed_alert(alert_id="old", itinerary_ref="DL-ABC", status=AlertStatus.EXPIRED)

        count = resolve_alerts_for_itinerary("9999999", "DL-ABC", "Itinerary changed")

        assert count == 0
        assert _get_item(alert_id="old")["status"] == AlertStatus.EXPIRED.value

    def test_does_not_touch_other_members_on_same_booking(self):
        _seed_alert(alert_id="a1", skymiles_number="1111111", itinerary_ref="DL-GROUP")
        _seed_alert(alert_id="a1", skymiles_number="2222222", itinerary_ref="DL-GROUP")

        count = resolve_alerts_for_itinerary("1111111", "DL-GROUP", "Itinerary changed")

        assert count == 1
        assert _get_item("1111111", "a1")["status"] == AlertStatus.RESOLVED.value
        assert _get_item("2222222", "a1")["status"] == AlertStatus.ACTIVE.value

    def test_returns_zero_when_nothing_matches(self):
        count = resolve_alerts_for_itinerary("9999999", "DL-NONE", "Itinerary changed")

        assert count == 0


# ------------------------------------------------------------------
# get_alerts_by_itinerary
# ------------------------------------------------------------------

class TestGetAlertsByItinerary:
    """Queries the itinerary-ref-index GSI across all travelers on a booking."""

    def test_returns_alerts_across_travelers(self):
        _seed_alert(alert_id="a1", skymiles_number="1111111", itinerary_ref="DL-GROUP")
        _seed_alert(alert_id="a1", skymiles_number="2222222", itinerary_ref="DL-GROUP")
        _seed_alert(alert_id="a2", skymiles_number="2222222", itinerary_ref="DL-GROUP")

        alerts = get_alerts_by_itinerary("DL-GROUP")

        assert len(alerts) == 3
        assert {a["skymiles_number"] for a in alerts} == {"1111111", "2222222"}

    def test_excludes_other_itineraries(self):
        _seed_alert(alert_id="a1", itinerary_ref="DL-GROUP")
        _seed_alert(alert_id="a2", itinerary_ref="DL-OTHER")

        alerts = get_alerts_by_itinerary("DL-GROUP")

        assert [a["alert_id"] for a in alerts] == ["a1"]

    def test_returns_empty_list_for_unknown_itinerary(self):
        alerts = get_alerts_by_itinerary("DL-NONE")

        assert alerts == []


# ------------------------------------------------------------------
# get_group_alert_summary
# ------------------------------------------------------------------

class TestGetGroupAlertSummary:
    """Alerts for a booking are bucketed by traveler skymiles_number."""

    def test_groups_alerts_by_traveler(self):
        _seed_alert(alert_id="pp", skymiles_number="1111111", itinerary_ref="DL-GROUP",
                    alert_type=AlertType.PASSPORT)
        _seed_alert(alert_id="visa", skymiles_number="1111111", itinerary_ref="DL-GROUP",
                    alert_type=AlertType.VISA)
        _seed_alert(alert_id="visa", skymiles_number="2222222", itinerary_ref="DL-GROUP",
                    alert_type=AlertType.VISA)

        grouped = get_group_alert_summary("DL-GROUP")

        assert set(grouped) == {"1111111", "2222222"}
        assert sorted(a["alert_id"] for a in grouped["1111111"]) == ["pp", "visa"]
        assert [a["alert_id"] for a in grouped["2222222"]] == ["visa"]

    def test_travelers_without_alerts_are_absent(self):
        _seed_alert(alert_id="a1", skymiles_number="1111111", itinerary_ref="DL-GROUP")

        grouped = get_group_alert_summary("DL-GROUP")

        assert list(grouped) == ["1111111"]

    def test_returns_empty_dict_for_unknown_itinerary(self):
        grouped = get_group_alert_summary("DL-NONE")

        assert grouped == {}


# ------------------------------------------------------------------
# expire_stale_alerts
# ------------------------------------------------------------------

class TestExpireStaleAlerts:
    """Only actionable alerts whose TTL has passed transition to EXPIRED."""

    def test_expires_active_alert_with_past_ttl(self):
        _seed_alert(alert_id="a1", status=AlertStatus.ACTIVE, ttl=_past_epoch())

        count = expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item(alert_id="a1")["status"] == AlertStatus.EXPIRED.value

    def test_expires_acknowledged_alert_with_past_ttl(self):
        _seed_alert(alert_id="a1", status=AlertStatus.ACKNOWLEDGED, ttl=_past_epoch())

        count = expire_stale_alerts("9999999")

        assert count == 1
        assert _get_item(alert_id="a1")["status"] == AlertStatus.EXPIRED.value

    def test_expires_alert_whose_ttl_is_exactly_now(self):
        _seed_alert(alert_id="a1", ttl=int(time.time()))

        count = expire_stale_alerts("9999999")

        assert count == 1

    def test_skips_alert_with_future_ttl(self):
        _seed_alert(alert_id="a1", ttl=_future_epoch())

        count = expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item(alert_id="a1")["status"] == AlertStatus.ACTIVE.value

    def test_skips_alert_without_ttl(self):
        _seed_alert(alert_id="a1")

        count = expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item(alert_id="a1")["status"] == AlertStatus.ACTIVE.value

    def test_skips_resolved_alert_with_past_ttl(self):
        _seed_alert(alert_id="a1", status=AlertStatus.RESOLVED, ttl=_past_epoch())

        count = expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item(alert_id="a1")["status"] == AlertStatus.RESOLVED.value

    def test_skips_already_expired_alert(self):
        _seed_alert(alert_id="a1", status=AlertStatus.EXPIRED, ttl=_past_epoch())

        count = expire_stale_alerts("9999999")

        assert count == 0

    def test_counts_only_transitioned_alerts_in_mixed_set(self):
        _seed_alert(alert_id="stale-active", status=AlertStatus.ACTIVE, ttl=_past_epoch())
        _seed_alert(alert_id="stale-ack", status=AlertStatus.ACKNOWLEDGED, ttl=_past_epoch())
        _seed_alert(alert_id="fresh", status=AlertStatus.ACTIVE, ttl=_future_epoch())
        _seed_alert(alert_id="no-ttl", status=AlertStatus.ACTIVE)
        _seed_alert(alert_id="stale-resolved", status=AlertStatus.RESOLVED, ttl=_past_epoch())

        count = expire_stale_alerts("9999999")

        assert count == 2
        assert _get_item(alert_id="stale-active")["status"] == AlertStatus.EXPIRED.value
        assert _get_item(alert_id="stale-ack")["status"] == AlertStatus.EXPIRED.value
        assert _get_item(alert_id="fresh")["status"] == AlertStatus.ACTIVE.value
        assert _get_item(alert_id="no-ttl")["status"] == AlertStatus.ACTIVE.value
        assert _get_item(alert_id="stale-resolved")["status"] == AlertStatus.RESOLVED.value

    def test_does_not_touch_other_members(self):
        _seed_alert(alert_id="a1", skymiles_number="1111111", ttl=_past_epoch())

        count = expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item("1111111", "a1")["status"] == AlertStatus.ACTIVE.value

    def test_returns_zero_for_member_without_alerts(self):
        count = expire_stale_alerts("0000000")

        assert count == 0
