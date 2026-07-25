"""Tests for the DynamoDB-backed alert store."""

from datetime import datetime

import boto3
import moto
import pytest

import src.services.alert_store as alert_store
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto and point the module-level table at a mocked ConciergeAlerts table."""
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
            yield alert_store.table
        finally:
            alert_store.dynamodb = orig_dynamodb
            alert_store.table = orig_table


@pytest.fixture
def table(_mock_aws):
    """Expose the mocked DynamoDB table resource."""
    return _mock_aws


def make_alert(
    alert_id: str = "alert-1",
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


class TestSaveAlert:
    """save_alert persists required fields and only includes optionals when set."""

    def test_saves_required_fields(self, table):
        alert_store.save_alert(make_alert())

        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-1"}
        )["Item"]
        assert item["alert_type"] == "VISA"
        assert item["severity"] == "CRITICAL"
        assert item["status"] == "ACTIVE"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["reasons"] == ["No visa on file for CN"]
        assert item["created_at"] == "2026-01-01T00:00:00"

    def test_optional_fields_omitted_when_none(self, table):
        alert_store.save_alert(make_alert())

        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-1"}
        )["Item"]
        assert "ttl" not in item
        assert "resolved_at" not in item
        assert "resolution" not in item

    def test_ttl_persisted_when_set(self, table):
        alert_store.save_alert(make_alert(ttl=1893456000))

        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-1"}
        )["Item"]
        assert int(item["ttl"]) == 1893456000

    def test_resolution_fields_persisted_when_set(self, table):
        alert_store.save_alert(
            make_alert(
                status=AlertStatus.RESOLVED,
                resolved_at="2026-02-01T00:00:00",
                resolution="Visa obtained",
            )
        )

        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-1"}
        )["Item"]
        assert item["status"] == "RESOLVED"
        assert item["resolved_at"] == "2026-02-01T00:00:00"
        assert item["resolution"] == "Visa obtained"


class TestGetAlertsByMember:
    """get_alerts_by_member returns only the queried member's alerts."""

    def test_returns_all_alerts_for_member(self):
        alert_store.save_alert(make_alert(alert_id="alert-1"))
        alert_store.save_alert(make_alert(alert_id="alert-2"))

        alerts = alert_store.get_alerts_by_member("9999999")

        assert {a["alert_id"] for a in alerts} == {"alert-1", "alert-2"}

    def test_excludes_other_members(self):
        alert_store.save_alert(make_alert(alert_id="alert-1"))
        alert_store.save_alert(
            make_alert(alert_id="alert-2", skymiles_number="1111111")
        )

        alerts = alert_store.get_alerts_by_member("9999999")

        assert [a["alert_id"] for a in alerts] == ["alert-1"]

    def test_returns_empty_list_for_unknown_member(self):
        alerts = alert_store.get_alerts_by_member("0000000")

        assert alerts == []


class TestAcknowledgeAlert:
    """acknowledge_alert transitions an alert to ACKNOWLEDGED."""

    def test_sets_status_acknowledged(self, table):
        alert_store.save_alert(make_alert())

        alert_store.acknowledge_alert("9999999", "alert-1")

        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-1"}
        )["Item"]
        assert item["status"] == "ACKNOWLEDGED"

    def test_does_not_affect_other_alerts(self, table):
        alert_store.save_alert(make_alert(alert_id="alert-1"))
        alert_store.save_alert(make_alert(alert_id="alert-2"))

        alert_store.acknowledge_alert("9999999", "alert-1")

        other = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-2"}
        )["Item"]
        assert other["status"] == "ACTIVE"


class TestResolveAlert:
    """resolve_alert stamps status, resolution text and a resolved_at timestamp."""

    def test_sets_status_and_resolution(self, table):
        alert_store.save_alert(make_alert())

        alert_store.resolve_alert("9999999", "alert-1", "Visa obtained")

        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-1"}
        )["Item"]
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Visa obtained"

    def test_records_resolved_at_timestamp(self, table):
        alert_store.save_alert(make_alert())

        alert_store.resolve_alert("9999999", "alert-1", "Visa obtained")

        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "alert-1"}
        )["Item"]
        assert datetime.fromisoformat(item["resolved_at"])


class TestGetActiveAlerts:
    """get_active_alerts filters to ACTIVE and ACKNOWLEDGED alerts only."""

    def test_includes_active_and_acknowledged(self):
        alert_store.save_alert(make_alert(alert_id="a-active"))
        alert_store.save_alert(
            make_alert(alert_id="a-ack", status=AlertStatus.ACKNOWLEDGED)
        )

        active = alert_store.get_active_alerts("9999999")

        assert {a["alert_id"] for a in active} == {"a-active", "a-ack"}

    def test_excludes_resolved_and_expired(self):
        alert_store.save_alert(make_alert(alert_id="a-active"))
        alert_store.save_alert(
            make_alert(alert_id="a-resolved", status=AlertStatus.RESOLVED)
        )
        alert_store.save_alert(
            make_alert(alert_id="a-expired", status=AlertStatus.EXPIRED)
        )

        active = alert_store.get_active_alerts("9999999")

        assert [a["alert_id"] for a in active] == ["a-active"]

    def test_returns_empty_when_none_active(self):
        alert_store.save_alert(
            make_alert(alert_id="a-resolved", status=AlertStatus.RESOLVED)
        )

        assert alert_store.get_active_alerts("9999999") == []


class TestResolveAlertsForItinerary:
    """Itinerary changes auto-resolve matching open alerts and nothing else."""

    def test_resolves_matching_open_alerts(self, table):
        alert_store.save_alert(make_alert(alert_id="a-1", itinerary_ref="DL-AAA"))
        alert_store.save_alert(
            make_alert(
                alert_id="a-2",
                itinerary_ref="DL-AAA",
                status=AlertStatus.ACKNOWLEDGED,
            )
        )

        count = alert_store.resolve_alerts_for_itinerary(
            "9999999", "DL-AAA", "Auto-resolved: itinerary changed"
        )

        assert count == 2
        items = table.scan()["Items"]
        assert all(i["status"] == "RESOLVED" for i in items)
        assert all(
            i["resolution"] == "Auto-resolved: itinerary changed" for i in items
        )

    def test_ignores_other_itineraries(self, table):
        alert_store.save_alert(make_alert(alert_id="a-1", itinerary_ref="DL-AAA"))
        alert_store.save_alert(make_alert(alert_id="a-2", itinerary_ref="DL-BBB"))

        count = alert_store.resolve_alerts_for_itinerary(
            "9999999", "DL-AAA", "Auto-resolved: itinerary changed"
        )

        assert count == 1
        untouched = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a-2"}
        )["Item"]
        assert untouched["status"] == "ACTIVE"

    def test_ignores_already_resolved_alerts(self):
        alert_store.save_alert(
            make_alert(
                alert_id="a-1", itinerary_ref="DL-AAA", status=AlertStatus.RESOLVED
            )
        )

        count = alert_store.resolve_alerts_for_itinerary(
            "9999999", "DL-AAA", "Auto-resolved: itinerary changed"
        )

        assert count == 0

    def test_returns_zero_when_no_alerts_exist(self):
        count = alert_store.resolve_alerts_for_itinerary(
            "9999999", "DL-AAA", "Auto-resolved: itinerary changed"
        )

        assert count == 0


class TestGetAlertsByItinerary:
    """The itinerary-ref GSI returns alerts across every traveler on a booking."""

    def test_returns_alerts_across_travelers(self):
        alert_store.save_alert(
            make_alert(alert_id="a-1", skymiles_number="1111111", itinerary_ref="DL-AAA")
        )
        alert_store.save_alert(
            make_alert(alert_id="a-2", skymiles_number="2222222", itinerary_ref="DL-AAA")
        )

        alerts = alert_store.get_alerts_by_itinerary("DL-AAA")

        assert {a["skymiles_number"] for a in alerts} == {"1111111", "2222222"}

    def test_excludes_other_itineraries(self):
        alert_store.save_alert(make_alert(alert_id="a-1", itinerary_ref="DL-AAA"))
        alert_store.save_alert(make_alert(alert_id="a-2", itinerary_ref="DL-BBB"))

        alerts = alert_store.get_alerts_by_itinerary("DL-AAA")

        assert [a["alert_id"] for a in alerts] == ["a-1"]

    def test_returns_empty_for_unknown_itinerary(self):
        assert alert_store.get_alerts_by_itinerary("DL-NONE") == []


class TestGetGroupAlertSummary:
    """Group summaries bucket a booking's alerts by traveler."""

    def test_groups_alerts_by_skymiles_number(self):
        alert_store.save_alert(
            make_alert(alert_id="a-1", skymiles_number="1111111", itinerary_ref="DL-AAA")
        )
        alert_store.save_alert(
            make_alert(alert_id="a-2", skymiles_number="1111111", itinerary_ref="DL-AAA")
        )
        alert_store.save_alert(
            make_alert(alert_id="a-3", skymiles_number="2222222", itinerary_ref="DL-AAA")
        )

        grouped = alert_store.get_group_alert_summary("DL-AAA")

        assert set(grouped) == {"1111111", "2222222"}
        assert len(grouped["1111111"]) == 2
        assert len(grouped["2222222"]) == 1

    def test_returns_empty_dict_when_no_alerts(self):
        assert alert_store.get_group_alert_summary("DL-NONE") == {}


class TestExpireStaleAlerts:
    """Only open alerts whose TTL has elapsed transition to EXPIRED."""

    def _past_ttl(self) -> int:
        return int(datetime.utcnow().timestamp()) - 3600

    def _future_ttl(self) -> int:
        return int(datetime.utcnow().timestamp()) + 3600

    def test_expires_past_ttl_active_alert(self, table):
        alert_store.save_alert(make_alert(alert_id="a-1", ttl=self._past_ttl()))

        count = alert_store.expire_stale_alerts("9999999")

        assert count == 1
        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a-1"}
        )["Item"]
        assert item["status"] == "EXPIRED"

    def test_expires_past_ttl_acknowledged_alert(self, table):
        alert_store.save_alert(
            make_alert(
                alert_id="a-1",
                ttl=self._past_ttl(),
                status=AlertStatus.ACKNOWLEDGED,
            )
        )

        count = alert_store.expire_stale_alerts("9999999")

        assert count == 1
        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a-1"}
        )["Item"]
        assert item["status"] == "EXPIRED"

    def test_skips_alerts_without_ttl(self, table):
        alert_store.save_alert(make_alert(alert_id="a-1"))

        count = alert_store.expire_stale_alerts("9999999")

        assert count == 0
        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a-1"}
        )["Item"]
        assert item["status"] == "ACTIVE"

    def test_skips_future_ttl(self, table):
        alert_store.save_alert(make_alert(alert_id="a-1", ttl=self._future_ttl()))

        count = alert_store.expire_stale_alerts("9999999")

        assert count == 0
        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a-1"}
        )["Item"]
        assert item["status"] == "ACTIVE"

    def test_skips_resolved_alert_past_ttl(self, table):
        alert_store.save_alert(
            make_alert(
                alert_id="a-1",
                ttl=self._past_ttl(),
                status=AlertStatus.RESOLVED,
            )
        )

        count = alert_store.expire_stale_alerts("9999999")

        assert count == 0
        item = table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "a-1"}
        )["Item"]
        assert item["status"] == "RESOLVED"

    def test_counts_only_expired_alerts_in_mixed_set(self):
        alert_store.save_alert(make_alert(alert_id="a-1", ttl=self._past_ttl()))
        alert_store.save_alert(make_alert(alert_id="a-2", ttl=self._future_ttl()))
        alert_store.save_alert(make_alert(alert_id="a-3"))

        assert alert_store.expire_stale_alerts("9999999") == 1

    def test_returns_zero_for_member_without_alerts(self):
        assert alert_store.expire_stale_alerts("0000000") == 0
