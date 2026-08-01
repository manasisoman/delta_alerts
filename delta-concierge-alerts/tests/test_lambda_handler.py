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


class TestRequirementsOverride:
    """Overrides in the event replace the default country requirements."""

    def test_override_removes_visa_requirement(self, endpoint_arn):
        """requires_visa=False for CN should suppress the visa alert."""
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {
            "CN": {"requires_visa": False, "passport_validity_months": 6}
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_override_exempt_nationality(self, endpoint_arn):
        """Adding the traveler's nationality to the exempt list clears the alert."""
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {
            "CN": {"visa_exempt_nationalities": ["IN"]}
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_override_tightens_passport_validity(self, endpoint_arn):
        """A large passport_validity_months requirement triggers a passport alert."""
        event = _base_event(endpoint_arn)
        event["profile"]["passport_expiry"] = "2026-10-01"
        event["requirements_override"] = {
            "CN": {"requires_visa": False, "passport_validity_months": 24}
        }

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"

    def test_override_for_unknown_country_is_used(self, endpoint_arn):
        """An override may introduce a country absent from the defaults."""
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"][0]["destination"] = "ZZ"
        event["requirements_override"] = {
            "ZZ": {"requires_visa": True, "visa_exempt_nationalities": []}
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestGroupDelegation:
    """Events containing a ``travelers`` key are delegated to the group handler."""

    def test_group_event_returns_group_response(self, endpoint_arn):
        """A group payload should produce a per-traveler group response body."""
        event = {
            "confirmation_number": "DL-GROUP",
            "primary_traveler": "1111111111",
            "travelers": [
                {
                    "skymiles_number": "1111111111",
                    "first_name": "A",
                    "last_name": "One",
                    "nationality": "IN",
                    "passport_number": "P111",
                    "passport_expiry": "2030-01-01",
                    "endpoint_arn": endpoint_arn,
                    "visa_records": [],
                },
                {
                    "skymiles_number": "2222222222",
                    "first_name": "B",
                    "last_name": "Two",
                    "nationality": "US",
                    "passport_number": "P222",
                    "passport_expiry": "2030-01-01",
                    "endpoint_arn": endpoint_arn,
                    "visa_records": [],
                },
            ],
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
        }

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["confirmation_number"] == "DL-GROUP"
        assert len(response["body"]["traveler_results"]) == 2


class TestLayoverHandling:
    """Layover segments only require a transit visa when the country demands one."""

    def test_layover_without_transit_requirement_is_ignored(self, endpoint_arn):
        """A DE layover (transit_visa_required=False) should not alert."""
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"] = [
            {
                "flight_number": "DL300",
                "origin": "JFK",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-15",
                "is_layover": True,
            }
        ]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_layover_with_transit_requirement_alerts(self, endpoint_arn):
        """A CN layover (transit_visa_required=True) still needs a visa."""
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"][0]["is_layover"] = True

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestVisaRecordParsing:
    """Visa records on the profile are deserialized and honored."""

    def test_valid_visa_clears_alert(self, endpoint_arn):
        """A valid, far-future CN visa should clear the visa alert."""
        event = _base_event(endpoint_arn)
        event["profile"]["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2025-01-01",
                "expiry_date": "2030-01-01",
                "visa_number": "V001",
            }
        ]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_expired_visa_triggers_critical(self, endpoint_arn):
        """An expired CN visa should produce a CRITICAL visa alert."""
        event = _base_event(endpoint_arn)
        event["profile"]["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2018-01-01",
                "expiry_date": "2020-01-01",
                "visa_number": "V002",
            }
        ]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_latest_expiry_visa_wins(self, endpoint_arn):
        """With multiple CN visas the one with the latest expiry is selected."""
        event = _base_event(endpoint_arn)
        event["profile"]["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2018-01-01",
                "expiry_date": "2020-01-01",
                "visa_number": "V-OLD",
            },
            {
                "country_code": "CN",
                "visa_type": "BUSINESS",
                "issue_date": "2025-01-01",
                "expiry_date": "2030-01-01",
                "visa_number": "V-NEW",
            },
        ]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"


class TestChangedItinerary:
    """Re-evaluating a modified itinerary reflects the new segments."""

    def test_added_segment_introduces_alert(self, endpoint_arn):
        """Adding a CN segment to a clean DE itinerary produces a visa alert."""
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
        clean = handler(event, None)
        assert clean["body"]["visa_status"] == "OK"

        event["itinerary"]["segments"].append(
            {
                "flight_number": "DL201",
                "origin": "FRA",
                "destination": "CN",
                "departure_date": "2026-09-20",
                "arrival_date": "2026-09-21",
                "is_layover": False,
            }
        )
        changed = handler(event, None)

        assert changed["body"]["visa_status"] == "CRITICAL"
        assert changed["body"]["alerts_sent"] >= 1


class TestComputeTtl:
    """TTL is 30 days after the final arrival date, with a today() fallback."""

    def test_ttl_uses_last_arrival(self, endpoint_arn):
        """The TTL should be derived from the latest arrival date."""
        itinerary = _parse_itinerary(
            {
                "confirmation_number": "DL-TTL",
                "segments": [
                    {
                        "flight_number": "DL1",
                        "origin": "JFK",
                        "destination": "DE",
                        "departure_date": "2026-09-01",
                        "arrival_date": "2026-09-02",
                    },
                    {
                        "flight_number": "DL2",
                        "origin": "FRA",
                        "destination": "CN",
                        "departure_date": "2026-09-10",
                        "arrival_date": "2026-09-11",
                    },
                ],
            }
        )

        ttl = _compute_ttl(itinerary)

        expected = int(
            (
                datetime.combine(date(2026, 9, 11), datetime.min.time())
                + timedelta(days=30)
            ).timestamp()
        )
        assert ttl == expected

    def test_ttl_falls_back_to_today_when_no_segments(self, endpoint_arn):
        """An itinerary with no segments falls back to today + 30 days."""
        itinerary = Itinerary(confirmation_number="DL-EMPTY", segments=[])

        ttl = _compute_ttl(itinerary)

        expected = int(
            (
                datetime.combine(date.today(), datetime.min.time())
                + timedelta(days=30)
            ).timestamp()
        )
        assert ttl == expected


class TestEvaluateItinerary:
    """``evaluate_itinerary`` is a thin wrapper over ``handler``."""

    def test_matches_handler_response(self, endpoint_arn):
        """The wrapper returns the same response shape as calling handler directly."""
        event = _base_event(endpoint_arn)

        response = evaluate_itinerary(event)

        assert response["statusCode"] == 200
        assert response["body"]["visa_status"] == "CRITICAL"
