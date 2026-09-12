"""Critical-path tests for the Lambda handler end-to-end flow."""

from datetime import date, datetime, timedelta

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.lambda_handler import (
    _compute_ttl,
    _parse_itinerary,
    evaluate_itinerary,
    handler,
)
from src.models.types import Itinerary


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up DynamoDB table + SNS endpoint for every test."""
    with moto.mock_aws():
        # DynamoDB
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
            # Restore original clients so other test modules aren't affected
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoint_arn(_mock_aws):
    """Expose the moto SNS endpoint ARN created by _mock_aws."""
    return _mock_aws


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid Lambda event with optional field overrides."""
    event = {
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
        "requirements_override": None,
    }
    for key, val in overrides.items():
        if key in event["profile"]:
            event["profile"][key] = val
    return event


class TestHandlerEndToEnd:
    """Full round-trip: event → evaluations → DynamoDB + SNS → response."""

    def test_alerts_persisted_and_response_shape(self, endpoint_arn):
        """Missing visa for CN should persist alert and return CRITICAL."""
        event = _base_event(endpoint_arn)
        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["visa_status"] == "CRITICAL"
        assert body["alerts_sent"] >= 1

        # Verify DynamoDB has the record
        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert len(visa_alerts) >= 1
        assert "No visa on file for CN" in visa_alerts[0]["reasons"]

    def test_no_alerts_for_clean_traveler(self, endpoint_arn):
        """US national to DE with valid passport → no alerts."""
        event = _base_event(endpoint_arn)
        event["profile"]["nationality"] = "US"
        event["itinerary"]["segments"] = [
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

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["passport_status"] == "OK"
        assert body["visa_status"] == "OK"
        assert body["alerts_sent"] == 0

    def test_expired_passport_triggers_critical(self, endpoint_arn):
        """Expired passport should return CRITICAL passport status."""
        event = _base_event(endpoint_arn)
        event["profile"]["passport_expiry"] = "2020-01-01"

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["passport_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] >= 1

    def test_missing_passport_triggers_critical(self, endpoint_arn):
        """Null passport fields should return CRITICAL passport status."""
        event = _base_event(endpoint_arn)
        event["profile"]["passport_number"] = None
        event["profile"]["passport_expiry"] = None

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["passport_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] >= 1


class TestGroupDelegation:
    """Events containing a ``travelers`` key are routed to the group handler."""

    def test_travelers_key_delegates_to_group_handler(self, endpoint_arn):
        """Group payload with a clean US traveler → group response shape, no alerts."""
        event = {
            "confirmation_number": "DL-GROUP",
            "primary_traveler": "1111111111",
            "travelers": [
                {
                    "skymiles_number": "1111111111",
                    "first_name": "Group",
                    "last_name": "Lead",
                    "nationality": "US",
                    "passport_number": "P111111",
                    "passport_expiry": "2030-01-01",
                    "endpoint_arn": endpoint_arn,
                    "visa_records": [],
                }
            ],
            "segments": [
                {
                    "flight_number": "DL300",
                    "origin": "ATL",
                    "destination": "DE",
                    "departure_date": "2026-09-15",
                    "arrival_date": "2026-09-16",
                    "is_layover": False,
                }
            ],
        }

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["confirmation_number"] == "DL-GROUP"
        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["group_severity"] == "OK"
        assert alert_store_mod.table.scan()["Items"] == []


class TestRequirementsOverride:
    """``requirements_override`` replaces the default rules for a country."""

    def test_override_marks_nationality_visa_exempt(self, endpoint_arn):
        """Overriding CN to exempt IN nationals clears the visa alert."""
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {
            "CN": {
                "requires_visa": True,
                "transit_visa_required": True,
                "passport_validity_months": 6,
                "visa_exempt_nationalities": ["IN"],
            }
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_override_uses_defaults_for_missing_fields(self, endpoint_arn):
        """An empty override dict falls back to requires_visa=True → CRITICAL."""
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {"CN": {}}

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] == 1

    def test_override_adds_unknown_country(self, endpoint_arn):
        """Override for a country not in defaults with requires_visa=False → OK."""
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"][0]["destination"] = "ZZ"
        event["requirements_override"] = {"ZZ": {"requires_visa": False}}

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0


class TestVisaRecordParsing:
    """Visa records on the profile are deserialized and used by the evaluator."""

    def test_valid_visa_record_clears_visa_alert(self, endpoint_arn):
        """A far-future CN visa on file → no visa alert."""
        event = _base_event(endpoint_arn)
        event["profile"]["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2025-01-01",
                "expiry_date": "2030-01-01",
                "visa_number": "V123",
            }
        ]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_expired_visa_record_triggers_critical(self, endpoint_arn):
        """An already-expired CN visa → CRITICAL visa alert."""
        event = _base_event(endpoint_arn)
        event["profile"]["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2015-01-01",
                "expiry_date": "2020-01-01",
                "visa_number": "V123",
            }
        ]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] == 1


class TestPersistedAlertFields:
    """Persisted alert records carry the correct TTL, status, and itinerary ref."""

    def test_ttl_is_thirty_days_after_last_arrival(self, endpoint_arn):
        """TTL epoch equals midnight of latest arrival_date + 30 days."""
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"].append(
            {
                "flight_number": "DL101",
                "origin": "CN",
                "destination": "JP",
                "departure_date": "2026-09-20",
                "arrival_date": "2026-09-21",
                "is_layover": False,
            }
        )

        handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        assert len(items) >= 1
        expected = _compute_ttl(_parse_itinerary(event["itinerary"]))
        expected_dt = datetime.combine(date(2026, 9, 21), datetime.min.time()) + timedelta(days=30)
        assert expected == int(expected_dt.timestamp())
        assert all(int(i["ttl"]) == expected for i in items)
        assert all(i["status"] == "ACTIVE" for i in items)
        assert all(i["itinerary_ref"] == "DL-TEST" for i in items)

    def test_compute_ttl_with_no_segments_uses_today(self):
        """Empty itinerary → TTL is 30 days from today."""
        itinerary = Itinerary(confirmation_number="DL-EMPTY", segments=[])

        ttl = _compute_ttl(itinerary)

        expected_dt = datetime.combine(date.today(), datetime.min.time()) + timedelta(days=30)
        assert ttl == int(expected_dt.timestamp())


class TestEvaluateItinerary:
    """``evaluate_itinerary`` is a thin wrapper around ``handler``."""

    def test_returns_same_response_as_handler(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["profile"]["nationality"] = "US"
        event["itinerary"]["segments"][0]["destination"] = "DE"

        response = evaluate_itinerary(event)

        assert response == {
            "statusCode": 200,
            "body": {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"},
        }
