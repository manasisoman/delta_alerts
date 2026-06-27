"""Tests for the group Lambda handler end-to-end flow."""

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.group_lambda_handler import (
    _find_traveler,
    _parse_group_itinerary,
    group_handler,
)
from src.models.types import GroupItinerary, SkyMilesProfile


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
            PlatformApplicationArn=app_arn, Token="fake-token-1",
        )["EndpointArn"]
        endpoint_arn_2 = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token-2",
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
            yield {"endpoint_arn": endpoint_arn, "endpoint_arn_2": endpoint_arn_2}
        finally:
            # Restore original clients so other test modules aren't affected
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def arns(_mock_aws):
    """Expose the moto SNS endpoint ARNs created by _mock_aws."""
    return _mock_aws


def _base_group_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid group Lambda event with sensible defaults."""
    event = {
        "confirmation_number": "GRP-001",
        "primary_traveler": "1111111",
        "segments": [
            {
                "flight_number": "DL100",
                "origin": "ATL",
                "destination": "CN",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ],
        "travelers": [
            {
                "skymiles_number": "1111111",
                "first_name": "Alice",
                "last_name": "Smith",
                "nationality": "US",
                "passport_number": "P111111",
                "passport_expiry": "2030-01-01",
                "endpoint_arn": endpoint_arn,
                "visa_records": [
                    {
                        "country_code": "CN",
                        "visa_type": "tourist",
                        "issue_date": "2025-01-01",
                        "expiry_date": "2030-01-01",
                        "visa_number": "V111",
                    }
                ],
            }
        ],
        "requirements_override": None,
    }
    for key, val in overrides.items():
        event[key] = val
    return event


class TestGroupHandlerHappyPath:
    """All travelers valid — no alerts generated."""

    def test_all_travelers_valid(self, arns):
        """US national with valid passport and visa for CN → no alerts."""
        event = _base_group_event(arns["endpoint_arn"])

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "GRP-001"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert len(body["traveler_results"]) == 1
        assert body["traveler_results"][0]["passport_status"] == "OK"
        assert body["traveler_results"][0]["visa_status"] == "OK"
        assert body["traveler_results"][0]["alerts_sent"] == 0

    def test_multiple_valid_travelers(self, arns):
        """Multiple travelers all with valid documents → no alerts."""
        event = _base_group_event(arns["endpoint_arn"])
        event["travelers"].append({
            "skymiles_number": "2222222",
            "first_name": "Bob",
            "last_name": "Jones",
            "nationality": "US",
            "passport_number": "P222222",
            "passport_expiry": "2030-06-01",
            "endpoint_arn": arns["endpoint_arn_2"],
            "visa_records": [
                {
                    "country_code": "CN",
                    "visa_type": "tourist",
                    "issue_date": "2025-01-01",
                    "expiry_date": "2030-01-01",
                    "visa_number": "V222",
                }
            ],
        })

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert len(body["traveler_results"]) == 2


class TestGroupHandlerWithAlerts:
    """Travelers with document issues trigger alerts and notifications."""

    def test_missing_visa_triggers_alert(self, arns):
        """IN national with no visa for CN → CRITICAL visa alert persisted."""
        event = _base_group_event(arns["endpoint_arn"])
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = []

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["total_alerts_sent"] >= 1
        assert body["group_severity"] == "CRITICAL"
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"

        # Verify DynamoDB has the record
        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert len(visa_alerts) >= 1
        assert "No visa on file for CN" in visa_alerts[0]["reasons"]

    def test_expired_passport_triggers_alert(self, arns):
        """Expired passport → CRITICAL passport alert persisted."""
        event = _base_group_event(arns["endpoint_arn"])
        event["travelers"][0]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert body["total_alerts_sent"] >= 1

        items = alert_store_mod.table.scan()["Items"]
        passport_alerts = [i for i in items if i["alert_type"] == "PASSPORT"]
        assert len(passport_alerts) >= 1

    def test_missing_passport_triggers_alert(self, arns):
        """Null passport fields → CRITICAL passport alert."""
        event = _base_group_event(arns["endpoint_arn"])
        event["travelers"][0]["passport_number"] = None
        event["travelers"][0]["passport_expiry"] = None

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert body["total_alerts_sent"] >= 1

    def test_per_traveler_results_structure(self, arns):
        """Verify per-traveler results include correct fields."""
        event = _base_group_event(arns["endpoint_arn"])
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = []

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["skymiles_number"] == "1111111"
        assert result["first_name"] == "Alice"
        assert result["last_name"] == "Smith"
        assert "passport_status" in result
        assert "visa_status" in result
        assert "alerts_sent" in result


class TestGroupHandlerMultipleTravelers:
    """Mix of valid and invalid travelers — verify independent evaluation."""

    def test_mixed_valid_and_invalid(self, arns):
        """One valid, one invalid → only invalid traveler gets alerts."""
        event = _base_group_event(arns["endpoint_arn"])
        # First traveler: valid (US with CN visa)
        # Second traveler: IN national with no CN visa
        event["travelers"].append({
            "skymiles_number": "3333333",
            "first_name": "Charlie",
            "last_name": "Brown",
            "nationality": "IN",
            "passport_number": "P333333",
            "passport_expiry": "2030-01-01",
            "endpoint_arn": arns["endpoint_arn_2"],
            "visa_records": [],
        })

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["group_severity"] == "CRITICAL"
        assert len(body["traveler_results"]) == 2

        # First traveler (valid)
        assert body["traveler_results"][0]["passport_status"] == "OK"
        assert body["traveler_results"][0]["visa_status"] == "OK"
        assert body["traveler_results"][0]["alerts_sent"] == 0

        # Second traveler (invalid)
        assert body["traveler_results"][1]["visa_status"] == "CRITICAL"
        assert body["traveler_results"][1]["alerts_sent"] >= 1

    def test_multiple_travelers_both_invalid(self, arns):
        """Both travelers with issues → alerts for each."""
        event = _base_group_event(arns["endpoint_arn"])
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = []
        event["travelers"].append({
            "skymiles_number": "4444444",
            "first_name": "Diana",
            "last_name": "Prince",
            "nationality": "IN",
            "passport_number": "P444444",
            "passport_expiry": "2020-01-01",
            "endpoint_arn": arns["endpoint_arn_2"],
            "visa_records": [],
        })

        response = group_handler(event, None)

        body = response["body"]
        assert body["group_severity"] == "CRITICAL"
        assert body["total_alerts_sent"] >= 2
        # Both travelers should have alerts
        assert body["traveler_results"][0]["alerts_sent"] >= 1
        assert body["traveler_results"][1]["alerts_sent"] >= 1

    def test_primary_traveler_not_in_travelers_list(self, arns):
        """Primary traveler skymiles not matching any traveler → group notification skipped."""
        event = _base_group_event(arns["endpoint_arn"])
        event["primary_traveler"] = "9999999"
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = []

        response = group_handler(event, None)

        # Should still succeed even if primary traveler isn't found
        assert response["statusCode"] == 200
        body = response["body"]
        assert body["total_alerts_sent"] >= 1


class TestGroupHandlerRequirementsOverride:
    """Custom requirements_override in event overrides default requirements."""

    def test_override_makes_visa_exempt(self, arns):
        """Override CN to exempt IN nationals → no visa alert."""
        event = _base_group_event(arns["endpoint_arn"])
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = []
        event["requirements_override"] = {
            "CN": {
                "country_code": "CN",
                "requires_visa": True,
                "transit_visa_required": False,
                "passport_validity_months": 6,
                "visa_exempt_nationalities": ["IN", "US"],
            }
        }

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "OK"
        assert body["total_alerts_sent"] == 0

    def test_override_reduces_passport_validity(self, arns):
        """Override passport_validity_months to a lower value."""
        event = _base_group_event(arns["endpoint_arn"])
        event["requirements_override"] = {
            "CN": {
                "country_code": "CN",
                "requires_visa": True,
                "transit_visa_required": False,
                "passport_validity_months": 1,
                "visa_exempt_nationalities": ["US"],
            }
        }

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["passport_status"] == "OK"

    def test_override_with_partial_fields(self, arns):
        """Override with only some fields uses handler defaults (not original country values) for missing ones."""
        event = _base_group_event(arns["endpoint_arn"])
        event["requirements_override"] = {
            "CN": {
                "visa_exempt_nationalities": ["US", "IN"],
            }
        }
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = []

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "OK"

    def test_no_override_uses_defaults(self, arns):
        """Null requirements_override uses COUNTRY_REQUIREMENTS defaults."""
        event = _base_group_event(arns["endpoint_arn"])
        event["requirements_override"] = None
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = []

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"


class TestParseGroupItinerary:
    """_parse_group_itinerary produces correct GroupItinerary from raw event."""

    def test_basic_parsing(self, arns):
        """Standard event parses into GroupItinerary with correct fields."""
        event = _base_group_event(arns["endpoint_arn"])

        result = _parse_group_itinerary(event)

        assert isinstance(result, GroupItinerary)
        assert result.confirmation_number == "GRP-001"
        assert result.primary_traveler == "1111111"
        assert len(result.segments) == 1
        assert result.segments[0].flight_number == "DL100"
        assert result.segments[0].origin == "ATL"
        assert result.segments[0].destination == "CN"
        assert result.segments[0].is_layover is False
        assert len(result.travelers) == 1
        assert result.travelers[0].skymiles_number == "1111111"

    def test_multiple_segments(self, arns):
        """Multiple segments are all parsed correctly."""
        event = _base_group_event(arns["endpoint_arn"])
        event["segments"] = [
            {
                "flight_number": "DL100",
                "origin": "ATL",
                "destination": "NRT",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": True,
            },
            {
                "flight_number": "DL200",
                "origin": "NRT",
                "destination": "CN",
                "departure_date": "2026-09-16",
                "arrival_date": "2026-09-17",
                "is_layover": False,
            },
        ]

        result = _parse_group_itinerary(event)

        assert len(result.segments) == 2
        assert result.segments[0].is_layover is True
        assert result.segments[1].is_layover is False
        assert result.segments[0].destination == "NRT"
        assert result.segments[1].destination == "CN"

    def test_is_layover_defaults_to_false(self, arns):
        """When is_layover not in segment, defaults to False."""
        event = _base_group_event(arns["endpoint_arn"])
        del event["segments"][0]["is_layover"]

        result = _parse_group_itinerary(event)

        assert result.segments[0].is_layover is False

    def test_multiple_travelers_parsed(self, arns):
        """Multiple travelers are all parsed into SkyMilesProfiles."""
        event = _base_group_event(arns["endpoint_arn"])
        event["travelers"].append({
            "skymiles_number": "5555555",
            "first_name": "Eve",
            "last_name": "Wilson",
            "nationality": "GB",
            "passport_number": "P555555",
            "passport_expiry": "2030-06-01",
            "endpoint_arn": arns["endpoint_arn_2"],
            "visa_records": [],
        })

        result = _parse_group_itinerary(event)

        assert len(result.travelers) == 2
        assert result.travelers[0].nationality == "US"
        assert result.travelers[1].nationality == "GB"
        assert result.travelers[1].skymiles_number == "5555555"


class TestFindTraveler:
    """_find_traveler returns the correct traveler or None."""

    def test_finds_existing_traveler(self, arns):
        """Returns the matching SkyMilesProfile by skymiles_number."""
        event = _base_group_event(arns["endpoint_arn"])
        group = _parse_group_itinerary(event)

        result = _find_traveler(group, "1111111")

        assert result is not None
        assert isinstance(result, SkyMilesProfile)
        assert result.skymiles_number == "1111111"
        assert result.first_name == "Alice"

    def test_returns_none_for_nonexistent(self, arns):
        """Returns None when skymiles_number not in travelers list."""
        event = _base_group_event(arns["endpoint_arn"])
        group = _parse_group_itinerary(event)

        result = _find_traveler(group, "9999999")

        assert result is None

    def test_finds_second_traveler(self, arns):
        """Correctly finds a traveler that is not the first in the list."""
        event = _base_group_event(arns["endpoint_arn"])
        event["travelers"].append({
            "skymiles_number": "6666666",
            "first_name": "Frank",
            "last_name": "Miller",
            "nationality": "GB",
            "passport_number": "P666666",
            "passport_expiry": "2030-01-01",
            "endpoint_arn": arns["endpoint_arn_2"],
            "visa_records": [],
        })
        group = _parse_group_itinerary(event)

        result = _find_traveler(group, "6666666")

        assert result is not None
        assert result.first_name == "Frank"


class TestGroupHandlerLayover:
    """Layover segments respect transit_visa_required setting."""

    def test_layover_with_transit_visa_required(self, arns):
        """Layover in CN (transit_visa_required=True) without visa → alert."""
        event = _base_group_event(arns["endpoint_arn"])
        event["segments"] = [
            {
                "flight_number": "DL100",
                "origin": "ATL",
                "destination": "CN",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": True,
            }
        ]
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = []

        response = group_handler(event, None)

        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"

    def test_layover_without_transit_visa_required(self, arns):
        """Layover in DE (transit_visa_required=False) without visa → no alert."""
        event = _base_group_event(arns["endpoint_arn"])
        event["segments"] = [
            {
                "flight_number": "DL100",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": True,
            }
        ]
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = []

        response = group_handler(event, None)

        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "OK"
