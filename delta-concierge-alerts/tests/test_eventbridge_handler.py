"""Tests for the EventBridge handler that processes ItineraryChanged events.

Covers:
- Happy-path processing with alert re-evaluation
- change_type extraction and default fallback to UNKNOWN
- Stale-alert resolution before re-evaluation
- End-to-end response shape and content
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
    """Expose the moto SNS endpoint ARN created by _mock_aws."""
    return _mock_aws


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

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
            "reasons": ["No visa on file for CN"],
            "created_at": "2026-01-01T00:00:00",
            "itinerary_ref": itinerary_ref,
            "status": status,
        }
    )


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid EventBridge event with optional field overrides.

    Overrides can include:
      - change_type: replaces the change_type in detail
      - profile fields: merged into the profile dict
      - Any top-level detail key
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
    for key, val in overrides.items():
        if key == "change_type":
            event["detail"]["change_type"] = val
        elif key in event["detail"]["profile"]:
            event["detail"]["profile"][key] = val
        else:
            event["detail"][key] = val
    return event


# ------------------------------------------------------------------
# 1. Happy-path end-to-end flow
# ------------------------------------------------------------------

class TestHappyPath:
    """EventBridge handler processes an itinerary change and returns evaluation results."""

    def test_returns_200_with_evaluation_result(self, endpoint_arn):
        """Handler should return statusCode 200 and include evaluation_result."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert "evaluation_result" in body
        assert "resolved_alerts" in body
        assert "change_type" in body

    def test_evaluation_result_contains_passport_and_visa_status(self, endpoint_arn):
        """The nested evaluation_result should contain passport and visa statuses."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        eval_result = response["body"]["evaluation_result"]
        assert eval_result["statusCode"] == 200
        assert "passport_status" in eval_result["body"]
        assert "visa_status" in eval_result["body"]

    def test_clean_traveler_no_alerts(self, endpoint_arn):
        """US national to DE with valid passport → OK statuses, no alerts."""
        event = _base_event(endpoint_arn, nationality="US")
        event["detail"]["itinerary"]["segments"] = [
            {
                "flight_number": "DL200",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ]

        response = handler(event, None)

        eval_result = response["body"]["evaluation_result"]
        assert eval_result["body"]["passport_status"] == "OK"
        assert eval_result["body"]["visa_status"] == "OK"
        assert eval_result["body"]["alerts_sent"] == 0


# ------------------------------------------------------------------
# 2. change_type extraction
# ------------------------------------------------------------------

class TestChangeType:
    """Handler correctly extracts change_type and falls back to UNKNOWN."""

    def test_explicit_change_type_returned(self, endpoint_arn):
        """Explicit change_type should appear in the response body."""
        event = _base_event(endpoint_arn, change_type="ROUTE_CHANGE")

        response = handler(event, None)

        assert response["body"]["change_type"] == "ROUTE_CHANGE"

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        """When change_type is absent, it should default to UNKNOWN."""
        event = _base_event(endpoint_arn)
        del event["detail"]["change_type"]

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"

    def test_segment_added_change_type(self, endpoint_arn):
        """SEGMENT_ADDED change_type should pass through correctly."""
        event = _base_event(endpoint_arn, change_type="SEGMENT_ADDED")

        response = handler(event, None)

        assert response["body"]["change_type"] == "SEGMENT_ADDED"

    def test_segment_removed_change_type(self, endpoint_arn):
        """SEGMENT_REMOVED change_type should pass through correctly."""
        event = _base_event(endpoint_arn, change_type="SEGMENT_REMOVED")

        response = handler(event, None)

        assert response["body"]["change_type"] == "SEGMENT_REMOVED"


# ------------------------------------------------------------------
# 3. Stale-alert resolution
# ------------------------------------------------------------------

class TestStaleAlertResolution:
    """Handler resolves existing alerts for the itinerary before re-evaluation."""

    def test_resolves_active_alerts_for_itinerary(self, endpoint_arn):
        """Pre-existing ACTIVE alerts for the same itinerary should be resolved."""
        _seed_alert(alert_id="stale-001", itinerary_ref="DL-TEST")
        _seed_alert(alert_id="stale-002", itinerary_ref="DL-TEST")

        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 2

        # Verify alerts were actually resolved in DynamoDB
        item1 = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "stale-001"}
        )["Item"]
        assert item1["status"] == AlertStatus.RESOLVED.value

        item2 = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "stale-002"}
        )["Item"]
        assert item2["status"] == AlertStatus.RESOLVED.value

    def test_does_not_resolve_alerts_for_different_itinerary(self, endpoint_arn):
        """Alerts for a different confirmation_number should not be resolved."""
        _seed_alert(alert_id="other-001", itinerary_ref="DL-OTHER")

        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0

        # The other alert should remain ACTIVE
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": "other-001"}
        )["Item"]
        assert item["status"] == AlertStatus.ACTIVE.value

    def test_no_stale_alerts_returns_zero_resolved(self, endpoint_arn):
        """When no pre-existing alerts exist, resolved_alerts should be 0."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0

    def test_already_resolved_alerts_not_counted(self, endpoint_arn):
        """Already-resolved alerts should not be resolved again."""
        _seed_alert(
            alert_id="resolved-001",
            itinerary_ref="DL-TEST",
            status=AlertStatus.RESOLVED.value,
        )

        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0


# ------------------------------------------------------------------
# 4. Re-evaluation triggers alerts for document issues
# ------------------------------------------------------------------

class TestReEvaluation:
    """Handler delegates to evaluate_itinerary which creates new alerts."""

    def test_missing_visa_triggers_critical_evaluation(self, endpoint_arn):
        """IN national to CN without visa should produce CRITICAL visa status."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        eval_result = response["body"]["evaluation_result"]
        assert eval_result["body"]["visa_status"] == "CRITICAL"
        assert eval_result["body"]["alerts_sent"] >= 1

    def test_expired_passport_triggers_critical_evaluation(self, endpoint_arn):
        """Expired passport should produce CRITICAL passport status."""
        event = _base_event(endpoint_arn, passport_expiry="2020-01-01")

        response = handler(event, None)

        eval_result = response["body"]["evaluation_result"]
        assert eval_result["body"]["passport_status"] == "CRITICAL"

    def test_stale_alerts_resolved_then_new_alerts_created(self, endpoint_arn):
        """Old alerts should be resolved and new ones created in one pass."""
        _seed_alert(alert_id="old-001", itinerary_ref="DL-TEST")

        event = _base_event(endpoint_arn)

        response = handler(event, None)

        # Old alert resolved
        assert response["body"]["resolved_alerts"] == 1

        # New alert created by re-evaluation (visa CRITICAL for IN→CN)
        eval_result = response["body"]["evaluation_result"]
        assert eval_result["body"]["alerts_sent"] >= 1
