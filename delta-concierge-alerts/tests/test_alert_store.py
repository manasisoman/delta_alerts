"""Unit tests for the DynamoDB-backed alert store.

Exercises the full alert lifecycle (save, query, acknowledge, resolve, expire)
against a moto-mocked DynamoDB table, matching the mocking pattern used in
``tests/test_lambda_handler.py``.
"""

from datetime import datetime

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


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto and provision the ConciergeAlerts table with its GSIs."""
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
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "created_at", "AttributeType": "S"},
                {"AttributeName": "itinerary_ref", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "status-created_at-index",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "created_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
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


def make_alert(
    skymiles_number: str = "1234567890",
    alert_id: str = "alert-1",
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
    """Build an AlertRecord with sensible defaults; override one field per test."""
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


def _get_item(skymiles_number: str, alert_id: str) -> dict:
    """Fetch a single stored item directly from the mocked table."""
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


class TestSaveAlert:
    """save_alert persists the record with its enum values and optional fields."""

    def test_saves_required_fields_and_defaults_active(self):
        record = make_alert()

        alert_store_mod.save_alert(record)

        item = _get_item(record.skymiles_number, record.alert_id)
        assert item["alert_type"] == "VISA"
        assert item["severity"] == "CRITICAL"
        assert item["status"] == AlertStatus.ACTIVE.value
        assert item["itinerary_ref"] == "DL-TEST"

    def test_omits_optional_fields_when_none(self):
        record = make_alert(ttl=None, resolved_at=None, resolution=None)

        alert_store_mod.save_alert(record)

        item = _get_item(record.skymiles_number, record.alert_id)
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_persists_ttl_when_present(self):
        record = make_alert(ttl=1893456000)

        alert_store_mod.save_alert(record)

        item = _get_item(record.skymiles_number, record.alert_id)
        assert int(item["ttl"]) == 1893456000

    def test_persists_resolved_at_and_resolution_when_present(self):
        record = make_alert(
            status=AlertStatus.RESOLVED,
            resolved_at="2030-02-01T00:00:00",
            resolution="Handled manually",
        )

        alert_store_mod.save_alert(record)

        item = _get_item(record.skymiles_number, record.alert_id)
        assert item["resolved_at"] == "2030-02-01T00:00:00"
        assert item["resolution"] == "Handled manually"


class TestGetAlertsByMember:
    """get_alerts_by_member returns only the queried member's alerts."""

    def test_returns_all_alerts_for_member(self):
        alert_store_mod.save_alert(make_alert(alert_id="a1"))
        alert_store_mod.save_alert(make_alert(alert_id="a2"))
        alert_store_mod.save_alert(
            make_alert(skymiles_number="other", alert_id="a3")
        )

        results = alert_store_mod.get_alerts_by_member("1234567890")

        assert {r["alert_id"] for r in results} == {"a1", "a2"}

    def test_returns_empty_for_unknown_member(self):
        results = alert_store_mod.get_alerts_by_member("no-such-member")

        assert results == []


class TestAcknowledgeAlert:
    """acknowledge_alert transitions a stored alert to ACKNOWLEDGED."""

    def test_sets_status_acknowledged(self):
        record = make_alert()
        alert_store_mod.save_alert(record)

        alert_store_mod.acknowledge_alert(
            record.skymiles_number, record.alert_id
        )

        item = _get_item(record.skymiles_number, record.alert_id)
        assert item["status"] == AlertStatus.ACKNOWLEDGED.value


class TestResolveAlert:
    """resolve_alert sets RESOLVED status plus resolution metadata."""

    def test_sets_status_and_resolution_fields(self):
        record = make_alert()
        alert_store_mod.save_alert(record)

        alert_store_mod.resolve_alert(
            record.skymiles_number, record.alert_id, "Traveler obtained visa"
        )

        item = _get_item(record.skymiles_number, record.alert_id)
        assert item["status"] == AlertStatus.RESOLVED.value
        assert item["resolution"] == "Traveler obtained visa"
        assert "resolved_at" in item


class TestGetActiveAlerts:
    """get_active_alerts returns only ACTIVE or ACKNOWLEDGED alerts."""

    def test_includes_active_and_acknowledged_excludes_resolved(self):
        alert_store_mod.save_alert(
            make_alert(alert_id="active", status=AlertStatus.ACTIVE)
        )
        alert_store_mod.save_alert(
            make_alert(alert_id="ack", status=AlertStatus.ACKNOWLEDGED)
        )
        alert_store_mod.save_alert(
            make_alert(alert_id="resolved", status=AlertStatus.RESOLVED)
        )

        results = alert_store_mod.get_active_alerts("1234567890")

        assert {r["alert_id"] for r in results} == {"active", "ack"}


class TestResolveAlertsForItinerary:
    """resolve_alerts_for_itinerary resolves matching actionable alerts."""

    def test_resolves_only_matching_itinerary_and_status(self):
        alert_store_mod.save_alert(
            make_alert(alert_id="match1", itinerary_ref="DL-AAA")
        )
        alert_store_mod.save_alert(
            make_alert(
                alert_id="match2",
                itinerary_ref="DL-AAA",
                status=AlertStatus.ACKNOWLEDGED,
            )
        )
        alert_store_mod.save_alert(
            make_alert(
                alert_id="already-resolved",
                itinerary_ref="DL-AAA",
                status=AlertStatus.RESOLVED,
            )
        )
        alert_store_mod.save_alert(
            make_alert(alert_id="other-itin", itinerary_ref="DL-BBB")
        )

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "1234567890", "DL-AAA", "Auto-resolved: itinerary changed"
        )

        assert count == 2
        assert _get_item("1234567890", "match1")["status"] == "RESOLVED"
        assert _get_item("1234567890", "match2")["status"] == "RESOLVED"
        assert _get_item("1234567890", "other-itin")["status"] == "ACTIVE"

    def test_returns_zero_when_no_matches(self):
        alert_store_mod.save_alert(make_alert(itinerary_ref="DL-AAA"))

        count = alert_store_mod.resolve_alerts_for_itinerary(
            "1234567890", "DL-NONE", "resolution"
        )

        assert count == 0


class TestGetAlertsByItinerary:
    """get_alerts_by_itinerary queries the itinerary-ref-index GSI."""

    def test_returns_alerts_across_members_for_confirmation(self):
        alert_store_mod.save_alert(
            make_alert(skymiles_number="m1", alert_id="a1", itinerary_ref="DL-G")
        )
        alert_store_mod.save_alert(
            make_alert(skymiles_number="m2", alert_id="a2", itinerary_ref="DL-G")
        )
        alert_store_mod.save_alert(
            make_alert(skymiles_number="m3", alert_id="a3", itinerary_ref="DL-X")
        )

        results = alert_store_mod.get_alerts_by_itinerary("DL-G")

        assert {r["skymiles_number"] for r in results} == {"m1", "m2"}

    def test_returns_empty_for_unknown_itinerary(self):
        results = alert_store_mod.get_alerts_by_itinerary("DL-UNKNOWN")

        assert results == []


class TestGetGroupAlertSummary:
    """get_group_alert_summary groups itinerary alerts by skymiles_number."""

    def test_groups_alerts_by_member(self):
        alert_store_mod.save_alert(
            make_alert(skymiles_number="m1", alert_id="a1", itinerary_ref="DL-G")
        )
        alert_store_mod.save_alert(
            make_alert(skymiles_number="m1", alert_id="a2", itinerary_ref="DL-G")
        )
        alert_store_mod.save_alert(
            make_alert(skymiles_number="m2", alert_id="a3", itinerary_ref="DL-G")
        )

        summary = alert_store_mod.get_group_alert_summary("DL-G")

        assert set(summary.keys()) == {"m1", "m2"}
        assert {a["alert_id"] for a in summary["m1"]} == {"a1", "a2"}
        assert {a["alert_id"] for a in summary["m2"]} == {"a3"}

    def test_returns_empty_dict_for_unknown_itinerary(self):
        summary = alert_store_mod.get_group_alert_summary("DL-UNKNOWN")

        assert summary == {}


class TestExpireStaleAlerts:
    """expire_stale_alerts transitions past-TTL actionable alerts to EXPIRED."""

    def test_expires_only_past_ttl_actionable_alerts(self):
        past_ttl = int(datetime(2020, 1, 1).timestamp())
        future_ttl = int(datetime(2035, 1, 1).timestamp())
        alert_store_mod.save_alert(
            make_alert(alert_id="stale-active", ttl=past_ttl)
        )
        alert_store_mod.save_alert(
            make_alert(
                alert_id="stale-ack",
                ttl=past_ttl,
                status=AlertStatus.ACKNOWLEDGED,
            )
        )
        alert_store_mod.save_alert(
            make_alert(alert_id="fresh", ttl=future_ttl)
        )
        alert_store_mod.save_alert(
            make_alert(
                alert_id="stale-resolved",
                ttl=past_ttl,
                status=AlertStatus.RESOLVED,
            )
        )
        alert_store_mod.save_alert(make_alert(alert_id="no-ttl", ttl=None))

        count = alert_store_mod.expire_stale_alerts("1234567890")

        assert count == 2
        assert _get_item("1234567890", "stale-active")["status"] == "EXPIRED"
        assert _get_item("1234567890", "stale-ack")["status"] == "EXPIRED"
        assert _get_item("1234567890", "fresh")["status"] == "ACTIVE"
        assert _get_item("1234567890", "stale-resolved")["status"] == "RESOLVED"
        assert _get_item("1234567890", "no-ttl")["status"] == "ACTIVE"

    def test_returns_zero_when_no_stale_alerts(self):
        future_ttl = int(datetime(2035, 1, 1).timestamp())
        alert_store_mod.save_alert(make_alert(ttl=future_ttl))

        count = alert_store_mod.expire_stale_alerts("1234567890")

        assert count == 0
