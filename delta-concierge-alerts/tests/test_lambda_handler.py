"""Critical-path tests for the Lambda handler end-to-end flow."""

from datetime import date, datetime, timedelta

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.lambda_handler import _compute_ttl, evaluate_itinerary, handler
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
    """A requirements_override entry replaces the default country rules."""

    def test_override_waives_visa_requirement(self, endpoint_arn):
        """requires_visa=False for CN should clear the missing-visa alert."""
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {
            "CN": {
                "requires_visa": False,
                "transit_visa_required": False,
                "passport_validity_months": 6,
                "visa_exempt_nationalities": [],
            }
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_override_exempt_nationality(self, endpoint_arn):
        """Listing the traveler's nationality as exempt should clear the alert."""
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {
            "CN": {"visa_exempt_nationalities": ["IN"]}
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_override_raises_passport_validity_months(self, endpoint_arn):
        """A long validity requirement should flag an otherwise-valid passport."""
        event = _base_event(endpoint_arn)
        event["profile"]["passport_expiry"] = "2030-06-01"
        event["itinerary"]["segments"][0]["departure_date"] = "2030-01-10"
        event["itinerary"]["segments"][0]["arrival_date"] = "2030-01-11"
        event["requirements_override"] = {
            "CN": {"requires_visa": False, "passport_validity_months": 12}
        }

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] == 1

    def test_override_for_layover_transit_visa(self, endpoint_arn):
        """A layover requiring a transit visa should still raise an alert."""
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"] = [
            {
                "flight_number": "DL300",
                "origin": "JFK",
                "destination": "CN",
                "departure_date": "2030-03-01",
                "arrival_date": "2030-03-01",
                "is_layover": True,
            },
            {
                "flight_number": "DL301",
                "origin": "CN",
                "destination": "JP",
                "departure_date": "2030-03-02",
                "arrival_date": "2030-03-02",
                "is_layover": False,
            },
        ]
        event["requirements_override"] = {
            "JP": {"requires_visa": False},
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert any("CN" in reason for reason in visa_alerts[0]["reasons"])

    def test_layover_without_transit_visa_is_skipped(self, endpoint_arn):
        """A layover in a country not requiring transit visas raises no visa alert."""
        event = _base_event(endpoint_arn)
        event["profile"]["passport_expiry"] = "2035-01-01"
        event["itinerary"]["segments"] = [
            {
                "flight_number": "DL400",
                "origin": "JFK",
                "destination": "CN",
                "departure_date": "2030-04-01",
                "arrival_date": "2030-04-01",
                "is_layover": True,
            }
        ]
        event["requirements_override"] = {
            "CN": {"transit_visa_required": False},
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0


class TestGroupDelegation:
    """Events carrying a ``travelers`` key are delegated to the group handler."""

    def _group_event(self, endpoint_arn: str) -> dict:
        return {
            "confirmation_number": "DL-GROUP",
            "primary_traveler": "1111111111",
            "travelers": [
                {
                    "skymiles_number": "1111111111",
                    "first_name": "Primary",
                    "last_name": "Traveler",
                    "nationality": "US",
                    "passport_number": "P111111",
                    "passport_expiry": "2035-01-01",
                    "endpoint_arn": endpoint_arn,
                    "visa_records": [],
                },
                {
                    "skymiles_number": "2222222222",
                    "first_name": "Second",
                    "last_name": "Traveler",
                    "nationality": "IN",
                    "passport_number": "P222222",
                    "passport_expiry": "2035-01-01",
                    "endpoint_arn": endpoint_arn,
                    "visa_records": [],
                },
            ],
            "segments": [
                {
                    "flight_number": "DL500",
                    "origin": "JFK",
                    "destination": "CN",
                    "departure_date": "2030-05-01",
                    "arrival_date": "2030-05-02",
                    "is_layover": False,
                }
            ],
        }

    def test_group_event_returns_per_traveler_results(self, endpoint_arn):
        """The group response shape is returned instead of the single-traveler one."""
        event = self._group_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert len(body["traveler_results"]) == 2
        assert body["total_alerts_sent"] >= 1

    def test_group_alerts_persisted_per_traveler(self, endpoint_arn):
        """Each traveler with an issue gets their own DynamoDB alert record."""
        event = self._group_event(endpoint_arn)

        handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        assert {i["skymiles_number"] for i in items} == {"1111111111", "2222222222"}
        assert all(i["itinerary_ref"] == "DL-GROUP" for i in items)


class TestEvaluateItinerary:
    """``evaluate_itinerary`` is a thin wrapper delegating to ``handler``."""

    def test_matches_handler_response(self, endpoint_arn):
        """Calling the wrapper yields the same response body as the handler."""
        event = _base_event(endpoint_arn)

        wrapper_response = evaluate_itinerary(event)

        assert wrapper_response["statusCode"] == 200
        assert wrapper_response["body"]["visa_status"] == "CRITICAL"

    def test_wrapper_delegates_group_events(self, endpoint_arn):
        """Group payloads passed to the wrapper reach the group handler."""
        event = {
            "confirmation_number": "DL-GROUP2",
            "primary_traveler": "3333333333",
            "travelers": [
                {
                    "skymiles_number": "3333333333",
                    "first_name": "Solo",
                    "last_name": "Group",
                    "nationality": "US",
                    "passport_number": "P333333",
                    "passport_expiry": "2035-01-01",
                    "endpoint_arn": endpoint_arn,
                    "visa_records": [],
                }
            ],
            "segments": [
                {
                    "flight_number": "DL600",
                    "origin": "ATL",
                    "destination": "DE",
                    "departure_date": "2030-06-01",
                    "arrival_date": "2030-06-02",
                    "is_layover": False,
                }
            ],
        }

        response = evaluate_itinerary(event)

        assert response["body"]["confirmation_number"] == "DL-GROUP2"


class TestTtlComputation:
    """The alert TTL is 30 days after the itinerary's last arrival date."""

    def test_ttl_uses_latest_arrival(self, endpoint_arn):
        """Multi-segment itineraries use the maximum arrival date."""
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"] = [
            {
                "flight_number": "DL700",
                "origin": "JFK",
                "destination": "CN",
                "departure_date": "2030-07-01",
                "arrival_date": "2030-07-02",
                "is_layover": False,
            },
            {
                "flight_number": "DL701",
                "origin": "CN",
                "destination": "CN",
                "departure_date": "2030-07-10",
                "arrival_date": "2030-07-11",
                "is_layover": False,
            },
        ]

        handler(event, None)

        expected = int(
            datetime.combine(date(2030, 7, 11), datetime.min.time()).timestamp()
        ) + 30 * 86400
        items = alert_store_mod.table.scan()["Items"]
        assert all(int(i["ttl"]) == expected for i in items)

    def test_ttl_falls_back_to_today_without_segments(self):
        """An itinerary with no segments computes the TTL from today."""
        itinerary = Itinerary(confirmation_number="DL-EMPTY", segments=[])

        ttl = _compute_ttl(itinerary)

        expected = int(
            (
                datetime.combine(date.today(), datetime.min.time())
                + timedelta(days=30)
            ).timestamp()
        )
        assert ttl == expected
