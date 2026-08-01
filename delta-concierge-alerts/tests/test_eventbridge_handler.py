"""Tests for the EventBridge ItineraryChanged handler."""

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


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid EventBridge ItineraryChanged envelope with overrides on ``detail``."""
    detail = {
        "profile": {
            "skymiles_number": "1234567890",
            "first_name": "Test",
            "last_name": "User",
            "nationality": "IN",
            "passport_number": "P999999",
            "passport_expiry": "2035-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": "DL-CHANGED",
            "segments": [
                {
                    "flight_number": "DL100",
                    "origin": "JFK",
                    "destination": "CN",
                    "departure_date": "2030-09-15",
                    "arrival_date": "2030-09-16",
                    "is_layover": False,
                }
            ],
        },
        "requirements_override": None,
        "change_type": "DATE_CHANGE",
    }
    detail.update(overrides)
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


def _clean_segment(destination: str = "DE") -> dict:
    return {
        "flight_number": "DL200",
        "origin": "ATL",
        "destination": destination,
        "departure_date": "2030-09-15",
        "arrival_date": "2030-09-16",
        "is_layover": False,
    }


class TestHappyPath:
    """A valid ItineraryChanged event re-evaluates and returns a 200 envelope."""

    def test_response_shape_with_no_prior_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 0
        assert body["evaluation_result"]["statusCode"] == 200

    def test_clean_traveler_produces_no_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["detail"]["profile"]["nationality"] = "US"
        event["detail"]["itinerary"]["segments"] = [_clean_segment("DE")]

        response = handler(event, None)

        result_body = response["body"]["evaluation_result"]["body"]
        assert result_body["passport_status"] == "OK"
        assert result_body["visa_status"] == "OK"
        assert result_body["alerts_sent"] == 0


class TestChangeType:
    """``change_type`` is echoed back, defaulting to UNKNOWN when absent."""

    @pytest.mark.parametrize(
        "change_type",
        ["DATE_CHANGE", "SEGMENT_ADDED", "SEGMENT_REMOVED", "ROUTE_CHANGE"],
    )
    def test_known_change_types_echoed(self, endpoint_arn, change_type):
        event = _base_event(endpoint_arn, change_type=change_type)

        response = handler(event, None)

        assert response["body"]["change_type"] == change_type

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["change_type"]

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"


class TestStaleAlertResolution:
    """Alerts for the changed itinerary are resolved before re-evaluation."""

    def test_existing_alert_for_itinerary_is_resolved(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        alert_store_mod.table.put_item(
            Item={
                "skymiles_number": "1234567890",
                "alert_id": "stale-1",
                "alert_type": "VISA",
                "severity": "CRITICAL",
                "reasons": ["No visa on file for CN"],
                "created_at": "2026-01-01T00:00:00",
                "itinerary_ref": "DL-CHANGED",
                "status": "ACTIVE",
            }
        )

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "stale-1"}
        )["Item"]
        assert item["status"] == "RESOLVED"

    def test_alert_for_other_itinerary_is_untouched(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        alert_store_mod.table.put_item(
            Item={
                "skymiles_number": "1234567890",
                "alert_id": "other-1",
                "alert_type": "VISA",
                "severity": "CRITICAL",
                "reasons": ["No visa on file for CN"],
                "created_at": "2026-01-01T00:00:00",
                "itinerary_ref": "DL-OTHER",
                "status": "ACTIVE",
            }
        )

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "other-1"}
        )["Item"]
        assert item["status"] == "ACTIVE"


class TestMalformedEvent:
    """Missing required envelope keys surface as KeyError."""

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
