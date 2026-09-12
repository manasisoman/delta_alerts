"""Tests for the EventBridge ``ItineraryChanged`` handler."""

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler
from src.models.types import AlertStatus

SKYMILES = "1234567890"
CONFIRMATION = "DL-TEST"


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up DynamoDB table + SNS endpoint for every test."""
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
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token",
        )["EndpointArn"]

        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client

        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns

        try:
            yield endpoint_arn
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoint_arn(_mock_aws):
    """Expose the moto SNS endpoint ARN created by _mock_aws."""
    return _mock_aws


def _base_detail(endpoint_arn: str, **overrides) -> dict:
    """Build a valid ``detail`` payload with optional profile-field overrides."""
    detail = {
        "profile": {
            "skymiles_number": SKYMILES,
            "first_name": "Test",
            "last_name": "User",
            "nationality": "IN",
            "passport_number": "P999999",
            "passport_expiry": "2030-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": CONFIRMATION,
            "segments": [
                {
                    "flight_number": "DL100",
                    "origin": "JFK",
                    "destination": "CN",
                    "departure_date": "2026-09-15",
                    "arrival_date": "2026-09-16",
                    "is_layover": False,
                }
            ],
        },
        "requirements_override": None,
        "change_type": "DATE_CHANGE",
    }
    for key, val in overrides.items():
        if key in detail["profile"]:
            detail["profile"][key] = val
        else:
            detail[key] = val
    return detail


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Wrap a detail payload in an EventBridge envelope."""
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": _base_detail(endpoint_arn, **overrides),
    }


def _seed_alert(
    alert_id: str,
    skymiles_number: str = SKYMILES,
    itinerary_ref: str = CONFIRMATION,
    status: AlertStatus = AlertStatus.ACTIVE,
) -> None:
    """Insert a pre-existing alert row directly into the mocked table."""
    alert_store_mod.table.put_item(
        Item={
            "skymiles_number": skymiles_number,
            "alert_id": alert_id,
            "alert_type": "VISA",
            "severity": "CRITICAL",
            "reasons": ["seeded"],
            "created_at": "2026-01-01T00:00:00",
            "itinerary_ref": itinerary_ref,
            "status": status.value,
        }
    )


def _get_alert(alert_id: str, skymiles_number: str = SKYMILES) -> dict:
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


class TestResponseShape:
    """Handler returns 200 with change_type, resolved count and nested evaluation."""

    def test_happy_path_response(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 0
        evaluation = body["evaluation_result"]
        assert evaluation["statusCode"] == 200
        assert evaluation["body"]["visa_status"] == "CRITICAL"
        assert evaluation["body"]["alerts_sent"] >= 1

    def test_change_type_is_echoed(self, endpoint_arn):
        event = _base_event(endpoint_arn, change_type="ROUTE_CHANGE")

        response = handler(event, None)

        assert response["body"]["change_type"] == "ROUTE_CHANGE"

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["change_type"]

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"

    def test_clean_traveler_yields_no_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn, nationality="US")
        event["detail"]["itinerary"]["segments"][0]["destination"] = "DE"

        response = handler(event, None)

        evaluation = response["body"]["evaluation_result"]["body"]
        assert evaluation["passport_status"] == "OK"
        assert evaluation["visa_status"] == "OK"
        assert evaluation["alerts_sent"] == 0


class TestStaleAlertResolution:
    """Existing ACTIVE/ACKNOWLEDGED alerts for the itinerary are auto-resolved."""

    def test_active_alert_resolved(self, endpoint_arn):
        _seed_alert("old-1")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        item = _get_alert("old-1")
        assert item["status"] == AlertStatus.RESOLVED.value
        assert item["resolution"] == "Auto-resolved: itinerary changed, re-evaluating"
        assert "resolved_at" in item

    def test_acknowledged_alert_resolved(self, endpoint_arn):
        _seed_alert("old-1", status=AlertStatus.ACKNOWLEDGED)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _get_alert("old-1")["status"] == AlertStatus.RESOLVED.value

    def test_multiple_alerts_all_resolved(self, endpoint_arn):
        _seed_alert("old-1")
        _seed_alert("old-2", status=AlertStatus.ACKNOWLEDGED)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 2

    def test_already_resolved_alert_not_counted(self, endpoint_arn):
        _seed_alert("old-1", status=AlertStatus.RESOLVED)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0

    def test_other_itinerary_alert_untouched(self, endpoint_arn):
        _seed_alert("other-1", itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_alert("other-1")["status"] == AlertStatus.ACTIVE.value

    def test_other_member_alert_untouched(self, endpoint_arn):
        _seed_alert("other-1", skymiles_number="0000000000")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert (
            _get_alert("other-1", skymiles_number="0000000000")["status"]
            == AlertStatus.ACTIVE.value
        )

    def test_new_alert_persisted_after_resolving_old(self, endpoint_arn):
        _seed_alert("old-1")
        event = _base_event(endpoint_arn)

        handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        active = [i for i in items if i["status"] == AlertStatus.ACTIVE.value]
        resolved = [i for i in items if i["status"] == AlertStatus.RESOLVED.value]
        assert len(resolved) == 1
        assert len(active) >= 1
        assert all(i["itinerary_ref"] == CONFIRMATION for i in active)


class TestMalformedEvents:
    """Required envelope/detail fields raise KeyError when absent."""

    def test_missing_detail_raises(self, endpoint_arn):
        event = {"source": "delta.booking-system", "detail-type": "ItineraryChanged"}

        with pytest.raises(KeyError):
            handler(event, None)

    def test_missing_skymiles_number_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["profile"]["skymiles_number"]

        with pytest.raises(KeyError):
            handler(event, None)

    def test_missing_confirmation_number_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]["confirmation_number"]

        with pytest.raises(KeyError):
            handler(event, None)
