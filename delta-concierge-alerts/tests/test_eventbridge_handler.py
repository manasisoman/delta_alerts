"""Tests for the EventBridge handler for itinerary-change events.

Covers the happy path (resolve stale alerts then re-evaluate), the default
change_type fallback, and the response shape for various change types.
"""

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler
from src.models.types import AlertSeverity, AlertStatus, AlertType


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up DynamoDB table + SNS for every test."""
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
            Name="TestApp",
            Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn,
            Token="fake-token",
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
    """Expose the moto SNS endpoint ARN."""
    return _mock_aws


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _seed_alert(
    skymiles_number: str = "9999999",
    alert_id: str = "alert-001",
    status: str = AlertStatus.ACTIVE.value,
    itinerary_ref: str = "DL-TEST",
):
    """Insert a minimal alert row into the mocked DynamoDB table."""
    alert_store_mod.table.put_item(
        Item={
            "skymiles_number": skymiles_number,
            "alert_id": alert_id,
            "alert_type": AlertType.VISA.value,
            "severity": AlertSeverity.CRITICAL.value,
            "reasons": ["No visa on file for CN"],
            "created_at": "2026-01-01T00:00:00",
            "itinerary_ref": itinerary_ref,
            "status": status,
        }
    )


def _eventbridge_event(
    endpoint_arn: str,
    change_type: str | None = "DATE_CHANGE",
    nationality: str = "IN",
    destination: str = "CN",
    confirmation_number: str = "DL-TEST",
) -> dict:
    """Build an EventBridge ItineraryChanged event envelope."""
    detail: dict = {
        "profile": {
            "skymiles_number": "9999999",
            "first_name": "Test",
            "last_name": "User",
            "nationality": nationality,
            "passport_number": "P999999",
            "passport_expiry": "2030-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": confirmation_number,
            "segments": [
                {
                    "flight_number": "DL100",
                    "origin": "ATL",
                    "destination": destination,
                    "departure_date": "2026-09-15",
                    "arrival_date": "2026-09-16",
                    "is_layover": False,
                }
            ],
        },
    }
    if change_type is not None:
        detail["change_type"] = change_type

    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

class TestEventBridgeHandler:
    """End-to-end tests for the EventBridge itinerary-change handler."""

    def test_happy_path_resolves_stale_alerts_and_re_evaluates(self, endpoint_arn):
        """Existing active alerts are resolved, then itinerary is re-evaluated."""
        _seed_alert(alert_id="stale-1", itinerary_ref="DL-TEST")
        _seed_alert(alert_id="stale-2", itinerary_ref="DL-TEST")

        event = _eventbridge_event(endpoint_arn, change_type="DATE_CHANGE")
        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 2

        # The evaluation result should be a valid handler response
        eval_result = body["evaluation_result"]
        assert eval_result["statusCode"] == 200
        assert "passport_status" in eval_result["body"]
        assert "visa_status" in eval_result["body"]

        # Verify stale alerts were resolved in DynamoDB
        item1 = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "stale-1"}
        )["Item"]
        assert item1["status"] == AlertStatus.RESOLVED.value

        item2 = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "stale-2"}
        )["Item"]
        assert item2["status"] == AlertStatus.RESOLVED.value

    def test_no_stale_alerts_to_resolve(self, endpoint_arn):
        """When no pre-existing alerts exist, resolved_alerts is 0."""
        event = _eventbridge_event(endpoint_arn, change_type="SEGMENT_ADDED")
        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "SEGMENT_ADDED"
        assert body["resolved_alerts"] == 0
        assert body["evaluation_result"]["statusCode"] == 200

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        """When change_type is absent from the detail, it defaults to UNKNOWN."""
        event = _eventbridge_event(endpoint_arn, change_type=None)
        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["change_type"] == "UNKNOWN"

    def test_only_matching_itinerary_alerts_resolved(self, endpoint_arn):
        """Only alerts for the matching confirmation_number are resolved."""
        _seed_alert(alert_id="match-1", itinerary_ref="DL-TEST")
        _seed_alert(alert_id="other-1", itinerary_ref="DL-OTHER")

        event = _eventbridge_event(endpoint_arn, change_type="ROUTE_CHANGE")
        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1

        # The matching alert should be resolved
        match = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "match-1"}
        )["Item"]
        assert match["status"] == AlertStatus.RESOLVED.value

        # The other alert should remain active
        other = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "other-1"}
        )["Item"]
        assert other["status"] == AlertStatus.ACTIVE.value

    def test_already_resolved_alerts_not_counted(self, endpoint_arn):
        """Already-resolved alerts are not re-resolved."""
        _seed_alert(
            alert_id="already-resolved",
            itinerary_ref="DL-TEST",
            status=AlertStatus.RESOLVED.value,
        )

        event = _eventbridge_event(endpoint_arn, change_type="SEGMENT_REMOVED")
        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0

    def test_re_evaluation_produces_new_alerts(self, endpoint_arn):
        """After resolving stale alerts, re-evaluation creates fresh alerts."""
        _seed_alert(alert_id="stale-visa", itinerary_ref="DL-TEST")

        # IN national travelling to CN without a visa → CRITICAL visa alert
        event = _eventbridge_event(
            endpoint_arn, change_type="DATE_CHANGE", destination="CN"
        )
        response = handler(event, None)

        eval_result = response["body"]["evaluation_result"]
        assert eval_result["body"]["visa_status"] == "CRITICAL"
        assert eval_result["body"]["alerts_sent"] >= 1

    def test_clean_traveler_re_evaluation_no_new_alerts(self, endpoint_arn):
        """US national to DE (visa-exempt) with valid passport → no new alerts."""
        event = _eventbridge_event(
            endpoint_arn,
            change_type="DATE_CHANGE",
            nationality="US",
            destination="DE",
        )
        response = handler(event, None)

        eval_result = response["body"]["evaluation_result"]
        assert eval_result["body"]["passport_status"] == "OK"
        assert eval_result["body"]["visa_status"] == "OK"
        assert eval_result["body"]["alerts_sent"] == 0
