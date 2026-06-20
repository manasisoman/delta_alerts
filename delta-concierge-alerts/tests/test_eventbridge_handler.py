"""Tests for the EventBridge ItineraryChanged handler.

Covers change_type extraction (including the UNKNOWN default), alert
resolution for stale itinerary alerts, and re-evaluation of updated
itineraries for both clean and alerting traveler profiles.
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

def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid EventBridge ItineraryChanged event with optional overrides."""
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
                        "origin": "ATL",
                        "destination": "DE",
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
        if key in detail:
            detail[key] = val
        elif key in detail["profile"]:
            detail["profile"][key] = val
        elif key in detail["itinerary"]:
            detail["itinerary"][key] = val
    return event


def _seed_alert(
    skymiles_number: str = "1234567890",
    alert_id: str = "alert-001",
    itinerary_ref: str = "DL-TEST",
    status: str = AlertStatus.ACTIVE.value,
):
    """Insert a minimal alert row into the mocked DynamoDB table."""
    alert_store_mod.table.put_item(
        Item={
            "skymiles_number": skymiles_number,
            "alert_id": alert_id,
            "alert_type": AlertType.VISA.value,
            "severity": AlertSeverity.CRITICAL.value,
            "reasons": ["No visa on file for DE"],
            "created_at": "2026-01-01T00:00:00",
            "itinerary_ref": itinerary_ref,
            "status": status,
        }
    )


# ------------------------------------------------------------------
# 1. Basic ItineraryChanged with DATE_CHANGE
# ------------------------------------------------------------------

class TestBasicDateChange:
    """DATE_CHANGE event resolves stale alerts and re-evaluates the itinerary."""

    def test_date_change_returns_200_with_expected_shape(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert "resolved_alerts" in body
        assert "evaluation_result" in body

    def test_evaluation_result_contains_status_fields(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        eval_result = response["body"]["evaluation_result"]
        assert eval_result["statusCode"] == 200
        assert "passport_status" in eval_result["body"]
        assert "visa_status" in eval_result["body"]


# ------------------------------------------------------------------
# 2. Missing change_type defaults to UNKNOWN
# ------------------------------------------------------------------

class TestMissingChangeType:
    """When change_type is absent from the detail, it defaults to UNKNOWN."""

    def test_no_change_type_defaults_to_unknown(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["change_type"]

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["change_type"] == "UNKNOWN"


# ------------------------------------------------------------------
# 3. Existing alerts to resolve
# ------------------------------------------------------------------

class TestResolvesExistingAlerts:
    """When stale alerts exist for the itinerary they are resolved before re-evaluation."""

    def test_resolves_active_alerts_for_itinerary(self, endpoint_arn):
        _seed_alert(alert_id="stale-1", status=AlertStatus.ACTIVE.value)
        _seed_alert(alert_id="stale-2", status=AlertStatus.ACKNOWLEDGED.value)

        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 2

        item1 = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "stale-1"}
        )["Item"]
        assert item1["status"] == AlertStatus.RESOLVED.value

        item2 = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "stale-2"}
        )["Item"]
        assert item2["status"] == AlertStatus.RESOLVED.value

    def test_does_not_resolve_alerts_for_different_itinerary(self, endpoint_arn):
        _seed_alert(alert_id="other-1", itinerary_ref="DL-OTHER")

        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0

        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "other-1"}
        )["Item"]
        assert item["status"] == AlertStatus.ACTIVE.value

    def test_does_not_resolve_already_resolved_alerts(self, endpoint_arn):
        _seed_alert(alert_id="resolved-1", status=AlertStatus.RESOLVED.value)

        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0


# ------------------------------------------------------------------
# 4. No existing alerts to resolve
# ------------------------------------------------------------------

class TestNoAlertsToResolve:
    """When there are no stale alerts the resolved count is 0."""

    def test_resolved_count_zero_when_no_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0


# ------------------------------------------------------------------
# 5. Re-evaluation produces alerts (expired passport)
# ------------------------------------------------------------------

class TestReEvaluationProducesAlerts:
    """After resolving old alerts, re-evaluation flags new document issues."""

    def test_expired_passport_triggers_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["detail"]["profile"]["passport_expiry"] = "2020-01-01"

        response = handler(event, None)

        eval_result = response["body"]["evaluation_result"]
        assert eval_result["body"]["passport_status"] == "CRITICAL"
        assert eval_result["body"]["alerts_sent"] >= 1

    def test_missing_visa_triggers_alert(self, endpoint_arn):
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

        eval_result = response["body"]["evaluation_result"]
        assert eval_result["body"]["visa_status"] == "CRITICAL"
        assert eval_result["body"]["alerts_sent"] >= 1

    def test_resolves_old_then_creates_new_alerts(self, endpoint_arn):
        _seed_alert(alert_id="old-1")

        event = _base_event(endpoint_arn)
        event["detail"]["profile"]["passport_expiry"] = "2020-01-01"

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        eval_result = response["body"]["evaluation_result"]
        assert eval_result["body"]["alerts_sent"] >= 1


# ------------------------------------------------------------------
# 6. Re-evaluation produces no alerts (clean traveler)
# ------------------------------------------------------------------

class TestReEvaluationCleanTraveler:
    """A fully compliant traveler produces no new alerts after re-evaluation."""

    def test_clean_traveler_no_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["detail"]["profile"]["nationality"] = "US"

        response = handler(event, None)

        eval_result = response["body"]["evaluation_result"]
        assert eval_result["body"]["passport_status"] == "OK"
        assert eval_result["body"]["visa_status"] == "OK"
        assert eval_result["body"]["alerts_sent"] == 0
