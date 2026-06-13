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
        # DynamoDB with GSI for itinerary-ref-index
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
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # SNS
        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token",
        )["EndpointArn"]

        # Save original module-level boto3 clients
        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client

        # Patch module-level boto3 clients to use the moto context
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


def _base_event(endpoint_arn: str, *, change_type: str = "DATE_CHANGE") -> dict:
    """Build a valid EventBridge ItineraryChanged event with optional overrides."""
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": {
            "profile": {
                "skymiles_number": "1234567890",
                "first_name": "Test",
                "last_name": "User",
                "nationality": "US",
                "passport_number": "P999999",
                "passport_expiry": "2030-01-01",
                "endpoint_arn": endpoint_arn,
                "visa_records": [],
            },
            "itinerary": {
                "confirmation_number": "DL-EB-001",
                "segments": [
                    {
                        "flight_number": "DL200",
                        "origin": "ATL",
                        "destination": "DE",
                        "departure_date": "2026-09-15",
                        "arrival_date": "2026-09-16",
                        "is_layover": False,
                    }
                ],
            },
            "change_type": change_type,
            "requirements_override": None,
        },
    }


def _seed_alert(skymiles_number: str, confirmation_number: str, alert_id: str, status: str = "ACTIVE") -> None:
    """Insert an alert record directly into the mocked DynamoDB table."""
    alert_store_mod.table.put_item(Item={
        "skymiles_number": skymiles_number,
        "alert_id": alert_id,
        "alert_type": "VISA",
        "severity": "CRITICAL",
        "reasons": "No visa on file for CN",
        "created_at": "2026-01-01T00:00:00",
        "itinerary_ref": confirmation_number,
        "status": status,
    })


class TestHappyPath:
    """EventBridge handler processes a valid event with no pre-existing alerts."""

    def test_returns_200_with_expected_shape(self, endpoint_arn):
        """Valid event returns statusCode 200 and body with all required keys."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert "change_type" in body
        assert "resolved_alerts" in body
        assert "evaluation_result" in body

    def test_no_alerts_resolved_when_none_exist(self, endpoint_arn):
        """With no pre-existing alerts, resolved_alerts should be 0."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0

    def test_evaluation_result_contains_lambda_response(self, endpoint_arn):
        """evaluation_result should be the full lambda handler response."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        eval_result = response["body"]["evaluation_result"]
        assert eval_result["statusCode"] == 200
        assert "passport_status" in eval_result["body"]
        assert "visa_status" in eval_result["body"]


class TestAlertResolution:
    """EventBridge handler resolves stale alerts for the affected itinerary."""

    def test_active_alerts_get_resolved(self, endpoint_arn):
        """Pre-existing ACTIVE alerts for the itinerary are resolved."""
        _seed_alert("1234567890", "DL-EB-001", "alert-001", status="ACTIVE")
        _seed_alert("1234567890", "DL-EB-001", "alert-002", status="ACTIVE")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 2

    def test_acknowledged_alerts_get_resolved(self, endpoint_arn):
        """Pre-existing ACKNOWLEDGED alerts for the itinerary are resolved."""
        _seed_alert("1234567890", "DL-EB-001", "alert-003", status="ACKNOWLEDGED")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1

    def test_resolved_alerts_not_re_resolved(self, endpoint_arn):
        """Already RESOLVED alerts should not be counted again."""
        _seed_alert("1234567890", "DL-EB-001", "alert-004", status="RESOLVED")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0

    def test_alerts_for_different_itinerary_not_affected(self, endpoint_arn):
        """Alerts for a different confirmation_number are left alone."""
        _seed_alert("1234567890", "DL-OTHER", "alert-005", status="ACTIVE")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0


class TestChangeTypeVariations:
    """EventBridge handler correctly passes through change_type values."""

    @pytest.mark.parametrize("change_type", [
        "DATE_CHANGE",
        "SEGMENT_ADDED",
        "SEGMENT_REMOVED",
        "ROUTE_CHANGE",
    ])
    def test_known_change_types(self, endpoint_arn, change_type):
        """Known change_type values are returned in the response body."""
        event = _base_event(endpoint_arn, change_type=change_type)

        response = handler(event, None)

        assert response["body"]["change_type"] == change_type

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        """When change_type is absent from the event, defaults to UNKNOWN."""
        event = _base_event(endpoint_arn)
        del event["detail"]["change_type"]

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"


class TestEvaluationIntegration:
    """EventBridge handler delegates evaluation to evaluate_itinerary correctly."""

    def test_clean_traveler_produces_ok_status(self, endpoint_arn):
        """US national to DE with valid passport → OK statuses in evaluation."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        eval_body = response["body"]["evaluation_result"]["body"]
        assert eval_body["passport_status"] == "OK"
        assert eval_body["visa_status"] == "OK"

    def test_missing_visa_triggers_critical(self, endpoint_arn):
        """IN national to CN without visa → CRITICAL visa status."""
        event = _base_event(endpoint_arn)
        event["detail"]["profile"]["nationality"] = "IN"
        event["detail"]["itinerary"]["segments"] = [
            {
                "flight_number": "DL300",
                "origin": "JFK",
                "destination": "CN",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ]

        response = handler(event, None)

        eval_body = response["body"]["evaluation_result"]["body"]
        assert eval_body["visa_status"] == "CRITICAL"

    def test_expired_passport_triggers_critical(self, endpoint_arn):
        """Expired passport → CRITICAL passport status in evaluation."""
        event = _base_event(endpoint_arn)
        event["detail"]["profile"]["passport_expiry"] = "2020-01-01"

        response = handler(event, None)

        eval_body = response["body"]["evaluation_result"]["body"]
        assert eval_body["passport_status"] == "CRITICAL"

    def test_resolves_then_reevaluates(self, endpoint_arn):
        """Stale alerts are resolved before re-evaluation creates new ones."""
        _seed_alert("1234567890", "DL-EB-001", "alert-old", status="ACTIVE")

        event = _base_event(endpoint_arn)
        event["detail"]["profile"]["nationality"] = "IN"
        event["detail"]["itinerary"]["segments"] = [
            {
                "flight_number": "DL300",
                "origin": "JFK",
                "destination": "CN",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ]

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        eval_body = response["body"]["evaluation_result"]["body"]
        assert eval_body["alerts_sent"] >= 1
