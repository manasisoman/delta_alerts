"""Tests for the group itinerary Lambda handler end-to-end flow."""

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


def _traveler(
    endpoint_arn: str,
    skymiles_number: str = "1000000001",
    nationality: str = "US",
    first_name: str = "Test",
    last_name: str = "User",
    passport_number: str | None = "P999999",
    passport_expiry: str | None = "2035-01-01",
    visa_records: list[dict] | None = None,
) -> dict:
    """Build a traveler payload dict with sensible defaults."""
    return {
        "skymiles_number": skymiles_number,
        "first_name": first_name,
        "last_name": last_name,
        "nationality": nationality,
        "passport_number": passport_number,
        "passport_expiry": passport_expiry,
        "endpoint_arn": endpoint_arn,
        "visa_records": visa_records or [],
    }


def _segment(
    destination: str = "DE",
    departure_date: str = "2030-09-15",
    arrival_date: str = "2030-09-16",
    is_layover: bool = False,
    flight_number: str = "DL100",
    origin: str = "JFK",
) -> dict:
    """Build a flight segment payload dict with sensible defaults."""
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "arrival_date": arrival_date,
        "is_layover": is_layover,
    }


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid group Lambda event with optional top-level overrides."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": "1000000001",
        "travelers": [_traveler(endpoint_arn)],
        "segments": [_segment()],
        "requirements_override": None,
    }
    event.update(overrides)
    return event


class TestHappyPath:
    """A clean group produces no alerts and an OK group severity."""

    def test_clean_group_returns_ok(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert body["traveler_results"] == [
            {
                "skymiles_number": "1000000001",
                "first_name": "Test",
                "last_name": "User",
                "passport_status": "OK",
                "visa_status": "OK",
                "alerts_sent": 0,
            }
        ]
        assert alert_store_mod.table.scan()["Items"] == []


class TestPassportAlerts:
    """Passport problems persist a PASSPORT alert and surface in the response."""

    def test_expired_passport_persists_critical_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        body = response["body"]
        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert body["group_severity"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        passport_alerts = [i for i in items if i["alert_type"] == "PASSPORT"]
        assert len(passport_alerts) == 1
        assert passport_alerts[0]["itinerary_ref"] == "DL-GROUP"

    def test_missing_passport_fields_persist_critical_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["passport_number"] = None
        event["travelers"][0]["passport_expiry"] = None

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert response["body"]["total_alerts_sent"] == 1


class TestVisaAlerts:
    """Visa problems persist a VISA alert with the evaluator's reasons."""

    def test_missing_visa_persists_critical_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"
        event["segments"] = [_segment(destination="CN")]

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert len(visa_alerts) == 1
        assert "No visa on file for CN" in visa_alerts[0]["reasons"]

    def test_visa_exempt_nationality_produces_no_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="CN")]
        event["travelers"][0]["nationality"] = "US"
        event["requirements_override"] = {
            "CN": {"requires_visa": True, "visa_exempt_nationalities": ["US"]}
        }

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_valid_visa_produces_no_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="CN")]
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2024-01-01",
                "expiry_date": "2035-01-01",
                "visa_number": "V1",
            }
        ]

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0


class TestLayoverSegments:
    """Layovers only trigger visa alerts when a transit visa is required."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"
        event["segments"] = [_segment(destination="DE", is_layover=True)]

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_layover_requiring_transit_visa_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"
        event["segments"] = [_segment(destination="CN", is_layover=True)]

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert response["body"]["total_alerts_sent"] == 1


class TestRequirementsOverride:
    """requirements_override replaces the default country requirements."""

    def test_override_disabling_visa_clears_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"
        event["segments"] = [_segment(destination="CN")]
        event["requirements_override"] = {
            "CN": {
                "requires_visa": False,
                "passport_validity_months": 0,
            }
        }

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_override_raising_passport_validity_triggers_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["passport_expiry"] = "2030-10-01"
        event["requirements_override"] = {
            "DE": {
                "requires_visa": False,
                "passport_validity_months": 12,
                "visa_exempt_nationalities": ["US"],
            }
        }

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert response["body"]["group_severity"] == "CRITICAL"

    def test_missing_override_key_leaves_defaults(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["requirements_override"]

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0


class TestMultipleTravelers:
    """Each traveler is evaluated independently and aggregated into the group."""

    def test_per_traveler_results_and_group_severity(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="CN")]
        event["travelers"] = [
            _traveler(endpoint_arn, skymiles_number="1000000001", nationality="US",
                      first_name="Clean", last_name="Traveler",
                      visa_records=[{
                          "country_code": "CN",
                          "visa_type": "TOURIST",
                          "issue_date": "2024-01-01",
                          "expiry_date": "2035-01-01",
                          "visa_number": "V1",
                      }]),
            _traveler(endpoint_arn, skymiles_number="1000000002", nationality="IN",
                      first_name="No", last_name="Visa"),
            _traveler(endpoint_arn, skymiles_number="1000000003", nationality="IN",
                      first_name="Bad", last_name="Passport",
                      passport_expiry="2020-01-01",
                      visa_records=[{
                          "country_code": "CN",
                          "visa_type": "TOURIST",
                          "issue_date": "2024-01-01",
                          "expiry_date": "2035-01-01",
                          "visa_number": "V2",
                      }]),
        ]

        response = group_handler(event, None)

        results = {r["skymiles_number"]: r for r in response["body"]["traveler_results"]}
        assert len(results) == 3
        assert results["1000000001"]["alerts_sent"] == 0
        assert results["1000000002"]["visa_status"] == "CRITICAL"
        assert results["1000000003"]["passport_status"] == "CRITICAL"
        assert response["body"]["total_alerts_sent"] == 2
        assert response["body"]["group_severity"] == "CRITICAL"

    def test_traveler_with_both_alerts_counts_twice(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="CN")]
        event["travelers"] = [
            _traveler(endpoint_arn, nationality="IN", passport_expiry="2020-01-01")
        ]

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["alerts_sent"] == 2
        assert response["body"]["total_alerts_sent"] == 2
        items = alert_store_mod.table.scan()["Items"]
        assert {i["alert_type"] for i in items} == {"PASSPORT", "VISA"}


class TestPrimaryTravelerNotification:
    """The group summary notification is only sent when the primary is in the group."""

    def test_unknown_primary_traveler_skips_group_notification(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["primary_traveler"] = "9999999999"

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["total_alerts_sent"] == 0


class TestParsing:
    """Event deserialization helpers build the expected domain objects."""

    def test_parse_group_itinerary_defaults_layover_to_false(self, endpoint_arn):
        segment = _segment()
        del segment["is_layover"]
        event = _base_event(endpoint_arn, segments=[segment])

        group = _parse_group_itinerary(event)

        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == "1000000001"
        assert len(group.travelers) == 1
        assert group.segments[0].is_layover is False
        assert group.segments[0].departure_date.isoformat() == "2030-09-15"

    def test_find_traveler_returns_none_when_absent(self, endpoint_arn):
        group = _parse_group_itinerary(_base_event(endpoint_arn))

        assert _find_traveler(group, "1000000001") is group.travelers[0]
        assert _find_traveler(group, "0000000000") is None


class TestMultipleSegments:
    """Alerts consolidate across every segment of the shared itinerary."""

    def test_second_segment_destination_is_evaluated(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"
        event["segments"] = [
            _segment(destination="DE", departure_date="2030-09-15",
                     arrival_date="2030-09-16"),
            _segment(destination="CN", departure_date="2030-09-20",
                     arrival_date="2030-09-21", flight_number="DL200"),
        ]

        response = group_handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert len(visa_alerts) == 1
        reasons = visa_alerts[0]["reasons"]
        assert "No visa on file for DE" in reasons
        assert "No visa on file for CN" in reasons
