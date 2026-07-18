"""Critical-path tests for the group itinerary Lambda handler end-to-end flow."""

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


def _traveler(
    endpoint_arn: str,
    skymiles_number: str = "1111111",
    nationality: str = "IN",
    passport_number: str | None = "P999999",
    passport_expiry: str | None = "2035-01-01",
    visa_records: list | None = None,
    first_name: str = "Test",
    last_name: str = "User",
) -> dict:
    """Build a valid traveler profile dict with sensible defaults."""
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


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid group Lambda event with optional top-level field overrides.

    Defaults to a single IN-national traveler flying to CN (which requires a
    visa for IN nationals), so the happy path produces a CRITICAL visa alert.
    """
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": "1111111",
        "segments": [
            {
                "flight_number": "DL100",
                "origin": "JFK",
                "destination": "CN",
                "departure_date": "2030-09-15",
                "arrival_date": "2030-09-16",
                "is_layover": False,
            }
        ],
        "travelers": [_traveler(endpoint_arn)],
        "requirements_override": None,
    }
    event.update(overrides)
    return event


class TestGroupHandlerHappyPath:
    """Alerts are evaluated, persisted to DynamoDB, and summarized in the response."""

    def test_alerts_persisted_and_response_shape(self, endpoint_arn):
        """IN national to CN with no visa persists a CRITICAL visa alert."""
        event = _base_event(endpoint_arn)

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["group_severity"] == "CRITICAL"
        assert body["total_alerts_sent"] == 1
        assert len(body["traveler_results"]) == 1

        result = body["traveler_results"][0]
        assert result["skymiles_number"] == "1111111"
        assert result["visa_status"] == "CRITICAL"
        assert result["passport_status"] == "OK"
        assert result["alerts_sent"] == 1

        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert len(visa_alerts) == 1
        assert "No visa on file for CN" in visa_alerts[0]["reasons"]


class TestCleanTravelers:
    """A group with no document issues produces no alerts and an OK severity."""

    def test_no_alerts_for_clean_travelers(self, endpoint_arn):
        """US nationals to DE with a valid passport trigger no alerts."""
        traveler = _traveler(endpoint_arn, nationality="US")
        event = _base_event(
            endpoint_arn,
            travelers=[traveler],
            segments=[
                {
                    "flight_number": "DL200",
                    "origin": "ATL",
                    "destination": "DE",
                    "departure_date": "2030-09-15",
                    "arrival_date": "2030-09-16",
                    "is_layover": False,
                }
            ],
        )

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert body["traveler_results"][0]["passport_status"] == "OK"
        assert body["traveler_results"][0]["visa_status"] == "OK"
        assert body["traveler_results"][0]["alerts_sent"] == 0

        assert alert_store_mod.table.scan()["Items"] == []


class TestPassportAlerts:
    """Passport problems produce a persisted PASSPORT alert."""

    def test_expired_passport_triggers_critical(self, endpoint_arn):
        """A past passport expiry yields a CRITICAL passport status and alert."""
        traveler = _traveler(
            endpoint_arn, nationality="US", passport_expiry="2020-01-01"
        )
        event = _base_event(
            endpoint_arn,
            travelers=[traveler],
            segments=[
                {
                    "flight_number": "DL200",
                    "origin": "ATL",
                    "destination": "DE",
                    "departure_date": "2030-09-15",
                    "arrival_date": "2030-09-16",
                    "is_layover": False,
                }
            ],
        )

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        result = response["body"]["traveler_results"][0]
        assert result["passport_status"] == "CRITICAL"
        assert result["alerts_sent"] == 1

        items = alert_store_mod.table.scan()["Items"]
        passport_alerts = [i for i in items if i["alert_type"] == "PASSPORT"]
        assert len(passport_alerts) == 1


class TestRequirementsOverride:
    """requirements_override merges into the defaults used for evaluation."""

    def test_override_suppresses_visa_alert(self, endpoint_arn):
        """Overriding CN to not require a visa removes the would-be visa alert."""
        event = _base_event(
            endpoint_arn,
            requirements_override={
                "CN": {
                    "country_code": "CN",
                    "requires_visa": False,
                    "transit_visa_required": False,
                    "passport_validity_months": 6,
                    "visa_exempt_nationalities": [],
                }
            },
        )

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert alert_store_mod.table.scan()["Items"] == []


class TestMultipleTravelers:
    """Group severity aggregates the highest severity across all travelers."""

    def test_mixed_group_aggregates_and_isolates_alerts(self, endpoint_arn):
        """A clean traveler gets no alert while a flagged one drives group severity."""
        clean = _traveler(
            endpoint_arn, skymiles_number="2000001", nationality="US",
            first_name="Clean", last_name="Flyer",
        )
        flagged = _traveler(
            endpoint_arn, skymiles_number="2000002", nationality="IN",
            first_name="Flagged", last_name="Flyer",
        )
        event = _base_event(
            endpoint_arn,
            primary_traveler="2000001",
            travelers=[clean, flagged],
            segments=[
                {
                    "flight_number": "DL300",
                    "origin": "JFK",
                    "destination": "DE",
                    "departure_date": "2030-09-15",
                    "arrival_date": "2030-09-16",
                    "is_layover": False,
                }
            ],
        )

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["group_severity"] == "CRITICAL"
        assert body["total_alerts_sent"] == 1

        results = {r["skymiles_number"]: r for r in body["traveler_results"]}
        assert results["2000001"]["alerts_sent"] == 0
        assert results["2000002"]["alerts_sent"] == 1
        assert results["2000002"]["visa_status"] == "CRITICAL"


class TestPrimaryTravelerNotFound:
    """A primary_traveler not present in the group skips the group notification."""

    def test_missing_primary_traveler_still_returns_200(self, endpoint_arn):
        """An unknown primary_traveler does not raise and per-traveler results stand."""
        event = _base_event(endpoint_arn, primary_traveler="0000000")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["traveler_results"][0]["skymiles_number"] == "1111111"


class TestMalformedInput:
    """Malformed events raise as the fields are dereferenced during parsing."""

    def test_missing_segments_key_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["segments"]

        with pytest.raises(KeyError):
            group_handler(event, None)

    def test_missing_travelers_key_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["travelers"]

        with pytest.raises(KeyError):
            group_handler(event, None)

    def test_missing_confirmation_number_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["confirmation_number"]

        with pytest.raises(KeyError):
            group_handler(event, None)

    def test_invalid_departure_date_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["segments"][0]["departure_date"] = "not-a-date"

        with pytest.raises(ValueError):
            group_handler(event, None)


class TestParseGroupItinerary:
    """_parse_group_itinerary deserializes the event into a GroupItinerary."""

    def test_parses_segments_and_travelers(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        group = _parse_group_itinerary(event)

        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == "1111111"
        assert len(group.segments) == 1
        assert group.segments[0].destination == "CN"
        assert group.segments[0].is_layover is False
        assert len(group.travelers) == 1
        assert group.travelers[0].skymiles_number == "1111111"

    def test_segment_layover_defaults_to_false(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["segments"][0]["is_layover"]

        group = _parse_group_itinerary(event)

        assert group.segments[0].is_layover is False


class TestFindTraveler:
    """_find_traveler returns the matching profile or None."""

    def test_returns_matching_traveler(self, endpoint_arn):
        group = _parse_group_itinerary(_base_event(endpoint_arn))

        found = _find_traveler(group, "1111111")

        assert found is not None
        assert found.skymiles_number == "1111111"

    def test_returns_none_when_not_found(self, endpoint_arn):
        group = _parse_group_itinerary(_base_event(endpoint_arn))

        found = _find_traveler(group, "9999999")

        assert found is None
