"""Unit tests for the DynamoDB-backed alert store.

Covers the persistence and query surface of ``src.services.alert_store``:
optional ``save_alert`` fields, member/active/itinerary queries, resolving
matching alerts for an itinerary, group summaries, and the stale-alert
expiry branches.

Follows the project's established mocking pattern: moto ``mock_aws`` with an
autouse fixture that provisions the ``ConciergeAlerts`` DynamoDB table (plus
the ``itinerary-ref-index`` GSI) and monkey-patches the module-level
``dynamodb``/``table`` handles, restoring the originals in a ``finally``
block.
"""

from datetime import datetime

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and provision the ConciergeAlerts table for each test."""
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
# Factory helpers
# ------------------------------------------------------------------

def make_alert_record(
    alert_id: str = "alert-001",
    skymiles_number: str = "9999999",
    alert_type: AlertType = AlertType.VISA,
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    reasons: list[str] | None = None,
    created_at: str = "2026-01-01T00:00:00",
    itinerary_ref: str = "DL-TEST",
    status: AlertStatus = AlertStatus.ACTIVE,
    resolved_at: str | None = None,
    resolution: str | None = None,
    ttl: int | None = None,
) -> AlertRecord:
    """Build an AlertRecord with sensible defaults; override only what's tested."""
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


def seed_item(
    skymiles_number: str = "9999999",
    alert_id: str = "alert-001",
    status: str = AlertStatus.ACTIVE.value,
    itinerary_ref: str = "DL-TEST",
    ttl: int | None = None,
    extra: dict | None = None,
) -> dict:
    """Insert a minimal alert row directly into the mocked DynamoDB table."""
    item: dict = {
        "skymiles_number": skymiles_number,
        "alert_id": alert_id,
        "alert_type": AlertType.VISA.value,
        "severity": AlertSeverity.CRITICAL.value,
        "reasons": ["No visa on file for CN"],
        "created_at": "2026-01-01T00:00:00",
        "itinerary_ref": itinerary_ref,
        "status": status,
    }
    if ttl is not None:
        item["ttl"] = ttl
    if extra:
        item.update(extra)
    alert_store_mod.table.put_item(Item=item)
    return item


def _get(skymiles_number: str, alert_id: str) -> dict:
    """Fetch a single item back from DynamoDB."""
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


# ------------------------------------------------------------------
# save_alert — required vs optional fields
# ------------------------------------------------------------------

class TestSaveAlert:
    """save_alert persists required attributes and only writes optional ones when set."""

    def test_saves_required_fields_only(self):
        record = make_alert_record(alert_id="req-001")

        alert_store_mod.save_alert(record)

        item = _get("9999999", "req-001")
        assert item["alert_type"] == AlertType.VISA.value
        assert item["severity"] == AlertSeverity.CRITICAL.value
        assert item["reasons"] == ["No visa on file for CN"]
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == AlertStatus.ACTIVE.value

    def test_omits_optional_fields_when_none(self):
        record = make_alert_record(
            alert_id="req-002", ttl=None, resolved_at=None, resolution=None
        )

        alert_store_mod.save_alert(record)

        item = _get("9999999", "req-002")
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_persists_ttl_when_set(self):
        record = make_alert_record(alert_id="ttl-001", ttl=1893456000)

        alert_store_mod.save_alert(record)

        item = _get("9999999", "ttl-001")
        assert int(item["ttl"]) == 1893456000

    def test_persists_resolved_at_and_resolution_when_set(self):
        record = make_alert_record(
            alert_id="res-001",
            status=AlertStatus.RESOLVED,
            resolved_at="2026-02-01T00:00:00",
            resolution="Renewed visa",
        )

        alert_store_mod.save_alert(record)

        item = _get("9999999", "res-001")
        assert item["resolved_at"] == "2026-02-01T00:00:00"
        assert item["resolution"] == "Renewed visa"


# ------------------------------------------------------------------
# get_alerts_by_member
# ------------------------------------------------------------------

class TestGetAlertsByMember:
    """get_alerts_by_member returns every alert for the partition key."""

    def test_returns_all_alerts_for_member(self):
        seed_item(alert_id="a1")
        seed_item(alert_id="a2", status=AlertStatus.RESOLVED.value)
        seed_item(skymiles_number="1111111", alert_id="a3")

        alerts = alert_store_mod.get_alerts_by_member("9999999")

        assert len(alerts) == 2
        assert {a["alert_id"] for a in alerts} == {"a1", "a2"}

    def test_returns_empty_when_no_alerts(self):
        alerts = alert_store_mod.get_alerts_by_member("0000000")

        assert alerts == []


# ------------------------------------------------------------------
# acknowledge_alert / resolve_alert
# ------------------------------------------------------------------

class TestAcknowledgeAlert:
    """acknowledge_alert transitions an alert to ACKNOWLEDGED."""

    def test_marks_alert_acknowledged(self):
        seed_item(alert_id="ack-001")

        alert_store_mod.acknowledge_alert("9999999", "ack-001")

        assert _get("9999999", "ack-001")["status"] == AlertStatus.ACKNOWLEDGED.value


class TestResolveAlert:
    """resolve_alert sets RESOLVED status plus resolution and resolved_at."""

    def test_marks_alert_resolved_with_metadata(self):
        seed_item(alert_id="rslv-001")

        alert_store_mod.resolve_alert("9999999", "rslv-001", "Documents corrected")

        item = _get("9999999", "rslv-001")
        assert item["status"] == AlertStatus.RESOLVED.value
        assert item["resolution"] == "Documents corrected"
        # resolved_at is an ISO timestamp produced at call time
        assert datetime.fromisoformat(item["resolved_at"])


# ------------------------------------------------------------------
# get_active_alerts
# ------------------------------------------------------------------

class TestGetActiveAlerts:
    """get_active_alerts returns only ACTIVE or ACKNOWLEDGED alerts."""

    def test_filters_to_actionable_statuses(self):
        seed_item(alert_id="act-1", status=AlertStatus.ACTIVE.value)
        seed_item(alert_id="ack-1", status=AlertStatus.ACKNOWLEDGED.value)
        seed_item(alert_id="res-1", status=AlertStatus.RESOLVED.value)
        seed_item(alert_id="exp-1", status=AlertStatus.EXPIRED.value)

        alerts = alert_store_mod.get_active_alerts("9999999")

        assert {a["alert_id"] for a in alerts} == {"act-1", "ack-1"}


# ------------------------------------------------------------------
# resolve_alerts_for_itinerary
# ------------------------------------------------------------------

class TestResolveAlertsForItinerary:
    """resolve_alerts_for_itinerary resolves only matching actionable alerts."""

    def test_resolves_matching_active_and_acknowledged(self):
        seed_item(alert_id="m1", itinerary_ref="DL-AAA", status=AlertStatus.ACTIVE.value)
        seed_item(alert_id="m2", itinerary_ref="DL-AAA", status=AlertStatus.ACKNOWLEDGED.value)

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "9999999", "DL-AAA", "Itinerary changed, re-evaluating"
        )

        assert count == 2
        for alert_id in ("m1", "m2"):
            item = _get("9999999", alert_id)
            assert item["status"] == AlertStatus.RESOLVED.value
            assert item["resolution"] == "Itinerary changed, re-evaluating"

    def test_skips_other_itineraries(self):
        seed_item(alert_id="keep", itinerary_ref="DL-BBB", status=AlertStatus.ACTIVE.value)
        seed_item(alert_id="hit", itinerary_ref="DL-AAA", status=AlertStatus.ACTIVE.value)

        count = alert_store_mod.resolve_alerts_for_itinerary("9999999", "DL-AAA", "done")

        assert count == 1
        assert _get("9999999", "keep")["status"] == AlertStatus.ACTIVE.value

    def test_skips_already_resolved_or_expired(self):
        seed_item(alert_id="r1", itinerary_ref="DL-AAA", status=AlertStatus.RESOLVED.value)
        seed_item(alert_id="e1", itinerary_ref="DL-AAA", status=AlertStatus.EXPIRED.value)

        count = alert_store_mod.resolve_alerts_for_itinerary("9999999", "DL-AAA", "done")

        assert count == 0

    def test_returns_zero_when_no_matches(self):
        count = alert_store_mod.resolve_alerts_for_itinerary("9999999", "DL-NONE", "done")

        assert count == 0


# ------------------------------------------------------------------
# get_alerts_by_itinerary (GSI query)
# ------------------------------------------------------------------

class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the itinerary-ref-index across travelers."""

    def test_returns_alerts_across_travelers(self):
        seed_item(skymiles_number="9999999", alert_id="a1", itinerary_ref="DL-GRP")
        seed_item(skymiles_number="8888888", alert_id="a2", itinerary_ref="DL-GRP")
        seed_item(skymiles_number="7777777", alert_id="a3", itinerary_ref="DL-OTHER")

        alerts = alert_store_mod.get_alerts_by_itinerary("DL-GRP")

        assert {a["alert_id"] for a in alerts} == {"a1", "a2"}

    def test_returns_empty_for_unknown_itinerary(self):
        alerts = alert_store_mod.get_alerts_by_itinerary("DL-UNKNOWN")

        assert alerts == []


# ------------------------------------------------------------------
# get_group_alert_summary
# ------------------------------------------------------------------

class TestGetGroupAlertSummary:
    """get_group_alert_summary groups an itinerary's alerts by skymiles_number."""

    def test_groups_alerts_by_member(self):
        seed_item(skymiles_number="9999999", alert_id="a1", itinerary_ref="DL-GRP")
        seed_item(skymiles_number="9999999", alert_id="a2", itinerary_ref="DL-GRP")
        seed_item(skymiles_number="8888888", alert_id="a3", itinerary_ref="DL-GRP")

        summary = alert_store_mod.get_group_alert_summary("DL-GRP")

        assert set(summary.keys()) == {"9999999", "8888888"}
        assert len(summary["9999999"]) == 2
        assert len(summary["8888888"]) == 1

    def test_returns_empty_dict_when_no_alerts(self):
        summary = alert_store_mod.get_group_alert_summary("DL-EMPTY")

        assert summary == {}


# ------------------------------------------------------------------
# expire_stale_alerts
# ------------------------------------------------------------------

class TestExpireStaleAlerts:
    """expire_stale_alerts marks only past-TTL actionable alerts EXPIRED."""

    def test_expires_past_ttl_active_alert(self):
        past = int(datetime.utcnow().timestamp()) - 3600
        seed_item(alert_id="stale", status=AlertStatus.ACTIVE.value, ttl=past)

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 1
        assert _get("9999999", "stale")["status"] == AlertStatus.EXPIRED.value

    def test_expires_past_ttl_acknowledged_alert(self):
        past = int(datetime.utcnow().timestamp()) - 3600
        seed_item(alert_id="stale-ack", status=AlertStatus.ACKNOWLEDGED.value, ttl=past)

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 1
        assert _get("9999999", "stale-ack")["status"] == AlertStatus.EXPIRED.value

    def test_skips_alert_without_ttl(self):
        seed_item(alert_id="no-ttl", status=AlertStatus.ACTIVE.value, ttl=None)

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0
        assert _get("9999999", "no-ttl")["status"] == AlertStatus.ACTIVE.value

    def test_skips_future_ttl(self):
        future = int(datetime.utcnow().timestamp()) + 86400
        seed_item(alert_id="fresh", status=AlertStatus.ACTIVE.value, ttl=future)

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0
        assert _get("9999999", "fresh")["status"] == AlertStatus.ACTIVE.value

    def test_skips_resolved_alert_even_if_past_ttl(self):
        past = int(datetime.utcnow().timestamp()) - 3600
        seed_item(alert_id="done", status=AlertStatus.RESOLVED.value, ttl=past)

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 0
        assert _get("9999999", "done")["status"] == AlertStatus.RESOLVED.value

    def test_counts_multiple_expired(self):
        past = int(datetime.utcnow().timestamp()) - 3600
        seed_item(alert_id="s1", status=AlertStatus.ACTIVE.value, ttl=past)
        seed_item(alert_id="s2", status=AlertStatus.ACKNOWLEDGED.value, ttl=past)
        seed_item(alert_id="s3", status=AlertStatus.ACTIVE.value, ttl=past)

        count = alert_store_mod.expire_stale_alerts("9999999")

        assert count == 3
