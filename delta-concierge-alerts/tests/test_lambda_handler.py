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
    _parse_profile,
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


class TestRequirementsOverride:
    """requirements_override entries replace the default rules for that country."""

    def test_override_waives_visa_requirement(self, endpoint_arn):
        """Overriding CN to requires_visa=False should clear the visa alert."""
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {
            "CN": {"requires_visa": False, "passport_validity_months": 1}
        }

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["passport_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0
        assert alert_store_mod.table.scan()["Items"] == []

    def test_override_exempt_nationality_clears_visa(self, endpoint_arn):
        """Adding IN to CN's visa_exempt_nationalities should clear the visa alert."""
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {"CN": {"visa_exempt_nationalities": ["IN"]}}

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_override_defaults_fill_missing_fields(self, endpoint_arn):
        """An empty override dict defaults to requires_visa=True, so the alert remains."""
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {"CN": {}}

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] == 1

    def test_override_adds_unknown_country(self, endpoint_arn):
        """Overrides can introduce a country not present in the defaults."""
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"][0]["destination"] = "ZZ"
        event["requirements_override"] = {"ZZ": {"requires_visa": True}}

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        assert any("ZZ" in r for i in items for r in i["reasons"])

    def test_empty_override_dict_is_ignored(self, endpoint_arn):
        """An empty (falsy) override mapping leaves default rules untouched."""
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {}

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestGroupDelegation:
    """Events containing a ``travelers`` key are routed to the group handler."""

    def test_travelers_key_routes_to_group_handler(self, endpoint_arn):
        """A group payload returns the group response shape with per-traveler results."""
        event = {
            "confirmation_number": "DL-GROUP",
            "primary_traveler": "1111111111",
            "travelers": [
                {
                    "skymiles_number": "1111111111",
                    "first_name": "Primary",
                    "last_name": "Traveler",
                    "nationality": "US",
                    "passport_number": "P111",
                    "passport_expiry": "2030-01-01",
                    "endpoint_arn": endpoint_arn,
                    "visa_records": [],
                },
                {
                    "skymiles_number": "2222222222",
                    "first_name": "Second",
                    "last_name": "Traveler",
                    "nationality": "IN",
                    "passport_number": "P222",
                    "passport_expiry": "2030-01-01",
                    "endpoint_arn": endpoint_arn,
                    "visa_records": [],
                },
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
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert "traveler_results" in body
        assert len(body["traveler_results"]) == 2
        statuses = {r["skymiles_number"]: r["visa_status"] for r in body["traveler_results"]}
        assert statuses["1111111111"] == "OK"
        assert statuses["2222222222"] == "CRITICAL"
        assert body["total_alerts_sent"] == 1

        items = alert_store_mod.table.scan()["Items"]
        assert len(items) == 1
        assert items[0]["skymiles_number"] == "2222222222"

    def test_single_traveler_event_not_delegated(self, endpoint_arn):
        """Without a ``travelers`` key the single-traveler response shape is returned."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert "traveler_results" not in response["body"]
        assert set(response["body"]) == {"alerts_sent", "passport_status", "visa_status"}


class TestEvaluateItinerary:
    """evaluate_itinerary is a thin wrapper around handler."""

    def test_returns_same_result_as_handler(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = evaluate_itinerary(event)

        assert response["statusCode"] == 200
        assert response["body"]["visa_status"] == "CRITICAL"


class TestComputeTtl:
    """TTL is 30 days after the latest arrival date, or after today if no segments."""

    def test_ttl_uses_latest_arrival(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"] = [
            {
                "flight_number": "DL1",
                "origin": "JFK",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
            },
            {
                "flight_number": "DL2",
                "origin": "DE",
                "destination": "CN",
                "departure_date": "2026-09-20",
                "arrival_date": "2026-09-21",
            },
        ]
        itinerary = _parse_itinerary(event["itinerary"])

        ttl = _compute_ttl(itinerary)

        expected = int(
            (datetime(2026, 9, 21) + timedelta(days=30)).timestamp()
        )
        assert ttl == expected

    def test_ttl_falls_back_to_today_without_segments(self):
        itinerary = Itinerary(confirmation_number="EMPTY", segments=[])

        ttl = _compute_ttl(itinerary)

        expected = int(
            (datetime.combine(date.today(), datetime.min.time()) + timedelta(days=30)).timestamp()
        )
        assert ttl == expected

    def test_persisted_alert_carries_ttl(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        expected = int((datetime(2026, 9, 16) + timedelta(days=30)).timestamp())
        assert all(int(i["ttl"]) == expected for i in items)


class TestParsing:
    """_parse_profile / _parse_itinerary deserialize ISO dates and optional fields."""

    def test_parse_profile_with_visa_records(self, endpoint_arn):
        data = _base_event(endpoint_arn)["profile"]
        data["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2025-01-01",
                "expiry_date": "2030-01-01",
                "visa_number": "V123",
            }
        ]

        profile = _parse_profile(data)

        assert profile.passport_expiry == date(2030, 1, 1)
        assert len(profile.visa_records) == 1
        assert profile.visa_records[0].country_code == "CN"
        assert profile.visa_records[0].expiry_date == date(2030, 1, 1)

    def test_parse_profile_missing_optional_fields(self, endpoint_arn):
        data = _base_event(endpoint_arn)["profile"]
        del data["visa_records"]
        del data["passport_number"]
        data["passport_expiry"] = None

        profile = _parse_profile(data)

        assert profile.visa_records == []
        assert profile.passport_number is None
        assert profile.passport_expiry is None

    def test_parse_itinerary_defaults_is_layover(self, endpoint_arn):
        data = _base_event(endpoint_arn)["itinerary"]
        del data["segments"][0]["is_layover"]

        itinerary = _parse_itinerary(data)

        assert itinerary.confirmation_number == "DL-TEST"
        assert itinerary.segments[0].is_layover is False
        assert itinerary.segments[0].departure_date == date(2026, 9, 15)

    def test_valid_visa_record_clears_visa_alert(self, endpoint_arn):
        """A valid CN visa on file should yield visa_status OK end-to-end."""
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
