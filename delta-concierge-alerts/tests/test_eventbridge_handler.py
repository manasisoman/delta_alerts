"""Tests for the EventBridge handler that processes ItineraryChanged events."""

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
    """Build a full EventBridge event envelope with optional overrides.

    Overrides can include:
    - change_type: replaces detail.change_type
    - nationality: replaces profile.nationality
    - segments: replaces itinerary.segments
    - Any profile-level key (passport_expiry, visa_records, etc.)
    """
    event = {
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
                "confirmation_number": "DL-TEST",
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
            "change_type": "DATE_CHANGE",
        },
    }

    detail = event["detail"]
    for key, val in overrides.items():
        if key == "change_type":
            detail["change_type"] = val
        elif key == "segments":
            detail["itinerary"]["segments"] = val
        elif key in detail["profile"]:
            detail["profile"][key] = val

    return event


def _seed_alert(skymiles_number: str, alert_id: str, confirmation_number: str) -> None:
    """Insert a stale ACTIVE alert into DynamoDB for resolution testing."""
    alert_store_mod.table.put_item(
        Item={
            "skymiles_number": skymiles_number,
            "alert_id": alert_id,
            "alert_type": "VISA",
            "severity": "CRITICAL",
            "reasons": ["No visa on file for CN"],
            "created_at": "2026-01-01T00:00:00",
            "itinerary_ref": confirmation_number,
            "status": "ACTIVE",
        }
    )


class TestEventBridgeBasicFlow:
    """Basic ItineraryChanged event handling: status code, response shape, change_type."""

    def test_returns_200_with_evaluation_result(self, endpoint_arn):
        """Handler returns statusCode 200 and evaluation_result is present."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert "evaluation_result" in body
        assert "resolved_alerts" in body
        assert "change_type" in body

    def test_change_type_included_in_response(self, endpoint_arn):
        """change_type from the event is reflected in the response body."""
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


class TestStaleAlertResolution:
    """Stale alerts are resolved before re-evaluation."""

    def test_existing_alerts_resolved_before_reevaluation(self, endpoint_arn):
        """Seeded ACTIVE alerts become RESOLVED after handler processes the event."""
        _seed_alert("1234567890", "alert-001", "DL-TEST")
        _seed_alert("1234567890", "alert-002", "DL-TEST")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 2

        # Verify DynamoDB state
        items = alert_store_mod.table.scan()["Items"]
        seeded = [i for i in items if i["alert_id"] in ("alert-001", "alert-002")]
        for item in seeded:
            assert item["status"] == "RESOLVED"
            assert item["resolution"] == "Auto-resolved: itinerary changed, re-evaluating"

    def test_no_stale_alerts_returns_zero_resolved(self, endpoint_arn):
        """When no prior alerts exist, resolved_alerts is 0."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0

    def test_alerts_for_different_itinerary_not_resolved(self, endpoint_arn):
        """Alerts belonging to a different confirmation_number remain ACTIVE."""
        _seed_alert("1234567890", "alert-other", "DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "alert-other"}
        )["Item"]
        assert item["status"] == "ACTIVE"


class TestCleanTraveler:
    """US national flying to DE (visa-exempt) with valid passport → no new alerts."""

    def test_us_to_de_no_alerts_created(self, endpoint_arn):
        """Clean traveler scenario: no alerts created after re-evaluation."""
        event = _base_event(
            endpoint_arn,
            nationality="US",
            segments=[
                {
                    "flight_number": "DL200",
                    "origin": "ATL",
                    "destination": "DE",
                    "departure_date": "2026-09-15",
                    "arrival_date": "2026-09-16",
                    "is_layover": False,
                }
            ],
        )

        response = handler(event, None)

        assert response["statusCode"] == 200
        eval_result = response["body"]["evaluation_result"]
        eval_body = eval_result["body"]
        assert eval_body["passport_status"] == "OK"
        assert eval_body["visa_status"] == "OK"
        assert eval_body["alerts_sent"] == 0

        # No new alerts in DynamoDB
        items = alert_store_mod.table.scan()["Items"]
        assert len(items) == 0


class TestProblematicTraveler:
    """IN national flying to CN with no visa → new alerts created after re-evaluation."""

    def test_in_to_cn_no_visa_creates_alerts(self, endpoint_arn):
        """Indian national to China without visa triggers CRITICAL visa alert."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        eval_body = response["body"]["evaluation_result"]["body"]
        assert eval_body["visa_status"] == "CRITICAL"
        assert eval_body["alerts_sent"] >= 1

        # Verify new alerts were persisted
        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert len(visa_alerts) >= 1

    def test_stale_resolved_then_new_alerts_created(self, endpoint_arn):
        """Stale alert resolved AND new alert created in a single handler call."""
        _seed_alert("1234567890", "old-alert", "DL-TEST")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert response["body"]["evaluation_result"]["body"]["alerts_sent"] >= 1

        items = alert_store_mod.table.scan()["Items"]
        old = [i for i in items if i["alert_id"] == "old-alert"]
        assert old[0]["status"] == "RESOLVED"

        new_active = [i for i in items if i["status"] == "ACTIVE"]
        assert len(new_active) >= 1
