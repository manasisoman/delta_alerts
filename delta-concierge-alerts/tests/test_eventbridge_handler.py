"""Tests for the EventBridge itinerary-changed handler."""

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up DynamoDB table (with GSI) + SNS endpoint."""
    with moto.mock_aws():
        # DynamoDB with GSI for itinerary_ref lookups
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

        # Save originals
        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client

        # Monkey-patch module-level clients
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
    """Expose the moto SNS endpoint ARN."""
    return _mock_aws


def _base_event(endpoint_arn: str, change_type: str = "DATE_CHANGE") -> dict:
    """Build a valid EventBridge ItineraryChanged event."""
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": {
            "profile": {
                "skymiles_number": "1234567890",
                "first_name": "Test",
                "last_name": "User",
                "nationality": "IN",
                "passport_number": "P999999",
                "passport_expiry": "2030-01-01",
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


def _seed_alert(skymiles_number: str, alert_id: str, confirmation: str, status: str = "ACTIVE"):
    """Insert a pre-existing alert into the mocked DynamoDB table."""
    alert_store_mod.table.put_item(Item={
        "skymiles_number": skymiles_number,
        "alert_id": alert_id,
        "alert_type": "VISA",
        "severity": "CRITICAL",
        "reasons": "No visa on file for CN",
        "created_at": "2026-01-01T00:00:00",
        "itinerary_ref": confirmation,
        "status": status,
    })


class TestItineraryChangedEvent:
    """Verify EventBridge handler resolves stale alerts and re-evaluates itineraries."""

    def test_resolves_existing_active_alert(self, endpoint_arn):
        """Pre-seeded active alert is resolved and evaluation re-runs."""
        _seed_alert("1234567890", "alert-001", "DL-CHANGED", status="ACTIVE")

        event = _base_event(endpoint_arn, change_type="DATE_CHANGE")
        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 1
        assert "evaluation_result" in body

        # Verify alert was actually resolved in DynamoDB
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "alert-001"}
        )["Item"]
        assert item["status"] == "RESOLVED"
        assert "itinerary changed" in item["resolution"]

    def test_resolves_acknowledged_alert(self, endpoint_arn):
        """Acknowledged alerts are also resolved on itinerary change."""
        _seed_alert("1234567890", "alert-002", "DL-CHANGED", status="ACKNOWLEDGED")

        event = _base_event(endpoint_arn, change_type="SEGMENT_ADDED")
        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["resolved_alerts"] == 1

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "alert-002"}
        )["Item"]
        assert item["status"] == "RESOLVED"

    def test_no_existing_alerts_returns_zero_resolved(self, endpoint_arn):
        """With no pre-existing alerts, resolved_count is 0 and evaluation still runs."""
        event = _base_event(endpoint_arn, change_type="ROUTE_CHANGE")
        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "ROUTE_CHANGE"
        assert body["resolved_alerts"] == 0
        assert body["evaluation_result"] is not None

    def test_does_not_resolve_already_resolved_alerts(self, endpoint_arn):
        """Alerts already resolved are not double-resolved."""
        _seed_alert("1234567890", "alert-003", "DL-CHANGED", status="RESOLVED")

        event = _base_event(endpoint_arn, change_type="SEGMENT_REMOVED")
        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["resolved_alerts"] == 0

    def test_does_not_resolve_alerts_for_different_itinerary(self, endpoint_arn):
        """Alerts for a different confirmation_number are untouched."""
        _seed_alert("1234567890", "alert-004", "DL-OTHER", status="ACTIVE")

        event = _base_event(endpoint_arn, change_type="DATE_CHANGE")
        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0

        # Original alert remains ACTIVE
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "alert-004"}
        )["Item"]
        assert item["status"] == "ACTIVE"

    def test_resolves_multiple_alerts_for_same_itinerary(self, endpoint_arn):
        """Multiple active alerts for the same itinerary are all resolved."""
        _seed_alert("1234567890", "alert-005", "DL-CHANGED", status="ACTIVE")
        _seed_alert("1234567890", "alert-006", "DL-CHANGED", status="ACTIVE")
        _seed_alert("1234567890", "alert-007", "DL-CHANGED", status="ACKNOWLEDGED")

        event = _base_event(endpoint_arn, change_type="DATE_CHANGE")
        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 3

    def test_change_type_segment_added(self, endpoint_arn):
        """SEGMENT_ADDED change_type is passed through in the response."""
        event = _base_event(endpoint_arn, change_type="SEGMENT_ADDED")
        response = handler(event, None)

        assert response["body"]["change_type"] == "SEGMENT_ADDED"

    def test_change_type_segment_removed(self, endpoint_arn):
        """SEGMENT_REMOVED change_type is passed through in the response."""
        event = _base_event(endpoint_arn, change_type="SEGMENT_REMOVED")
        response = handler(event, None)

        assert response["body"]["change_type"] == "SEGMENT_REMOVED"

    def test_change_type_route_change(self, endpoint_arn):
        """ROUTE_CHANGE change_type is passed through in the response."""
        event = _base_event(endpoint_arn, change_type="ROUTE_CHANGE")
        response = handler(event, None)

        assert response["body"]["change_type"] == "ROUTE_CHANGE"

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        """When change_type is absent from detail, it defaults to UNKNOWN."""
        event = _base_event(endpoint_arn)
        del event["detail"]["change_type"]

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["change_type"] == "UNKNOWN"

    def test_evaluation_result_contains_expected_keys(self, endpoint_arn):
        """The evaluation_result is a valid response dict from evaluate_itinerary."""
        event = _base_event(endpoint_arn, change_type="DATE_CHANGE")
        response = handler(event, None)

        eval_result = response["body"]["evaluation_result"]
        assert eval_result["statusCode"] == 200
        assert "body" in eval_result
