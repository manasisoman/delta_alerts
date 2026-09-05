"""Tests for the EventBridge ``ItineraryChanged`` handler."""

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler


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


SKYMILES = "1234567890"
CONFIRMATION = "DL-TEST"


def _base_event(
    endpoint_arn: str,
    change_type: str | None = "DATE_CHANGE",
    nationality: str = "IN",
    destination: str = "CN",
    confirmation: str = CONFIRMATION,
) -> dict:
    """Build a valid EventBridge ``ItineraryChanged`` envelope."""
    detail = {
        "profile": {
            "skymiles_number": SKYMILES,
            "first_name": "Test",
            "last_name": "User",
            "nationality": nationality,
            "passport_number": "P999999",
            "passport_expiry": "2035-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": confirmation,
            "segments": [
                {
                    "flight_number": "DL100",
                    "origin": "JFK",
                    "destination": destination,
                    "departure_date": "2030-09-15",
                    "arrival_date": "2030-09-16",
                    "is_layover": False,
                }
            ],
        },
        "requirements_override": None,
    }
    if change_type is not None:
        detail["change_type"] = change_type
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


def _seed_alert(
    alert_id: str,
    status: str = "ACTIVE",
    skymiles_number: str = SKYMILES,
    itinerary_ref: str = CONFIRMATION,
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
            "status": status,
        }
    )


def _status_of(alert_id: str) -> str:
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": SKYMILES, "alert_id": alert_id}
    )["Item"]["status"]


class TestResponseShape:
    """Handler returns a 200 envelope echoing change_type and evaluation result."""

    def test_happy_path_response(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 0
        assert body["evaluation_result"]["statusCode"] == 200
        assert body["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        event = _base_event(endpoint_arn, change_type=None)

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"

    def test_clean_traveler_evaluation_is_ok(self, endpoint_arn):
        event = _base_event(endpoint_arn, nationality="US", destination="DE")

        response = handler(event, None)

        result_body = response["body"]["evaluation_result"]["body"]
        assert result_body["passport_status"] == "OK"
        assert result_body["visa_status"] == "OK"
        assert result_body["alerts_sent"] == 0


class TestStaleAlertResolution:
    """Active/acknowledged alerts for the changed itinerary are auto-resolved."""

    def test_active_alert_resolved(self, endpoint_arn):
        _seed_alert("old-1", status="ACTIVE")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _status_of("old-1") == "RESOLVED"

    def test_acknowledged_alert_resolved(self, endpoint_arn):
        _seed_alert("old-1", status="ACKNOWLEDGED")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _status_of("old-1") == "RESOLVED"

    def test_already_resolved_alert_not_counted(self, endpoint_arn):
        _seed_alert("old-1", status="RESOLVED")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0

    def test_other_itinerary_alert_untouched(self, endpoint_arn):
        _seed_alert("other-1", itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _status_of("other-1") == "ACTIVE"

    def test_resolution_text_applied(self, endpoint_arn):
        _seed_alert("old-1")
        event = _base_event(endpoint_arn)

        handler(event, None)

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": SKYMILES, "alert_id": "old-1"}
        )["Item"]
        assert item["resolution"] == "Auto-resolved: itinerary changed, re-evaluating"
        assert "resolved_at" in item

    def test_re_evaluation_creates_fresh_alert(self, endpoint_arn):
        _seed_alert("old-1")
        event = _base_event(endpoint_arn)

        handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        active = [i for i in items if i["status"] == "ACTIVE"]
        assert len(active) >= 1
        assert all(i["alert_id"] != "old-1" for i in active)


class TestMalformedEvents:
    """Required envelope fields raise KeyError when absent."""

    def test_missing_detail_raises(self):
        with pytest.raises(KeyError):
            handler({"source": "delta.booking-system"}, None)

    def test_missing_profile_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["profile"]

        with pytest.raises(KeyError):
            handler(event, None)

    def test_missing_confirmation_number_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]["confirmation_number"]

        with pytest.raises(KeyError):
            handler(event, None)
