"""Unit tests for the DynamoDB-backed alert store."""

from datetime import datetime

import boto3
import moto
import pytest

import src.services.alert_store as alert_store
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto and point the module-level table at a mocked DynamoDB."""
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
            yield
        finally:
            alert_store.dynamodb = orig_dynamodb
            alert_store.table = orig_table


def _make_record(
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
        reasons=reasons if reasons is not None else ["Passport expires too soon"],
        created_at=created_at,
        itinerary_ref=itinerary_ref,
        status=status,
        resolved_at=resolved_at,
        resolution=resolution,
        ttl=ttl,
    )


def _get_item(skymiles_number: str = "9999999", alert_id: str = "alert-1") -> dict:
    """Read a single item straight from the mocked table."""
    return alert_store.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


class TestSaveAlert:
    """save_alert persists required fields and only includes set optionals."""

    def test_saves_required_fields(self):
        record = _make_record()

        alert_store.save_alert(record)

        item = _get_item()
        assert item["skymiles_number"] == "9999999"
        assert item["alert_id"] == "alert-1"
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        assert item["reasons"] == ["Passport expires too soon"]
        assert item["created_at"] == "2026-01-01T00:00:00"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == "ACTIVE"

    def test_omits_optional_fields_when_unset(self):
        record = _make_record()

        alert_store.save_alert(record)

        item = _get_item()
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_includes_ttl_when_set(self):
        record = _make_record(ttl=1893456000)

        alert_store.save_alert(record)

        assert int(_get_item()["ttl"]) == 1893456000

    def test_includes_resolved_at_and_resolution_when_set(self):
        record = _make_record(
            status=AlertStatus.RESOLVED,
            resolved_at="2026-02-01T00:00:00",
            resolution="Passport renewed",
        )

        alert_store.save_alert(record)

        item = _get_item()
        assert item["status"] == "RESOLVED"
        assert item["resolved_at"] == "2026-02-01T00:00:00"
        assert item["resolution"] == "Passport renewed"


class TestGetAlertsByMember:
    """get_alerts_by_member returns only the queried member's alerts."""

    def test_returns_empty_list_when_no_alerts(self):
        result = alert_store.get_alerts_by_member("9999999")

        assert result == []

    def test_returns_only_alerts_for_that_member(self):
        alert_store.save_alert(_make_record(alert_id="a1"))
        alert_store.save_alert(_make_record(alert_id="a2"))
        alert_store.save_alert(_make_record(alert_id="a3", skymiles_number="1111111"))

        result = alert_store.get_alerts_by_member("9999999")

        assert sorted(a["alert_id"] for a in result) == ["a1", "a2"]


class TestAcknowledgeAlert:
    """acknowledge_alert flips status to ACKNOWLEDGED."""

    def test_sets_status_to_acknowledged(self):
        alert_store.save_alert(_make_record())

        alert_store.acknowledge_alert("9999999", "alert-1")

        assert _get_item()["status"] == "ACKNOWLEDGED"


class TestResolveAlert:
    """resolve_alert sets status, resolution text and a resolution timestamp."""

    def test_sets_status_resolution_and_timestamp(self):
        alert_store.save_alert(_make_record())

        alert_store.resolve_alert("9999999", "alert-1", "Visa obtained")

        item = _get_item()
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Visa obtained"
        datetime.fromisoformat(item["resolved_at"])


class TestGetActiveAlerts:
    """get_active_alerts filters out RESOLVED and EXPIRED alerts."""

    def test_includes_active_and_acknowledged_only(self):
        alert_store.save_alert(_make_record(alert_id="a-active"))
        alert_store.save_alert(
            _make_record(alert_id="a-ack", status=AlertStatus.ACKNOWLEDGED)
        )
        alert_store.save_alert(
            _make_record(alert_id="a-resolved", status=AlertStatus.RESOLVED)
        )
        alert_store.save_alert(
            _make_record(alert_id="a-expired", status=AlertStatus.EXPIRED)
        )

        result = alert_store.get_active_alerts("9999999")

        assert sorted(a["alert_id"] for a in result) == ["a-ack", "a-active"]

    def test_returns_empty_list_when_none_active(self):
        alert_store.save_alert(
            _make_record(alert_id="a-resolved", status=AlertStatus.RESOLVED)
        )

        assert alert_store.get_active_alerts("9999999") == []


class TestResolveAlertsForItinerary:
    """resolve_alerts_for_itinerary resolves matching itinerary alerts only."""

    def test_resolves_only_matching_itinerary_and_status(self):
        alert_store.save_alert(_make_record(alert_id="a1", itinerary_ref="DL-AAA"))
        alert_store.save_alert(
            _make_record(
                alert_id="a2",
                itinerary_ref="DL-AAA",
                status=AlertStatus.ACKNOWLEDGED,
            )
        )
        alert_store.save_alert(
            _make_record(
                alert_id="a3", itinerary_ref="DL-AAA", status=AlertStatus.RESOLVED
            )
        )
        alert_store.save_alert(_make_record(alert_id="a4", itinerary_ref="DL-BBB"))

        count = alert_store.resolve_alerts_for_itinerary(
            "9999999", "DL-AAA", "Auto-resolved: itinerary changed"
        )

        assert count == 2
        assert _get_item(alert_id="a1")["status"] == "RESOLVED"
        assert _get_item(alert_id="a1")["resolution"] == (
            "Auto-resolved: itinerary changed"
        )
        assert _get_item(alert_id="a2")["status"] == "RESOLVED"
        assert _get_item(alert_id="a4")["status"] == "ACTIVE"

    def test_returns_zero_when_no_matches(self):
        alert_store.save_alert(_make_record(alert_id="a1", itinerary_ref="DL-BBB"))

        count = alert_store.resolve_alerts_for_itinerary(
            "9999999", "DL-AAA", "Auto-resolved"
        )

        assert count == 0
        assert _get_item(alert_id="a1")["status"] == "ACTIVE"


class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the GSI across all travelers."""

    def test_returns_alerts_from_all_travelers_on_the_booking(self):
        alert_store.save_alert(
            _make_record(alert_id="a1", skymiles_number="1111111", itinerary_ref="DL-G")
        )
        alert_store.save_alert(
            _make_record(alert_id="a2", skymiles_number="2222222", itinerary_ref="DL-G")
        )
        alert_store.save_alert(
            _make_record(
                alert_id="a3", skymiles_number="3333333", itinerary_ref="DL-OTHER"
            )
        )

        result = alert_store.get_alerts_by_itinerary("DL-G")

        assert sorted(a["alert_id"] for a in result) == ["a1", "a2"]

    def test_returns_empty_list_for_unknown_itinerary(self):
        assert alert_store.get_alerts_by_itinerary("DL-NONE") == []


class TestGetGroupAlertSummary:
    """get_group_alert_summary groups a booking's alerts by traveler."""

    def test_groups_alerts_by_skymiles_number(self):
        alert_store.save_alert(
            _make_record(alert_id="a1", skymiles_number="1111111", itinerary_ref="DL-G")
        )
        alert_store.save_alert(
            _make_record(alert_id="a2", skymiles_number="1111111", itinerary_ref="DL-G")
        )
        alert_store.save_alert(
            _make_record(alert_id="a3", skymiles_number="2222222", itinerary_ref="DL-G")
        )

        summary = alert_store.get_group_alert_summary("DL-G")

        assert set(summary) == {"1111111", "2222222"}
        assert sorted(a["alert_id"] for a in summary["1111111"]) == ["a1", "a2"]
        assert [a["alert_id"] for a in summary["2222222"]] == ["a3"]

    def test_returns_empty_dict_when_no_alerts(self):
        assert alert_store.get_group_alert_summary("DL-NONE") == {}


class TestExpireStaleAlerts:
    """expire_stale_alerts only expires past-TTL actionable alerts."""

    def test_expires_past_ttl_active_and_acknowledged_alerts(self):
        past = int(datetime(2020, 1, 1).timestamp())
        alert_store.save_alert(_make_record(alert_id="a1", ttl=past))
        alert_store.save_alert(
            _make_record(alert_id="a2", ttl=past, status=AlertStatus.ACKNOWLEDGED)
        )

        count = alert_store.expire_stale_alerts("9999999")

        assert count == 2
        assert _get_item(alert_id="a1")["status"] == "EXPIRED"
        assert _get_item(alert_id="a2")["status"] == "EXPIRED"

    def test_skips_alerts_without_ttl(self):
        alert_store.save_alert(_make_record(alert_id="a1"))

        count = alert_store.expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item(alert_id="a1")["status"] == "ACTIVE"

    def test_skips_future_ttl_alerts(self):
        future = int(datetime(2100, 1, 1).timestamp())
        alert_store.save_alert(_make_record(alert_id="a1", ttl=future))

        count = alert_store.expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item(alert_id="a1")["status"] == "ACTIVE"

    def test_skips_already_resolved_alerts(self):
        past = int(datetime(2020, 1, 1).timestamp())
        alert_store.save_alert(
            _make_record(alert_id="a1", ttl=past, status=AlertStatus.RESOLVED)
        )

        count = alert_store.expire_stale_alerts("9999999")

        assert count == 0
        assert _get_item(alert_id="a1")["status"] == "RESOLVED"
